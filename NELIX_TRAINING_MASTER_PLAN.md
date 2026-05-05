# Nelix Training Master Plan

Date: 2026-05-05
Status: Proposed master plan
Scope: Best-quality adaptive valuation training system, not quickest incremental fix

## 1. Executive Summary

The current Nelix learning system is collecting useful evidence, but it is not yet optimized to improve valuation predictions as much as it could. The core issue is not lack of data volume. The system already has tens of thousands of prediction, outcome, and postmortem records. The issue is that different kinds of learning evidence are mixed together, noisy rows are allowed to influence calibration, and the largest miss driver, market EV/price rerating, is not modeled directly enough.

The best improvement is therefore not simply "train longer". The best improvement is to redesign the training architecture into a point-in-time, quality-gated, target-separated, market-implied hybrid valuation engine.

The target model should be a layered ensemble:

1. A deterministic DCF base model.
2. A cleaned operating-assumption calibration model for revenue, margins, reinvestment, WACC, terminal growth, and beta.
3. A market-implied residual model that learns EV/price misses directly.
4. Sector and regime specialist models.
5. A blend/gating model that decides how much to trust DCF vs learned market residual vs peer multiple evidence.
6. A conformal uncertainty layer that gives realistic error bands instead of fake precision.

This plan assumes the goal is maximum prediction quality and institutional robustness, even if implementation takes longer and requires a more sophisticated offline training stack.

## 2. Current Diagnosis

Recent local diagnostics showed the model is learning, but the learning target is incomplete.

Snapshot from the current training database:

| Metric | Value |
|---|---:|
| Universe | 10,512 tickers |
| With predictions | 4,353, or 41.4% |
| Total predictions | 19,392 |
| Realized outcomes | 39,578 |
| Postmortems | 19,319 |
| Complete postmortems | 15,967 |
| DCF-quality learning rows | 9,394 / 19,247, or 48.8% |

Quality-filtered errors:

| Target | Median | Mean | P95 |
|---|---:|---:|---:|
| Revenue error | 11.3% | 22.9% | 59.9% |
| Margin error | 306 bps | 808 bps | 3,410 bps |
| EV error | 70.3% | 397.6% | 1,139% |
| Price error | 79.0% | 415.5% | 1,432% |

Postmortem attribution:

| Driver | Approx Share |
|---|---:|
| Price return | 46% |
| Enterprise value | 41% |
| Margin | 9% |
| Revenue | 4% |

Interpretation:

- Revenue forecasting is the healthiest part of the system.
- Margin forecasting is noisy but improvable with quality gates and sector specialists.
- EV/price prediction is structurally weak because market multiple rerating is not directly learned.
- The current learning engine tries to correct valuation misses mostly through operating-assumption nudges. That creates a ceiling.
- More training without target separation will add more data but may not meaningfully reduce EV/price error.

## 3. Design Principles

### 3.1 Optimize For Truth, Not Speed

This plan favors correctness, walk-forward validity, robust labels, and interpretable uncertainty over fast-looking gains.

A fast improvement would add simple filters and increase weights. The best improvement builds a durable training system that can survive noisy international data, restatements, splits, currency issues, sector-specific accounting, and regime changes.

### 3.2 Keep DCF As The Spine

The DCF remains the primary explanatory model. The model should not become a black-box price predictor that loses valuation discipline.

However, a pure DCF is not enough for public-market price/EV prediction because much of the error comes from changing market multiples. The final engine should be a hybrid valuation system:

```text
Final Value = DCF Base Value
            + Operating-Assumption Calibration
            + Market-Implied Valuation Residual
            + Peer/Multiple Sanity Overlay
            + Regime/Momentum Adjustment
```

### 3.3 Separate What Can Be Learned From What Cannot

Revenue and margins are operational targets. EV and price are market targets. WACC and terminal growth are partially observable implied quantities, not direct actuals.

The training system must separate:

- Fundamental outcome learning.
- Cash-flow quality learning.
- Market valuation residual learning.
- Price return learning.
- Confidence and uncertainty learning.

### 3.4 Never Mix Invalid Targets

A quarterly revenue record must not train full DCF valuation. A price-only outcome must not train margins. A historical replay record with zero predicted EV must not train price/EV residuals.

Every observation must declare which targets it is eligible to train.

### 3.5 Train Offline, Serve Light

The best model may require heavier libraries such as scikit-learn, LightGBM, CatBoost, Optuna, statsmodels, or PyMC. Those should be offline-only dependencies in a separate training environment.

Vercel production should serve lightweight artifacts:

- JSON calibration priors.
- Coefficients.
- Small model manifests.
- Optional compressed tree model artifacts if size is acceptable.

Do not put heavy ML dependencies in the Vercel runtime bundle.

## 4. Target Architecture: Nelix Adaptive Valuation Engine V4

### 4.1 System Layers

```text
Raw Provider Data
    -> Point-In-Time Data Lake
    -> Data Quality + Entity Resolution
    -> Feature Store
    -> Label Store
    -> Observation Eligibility Layer
    -> Training Dataset Builder
    -> Model Stack
    -> Walk-Forward Evaluation
    -> Model Registry
    -> Lightweight Inference Artifacts
    -> Webapp / Workbook / API
```

### 4.2 Model Stack

The recommended best-quality model is not one model. It is a stacked, interpretable ensemble.

#### Layer A: Deterministic DCF Base

Inputs:

- Company history.
- Sector defaults.
- Macro rates.
- Current balance sheet.
- Working capital and reinvestment assumptions.
- Existing DCF mechanics.

Output:

- Base intrinsic value.
- Base EV.
- Base price per share.
- Base forecast assumptions.

Purpose:

- Maintain valuation discipline.
- Produce explainable cash-flow mechanics.
- Provide the anchor all learning layers adjust around.

#### Layer B: Operating Residual Calibrator

Targets:

- One-year revenue growth residual.
- Multi-year revenue CAGR residual.
- EBIT margin residual.
- UFCF margin residual.
- Reinvestment rate residual.
- Tax-rate residual.
- Capex intensity residual.

Recommended model:

- Hierarchical Bayesian shrinkage or empirical Bayes residual model.
- Grouped by sector, industry, canonical industry, exchange, market-cap regime, growth regime, profitability regime, and macro regime.
- With time decay and quality weighting.

Purpose:

- Improve actual DCF assumptions.
- Avoid overfitting thin cohorts.
- Let broad sector memory help sparse names without overwhelming company evidence.

#### Layer C: Market-Implied Valuation Residual Model

Targets:

- EV error percentage.
- Price return error percentage.
- Implied WACC delta.
- Implied terminal growth delta.
- Implied EV/revenue multiple delta.
- Implied EV/EBITDA multiple delta.
- Implied P/E or P/B delta where applicable.
- DCF-to-market residual multiplier.

Recommended model:

- Gradient-boosted trees for nonlinear interactions, plus monotonic constraints where sensible.
- Hierarchical fallback priors when tree model confidence is low.
- Separate specialists for:
  - Profitable mature companies.
  - High-growth unprofitable companies.
  - Financials.
  - REITs.
  - Commodity/mining/energy.
  - Microcaps and penny stocks.
  - International/ADR/cross-listed names.

Purpose:

- Directly learn the part current DCF learning misses: market multiple behavior.
- Avoid forcing market rerating errors into revenue/margin assumptions.

#### Layer D: Peer/Comps Residual Model

Targets:

- Peer-implied EV.
- Peer-implied equity value.
- Multiple percentile position.
- Spread between DCF value and peer-implied value.

Features:

- Peer basket quality.
- Same-industry peer count.
- Canonical industry distance.
- Market cap ratio.
- Growth/margin similarity.
- Capital intensity similarity.
- Exchange/country risk similarity.
- Pair memory strength from manual compare and automatic peer baskets.

Purpose:

- Let market pricing inform valuation without blindly copying comps.
- Improve high-growth and cyclical sectors where DCF can be unstable.

#### Layer E: Blend/Gating Model

Targets:

- Best blend weight between DCF, learned residual, and peer-implied value.
- Expected error by target.
- Whether model should widen uncertainty rather than shift point estimate.

Recommended model:

- Calibrated probabilistic classifier/regressor.
- Inputs include data quality, sector, maturity, forecast spread, confidence scores, model disagreement, and historical residuals.

Example output:

```json
{
  "dcf_weight": 0.55,
  "market_residual_weight": 0.30,
  "peer_weight": 0.15,
  "expected_abs_price_error_pct": 42.0,
  "prediction_interval_pct": {"p10": -35, "p50": 0, "p90": 80}
}
```

Purpose:

- Decide when to trust DCF and when to respect market-implied evidence.
- Stop one global blend from being applied to all companies.

#### Layer F: Uncertainty / Conformal Calibration

Targets:

- Prediction intervals for revenue, margin, EV, and price.
- Directional confidence.
- Error bands conditioned on sector and data quality.

Recommended method:

- Conformal prediction on walk-forward residuals.
- Separate intervals by model family and sector/regime.

Purpose:

- Report realistic uncertainty.
- Penalize low-quality cohorts.
- Avoid overconfident predictions on thin or unstable evidence.

## 5. Data Model Required

### 5.1 Prediction Observation Types

Every prediction record needs an explicit observation type.

Recommended values:

```text
annual_dcf_base
annual_dcf_live
annual_dcf_historical_replay
quarterly_revenue
price_only
peer_comps_snapshot
reverse_dcf_snapshot
manual_override_case
```

Each type should have target eligibility.

| Observation Type | Revenue | Margin | UFCF | WACC | Terminal Growth | EV | Price | Multiples |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| annual_dcf_base | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| annual_dcf_historical_replay | Yes | Yes | Yes | Maybe | Maybe | Yes if valid | Yes if valid | Yes if valid |
| quarterly_revenue | Yes | Limited | No | No | No | No | No | No |
| price_only | No | No | No | No | No | Limited | Yes | Limited |
| peer_comps_snapshot | No | No | No | No | No | Yes | Yes | Yes |

### 5.2 Label Store

Create a normalized label layer instead of relying only on columns in `prediction_records`.

Suggested table: `learning_labels`

```sql
CREATE TABLE learning_labels (
    label_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_value REAL,
    predicted_value REAL,
    residual_value REAL,
    residual_pct REAL,
    label_status TEXT NOT NULL,
    quality_score REAL NOT NULL,
    eligibility_scope TEXT NOT NULL,
    source_name TEXT,
    source_kind TEXT,
    as_of_date TEXT,
    aligned_period_end TEXT,
    source_payload_json TEXT,
    quality_reasons_json TEXT,
    created_at TEXT NOT NULL
);
```

Targets:

```text
revenue_mm
revenue_growth_pct
ebit_margin_pct
ufcf_margin_pct
reinvestment_rate_pct
ev_mm
equity_value_mm
price_per_share
price_return_pct
implied_wacc_pct
implied_terminal_growth_pct
ev_revenue_multiple
ev_ebitda_multiple
pe_multiple
pb_multiple
valuation_residual_pct
```

### 5.3 Feature Store

Suggested table: `learning_features`

```sql
CREATE TABLE learning_features (
    feature_row_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    feature_as_of_date TEXT NOT NULL,
    feature_vector_json TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    point_in_time_valid BOOLEAN NOT NULL,
    provider_payload_hash TEXT,
    quality_score REAL NOT NULL,
    created_at TEXT NOT NULL
);
```

Feature groups:

1. Company fundamentals.
2. Growth regime.
3. Profitability regime.
4. Capital intensity.
5. Balance sheet risk.
6. Cash-flow conversion.
7. Market valuation state.
8. Momentum and drawdown.
9. Analyst estimate revision if available.
10. Macro regime.
11. Sector and industry taxonomy.
12. Listing/exchange/currency metadata.
13. Peer basket quality.
14. Model disagreement features.
15. Data quality features.

### 5.4 Quality Store

Suggested table: `learning_quality_audit`

```sql
CREATE TABLE learning_quality_audit (
    audit_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    quality_score REAL NOT NULL,
    target_eligibility_json TEXT NOT NULL,
    exclusion_reasons_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Quality reasons should be explicit and displayable:

```text
predicted_ev_too_small
predicted_price_too_small
actual_revenue_too_small
missing_shares_outstanding
margin_out_of_bounds
currency_mismatch_possible
split_adjustment_unknown
price_only_label
quarterly_record_not_valuation_eligible
financial_sector_requires_specialist
reit_requires_specialist
mining_requires_specialist
insufficient_peer_quality
stale_price
lookahead_risk
restatement_risk
```

## 6. Data Quality Rules

### 6.1 Hard Exclusions For Full DCF Calibration

Exclude from full DCF calibration when:

- Predicted revenue is below $10m.
- Actual revenue is below $10m.
- Predicted EV is below $1m.
- Predicted price is <= $1.
- Shares outstanding is missing or implausible.
- Actual EBIT margin absolute value exceeds 100% after normalization.
- Predicted EBIT margin absolute value exceeds 100% after normalization.
- Currency conversion is unknown.
- Split adjustment cannot be verified.
- The record is quarterly-only.
- The record is price-only.
- The sector needs a specialist and no specialist exists.

These rows can still train restricted targets where valid.

### 6.2 Soft Downweights

Downweight when:

- Market cap is microcap.
- Ticker is OTC/PINK.
- Sector is biotech pre-revenue.
- Financial statements are sparse.
- Price is stale.
- Peer basket is weak.
- Restatement risk is high.
- Structural break is active.
- Label was backfilled from restated provider data.
- Horizon alignment required fallback.

### 6.3 Quality Score Formula

Suggested initial formula:

```text
quality_score = base
              * source_reliability
              * target_completeness
              * currency_confidence
              * split_confidence
              * entity_resolution_confidence
              * horizon_alignment_confidence
              * sector_model_eligibility
              * outlier_penalty
```

Clamp to `[0.0, 1.0]`.

Suggested interpretation:

| Score | Meaning |
|---:|---|
| 0.90-1.00 | High-quality institutional training row |
| 0.70-0.89 | Usable with normal weight |
| 0.50-0.69 | Usable with downweight |
| 0.30-0.49 | Restricted-target only |
| <0.30 | Audit only, do not train |

## 7. Feature Engineering Plan

### 7.1 Company Fundamental Features

- Revenue CAGR 1y, 3y, 5y.
- Revenue volatility.
- Gross margin level and trend.
- EBIT margin level, trend, and volatility.
- UFCF margin level and trend.
- FCF conversion ratio.
- Capex/revenue.
- D&A/revenue.
- SBC/revenue.
- Working capital days.
- Tax rate stability.
- Leverage.
- Net debt/EBITDA.
- Cash/revenue.
- Interest coverage.
- ROIC proxy.
- Asset turnover.

### 7.2 Market Features

- EV/revenue.
- EV/EBITDA.
- P/E where meaningful.
- P/B for financials.
- Price momentum 1m, 3m, 6m, 12m.
- Drawdown from 52-week high.
- Volatility.
- Market cap regime.
- Liquidity proxy.
- Listing exchange.
- Country/currency risk.

### 7.3 Macro Features

- Risk-free rate.
- ERP.
- Credit spread proxy if available.
- Inflation regime if available.
- Rate regime: low, neutral, rising, restrictive.
- Yield change over trailing year.
- Sector sensitivity to rates.

### 7.4 Valuation Model Features

- DCF terminal value percentage of EV.
- Spread: WACC minus terminal growth.
- Forecast fade steepness.
- Bull/base/bear spread.
- Scenario width multiplier.
- DCF vs peer median spread.
- Reverse DCF required growth.
- Reverse DCF required margin.
- Confidence sub-scores.
- Layer disagreement.

### 7.5 Peer Features

- Number of same-industry peers.
- Peer basket quality score.
- Median peer multiple.
- Subject percentile vs peers.
- Pair relationship strength.
- Analog similarity.
- Same-currency peer count.
- Same-exchange peer count.

## 8. Label Engineering Plan

### 8.1 Fundamental Labels

For each prediction horizon:

```text
actual_revenue_growth = actual_revenue / base_revenue - 1
revenue_growth_residual = actual_revenue_growth - predicted_revenue_growth
margin_residual = actual_ebit_margin - predicted_ebit_margin
ufcf_margin_residual = actual_ufcf_margin - predicted_ufcf_margin
reinvestment_residual = actual_reinvestment_rate - predicted_reinvestment_rate
```

### 8.2 Market-Implied Labels

For each complete annual postmortem:

```text
actual_ev = actual_price_at_horizon * shares_outstanding_at_horizon + net_debt_at_horizon
predicted_ev = DCF enterprise value from prediction date
valuation_residual_pct = actual_ev / predicted_ev - 1
```

Solve implied DCF variables:

```text
implied_wacc = WACC that makes DCF EV equal actual EV
implied_terminal_growth = terminal growth that makes DCF EV equal actual EV
implied_wacc_delta = implied_wacc - predicted_wacc
implied_terminal_growth_delta = implied_terminal_growth - predicted_terminal_growth
```

If the solver fails or produces implausible values, flag the label as restricted.

### 8.3 Multiple Labels

```text
actual_ev_revenue_multiple = actual_ev / actual_revenue
predicted_ev_revenue_multiple = predicted_ev / predicted_revenue
multiple_residual = log(actual_multiple / predicted_multiple)
```

Create separate labels for:

- EV/revenue.
- EV/EBITDA.
- P/E.
- P/B.
- EV/UFCF.

Only compute labels when denominators are meaningful.

### 8.4 Direction Labels

For price direction:

```text
predicted_return = predicted_price / price_at_prediction - 1
actual_return = actual_price_at_horizon / price_at_prediction - 1
direction_correct = sign(predicted_return) == sign(actual_return)
```

Direction accuracy should be tracked separately from price-level MAE.

## 9. Recommended Model Choices

### 9.1 Best Practical Offline Stack

Recommended training-only dependencies:

```text
scikit-learn
lightgbm or catboost
optuna
statsmodels
joblib
pyarrow
```

Optional advanced dependencies:

```text
pymc or numpyro
shap
polars
```

Keep these in `requirements-train.txt`, not root `requirements.txt`, so Vercel stays light.

### 9.2 Operating Assumption Model

Best initial model:

- Hierarchical empirical Bayes residual model.
- Stratified by sector, canonical industry, maturity bucket, market cap, growth regime, profitability regime, macro regime.
- Uses quality-weighted robust residual means.
- Applies time decay.
- Uses fallback hierarchy:

```text
company -> canonical industry -> industry family -> sector -> market-cap/macro -> global
```

Why not only gradient boosting here?

- Operating assumptions must remain interpretable.
- Cohort shrinkage prevents noisy sparse buckets from overfitting.
- This layer feeds DCF mechanics and should be stable.

### 9.3 Valuation Residual Model

Best model:

- Gradient-boosted regression trees for `valuation_residual_pct` and `log(actual_ev / predicted_ev)`.
- Separate model or multi-target setup for implied WACC delta and implied terminal growth delta.
- Monotonic constraints where sensible:
  - Higher leverage should not lower risk adjustment without offsetting evidence.
  - Higher uncertainty should widen bands.
  - Higher DCF terminal value share should increase expected error.

Fallback:

- Hierarchical prior if model confidence is low or feature quality is poor.

### 9.4 Blend/Gating Model

Best model:

- A calibrated regressor/classifier that predicts expected absolute error for each valuation source.
- It then assigns weights inversely proportional to expected error.

Example:

```text
weight_dcf = 1 / expected_error_dcf
weight_residual = 1 / expected_error_residual
weight_peer = 1 / expected_error_peer
normalize weights to sum to 1
```

This avoids a fixed DCF-vs-market blend.

### 9.5 Uncertainty Model

Best model:

- Conformal prediction on walk-forward residuals.
- Calibrated by sector, quality tier, and model family.

Output:

```json
{
  "value_p10": 42.0,
  "value_p50": 58.0,
  "value_p90": 91.0,
  "expected_abs_error_pct": 38.0,
  "confidence_score": 0.72
}
```

## 10. Training Pipeline

### 10.1 Daily Offline Pipeline

```text
1. Sync production snapshots from Supabase.
2. Build point-in-time feature snapshots.
3. Build normalized label rows.
4. Run quality audit.
5. Build training datasets by target family.
6. Run walk-forward validation.
7. Train candidate models.
8. Compare against current production artifact.
9. Register artifact only if it beats baseline on acceptance metrics.
10. Export lightweight artifacts for app inference.
11. Push artifact metadata and summary metrics to Supabase.
```

### 10.2 Walk-Forward Validation

Never random-split financial time-series labels.

Use expanding-window validation:

```text
Train through 2018 -> validate 2019
Train through 2019 -> validate 2020
Train through 2020 -> validate 2021
Train through 2021 -> validate 2022
Train through 2022 -> validate 2023
Train through 2023 -> validate 2024
Train through 2024 -> validate 2025
```

Track performance by:

- Sector.
- Canonical industry.
- Exchange/country.
- Market cap regime.
- Quality tier.
- Growth/profitability regime.
- Observation type.

### 10.3 Model Promotion Rule

A model is promoted only if it improves:

- Revenue MAE or does not degrade it by more than 2%.
- Margin MAE or does not degrade it by more than 5%.
- EV median absolute error by at least 10%.
- Price median absolute error by at least 10%.
- Direction accuracy by at least 3 percentage points or does not degrade.
- Calibration interval coverage stays within expected bounds.

If a candidate improves overall but hurts a sector badly, promote only with sector-specific gates.

## 11. Inference Design

### 11.1 Current Inference Problem

Current inference mainly outputs refined assumptions:

```text
revenue_growth_near
ebit_margin_target
wacc
terminal_growth
beta
capex_pct
```

This is necessary but incomplete because EV/price error is not purely an operating-assumption problem.

### 11.2 Target Inference Flow

```text
1. Build base DCF.
2. Apply operating assumption calibrator.
3. Re-run DCF.
4. Compute market-implied residual overlay.
5. Compute peer-implied valuation overlay.
6. Run blend/gating model.
7. Produce final value and uncertainty band.
8. Explain every adjustment.
```

### 11.3 Final Value Formula

```text
adjusted_dcf_value = DCF(value, learned_operating_assumptions)
residual_value = adjusted_dcf_value * (1 + learned_valuation_residual_pct)
peer_value = peer_implied_value

final_value = dcf_weight * adjusted_dcf_value
            + residual_weight * residual_value
            + peer_weight * peer_value
```

### 11.4 Guardrails

- Do not apply positive residual overlay when data quality is poor.
- Cap residual overlay when DCF terminal value is already too high.
- Use sector specialist for financials, REITs, mining, banks, insurance, and biotech pre-revenue.
- If peer evidence is poor, peer weight must be near zero.
- If company is structurally breaking, widen interval before shifting point estimate.
- If model disagreement is extreme, show lower confidence rather than forcing a precise value.

## 12. Sector Specialist Models

### 12.1 Financials

DCF UFCF is usually inappropriate.

Use:

- P/B.
- ROE.
- Cost of equity.
- Dividend discount or residual income.
- Net interest margin trends.
- CET1 / leverage where available.

### 12.2 REITs

Use:

- FFO/AFFO.
- Cap rates.
- NAV estimates.
- Debt maturity/rate sensitivity.
- Occupancy and same-store NOI if available.

### 12.3 Energy and Mining

Use:

- Commodity regime.
- Reserve life if available.
- Mid-cycle margin.
- EV/EBITDA and NAV-style overlays.
- Higher cyclicality uncertainty.

### 12.4 Biotech and Pre-Revenue

Use:

- Pipeline/stage risk only if data exists.
- Otherwise restrict to low confidence and avoid normal DCF calibration.

### 12.5 High-Growth Software / Platforms

Use:

- Revenue growth durability.
- Gross margin.
- Rule of 40.
- SBC dilution.
- EV/revenue residual model.
- Long-duration rate sensitivity.

## 13. Production Artifact Design

### 13.1 Artifact Files

Suggested artifact layout:

```text
auto_valuation/learning/artifacts/
    model_manifest.json
    operating_calibration_priors.json
    valuation_residual_model.json
    blend_model.json
    conformal_intervals.json
    feature_schema.json
    quality_rules.json
```

### 13.2 Manifest

```json
{
  "artifact_version": "2026-05-05-v1",
  "trained_at": "2026-05-05T00:00:00Z",
  "feature_version": "v4.0",
  "label_version": "v4.0",
  "training_rows": 9394,
  "validation_windows": [2019, 2020, 2021, 2022, 2023, 2024, 2025],
  "promotion_status": "candidate",
  "metrics": {
    "revenue_median_abs_error_pct": 11.3,
    "margin_median_abs_error_bps": 306,
    "ev_median_abs_error_pct": 70.3,
    "price_median_abs_error_pct": 79.0
  }
}
```

### 13.3 Serving Rule

The webapp should never need to train a heavy model during a request. It should only:

- Load artifact.
- Build feature vector.
- Apply lightweight inference.
- Return explainability.

## 14. Implementation Roadmap

### Phase 0: Freeze Baseline and Diagnostics

Goal: Establish trustworthy baselines before changing behavior.

Tasks:

- Keep `training_quality.py` as a quick diagnostic.
- Add a formal test/report script under `tools/learning_audit.py`.
- Save daily metrics to `output/learning_audits/`.
- Record baseline metrics by sector/exchange/source.

Acceptance:

- One command produces coverage, data quality, and target error metrics.
- Results separate raw rows from quality-filtered rows.
- No model changes yet.

### Phase 1: Observation Quality Layer

Goal: Stop bad rows from contaminating training.

Files:

- `auto_valuation/learning/quality.py`
- `tests/test_learning_quality.py`

Tasks:

- Implement quality scoring.
- Implement target eligibility flags.
- Add reason codes.
- Add tests for zero EV, zero price, micro revenue, extreme margin, quarterly-only rows, price-only rows.

Acceptance:

- Full DCF calibration receives only eligible rows.
- Restricted rows remain available for appropriate targets.
- Dashboard can explain excluded learning rows.

### Phase 2: Target Separation

Goal: Stop one ledger table from implying one training purpose.

Files:

- `auto_valuation/learning/labels.py`
- `auto_valuation/learning/datasets.py`
- `auto_valuation/learning/ledger.py`

Tasks:

- Normalize postmortems into labels.
- Create target-specific datasets.
- Split annual, quarterly, price-only, and replay records.
- Add schema migration or compatibility adapter.

Acceptance:

- Revenue training dataset includes quarterly records.
- Valuation residual dataset excludes quarterly and invalid EV records.
- Price-only labels do not train operating assumptions.

### Phase 3: Point-In-Time Feature Store

Goal: Remove look-ahead and restatement leakage.

Files:

- `auto_valuation/learning/features.py`
- `auto_valuation/learning/point_in_time.py`
- `tests/test_point_in_time_features.py`

Tasks:

- Store feature snapshots as of prediction date.
- Track provider payload hash.
- Track filing date and availability date.
- Add restatement labels.
- Add split/currency confidence.

Acceptance:

- Walk-forward features never use future data.
- Feature snapshots are reproducible.
- Restated rows can be downweighted.

### Phase 4: Market-Implied Label Engine

Goal: Directly learn EV/price rerating.

Files:

- `auto_valuation/learning/market_implied.py`
- `tests/test_market_implied_labels.py`

Tasks:

- Solve implied WACC from actual EV.
- Solve implied terminal growth from actual EV.
- Compute multiple residuals.
- Compute valuation residual multiplier.
- Add solver failure reason codes.

Acceptance:

- Complete annual postmortems produce market-implied labels.
- Implausible labels are flagged and excluded.
- TSLA-like multiple rerating appears as valuation residual, not margin/revenue error.

### Phase 5: Stratified Dataset Builder

Goal: Replace newest-row sampling with balanced evidence.

Files:

- `auto_valuation/learning/sampling.py`
- `tests/test_learning_sampling.py`

Tasks:

- Sample by sector, exchange, cap regime, source, quality tier, and recency.
- Preserve recent observations without letting noisy recent regions dominate.
- Add configurable sample budgets.

Acceptance:

- Live model uses a balanced sample.
- Low-quality rows have lower or zero training weight.
- Sample composition is visible in diagnostics.

### Phase 6: Operating Calibrator V4

Goal: Improve fundamentals without overfitting.

Files:

- `auto_valuation/learning/operating_calibrator.py`
- `webapp/data/knowledge_model.py`

Tasks:

- Move residual calibration to target-specific datasets.
- Add quality weights.
- Add fallback hierarchy.
- Add target-specific confidence.
- Keep existing explainability contract stable.

Acceptance:

- Revenue and margin MAE improve or remain stable.
- Cohort confidence is target-specific.
- Weak targets widen ranges instead of corrupting assumptions.

### Phase 7: Valuation Residual Model

Goal: Improve EV and price prediction directly.

Files:

- `auto_valuation/learning/valuation_residual.py`
- `auto_valuation/learning/train_valuation_residual.py`
- `requirements-train.txt`

Tasks:

- Train residual model offline.
- Export lightweight artifact.
- Add inference adapter.
- Add fallback hierarchical residual priors.

Acceptance:

- EV median error improves at least 10% in walk-forward validation.
- Price median error improves at least 10% in walk-forward validation.
- No degradation in revenue/margin beyond allowed thresholds.

### Phase 8: Blend/Gating Model

Goal: Decide when to trust each valuation source.

Files:

- `auto_valuation/learning/blend_model.py`
- `webapp/data/knowledge_model.py`

Tasks:

- Estimate source-specific expected error.
- Blend DCF, residual-adjusted DCF, and peer-implied value.
- Expose blend weights in dashboard.

Acceptance:

- Blend weights change sensibly across mature/early/high-growth/financial names.
- Confidence increases only when realized validation supports it.

### Phase 9: Conformal Uncertainty

Goal: Produce reliable confidence bands.

Files:

- `auto_valuation/learning/conformal.py`
- `auto_valuation/learning/confidence.py`

Tasks:

- Build prediction intervals by quality tier and sector.
- Replace static expected-error mappings where possible.
- Keep confidence score interpretable.

Acceptance:

- 80% interval contains realized value roughly 80% of the time in validation.
- Low-quality rows widen intervals.
- Confidence no longer pretends EV/price precision where history says errors are wide.

### Phase 10: UI and Workbook Explainability

Goal: Make the new learning system transparent.

Files:

- `webapp/templates/dashboard.html`
- `webapp/data/eodhd_client.py`
- Excel output builder files

Add display fields:

- Training data quality.
- Excluded row count and reasons.
- Target-specific evidence counts.
- DCF weight.
- Residual model weight.
- Peer weight.
- Expected EV error.
- Expected price error.
- Market-implied adjustment.
- Implied WACC/terminal growth diagnostics.

Acceptance:

- User can see why value moved.
- User can see when model refused to learn from bad rows.
- Workbook mirrors dashboard explanation.

## 15. Measurement Dashboard

Track daily:

```text
coverage_total_symbols
coverage_symbols_with_predictions
quality_rows_total
quality_rows_trainable_full_dcf
quality_rows_restricted
quality_exclusion_reasons
revenue_mae_median
margin_mae_median_bps
ev_mae_median
price_mae_median
direction_accuracy
interval_coverage_80
interval_coverage_90
model_promotion_status
```

Track by segment:

- Sector.
- Industry.
- Exchange suffix.
- Market cap regime.
- Quality tier.
- Observation type.
- Source type.
- Structural-break flag.

## 16. Expected Improvement Path

### Short-Term After Quality Gates

Expected:

- Less noisy calibration.
- Higher confidence integrity.
- Lower margin outlier impact.
- More stable live assumptions.

Likely metric impact:

- Revenue median error similar or slightly better.
- Margin mean error materially lower after excluding extreme rows.
- EV/price point estimates not drastically improved yet.

### Medium-Term After Market-Implied Labels

Expected:

- EV and price predictions start improving because the right target is modeled.
- High-growth names no longer force all error into revenue/margin.
- DCF remains explainable, but valuation residual handles market behavior.

Likely metric impact:

- EV median error improves 10-25%.
- Price median error improves 10-20%.
- Direction accuracy improves if momentum/regime features are included.

### Long-Term After Specialist Models and Blend Model

Expected:

- Sector-specific error reduction.
- Financials/REITs/mining/biotech stop corrupting generic DCF calibration.
- Final values become less one-size-fits-all.

Likely metric impact:

- Major improvement in non-standard sectors.
- Better uncertainty calibration.
- More reliable confidence scores.

## 17. Implementation Prompts

### Prompt A: Quality Layer

```text
Implement auto_valuation/learning/quality.py.

Create LearningObservationQuality with:
- quality_score: float
- target_eligibility: dict[str, bool]
- hard_exclusion_reasons: list[str]
- soft_warning_reasons: list[str]
- observation_type: str

Rules:
- Exclude from full DCF calibration if predicted EV <= 1, predicted price <= 1, predicted/actual revenue < 10, margin abs > 1 after decimal normalization, quarterly-only record, price-only label, invalid currency/split metadata, or sector specialist is required.
- Allow target-specific eligibility. Quarterly records may train revenue but not EV/price. Price-only records may train price direction but not revenue/margin.
- Add tests for every reason code.
- Integrate into webapp/data/knowledge_model.py::_load_learning_cohort so calibration receives only eligible rows for each target.
```

### Prompt B: Label Store

```text
Implement auto_valuation/learning/labels.py and auto_valuation/learning/datasets.py.

Parse prediction_records, realized_outcomes, and postmortem_records.payload_json into normalized LearningLabel rows.
Each label must include record_id, ticker, target_name, predicted_value, target_value, residual_value, residual_pct, label_status, quality_score, eligibility_scope, source metadata, and reason codes.

Build datasets for:
- operating_revenue
- operating_margin
- cashflow
- valuation_ev
- valuation_price
- market_implied
- direction

Add tests proving quarterly rows do not enter valuation_ev or valuation_price datasets.
```

### Prompt C: Market-Implied Model

```text
Implement auto_valuation/learning/market_implied.py.

For complete annual DCF records, compute:
- valuation_residual_pct
- implied_wacc_pct
- implied_terminal_growth_pct
- implied_wacc_delta_pct
- implied_terminal_growth_delta_pct
- ev_revenue_multiple_residual
- ev_ebitda_multiple_residual where EBITDA is meaningful
- price_return_error_pct

Use robust numerical solvers with plausible bounds.
Flag solver failures with reason codes instead of throwing.
Add tests using synthetic DCF cases where actual EV is known.
```

### Prompt D: Stratified Sampler

```text
Implement auto_valuation/learning/sampling.py.

Replace newest-limit learning sample with a quality-weighted stratified sample across:
- sector
- canonical industry
- exchange suffix
- market cap regime
- macro regime
- observation type
- quality tier
- recency bucket

Expose sample diagnostics: counts, quality score average, excluded rows, target eligibility counts.
Integrate into knowledge_model.py without breaking dashboard payload contracts.
```

### Prompt E: Valuation Residual Artifact

```text
Create offline training command tools/train_valuation_residual.py.

Use the normalized valuation dataset to train a residual model that predicts log(actual_ev / predicted_ev).
Start with scikit-learn HistGradientBoostingRegressor or LightGBM if requirements-train.txt is available.
Use walk-forward validation only.
Export a lightweight JSON model manifest and fallback hierarchical priors.
Do not add heavy ML dependencies to root requirements.txt.
```

### Prompt F: Hybrid Inference

```text
Integrate valuation residual inference into webapp/data/knowledge_model.py and eodhd_client.py.

Flow:
1. Build base DCF.
2. Apply operating assumption calibration.
3. Compute learned valuation residual overlay.
4. Compute peer-implied value where peer quality is sufficient.
5. Blend DCF, residual-adjusted DCF, and peer value using expected-error weights.
6. Return explainability fields for each component.

Keep existing dashboard keys backward compatible.
Add tests proving final value changes only when artifact confidence and data quality are sufficient.
```

## 18. Risk Register

| Risk | Severity | Mitigation |
|---|---:|---|
| Bad international currency scaling contaminates labels | High | Currency confidence and target eligibility gates |
| Restated data creates look-ahead bias | High | Point-in-time feature store and restatement downweights |
| Heavy ML dependencies break Vercel bundle | High | Offline training requirements only; lightweight artifacts in production |
| Market residual model becomes black box | Medium | Keep DCF base, expose residual features, use SHAP/offline diagnostics if available |
| Overfitting thin sectors | High | Hierarchical shrinkage, conformal intervals, promotion gates by segment |
| Peer model copies bad comps | Medium | Peer quality score and low peer weight when basket is weak |
| Price prediction remains noisy | Medium | Report realistic intervals and direction accuracy, not only point price |
| Financials/REITs corrupt generic DCF | High | Specialist models and target eligibility gates |

## 19. Definition Of Done

The training upgrade is done when:

1. Every observation has quality and target eligibility.
2. Full DCF calibration excludes invalid rows.
3. Quarterly and price-only rows train only valid targets.
4. Market-implied EV/price residual labels exist.
5. Walk-forward validation compares baseline vs learned vs hybrid.
6. Promotion is automatic but gated by strict metrics.
7. Vercel serves lightweight artifacts only.
8. Dashboard explains data quality, learning layers, blend weights, and uncertainty.
9. EV and price errors improve without degrading fundamentals.
10. Confidence bands are empirically calibrated.

## 20. Final Recommendation

The best improvement path is a full V4 learning architecture, not another small calibrator tweak.

Build in this order:

1. Quality layer.
2. Target separation.
3. Market-implied labels.
4. Stratified sampling.
5. Operating calibrator V4.
6. Valuation residual model.
7. Blend/gating model.
8. Conformal uncertainty.
9. Dashboard/workbook explainability.
10. Automated model promotion.

This preserves the DCF as the institutional core, but adds the missing market-implied intelligence needed to improve EV and price predictions. The current system has enough raw evidence to justify this upgrade. The main work now is making that evidence clean, target-specific, point-in-time, and directly connected to the valuation errors that matter most.
