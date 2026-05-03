from __future__ import annotations

import argparse
import json
from pathlib import Path

from auto_valuation.validation.shared_brain import evaluate_default_suite


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the shared-brain validation harness.")
    parser.add_argument("--no-diagnostics", action="store_true", help="Skip live ledger diagnostics and benchmark only the packaged suite.")
    parser.add_argument("--json-out", type=Path, help="Optional path to write the full validation report as JSON.")
    parser.add_argument("--performance-budget-ms", type=float, default=1500.0, help="Soft runtime budget for the packaged benchmark.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = evaluate_default_suite(
        include_diagnostics=not args.no_diagnostics,
        performance_budget_ms=args.performance_budget_ms,
    )

    if args.json_out:
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    print("Shared-Brain Validation")
    print(f"cases: {report.case_count} | time-aware violations: {report.time_aware_violations} | performance: {report.performance_ms:.2f}ms")
    for name, metric in report.metrics.items():
        print(
            f"{name}: baseline {metric.baseline_mae:.4f} -> shared {metric.shared_mae:.4f} "
            f"(improvement {metric.improvement:.4f}, {metric.relative_improvement_pct:.2f}%)"
        )
    print(
        f"valuation error p50: {report.valuation_error_distribution['baseline'].p50:.2f}% -> "
        f"{report.valuation_error_distribution['shared'].p50:.2f}%"
    )
    print(f"confidence ranking accuracy: {report.confidence_ranking_accuracy:.2%}")
    print(f"analog consistency rate: {report.analog_consistency_rate:.2%}")
    if report.operational_diagnostics is not None:
        diag = report.operational_diagnostics
        print(
            f"ledger: {diag.prediction_records} predictions | {diag.postmortem_records} postmortems | "
            f"{diag.quinquennial_reports} quinquennial reports | status {diag.status}"
        )
        for warning in diag.warnings:
            print(f"warning: {warning}")
    if report.acceptance is not None:
        print(f"acceptance: {report.acceptance.status}")
        print(report.acceptance.summary)
        for gap in report.acceptance.remaining_gaps:
            print(f"gap: {gap}")

    return 0 if report.acceptance is None or report.acceptance.status in {"accepted", "provisional"} else 1


if __name__ == "__main__":
    raise SystemExit(main())