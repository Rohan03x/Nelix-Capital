# Learning Accuracy Fix Plan

## Current Diagnosis

The local learning ledger is large enough to reveal model behavior: revenue learning is useful, while EV and price-return learning are still weak. The main accuracy blocker is not training volume; it is noisy residual memory entering calibration and an optimistic DCF enterprise-value bias.

Current baseline from the local ledger:
- Predictions: 29,219
- Realized outcomes: 59,355
- Postmortems: 29,146
- Calibration priors: 7,637
- Latest stable-company revenue MAE: about 9.2%
- Latest EV MAE: about 73.7%
- Latest EV median error: about -56.9%
- Priors with absolute correction above 10: 373 before residual caps

## Fixes Implemented

1. Central residual controls now live in `auto_valuation/learning/residual_controls.py`.
2. Layered live calibration clamps and robustly trims assumption residuals before applying or saving priors.
3. Legacy calibration and historical replay use the same caps, so rebuilt calibration stores cannot persist absurd correction means.
4. Market residual overlay now rejects extreme EV labels, clamps evidence residuals, and allows a stronger negative correction than positive correction because the measured model bias is optimistic.
5. `auto_valuation/learning/performance_report.py` provides repeatable JSON metrics for stable vs structural-break cohorts.
6. `learning_worker.py` runs local learning cycles without relying on the Flask dashboard process.
7. Production sync now chunks SQLite snapshots before publishing, so large local ledgers do not have to fit in one Supabase/Postgres JSON object.

## Why GPU Is Not The First Fix

The current learning system is not a neural-network training stack. It is mostly:
- SQLite reads and writes
- EODHD/API/cache fetching
- empirical-Bayes residual calibration
- DCF calculations
- robust statistics and peer/analog matching

Those operations are I/O-bound and small-tabular-data bound. A GPU would sit mostly idle unless we add a real model-training layer. With roughly 30k postmortems, CPU models are enough for the next stage.

## Better Model Options

Best near-term option: a CPU gradient-boosted residual model.

Recommended sequence:
1. Keep the current rules-based DCF as the base valuation engine.
2. Use the cleaned ledger to train an EV residual model with features such as sector, industry, market-cap regime, revenue growth, margins, WACC, terminal growth, leverage, structural-break flags, analog scores, and macro regime.
3. Start with CatBoost or XGBoost on CPU. They handle tabular nonlinear relationships better than a neural net at this data size.
4. Use GPU only if the dataset grows into hundreds of thousands or millions of clean rows, or if we add a graph neural network for peer relationships.

GPU-suitable future options:
- XGBoost GPU histogram for large residual datasets.
- CatBoost GPU for categorical-heavy sector/industry/ticker features.
- PyTorch graph neural network for peer/relationship memory once the peer graph is large and clean.

Current recommendation: do not add PyTorch/TensorFlow yet. Fix residual quality, production persistence, and EV calibration first; then evaluate CatBoost/XGBoost as an optional residual model.

## How To Operate

Print a benchmark report:

```powershell
& ".\.venv\Scripts\python.exe" -m auto_valuation.learning.performance_report
```

Run one local learning cycle:

```powershell
& ".\.venv\Scripts\python.exe" learning_worker.py --once
```

Run continuous local learning cycles:

```powershell
& ".\.venv\Scripts\python.exe" learning_worker.py
```

Check worker/report status:

```powershell
& ".\.venv\Scripts\python.exe" learning_worker.py --status
```

Repair already-persisted priors after changing residual bounds:

```powershell
& ".\.venv\Scripts\python.exe" -m auto_valuation.learning.calibration_repair
```

## Next Accuracy Gate

After rebuilding calibration with the new caps, the next pass should compare against these targets:
- absolute correction greater than 10: 0
- latest stable revenue MAE: around 9-10%
- latest EV MAE: below 60%
- latest EV median error: better than -35%
