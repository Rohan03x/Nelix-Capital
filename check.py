"""check.py - runtime health check for the DCF dashboard and shared-brain layer."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import asdict
from typing import Any

from auto_valuation.validation.shared_brain import collect_operational_diagnostics


BASE = "http://127.0.0.1:5000"
TICKERS = ["AMZN", "NKE", "AAPL", "TSLA"]
REQUIRED_KEYS = [
    "intrinsic_value", "price", "wacc", "terminal_growth",
    "upside_pct", "pv_ufcfs", "pv_terminal", "enterprise_value",
    "equity_value", "diluted_shares", "tv_pct",
]
REQUIRED_HIST = ["revenue", "ebit_margin", "roic", "fcf", "years"]
REQUIRED_KNOWLEDGE_MODEL_KEYS = ["summary", "global_learning", "assumption_weights", "layered_learning"]
WARN_LATENCY_MS = 2_500
FAIL_LATENCY_MS = 5_000

OK = "\033[92m OK \033[0m"
ERR = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"


def evaluate_dashboard_payload(
    ticker: str,
    data: dict[str, Any],
    *,
    response_ms: int,
    strict_learning: bool = False,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []

    for key in REQUIRED_KEYS:
        if data.get(key) is None:
            issues.append(f"None: {key}")

    hist = data.get("historical") or {}
    for key in REQUIRED_HIST:
        value = hist.get(key)
        if not value:
            issues.append(f"empty: historical.{key}")
        elif isinstance(value, list) and None in value:
            nones = [index for index, item in enumerate(value) if item is None]
            issues.append(f"None in historical.{key} idx={nones}")

    for index, row in enumerate(data.get("forecast") or []):
        for field in ["revenue", "ebit", "nopat", "ufcf", "pv"]:
            if row.get(field) is None:
                issues.append(f"None: forecast[{index}].{field}")

    knowledge_model = data.get("knowledge_model")
    if strict_learning or data.get("data_source") == "eodhd":
        if not isinstance(knowledge_model, dict):
            issues.append("missing: knowledge_model")
        else:
            for key in REQUIRED_KNOWLEDGE_MODEL_KEYS:
                if knowledge_model.get(key) in (None, {}, []):
                    issues.append(f"missing: knowledge_model.{key}")
            if int(knowledge_model.get("calibration_cohort_size") or 0) < 5:
                warnings.append("knowledge-model calibration cohort is still thin")

    if response_ms >= FAIL_LATENCY_MS:
        issues.append(f"latency: {response_ms}ms")
    elif response_ms >= WARN_LATENCY_MS:
        warnings.append(f"slow response: {response_ms}ms")

    return {
        "ticker": ticker,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "status": ERR if issues else OK,
        "source": (data.get("data_quality") or {}).get("source", "?")[:45],
        "intrinsic_value": data.get("intrinsic_value", "?"),
        "years": len(hist.get("years") or []),
        "confidence_score": data.get("confidence_score", "?"),
        "response_ms": response_ms,
    }


def check_ticker(base_url: str, ticker: str, *, strict_learning: bool = False) -> dict[str, Any]:
    url = f"{base_url}/api/dashboard/{ticker}"
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.load(response)
    except Exception as exc:
        return {
            "ticker": ticker,
            "ok": False,
            "issues": [f"unreachable: {exc}"],
            "warnings": [],
            "status": ERR,
            "source": "?",
            "intrinsic_value": "?",
            "years": 0,
            "confidence_score": "?",
            "response_ms": 0,
        }

    response_ms = int((time.time() - started) * 1000)
    return evaluate_dashboard_payload(ticker, data, response_ms=response_ms, strict_learning=strict_learning)


def check_server(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
            response.read()
        return True
    except Exception:
        return False


def _print_ticker_report(report: dict[str, Any]) -> None:
    print(
        f"  [{report['status']}] {report['ticker']:6s}  {report['response_ms']:4d}ms  "
        f"IV=${report['intrinsic_value']:<8}  {report['years']}yrs  conf={report['confidence_score']}/100  {report['source']}"
    )
    for issue in report["issues"][:5]:
        print(f"           └─ {WARN} {issue}")
    for warning in report["warnings"][:3]:
        print(f"           └─ {WARN} {warning}")
    extra = max(len(report["issues"]) - 5, 0) + max(len(report["warnings"]) - 3, 0)
    if extra > 0:
        print(f"           └─ ... +{extra} more")


def _print_operational_report(report: dict[str, Any]) -> None:
    status = OK if report["status"] == "pass" else (ERR if report["status"] == "fail" else WARN)
    print(
        f"  [{status}] learning-ledger  preds={report['prediction_records']}  matured={report['matured_records']}  "
        f"postmortems={report['postmortem_records']}  quinquennial={report['quinquennial_reports']}"
    )
    if report["maintenance_last_run_at"]:
        print(f"           └─ last maintenance: {report['maintenance_last_run_at']}")
    for warning in report["warnings"][:4]:
        print(f"           └─ {WARN} {warning}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the dashboard and shared-brain health checks.")
    parser.add_argument("--base-url", default=BASE, help="Dashboard base URL.")
    parser.add_argument("--tickers", nargs="*", default=TICKERS, help="Tickers to validate.")
    parser.add_argument("--strict-learning", action="store_true", help="Require the live payload to include the shared-brain diagnostics contract.")
    parser.add_argument("--skip-diagnostics", action="store_true", help="Skip local ledger and maintenance diagnostics.")
    parser.add_argument("--json", action="store_true", help="Print the full health-check result as JSON after the text summary.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    print("\n=== DCF Dashboard Health Check ===\n")

    if not check_server(args.base_url):
        print(f"  [{ERR}] Flask server not running at {args.base_url}")
        print("         Run: .venv\\Scripts\\python.exe run.py\n")
        return 1

    print(f"  [{OK}] Flask server is up at {args.base_url}\n")

    ticker_reports = [check_ticker(args.base_url, ticker, strict_learning=args.strict_learning) for ticker in args.tickers]
    for report in ticker_reports:
        _print_ticker_report(report)

    diagnostics_report: dict[str, Any] | None = None
    if not args.skip_diagnostics:
        print()
        diagnostics = collect_operational_diagnostics()
        diagnostics_report = asdict(diagnostics)
        _print_operational_report(diagnostics_report)

    all_ok = all(report["ok"] for report in ticker_reports)
    if diagnostics_report is not None and diagnostics_report["status"] == "fail":
        all_ok = False

    print()
    if all_ok:
        print(f"  [{OK}] Health checks completed without hard failures.\n")
    else:
        print(f"  [{ERR}] Health checks found at least one hard failure.\n")

    if args.json:
        print(
            json.dumps(
                {
                    "server_ok": True,
                    "tickers": ticker_reports,
                    "operational_diagnostics": diagnostics_report,
                    "all_ok": all_ok,
                },
                indent=2,
            )
        )

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
