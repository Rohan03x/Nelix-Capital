"""Learning performance diagnostics for calibration and valuation accuracy."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_valuation.learning._layered_calibrator import CalibrationStore
from auto_valuation.learning.ledger import LedgerReader


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for parser in (datetime.fromisoformat, lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S")):
        try:
            return parser(text)
        except Exception:
            continue
    return None


def _metric_stats(values: list[float | None], *, cap: float = 200.0) -> dict[str, Any]:
    raw = [float(value) for value in values if value is not None]
    clean = [value for value in raw if abs(value) <= cap]
    if not clean:
        return {"n": 0, "outliers": len(raw)}
    return {
        "n": len(clean),
        "outliers": len(raw) - len(clean),
        "mae": round(statistics.mean(abs(value) for value in clean), 2),
        "mean": round(statistics.mean(clean), 2),
        "median": round(statistics.median(clean), 2),
        "within_10_pct": round(sum(abs(value) <= 10 for value in clean) / len(clean) * 100.0, 1),
        "within_20_pct": round(sum(abs(value) <= 20 for value in clean) / len(clean) * 100.0, 1),
    }


def _postmortem_payloads(reader: LedgerReader) -> list[tuple[str, dict[str, Any]]]:
    with sqlite3.connect(reader.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT created_at, payload_json FROM postmortem_records ORDER BY created_at ASC").fetchall()
    payloads: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        try:
            payloads.append((str(row["created_at"] or ""), json.loads(row["payload_json"])))
        except Exception:
            continue
    return payloads


def _cohort_stats(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    payloads = [payload for _created_at, payload in rows]
    if not payloads:
        return {"rows": 0}
    return {
        "rows": len(payloads),
        "structural_break_rate_pct": round(sum(bool(p.get("structural_break_detected")) for p in payloads) / len(payloads) * 100.0, 1),
        "optimistic_rate_pct": round(sum(p.get("model_bias_signal") == "optimistic" for p in payloads) / len(payloads) * 100.0, 1),
        "revenue_error": _metric_stats([p.get("revenue_error_pct") for p in payloads]),
        "ev_error": _metric_stats([p.get("ev_error_pct") for p in payloads]),
        "price_return_error": _metric_stats([p.get("price_return_error_pct") for p in payloads]),
    }


def _split_cohorts(rows: list[tuple[str, dict[str, Any]]], *, chunk_size: int) -> dict[str, Any]:
    chunk_size = max(int(chunk_size or 0), 1)
    first = rows[:chunk_size]
    latest = rows[-chunk_size:] if rows else []
    cohorts = {
        "overall": rows,
        "first_chunk": first,
        "latest_chunk": latest,
        "latest_stable_only": [row for row in latest if not row[1].get("structural_break_detected")],
        "latest_structural_break_only": [row for row in latest if row[1].get("structural_break_detected")],
    }
    return {name: _cohort_stats(items) for name, items in cohorts.items()}


def _ledger_counts(reader: LedgerReader) -> dict[str, Any]:
    with sqlite3.connect(reader.db_path) as conn:
        counts: dict[str, Any] = {}
        for table in ("prediction_records", "realized_outcomes", "postmortem_records", "maintenance_runs"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[table] = 0
        for label, table in (
            ("latest_prediction", "prediction_records"),
            ("latest_postmortem", "postmortem_records"),
            ("latest_maintenance", "maintenance_runs"),
        ):
            try:
                counts[label] = conn.execute(f"SELECT MAX(created_at) FROM {table}").fetchone()[0]
            except Exception:
                counts[label] = None
    return counts


def _throughput(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    times = sorted(time for time in (_parse_dt(row[0]) for row in rows) if time is not None)
    if len(times) < 2:
        return {}
    result: dict[str, Any] = {}
    elapsed_hours = max((times[-1] - times[0]).total_seconds() / 3600.0, 1 / 3600.0)
    result["postmortems_per_hour_since_first"] = round(len(times) / elapsed_hours, 1)
    for size in (100, 500, 1000, 5000):
        if len(times) >= size:
            window = times[-size:]
            hours = max((window[-1] - window[0]).total_seconds() / 3600.0, 1 / 3600.0)
            result[f"latest_{size}_postmortems_per_hour"] = round(size / hours, 1)
    return result


def _calibration_summary(store: CalibrationStore) -> dict[str, Any]:
    if not Path(store.db_path).exists():
        return {"prior_count": 0}
    with sqlite3.connect(store.db_path) as conn:
        prior_count = conn.execute("SELECT COUNT(*) FROM calibration_priors").fetchone()[0]
        abs_gt_1 = conn.execute("SELECT COUNT(*) FROM calibration_priors WHERE ABS(correction_mean) > 1").fetchone()[0]
        abs_gt_10 = conn.execute("SELECT COUNT(*) FROM calibration_priors WHERE ABS(correction_mean) > 10").fetchone()[0]
        largest = conn.execute(
            """
            SELECT sector, industry, assumption_name, correction_mean, cohort_size, last_updated
            FROM calibration_priors
            ORDER BY ABS(correction_mean) DESC
            LIMIT 10
            """
        ).fetchall()
    return {
        "prior_count": prior_count,
        "abs_correction_gt_1": abs_gt_1,
        "abs_correction_gt_10": abs_gt_10,
        "largest_corrections": [
            {
                "sector": row[0],
                "industry": row[1],
                "assumption_name": row[2],
                "correction_mean": round(float(row[3]), 6),
                "cohort_size": int(row[4]),
                "last_updated": row[5],
            }
            for row in largest
        ],
    }


def build_learning_performance_report(
    *,
    reader: LedgerReader | None = None,
    calibration_store: CalibrationStore | None = None,
    chunk_size: int = 5000,
) -> dict[str, Any]:
    reader = reader or LedgerReader()
    calibration_store = calibration_store or CalibrationStore()
    postmortems = _postmortem_payloads(reader)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger": _ledger_counts(reader),
        "cohorts": _split_cohorts(postmortems, chunk_size=chunk_size),
        "throughput": _throughput(postmortems),
        "calibration": _calibration_summary(calibration_store),
        "targets": {
            "abs_correction_gt_10": 0,
            "latest_stable_revenue_mae_max_pct": 10.0,
            "latest_ev_median_error_goal_pct": -30.0,
            "latest_ev_mae_goal_pct": 60.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Print learning performance diagnostics as JSON.")
    parser.add_argument("--chunk-size", type=int, default=5000)
    args = parser.parse_args()
    print(json.dumps(build_learning_performance_report(chunk_size=args.chunk_size), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
