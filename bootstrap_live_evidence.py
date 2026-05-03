from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Sequence

from auto_valuation.learning.maintenance import run_live_evidence_bootstrap
from auto_valuation.validation.shared_brain import collect_operational_diagnostics


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay matured forecasts and materialize live learning evidence.")
    parser.add_argument("--tickers", nargs="*", help="Optional ticker list. Defaults to ledger tickers plus common cached names.")
    parser.add_argument("--max-tickers", type=int, default=3, help="Maximum number of tickers to process when --tickers is omitted.")
    parser.add_argument("--as-of-date", type=date.fromisoformat, help="Cutoff date for replay and realized evidence alignment.")
    parser.add_argument("--max-replay-predictions", type=int, default=6, help="Maximum replayed one-year predictions per ticker.")
    parser.add_argument("--no-replay", action="store_true", help="Skip prediction replay and only backfill matured existing records.")
    parser.add_argument("--skip-diagnostics", action="store_true", help="Skip the post-run shared-brain operational diagnostics check.")
    parser.add_argument("--json-out", type=Path, help="Optional path to write the bootstrap result and diagnostics as JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run_live_evidence_bootstrap(
        tickers=args.tickers or None,
        max_tickers=args.max_tickers,
        as_of_date=args.as_of_date,
        max_replay_predictions_per_ticker=args.max_replay_predictions,
        replay_enabled=not args.no_replay,
    )

    payload: dict[str, object] = {"bootstrap": result.to_dict()}
    print(
        json.dumps(
            {
                "as_of_date": result.as_of_date,
                "replay_predictions_created": result.replay_predictions_created,
                "realized_outcomes_created": result.realized_outcomes_created,
                "annual_postmortems_created": result.annual_postmortems_created,
                "quinquennial_reports_created": result.quinquennial_reports_created,
                "skipped_records": result.skipped_records,
                "missing_labels": result.missing_labels,
            },
            indent=2,
        )
    )

    if not args.skip_diagnostics:
        diagnostics = collect_operational_diagnostics()
        payload["diagnostics"] = asdict(diagnostics)
        print(
            json.dumps(
                {
                    "prediction_records": diagnostics.prediction_records,
                    "postmortem_records": diagnostics.postmortem_records,
                    "matured_without_postmortem": diagnostics.matured_without_postmortem,
                    "maintenance_stale": diagnostics.maintenance_stale,
                },
                indent=2,
            )
        )

    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return 0 if result.ran else 1


if __name__ == "__main__":
    raise SystemExit(main())