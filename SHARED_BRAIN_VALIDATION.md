# Shared-Brain Validation Snapshot

This document records the current evidence for the shared-brain system.
It is intentionally conservative: the goal is to show what improved, what remains stable, and what still blocks end-state acceptance.

## How To Re-Run

```bash
python validate_shared_brain.py --no-diagnostics
python check.py --strict-learning
```

The benchmark command measures out-of-sample forecast quality on the packaged time-aware suite.
The health-check command verifies that the live dashboard contract still holds and that the local learning ledger is operational.

## Current Benchmark Snapshot

Command:

```bash
python validate_shared_brain.py --no-diagnostics
```

Observed result:

- Cases: `8`
- Time-aware violations: `0`
- Performance: `395.17ms`
- Revenue growth MAE: `0.6875 -> 0.6250` (`+9.09%` improvement)
- EBIT margin MAE: `0.1978 -> 0.1890` (`+4.45%` improvement)
- UFCF error pct MAE: `1.7744 -> 1.7068` (`+3.81%` improvement)
- Valuation error pct MAE: `10.8950 -> 10.4527` (`+4.06%` improvement)
- Valuation error p50: `11.03% -> 10.91%`
- Confidence ranking accuracy: `66.67%`
- Analog consistency rate: `100.00%`
- Acceptance status: `provisional`

Current benchmark conclusion:

- The packaged out-of-sample suite now beats baseline across revenue growth, EBIT margin, UFCF error, and valuation error.

## Current Operational Snapshot

Command:

```bash
python check.py --strict-learning
```

Observed result:

- Live dashboard health check passed for `AMZN`, `NKE`, `AAPL`, and `TSLA`.
- The live EODHD path returned a `knowledge_model` payload without hard contract failures.
- The learning ledger reported `9` prediction records, `0` matured records, `0` postmortems, and `0` quinquennial reports.
- Scheduled maintenance is active and the latest run was recorded at `2026-05-03T02:45:14.864938+00:00`.
- The health check emitted warnings for a thin calibration cohort, one slow `AMZN` response, and the absence of postmortems, but no hard runtime failures.

## What Is Proven

- The shared-brain system is now integrated into the live EODHD dashboard payload rather than existing only as an unused side path.
- The validation harness is time-aware and catches contract regressions in the learning stack.
- The shared-brain layer improves revenue-growth, EBIT-margin, UFCF, and valuation accuracy on the packaged benchmark suite.
- The application can run with the new layer enabled while the runtime health check continues to pass.

## What Still Blocks Full Acceptance

- The learning system does not yet have enough realized postmortem evidence to claim durable field performance.
- The current status should remain `provisional` until the live ledger matures and the postmortem layer has enough realized evidence to support a full acceptance claim.

## Interpretation

The repository is now measurable and operationally safer, which was the main hardening goal.
It is not yet honest to claim full end-state acceptance.
The correct current position is: live integration works, the benchmark is now provisionally better than baseline, and the remaining limit is the thin realized evidence in the live learning ledger.