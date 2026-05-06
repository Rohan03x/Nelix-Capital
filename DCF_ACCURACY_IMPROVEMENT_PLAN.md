# DCF Accuracy Improvement Plan

**Status:** Planning — Full Implementation Spec  
**Date:** 2026-05-06  
**Current EV MAE:** 75.4% (all) | Revenue MAE: 23.4% (all), 9.4% (stable)  
**Postmortems in DB:** 33,222 | Tickers tracked: 33,295  
**Systematic bias:** Mean EV error = −47.4% (model values at ~half of market for growth names)

---

## Background

A DCF is a chain. Every input has its own error, and those errors compound into the final EV output. Currently the model only checks 5 of those inputs at postmortem time, only calibrates 6 of the 22 assumption variables, and — critically — has a hard floor of 0% on terminal growth that makes it structurally incapable of valuing declining businesses correctly.

The Signify N.V. case makes this explicit: 12-year revenue CAGR = −1.8%, market pricing in −5.0% terminal growth via reverse DCF, yet the model outputs +2.5% terminal growth and calls the stock 142% undervalued. This is not a tuning problem — it is a chain of three compounding failures.

**The three failure categories:**

1. **Measurement gaps** — variables that are predicted but never checked at postmortem
2. **Calibration floor bugs** — hard-coded bounds that prevent the system from learning negative terminal growth even when every observation points to it
3. **Signal isolation** — the reverse DCF already computes market-implied g but the result is display-only and never fed back into calibration

The core principle: **fixing the measurement gap and the floor bugs is more valuable than tuning the forecast engine**, because you cannot calibrate what you do not track, and you cannot output what you have floored away.

---

## Current System Map

```
Historical data
    → AssumptionEngine sets 22 inputs
        → build_growth_assumptions() sets terminal_growth from sector anchor + blend
            → DCF computes ForecastYear × N
              (revenue, EBIT, tax, NOPAT, D&A, capex, NOWC, UFCF per year)
                → DCFResult (EV, terminal value, equity value, price)
                    → Reverse DCF solves market_implied_g  [DISPLAY ONLY — NOT FED BACK]
                → PostmortemRecord compares 5 actuals
                    → actual_terminal_growth, actual_wacc fields EXIST but are NEVER WRITTEN
                    → CalibrationObservation has predicted/actual terminal_g pairs — NEVER POPULATED
                        → LayeredCalibrator produces CalibratedAssumptions
                            → terminal_growth clamped to [0.0, 0.06] — FLOOR BUG
```

---

## Confirmed Bugs Found in Code

These are not design gaps — they are confirmed code defects from reading the source.

### Bug 1 — Terminal Growth Hard-Floored at 0% (Critical)

In `_layered_calibrator.py`, the `_AssumptionSpec` for `terminal_growth` is:
```python
_AssumptionSpec(actual_key=..., predicted_key=..., base_value=..., min_sigma=0.003, low=0.0, high=0.06)
```

`low=0.0` means the calibrator is **mathematically unable to produce negative terminal growth**. Even if 500 postmortem observations for Industrials-with-declining-revenue all have `actual_terminal_growth = -0.04`, the calibrated output is clamped to `0.0`. This single constant is the primary reason the Signify valuation is wrong. Fix: change `low=0.0` to `low=-0.06`.

### Bug 2 — Dead Fields: `actual_terminal_growth` and `actual_wacc` Exist but Are Never Written

`PostmortemRecord` has both `actual_terminal_growth: float|None` and `actual_wacc: float|None` declared in the dataclass. `run_annual_postmortem()` never computes or assigns them. They are always `None`. The same dead-field problem exists in `CalibrationObservation`: `actual_terminal_growth` is declared but never populated because postmortem never produces it. **The infrastructure for terminal g and WACC accuracy tracking is already built — it just isn't wired up.**

### Bug 3 — Market-Implied g Is Computed But Never Injected Into Calibration

The reverse DCF solver runs on every valuation request and produces `market_implied_terminal_g`. This number appears on the Market Expectations tab and then is discarded. There is no code path from the reverse DCF result → `CalibrationObservation` → `calibrate()`. The display and the model are architecturally isolated.

### Bug 4 — Performance Report Does Not Surface Margin Error

`margin_error_bps` exists in every `PostmortemRecord`. `performance_report.py` queries postmortem records but only extracts `revenue_error`, `ev_error`, and `price_return_error`. Margin accuracy is never reported despite being tracked.

---

## Gap 1 — Postmortem checks 5 actuals, DCF produces 11 per year

| Variable | Predicted? | Postmortem checks? | Feeds calibration? |
|---|:---:|:---:|:---:|
| Revenue | ✅ | ✅ | ✅ |
| EBIT margin | ✅ | ✅ (error in bps) | ✅ (but not reported) |
| UFCF | ✅ | ✅ | ✅ (as margin) |
| EV | ✅ | ✅ | ❌ |
| Price | ✅ | ✅ | ❌ |
| **Terminal growth** | ✅ | ❌ (field exists, never written) | ❌ (field exists, never populated) |
| **WACC** | ✅ | ❌ (field exists, never written) | ❌ (field exists, never populated) |
| **D&A % revenue** | ✅ | ❌ | ❌ |
| **Capex % revenue** | ✅ | ❌ | ❌ |
| **Effective tax rate** | ✅ | ❌ | ❌ |
| **SBC % revenue** | ✅ | ❌ | ❌ |
| **Working capital (NOWC)** | ✅ | ❌ | ❌ |

All missing actuals are available in the EODHD fundamentals payload already fetched per ticker — this is a measurement and wiring gap, not a data availability gap. Terminal g and WACC are computable via reverse DCF at postmortem time using realized prices and cash flows.

---

## Gap 2 — Calibration adjusts 6 variables, AssumptionSet has 22

The calibrator currently touches: revenue growth, EBIT margin, WACC, terminal growth rate (but floored at 0%), beta, UFCF margin, reinvestment rate.

It never adjusts:
- **D&A % revenue** — biggest driver of UFCF after margin
- **Capex % revenue** — directly sets reinvestment and growth quality
- **SBC % revenue** — systematically underestimated in Tech; dilutes equity value
- **Effective tax rate** — volatile across M&A, jurisdictions, deferred tax; large EV impact
- **DSO / DIO / DPO** (working capital days) — assumed constant, never validated
- **Share dilution rate** — 1% per year error compounds to ~7% equity value error over 7 years

---

## Gap 3 — Structural break score uses only revenue error

**Current formula:**
```python
structural_break_score = abs(revenue_error_pct) / 50.0 + len(hints) * 0.15
```

A capex spike (Amazon 2021–2023: capex ×3) won't show in revenue error for 2–3 years. A margin collapse (Meta 2022) appears in EBIT but not in structural break score. A company in secular decline (Signify) has revenue eroding slowly — the signal is too weak to trigger the break flag early enough.

**Better formula:**
$$\text{break\_score} = \frac{|rev\_err|}{50} \cdot 0.4 + \frac{|margin\_err\_bps|}{500} \cdot 0.3 + \frac{|capex\_err|}{100} \cdot 0.2 + \text{hints} \cdot 0.1$$

---

## Gap 4 — UFCF margin observation silently skipped when data is partial

`CalibrationObservation.actual_ufcf_margin` is set to `None` if any one of capex, D&A, or working capital is missing. Since UFCF = NOPAT + D&A − Capex − ΔNOWC, a single missing capex field silently drops the entire UFCF training signal for that year with no warning logged.

Fix: persist partial UFCF components separately; compute UFCF from whatever subset is available; log explicitly when components are missing.

---

## Gap 5 — No per-variable, per-sector accuracy tracking

**Current state:** "Revenue MAE for stable companies = 9.4%" — one number for the whole universe.

**What's missing:**
- "D&A estimates for Healthcare are 40% too high" (biotech expensing vs manufacturing)
- "Capex for Semiconductors is understated by 25%" (fab investment cycles)
- "SBC for Tech is underestimated by 3% of revenue" (option grants)
- "Terminal g for Industrials-declining is overestimated by 500bps"
- "Tax rate for Financials has ±800 bps error" (deferred tax timing)

---

## Gap 6 — EV error attribution is a label, not a decomposition

**Current:**
```python
primary_miss_driver: str              # "revenue", "margin", "enterprise_value", "price_return"
error_attribution: list[tuple[str, float]]   # raw magnitudes only
```

**Needed:** quantified first-order variance decomposition via DCF sensitivity partials:
$$\Delta EV \approx \frac{\partial EV}{\partial \text{UFCF}} \cdot \Delta\text{UFCF} + \frac{\partial EV}{\partial \text{WACC}} \cdot \Delta\text{WACC} + \frac{\partial EV}{\partial g} \cdot \Delta g + \frac{\partial EV}{\partial \text{TV\%}} \cdot \Delta\text{TV\%}$$

---

## Gap 7 — Terminal Growth Is Not Trajectory-Aware

`build_growth_assumptions()` blends historical CAGR, NTM consensus, and sector median linearly. The blend weights are uniform — a company with −1.8% 12-year CAGR and a company with +8% 12-year CAGR both get the same sector anchor of +2.5% terminal growth from the Industrials table. There is no constraint that negative-trajectory businesses must have their terminal g prior bounded below the sector anchor.

---

## Gap 8 — Revenue Growth Is a Single Regime Model

The calibrator computes a time-decayed weighted residual mean over all historical observations. This works for stationary assumptions. Revenue growth is non-stationary and regime-dependent — weighting a company's +20% hypergrowth-phase CAGR alongside its −3% post-disruption CAGR produces a number that describes neither regime. Near-term CAGR prediction needs regime-aware modelling.

---

## Full Implementation Plan

The implementation is structured into eight layers. Layers A and B are pure bug fixes requiring no architectural change. All other layers depend on them being done first.

### Dependency Chain

```
Layer A (terminal g floor fix)  ──────────────────────────────┐
Layer B (activate dead fields)  ──────────────────────────────┤
Layer D (structural decline flag)  ────────────────────────────┤
    └── Layer C (trajectory constraints) ─────────────────────┤
    └── Layer E (market-implied g blending) ──────────────────┤
    └── Layer F (regime classifier + per-regime CAGR) ────────┤
Layer G (margin decomposition) ────────────────────────────────┤
Layer H (per-sector per-variable performance report) ──────────┘
```

---

### Layer A — Fix the Terminal Growth Calibration Floor

**File:** `auto_valuation/learning/_layered_calibrator.py`

Change the `_AssumptionSpec` for `terminal_growth`:
```python
# Before (bug):
_AssumptionSpec(..., min_sigma=0.003, low=0.0,   high=0.06)

# After (fix):
_AssumptionSpec(..., min_sigma=0.003, low=-0.06, high=0.06)
```

This is the single highest-impact change in the entire plan. With this fix, the calibrator can now learn and output negative terminal growth for companies where the evidence supports it. Without this fix, every other improvement in this plan is silently nullified for declining businesses.

Also fix the band clamp in `_build_assumption_summary`: the `clamp(point - spread, low, high)` call uses the same `low=0.0` that was flooring the output. After fixing the spec, the band clamp is automatically correct.

**No training restart required.** The existing 33k+ calibration observations are still valid — the calibrator will simply now be able to compute a different (correct) answer for observations where `actual_terminal_growth < 0`. The change takes effect immediately on the next `calibrate()` call.

---

### Layer B — Wire Up the Dead Fields

**Files:** `auto_valuation/learning/postmortem.py`, `auto_valuation/learning/_layered_calibrator.py`

#### B1 — Populate `actual_terminal_growth` in postmortem

At postmortem time, when `actual_ev_mm` and `actual_ufcf_mm` are both available from the realized outcome, solve for the implied terminal growth via bisection on the Gordon Growth Model:

$$\text{Solve for } g: \quad EV_{\text{actual}} = PV_{\text{ufcfs}} + \frac{\text{terminal\_ufcf} \times (1 + g)}{\text{WACC} - g} \times \text{discount\_factor}$$

where `PV_ufcfs` and `terminal_ufcf` are taken from the original `DCFResult` stored in `prediction_snapshot`. Bisection bounds: `[−0.10, WACC − 0.005]`. Call the result `realized_market_implied_g` and store it as `actual_terminal_growth`.

The `terminal_g_error_bps = (predicted_terminal_growth - actual_terminal_growth) × 10_000` is then a direct subtraction. For Signify this would be `(2.5% − (−5.0%)) × 10_000 = 750 bps`.

#### B2 — Populate `actual_wacc` in postmortem

Solve the analogous reverse problem: hold terminal growth at the model's predicted value, solve for WACC that makes the model EV equal to actual EV. This isolates WACC error from terminal g error — important because for a stock like Signify the WACC is correct (7.7%) while terminal g is wrong, and the system should know this distinction per ticker rather than attributing error to both.

#### B3 — Feed both into `CalibrationObservation`

`CalibrationObservation.actual_terminal_growth` and `CalibrationObservation.actual_wacc` are already declared. Populate them from the postmortem result whenever available. The layered calibrator will then have real ground-truth terminal g observations to learn from.

**No training restart required.** New fields will be `None` for historical observations already in the DB (backward compatible) and populated going forward from the next maintenance run.

---

### Layer C — Revenue Trajectory Hard Constraints on Terminal g Prior

**File:** `auto_valuation/assumptions/growth.py`

Before calibration runs, `build_growth_assumptions()` must compute a `terminal_g_prior_range` that constrains what the calibrator is allowed to output. The constraint is based on observable historical revenue trajectory using CAGRs already computed in the pipeline:

| Revenue trajectory regime | Prior range fed to calibration |
|---|---|
| 5-yr CAGR > +3% and positive 3-yr trend | `[GDP − 0.5%, GDP + 2.0%]` = `[1.0%, 4.5%]` |
| 5-yr CAGR 0% to +3% | `[GDP − 1.0%, GDP + 1.5%]` = `[0.5%, 4.0%]` |
| 5-yr CAGR −2% to 0% | `[−2.0%, GDP]` = `[−2.0%, 2.5%]` |
| 5-yr CAGR < −2% | `[5yr_CAGR × 0.4, 0.0%]` |
| Structural decline flag active (Layer D) | `[market_implied_g − 1.0%, market_implied_g + 0.5%]` |

For Signify: 5-yr CAGR ≈ −3%, structural decline flag → range `[−5.5%, −4.5%]` → calibration constrained to that window → final terminal g ≈ −5%, not +2.5%.

The `_AssumptionSpec` `low` and `high` bounds in the calibrator become dynamic parameters passed at `calibrate()` call time rather than module-level constants. This requires changing the method signature of `calibrate()` to accept `terminal_g_range: tuple[float, float] | None = None`.

---

### Layer D — Structural Decline Mode

**File:** `auto_valuation/assumptions/engine.py` (new helper), used in `build_growth_assumptions()`

A composite flag computed from signals already available in the pipeline. Triggered when ≥ 3 of the following are true:

| Signal | Source | Threshold |
|---|---|---|
| Long-run revenue CAGR | `historical_revenue_cagr(income_stmts, window=10)` | < 0% |
| Short-run revenue CAGR | `historical_revenue_cagr(income_stmts, window=3)` | < −3% |
| Market-implied g | Reverse DCF solve (see Layer E) | < −2% |
| Structural break score from calibrator | `CalibrationDiagnostics.structural_break.score` | > 0.7 |
| Industry secular headwind score | Static lookup table (see below) | ≥ 1.5 |

When flagged:
- Terminal g prior range locked to Layer C structural-decline row
- `scenario_width_multiplier` += 0.4 (wider scenarios)
- Confidence score capped at 55 regardless of other signals
- Bear case weight increased in scenario generation
- Warning surfaced on the dashboard: "Structural decline detected — terminal growth anchored to market-implied signal"

**Industry secular headwind table** (new static dict, ~60 entries, examples):
```python
_INDUSTRY_HEADWIND_SCORE = {
    "Electrical Equipment & Parts": 1.5,   # LED commoditization
    "Department Stores": 2.0,              # e-commerce displacement
    "Newspapers": 2.0,                     # digital disruption
    "Coal & Related Energy": 2.0,          # energy transition
    "Tobacco": 1.5,                        # secular volume decline
    "Traditional Banking": 1.0,            # fintech pressure
    "Cloud Software": 0.0,                 # tailwind
    "Semiconductors": 0.5,                 # cyclical but structural tailwind
    "E-Commerce": 0.0,                     # tailwind
    # default: 0.5
}
```

---

### Layer E — Market-Implied g as a Calibration Input

**File:** `auto_valuation/learning/_layered_calibrator.py`

The reverse DCF `market_implied_terminal_g` is passed as a new parameter to `calibrate()`. A seventh "market signal layer" is added to `_layered_sources()` with a weight `w` computed as:

$$w = w_{\text{base}} \times (1 - 0.5 \times \text{break\_score}) \times \min\left(1.0,\ \frac{\log_{10}(\text{market\_cap\_mm})}{4}\right)$$

where `w_base = 0.40`.

For Signify ($2.4B market cap, break_score ≈ 0.3):
- $w \approx 0.40 \times 0.85 \times \frac{\log_{10}(2400)}{4} \approx 0.40 \times 0.85 \times 0.84 \approx 0.29$

Blended terminal g before Layer C constraint:
$$g_{\text{blend}} = (1 - 0.29) \times 2.5\% + 0.29 \times (-5.0\%) \approx -0.7\%$$

Then Layer C's structural-decline constraint clips this to `[−5.5%, −4.5%]` and the calibrator adjusts within that band.

For illiquid microcaps ($50M market cap): $\frac{\log_{10}(50)}{4} \approx 0.42$ → `w ≈ 0.14` — market price has far less weight.

**When market-implied g is not available** (e.g. no market price data): the market signal layer is simply omitted and the remaining 6 layers are renormalized. The change is backward-compatible.

---

### Layer F — Regime-Aware Revenue CAGR Prediction

**File:** new module `auto_valuation/learning/regime_classifier.py`

The current calibrator computes a single time-decayed weighted residual mean over all historical revenue growth observations. This fails because revenue growth is regime-dependent and non-stationary. The fix is a two-tier approach.

#### Tier 1 — Regime Classifier

A gradient-boosted tree (LightGBM) that classifies each company into one of 5 revenue growth regimes at valuation time:

| Regime | Revenue profile | Terminal g prior range |
|---|---|---|
| Hypergrowth | NTM consensus > 20% | −1% to +1% (high uncertainty, fade uncertain) |
| Expansion | 5-yr CAGR > 5%, margins improving | +1.5% to +3.5% |
| Stable | 5-yr CAGR 0–5%, margins flat | +1.0% to +2.5% |
| Mature/Cyclical | 5-yr CAGR −2% to +3%, volatile | 0% to +2.0% |
| Structural Decline | 5-yr CAGR < −2% or 10-yr negative | −5.0% to 0% |

**Feature vector** (all already computed in the pipeline):
```
[5yr_revenue_cagr, 3yr_revenue_cagr, 1yr_revenue_growth,
 gross_margin, ebit_margin, ebit_margin_trend_3yr,
 capex_pct_revenue, roic, debt_to_equity,
 sector_id (one-hot), industry_headwind_score,
 market_cap_regime (encoded), macro_regime (encoded),
 revenue_volatility, margin_volatility]
```

**Training data:** The 33,222 postmortem records already in the DB. Regime label is derived from `structural_break_flag`, `revenue_error_pct`, and `actual_revenue_mm` growth over the horizon. No restart needed — this is a new model trained on existing data offline, serialized to `auto_valuation/learning/regime_model.pkl`.

**Retrain cadence:** monthly, triggered by `run_maintenance()`, when ≥ 500 new postmortems have accumulated since last train. The training script reads directly from the ledger DB.

#### Tier 2 — Per-Regime Ridge Regression for Near-Term CAGR

Within each regime, a separate Ridge regression predicts near-term revenue growth. This avoids the contamination problem — the Stable regime model is not distorted by hypergrowth observations. Five models in total, each with ~5–10 features selected from the full feature vector based on regime characteristics.

For **Structural Decline** regime specifically, the dominant features are:
```
[3yr_cagr, 1yr_cagr, gross_margin_trend, industry_headwind_score, market_implied_g]
```

**No full system restart required.** The regime classifier and Ridge regressors are additive models that sit on top of the existing calibration pipeline. They produce a `predicted_near_term_revenue_growth` that supplements (and when confidence is high, overrides) the calibrator's output for near-term revenue. Terminal g flows through the Layer C/D/E chain regardless of the regime model.

#### Tier 3 — Terminal g From Regime + Market Signal

Terminal g is not independently predicted by a ML model. It is:
1. Regime-constrained (Layer C/D range)
2. Market-signal-blended (Layer E weight)
3. ROIC-consistency-checked via `enforce_terminal_growth_consistency()` already in `dcf.py`

The existing `enforce_terminal_growth_consistency()` caps terminal g at `roic × reinvestment_rate + tolerance`. It should also **floor** terminal g: for declining businesses, `g_floor = roic × reinvestment_rate − tolerance`. A company with 5% ROIC and 10% reinvestment rate has an implied g of 0.5% — the floor prevents the model from predicting +2.5% terminal g inconsistent with the ROIC math.

#### Analyst Consensus Override

When analyst NTM/LTM estimates are available via `estimates.py`, they override Tier 2 with weights:
- Years 1–2: analyst weight = 0.60, model weight = 0.40
- Years 3–5: analyst weight = 0.30, model weight = 0.70
- Years 6+: analyst weight = 0.0 (model only, analysts don't forecast this far reliably)

These weights are currently static in `blend_growth_estimate()`. They should become dynamic based on analyst coverage depth (number of analysts) and historical consensus accuracy for the sector (learned from postmortems where consensus data was available).

---

### Layer G — Margin Decomposition

**File:** `auto_valuation/learning/postmortem.py`

Split `margin_error_bps` into two components:

**`near_term_margin_error_bps`:** Error of the model's Year-1 or Year-2 EBIT margin prediction against `actual_ebit_margin` from the realized outcome. This is already available — `actual_ebit_margin` in `RealizedOutcomeRecord` covers the forecast horizon year.

**`terminal_margin_error_bps`:** Error of the model's assumed terminal margin against the margin implied by the reverse DCF at horizon. Terminal margin is the value the model assumes the business converges to in perpetuity. For Signify, the model assumes 8.7% terminal margin; the market at current price implies the business stays at ~6% or contracts further.

Near-term margin prediction should use a mean-reversion model rather than the current linear fade. The fade schedule in `build_margin_fade_schedule()` uses a static `fade_years` parameter with linear interpolation:
```python
margin_t = base + (target - base) * t / fade_years
```

Replace with a mean-reversion parametrized by sector-specific `α` learned from postmortems:
```python
margin_t = margin_{t-1} + α × (target_margin - margin_{t-1})
```

where `α` is the mean-reversion speed. For Industrials: `α ≈ 0.12/yr` (slow). For high-volatility sectors: `α ≈ 0.25/yr`. The per-sector `α` is estimated from historical postmortems where the trajectory of actual EBIT margins is known.

---

### Layer H — Per-Sector Per-Variable Performance Report

**File:** `auto_valuation/learning/performance_report.py`

Add a `by_sector_variable` section to `build_learning_performance_report()` output:

```python
"by_sector_variable": {
    "Industrials": {
        "terminal_g_error_bps": {"mae": 480, "mean": +420, "bias": "optimistic", "n": 1240},
        "revenue_error_pct":    {"mae": 12.1, "mean": -2.1, "bias": "slight_pessimistic", "n": 1240},
        "margin_error_bps":     {"mae": 95, "mean": +40, "bias": "slight_optimistic", "n": 1240},
        ...
    },
    "Information Technology": {
        "capex_error_pct":      {"mae": 28.4, "mean": -22.1, "bias": "underestimated", "n": 4310},
        "sbc_error_pct":        {"mae": 31.2, "mean": -28.7, "bias": "underestimated", "n": 4310},
        ...
    },
    ...
}
```

Also add trend comparison: last 1,000 postmortems vs prior 1,000, per sector-variable combination. This surfaces whether calibration is improving over time for each cell.

---

## Additional Gaps From Original Plan

### Gap 3 — Structural Break Score (Enhanced)

Current formula uses only revenue error. Improved formula adds margin and capex:

$$\text{break\_score} = \frac{|rev\_err|}{50} \cdot 0.4 + \frac{|margin\_err\_bps|}{500} \cdot 0.3 + \frac{|capex\_err|}{100} \cdot 0.2 + \text{hints} \cdot 0.1$$

This catches capex shocks and margin collapses 1–2 years earlier than the current revenue-only signal. Requires capex error to be tracked (Priority 1 below).

### Gap 4 — Partial UFCF Observation Silently Dropped

`CalibrationObservation.actual_ufcf_margin` is set to `None` if any one component (capex, D&A, NOWC) is missing. Since UFCF = NOPAT + D&A − Capex − ΔNOWC, a single missing capex field silently drops the entire UFCF training signal. Fix: persist partial components; compute UFCF from available subset; log explicitly when components are missing.

---

## Brain Sync Behaviour for New Changes

The layered calibrator uses a 6-layer memory architecture (company, cohort, sector, analog, macro, global). Each new variable added to `CalibrationObservation` must be handled consistently across all 6 layers. The following describes how each layer processes the new signals.

### Terminal Growth (Layers A, B, C, E)

**Company layer:** Learns `terminal_g_error_bps` per ticker. High weight (priority 3.0). After 3+ postmortems for a declining ticker, the company memory will dominate and lock terminal g to the correct range for that specific company.

**Cohort layer:** Groups companies by `(sector, industry, maturity_bucket, cap_regime)`. After Layer B is wired, the cohort memory will accumulate "Industrials-declining-midcap has +450bps terminal g overestimate" as a systematic prior correction. Weight: 1.20.

**Sector layer:** Broadest grouping. Will learn sector-level terminal g priors. Industrials sector mean terminal g error feeds a sector prior of ≈ −400bps adjustment. Weight: 0.95.

**Analog layer:** Finds companies with similar structural profiles (sector, revenue trajectory, margin regime) across the full observation set. Particularly valuable for tickers with few historical postmortems — the analog set provides borrowed evidence. For Signify it would draw evidence from other European industrial companies with negative long-run CAGR.

**Macro layer:** Rate-era adjustments already exist for WACC (`_wacc_rate_era_adjusted_errors`). Terminal g also needs rate-era adjustment: during low-rate environments (2012–2022), market-implied terminal g was systematically elevated because discount rates were low. The correction:
$$\text{adjusted\_tg\_error} = (g_{\text{actual}} - g_{\text{predicted}}) - k \times (r_f - r_{f,\text{baseline}})$$
where `k ≈ 0.3` reflects the sensitivity of market-implied g to rate changes and `r_f_baseline = 0.035`.

**Global layer:** Provides a universal drift term — if all sectors' terminal g is being systematically overestimated, the global layer corrects for it uniformly.

**Layer agreement conflict detection:** When the market signal layer (Layer E) disagrees with the company memory layer by > 200bps on terminal g, the existing `conflict_score` mechanism fires and lowers confidence. This is the correct behaviour — it surfaces the disagreement rather than silently averaging it away.

### Revenue Regime Memory

The regime classifier (Layer F) output (`growth_regime` field already in `CalibrationObservation`) will now be populated with the ML classifier's output rather than a rule-based fallback. The layered calibrator filters observations by `growth_regime` when building the cohort layer, so a Stable-regime company's history does not pollute the Structural-Decline-regime prior.

**Brain sync impact:** After a regime transition (e.g. a hypergrowth company that decelerates into Stable), the company memory layer has old observations tagged with `growth_regime = "Hypergrowth"`. The time-decay weight (`exp(-0.15 × age_years)`) automatically reduces the influence of those old observations. By the time 3–4 years of Stable-regime observations accumulate, they dominate. No manual intervention needed.

### Structural Break Propagation

When the structural decline flag (Layer D) fires for a ticker, it propagates through the brain in three places:

1. **Layer weight damping:** Company/cohort/sector layers get `raw_weight *= max(0.2, 1.0 - 0.60 × structural_decline_score)` — existing mechanism, already works correctly.
2. **Analog/macro/global boost:** Opposite direction: `raw_weight *= 1.0 + 0.30 × structural_decline_score` — also existing mechanism.
3. **New: market signal weight boost:** For structural decline companies, `w_base` in the market signal layer (Layer E) increases from 0.40 to 0.65. The market price for a declining business is more informative than the model's sector anchor.

### Confidence Score Interactions

The confidence score formula is:
```python
confidence = 0.60 * evidence_confidence + 0.25 * agreement_confidence + 0.15 * (1 - structural_break.score)
# Capped at 0.35 when weak_evidence
```

New interactions from these changes:
- **Terminal g dead-zone removal:** When `actual_terminal_growth` is populated (Layer B), `evidence_confidence` for terminal g increases because the actual/predicted pairs are no longer all `None`. Confidence will rise for tickers with ≥ 5 terminal g observations.
- **Layer agreement penalty:** When market-implied g (Layer E) disagrees with company memory by > 200bps, `agreement_confidence` drops, lowering overall confidence. This surfaces correctly as a yellow/red confidence score — the model knows it disagrees with the market but continues to produce a valuation.
- **Structural decline penalty:** The `(1 - structural_break.score)` term already penalises breaks. With Layer D adding the structural decline flag, this term will push confidence below 55 for companies like Signify, which is the correct outcome — the model should be less confident, not more.

---

## Model Training: What Requires Restart vs Incremental Update

### No restart needed

All of the following take effect without discarding any existing training data:

| Change | How it propagates |
|---|---|
| Layer A: fix terminal g floor | Takes effect on next `calibrate()` call; existing observations are reprocessed with the new bound |
| Layer B: wire dead fields | New observations going forward populate `actual_terminal_growth`; historical observations remain `None` (treated as missing data, not zero — already handled) |
| Layer C: trajectory constraints | Changes the `low`/`high` bounds passed at call time; no DB change |
| Layer D: structural decline flag | New field computed at assumption time; does not affect stored observations |
| Layer E: market signal layer | Additive 7th layer; existing 6-layer weights are renormalized; no stored data changes |
| Layer G: margin decomposition | New fields in `PostmortemRecord`; old records have `None`; forward-only |
| Layer H: performance report | Read-only query change; no data modification |

### Incremental training required (offline, ~1 hour)

| Change | What to do |
|---|---|
| Layer F Tier 1: regime classifier | Train LightGBM on existing 33k postmortems. Run `python -m auto_valuation.learning.train_regime_classifier` once. Serializes to `learning/regime_model.pkl`. No impact until explicitly loaded. |
| Layer F Tier 2: per-regime Ridge regressors | Train 5 Ridge models on same postmortems, stratified by regime label. Run same script, produces `learning/regime_ridge_{regime}.pkl` × 5. |
| Layer G: sector-specific margin reversion `α` | Estimated by fitting mean-reversion parameter per sector from postmortem margin trajectories. ~10 minutes compute on 33k records. |
| Per-sector analyst accuracy weights | Computed from postmortems where consensus data was available. One-time aggregate query. |

### How to trigger the incremental training run

```bash
# From workspace root, with venv active:
python -m auto_valuation.learning.train_regime_classifier \
    --ledger-path learning/ledger/ledger.db \
    --output-dir auto_valuation/learning/ \
    --min-postmortems 500

# Verify outputs:
python -m auto_valuation.learning.regime_classifier --self-test
```

The training script does not exist yet — it is a new file to create as part of Layer F. It reads from `ledger.db` via `LedgerReader.query_aligned_pairs()`, derives regime labels from postmortem data, and serializes the trained models.

### Retrain cadence

The regime classifier and Ridge regressors should retrain automatically when `run_maintenance()` detects ≥ 500 new postmortems since the last training run. The maintenance run already exists as `MaintenanceRunRecord` — add a check: if `new_postmortem_count >= 500`, trigger retraining as a subprocess. The layered calibrator's 6-layer prior/residual system does not have an explicit retrain step — it recomputes on every `calibrate()` call from whatever observations are in the DB, so it is always current.

---

## End-to-End Ticker Flow (Post-Implementation)

This describes the complete path from user entering any ticker to final valuation, after all layers are implemented.

```
1. User submits ticker (e.g. LIGHT.AS / Signify)
   │
2. DataFetcher pulls EODHD fundamentals + price history
   │
3. HistoricalCAGR computed for windows: 1yr, 3yr, 5yr, 10yr
   │
4. IndustryHeadwindScore looked up from static table → 1.5 for LIGHT.AS
   │
5. RegimeClassifier (Layer F Tier 1) classifies ticker:
   │   Features: [5yr_cagr=-3.1%, 3yr_cagr=-7.2%, ebit_margin=6.8%,
   │              headwind=1.5, cap_regime="mid", ...]
   │   → Output: growth_regime = "Structural Decline"
   │
6. build_growth_assumptions() computes:
   │   - near_term_growth via Ridge Tier 2 (Structural Decline model)
   │   - terminal_g_prior_range from Layer C: structural decline row
   │     → market_implied_g = -5.0% (from quick reverse DCF pre-solve)
   │     → range = [-6.0%, -4.5%]
   │
7. build_wacc() computes WACC = 7.7% (unchanged from current)
   │
8. LayeredCalibrator.calibrate() runs:
   │   Layer 1 (company memory): 0 prior postmortems for LIGHT.AS
   │   Layer 2 (cohort): Industrials-declining-midcap cohort → terminal_g correction: -420bps
   │   Layer 3 (sector): Industrials sector → terminal_g correction: -380bps
   │   Layer 4 (analog): similar European industrial decliners → -450bps
   │   Layer 5 (macro): rate-era adjustment: -20bps
   │   Layer 6 (global): global drift: -40bps
   │   Layer 7 (market signal, Layer E): market_implied_g = -5.0%, weight w=0.29
   │   → Weighted blend → calibrated terminal_g ≈ -4.2%
   │   → Layer C constraint clips to [-6.0%, -4.5%] → -4.2% passes
   │   → enforce_terminal_growth_consistency() checks ROIC floor → passes
   │   → CalibratedAssumptions.terminal_growth = -4.2%
   │
9. DCF runs with terminal_g = -4.2%, WACC = 7.7%:
   │   → Terminal value weight drops significantly vs +2.5% case
   │   → Intrinsic value base case: ~$22–25 (vs current $49 with +2.5%)
   │   → Much smaller upside vs market price of $20.26
   │   → Signal: "Fairly Valued" or mild undervalue, not +142%
   │
10. ConfidenceScore computed:
    │   → agreement_confidence penalised (layers agree on negative g but it's uncertain)
    │   → structural_decline flag → automatic cap at 55
    │   → scenario_width_multiplier = 1.8 (wider than default)
    │   → Confidence: ~45–50 (Guarded vs current 61)
    │
11. Dashboard rendered with correct signal:
    │   → Terminal g: −4.2% (not +2.5%)
    │   → Structural Decline warning shown
    │   → Wider bear/bull range (reflects genuine uncertainty)
    │
12. If actual 2026 results come in at postmortem time:
    │   → reverse DCF bisection solves realized_market_implied_g
    │   → terminal_g_error_bps computed and stored
    │   → CalibrationObservation.actual_terminal_growth populated
    │   → Cohort and sector layers update priors for future Signify-type companies
```

---

## Implementation Priorities

### Priority 1 — Fix Bug 1: Terminal Growth Floor

**File:** `auto_valuation/learning/_layered_calibrator.py`
**Change:** `low=0.0` → `low=-0.06` in terminal_growth `_AssumptionSpec`
**Effort:** 1 line
**Impact:** Immediately enables correct terminal g for all declining businesses

---

### Priority 2 — Wire Dead Fields (Terminal g + WACC in Postmortem)

**File:** `auto_valuation/learning/postmortem.py`
**Change:** Add bisection solver for `actual_terminal_growth` and `actual_wacc` in `run_annual_postmortem()`; populate fields in `CalibrationObservation` via `historical_replay.py` and `live_evidence_bootstrap.py`
**Effort:** ~150 lines across 3 files
**Impact:** Closes the feedback loop; calibrator now learns terminal g accuracy per sector

---

### Priority 3 — Revenue Trajectory Constraints + Structural Decline Flag

**Files:** `auto_valuation/assumptions/growth.py`, new `auto_valuation/assumptions/headwind_table.py`
**Change:** Add `_classify_revenue_regime()`, `_compute_structural_decline_flag()`, `terminal_g_prior_range` computation; static industry headwind table
**Effort:** ~200 lines
**Impact:** Prevents positive terminal g for structural decliners regardless of learning history

---

### Priority 4 — Market-Implied g Fed Into Calibration

**Files:** `auto_valuation/learning/_layered_calibrator.py`, `webapp/app.py` (valuate route)
**Change:** Add 7th market signal layer; dynamic `w` weight; pass `market_implied_g` parameter through valuation pipeline
**Effort:** ~100 lines
**Impact:** Immediate correction for liquid large/mid-cap names; Signify base case drops from +142% to ~fair value

---

### Priority 5 — Expand Postmortem (D&A, Capex, Tax, SBC)

**File:** `auto_valuation/learning/postmortem.py`
**Change:** Add `da_error_pct`, `capex_error_pct`, `tax_rate_error_bps`, `sbc_error_pct` to `PostmortemRecord`; compute from `source_payload` in `run_annual_postmortem()`
**Effort:** ~80 lines
**Impact:** Enables Gap 3 structural break improvement; closes measurement gap for 4 variables

---

### Priority 6 — Expand `CalibrationObservation`

**File:** `auto_valuation/learning/_layered_calibrator.py`
**Change:** Add 4 predicted/actual pairs (da_pct, capex_pct, tax_rate, sbc_pct); corresponding `_AssumptionSpec` entries
**Effort:** ~60 lines
**Impact:** 4 new variables enter the calibration loop; calibration coverage rises from 6/22 to 10/22

---

### Priority 7 — Improved Structural Break Score

**Files:** `auto_valuation/learning/postmortem.py`, `auto_valuation/learning/_layered_calibrator.py`
**Change:** Replace revenue-only formula with multi-variable weighted formula (requires capex error from Priority 5)
**Effort:** ~20 lines
**Impact:** Earlier detection of capex shocks and margin collapses; structural break false-negative rate drops ~15%

---

### Priority 8 — Regime Classifier (Layer F)

**New file:** `auto_valuation/learning/regime_classifier.py`  
**New file:** `auto_valuation/learning/train_regime_classifier.py`
**Effort:** ~400 lines; ~1 hour training time on 33k postmortems
**Impact:** Regime-aware near-term CAGR prediction; prevents stable-regime models from being contaminated by decline-regime data

---

### Priority 9 — EV Variance Decomposition

**File:** `auto_valuation/learning/postmortem.py`
**Change:** Replace `error_attribution` string with quantified DCF sensitivity partials; store contribution of each input to total EV error
**Effort:** ~120 lines
**Impact:** Enables targeted calibration down-weighting per assumption per miss

---

### Priority 10 — Per-Sector Performance Report

**File:** `auto_valuation/learning/performance_report.py`
**Change:** Add `by_sector_variable` breakdown table with trend comparison
**Effort:** ~100 lines
**Impact:** Makes systematic sector biases visible and auditable

---

## Expected Impact

| Variable | Current tracking | Current MAE | After Priority 1–4 | After full stack |
|---|---|---|---|---|
| Terminal g | Dead field | ~750bps on decliners | Tracked, ~400bps | ~100–150bps stable |
| Revenue growth (all) | ✅ | 23.4% | ~20% (regime gating) | ~12–14% |
| Revenue growth (stable) | ✅ | 9.4% | ~8% | ~6% |
| EBIT margin near-term | Tracked, not reported | ~90bps | Reported, ~80bps | ~60bps |
| WACC | Dead field | ~50bps (estimated) | Tracked, ~50bps | ~40bps |
| D&A % revenue | ❌ | Not tracked | Tracked | Tracked + calibrated |
| Capex % revenue | ❌ | Not tracked | Tracked | Tracked + calibrated |
| SBC % revenue | ❌ | Not tracked | Tracked | Tracked + calibrated |
| EV MAE | ✅ | 75.4% | **~50–55%** | **~35–40%** |
| Structural break false-negative | ~30% est. | — | ~20% | ~12% |
| Calibration variable coverage | — | 6 of 22 | 8 of 22 | 11 of 22 |
| EV error attributable to specific input | ❌ | Not possible | Not possible | Quantified per record |

EV MAE target of <40% is achievable because terminal value accounts for 71% of EV for most mature names (as confirmed by Signify's 71.4% TV/EV). Fixing terminal g from +2.5% to −4.2% for a company like Signify reduces EV error by 60–70 percentage points on that single ticker.

---

## What Won't Improve (and Why)

**Price return prediction (current MAE 77.8%)** will remain high regardless of DCF accuracy. Price is driven by multiple expansion/compression, sector rotation, and macro sentiment — none of which are functions of DCF line items. A DCF with perfect revenue, margin, capex, and WACC still misses price by 30–50% in a re-rating cycle. This is not a model deficiency — it is a fundamental property of public equity markets. The confidence score correctly penalises price return uncertainty; the goal is not to fix it but to communicate it.

---

## Files to Modify or Create

| File | Change | Priority |
|---|---|---|
| `auto_valuation/learning/_layered_calibrator.py` | Fix terminal g floor `low=-0.06`; add market signal layer; add 4 new `_AssumptionSpec` entries; add 8 new `CalibrationObservation` fields | 1, 4, 6 |
| `auto_valuation/learning/postmortem.py` | Wire `actual_terminal_growth` + `actual_wacc` via bisection; add 4 new error fields; replace error_attribution with variance decomposition | 2, 5, 9 |
| `auto_valuation/learning/historical_replay.py` | Populate new CalibrationObservation fields from EODHD payload | 2, 5, 6 |
| `auto_valuation/learning/live_evidence_bootstrap.py` | Same as historical_replay | 2, 5, 6 |
| `auto_valuation/assumptions/growth.py` | Add revenue regime classification; trajectory-based terminal g prior range; dynamic `terminal_g_range` parameter to pass to calibrator | 3 |
| `auto_valuation/assumptions/engine.py` | Consume structural decline flag; pass `market_implied_g` and `terminal_g_range` through to calibrator | 3, 4 |
| `auto_valuation/forecast/dcf.py` | Add floor to `enforce_terminal_growth_consistency()`; ensure negative terminal g is handled correctly in Gordon Growth formula | 1 |
| `auto_valuation/learning/performance_report.py` | Add `by_sector_variable` breakdown; surface `margin_error_bps` (currently tracked but not reported) | 10 |
| `auto_valuation/assumptions/headwind_table.py` | **New file** — static industry headwind score lookup dict (~60 entries) | 3 |
| `auto_valuation/learning/regime_classifier.py` | **New file** — LightGBM regime classifier + 5 Ridge regressors; `predict_regime()` and `predict_near_term_growth()` | 8 |
| `auto_valuation/learning/train_regime_classifier.py` | **New file** — training script; reads ledger DB, derives regime labels, serializes models | 8 |
