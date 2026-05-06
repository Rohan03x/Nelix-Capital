# Nelix Brain — Full Architecture Audit & Improvement Plan
*Last updated: 2026-05-06 — full pass across all learning modules*

---

## EXECUTIVE SUMMARY

The brain has good structural bones but three **silent accuracy killers** that make most learning irrelevant:

1. **WACC/beta/terminal-growth calibration layers have zero signal** — historical replay sets `actual_wacc = wacc0` (sector default), making every WACC residual = 0. The model cannot learn WACC corrections at all.
2. **Implied WACC/TG deltas are computed but never stored as training data** — `market_implied.py` back-solves the market-implied WACC from every matured prediction, then discards it. The brain forgets what markets told it.
3. **Company memory priority is mathematically dominated by cohort size** — `priority × √n / scale_penalty` where cohort n=514 >> company n=5-51 means the 45M-record brain acts mostly as a sector/cohort engine, not a company-specific one.

Fix these three and accuracy improves structurally. Everything else below is enhancement.

---

## CRITICAL BUGS (Fix First — Zero Code Complexity, Massive Impact)

### BUG-1: WACC Calibration Layer Has No Signal (historical_replay.py)

**File:** `auto_valuation/learning/historical_replay.py` lines ~268–315

**What's wrong:**
```python
# Current code (BROKEN):
actual_wacc=wacc0,          # unobservable → zero WACC error  ← comment admits it
actual_wacc=wacc0,          # quarterly too — same problem
actual_beta=_DEFAULT_BETA,  # always 1.0 — zero beta residual
actual_terminal_growth=_DEFAULT_TGR,  # always ~0.025 — zero TG residual
```

`wacc0 = _SECTOR_WACC.get(sector, _DEFAULT_WACC)` — a static sector lookup.  
Since `predicted_wacc = wacc0` and `actual_wacc = wacc0`, every observation has a WACC residual of **exactly 0.0**. The calibration layer learns nothing. 29,000+ historical replay observations are contributing zero WACC correction signal.

**Fix:**
- Use EODHD's implied beta + historical risk-free rates to compute a time-specific actual WACC:
  ```python
  # In historical_replay.py — use beta from fundamentals if available
  actual_beta_val = _extract_historical_beta(fundamentals, year) or _DEFAULT_BETA
  rf_at_year = _historical_rf(int(year))
  erp_at_year = _historical_erp(int(year))  # Damodaran ERP table
  actual_wacc_val = rf_at_year + actual_beta_val * erp_at_year + _size_premium(cap)
  
  obs.append(CalibrationObservation(
      ...
      actual_wacc=actual_wacc_val,   # real signal now
      actual_beta=actual_beta_val,
      predicted_wacc=wacc0,          # sector prior = the "prediction"
      predicted_beta=_DEFAULT_BETA,
      ...
  ))
  ```
- If beta history isn't available from EODHD, use `market_implied.py`'s back-solved WACC instead:
  ```python
  implied_snap = compute_market_implied_snapshot(matured_record)
  actual_wacc_val = implied_snap.implied_wacc_pct / 100.0 if implied_snap.implied_wacc_pct else wacc0
  ```

**Impact:** Unlocks WACC calibration for all 29K+ replay observations. WACC accuracy improvement estimated at +15-25% (currently the layer contributes zero).

---

### BUG-2: Market-Implied WACC/TG Deltas Computed But Never Persisted (market_implied.py)

**File:** `auto_valuation/learning/market_implied.py`

**What's wrong:**  
`build_market_residual_overlay()` correctly computes:
```python
implied_wacc_delta_pct      # how wrong the WACC assumption was vs what market priced
implied_terminal_growth_delta_pct  # same for terminal growth
```
These are included in the return dict (`wacc_adj_pp`, `terminal_growth_adj_pp`) and applied as a live adjustment via `knowledge_model.py`. But they are **never written back as `CalibrationObservation` training data**.

Every time a prediction matures and the market-implied WACC differs from the predicted WACC, that signal is used once and then lost. The next run for the same ticker starts from scratch.

**Fix:**  
In `live_evidence_bootstrap.py` or the maturity processing pipeline, after computing `build_market_residual_overlay()`, create a `CalibrationObservation` with the implied actual:
```python
# After a prediction matures and implied signals are computed:
if implied_snap.implied_wacc_pct is not None:
    market_obs = CalibrationObservation(
        sector=record.sector,
        industry=record.industry,
        ...
        predicted_wacc=record.predicted_wacc,
        actual_wacc=implied_snap.implied_wacc_pct / 100.0,  # ← market-implied, not inferred
        predicted_terminal_growth=record.predicted_terminal_growth,
        actual_terminal_growth=implied_snap.implied_terminal_growth_pct / 100.0,
        ticker=record.ticker,
        as_of_year=record.forecast_horizon_year,
        quality_score=implied_snap.quality_score,  # gate on quality
        growth_regime="market_implied",  # flag the source
    )
    calibration_store.save_observation(market_obs)
```

**Impact:** Every matured prediction permanently enriches WACC and TG calibration. Over time, the brain learns which sectors/regimes have structurally mispriced WACCs.

---

### BUG-3: `len(errors)` in Weight Formula Uses Raw Count, Not Decay-Effective Count

**File:** `auto_valuation/learning/_layered_calibrator.py` lines ~690-700

**What's wrong:**
```python
# Current:
raw_weight = priority * math.sqrt(len(errors)) / scale_penalty
```

`_weighted_robust_mean()` correctly applies time-decay weights to compute the residual mean. But `len(errors)` is the **raw count** — a cohort with 514 observations, even if 400 are 5+ years old, contributes `√514 = 22.7` to the weight. The effective information content should be much lower because old observations are decayed.

**Fix:** Use effective sample size (the Kish approximation):
```python
# Compute decay-weighted effective count instead of raw len
current_year = date.today().year
weights = [_observation_weight(obs, current_year) for obs in cohort]
sum_w = sum(weights)
sum_w2 = sum(w * w for w in weights)
effective_n = (sum_w * sum_w) / max(sum_w2, 1e-9)  # Kish effective sample size

raw_weight = priority * math.sqrt(effective_n) / scale_penalty
```

For a cohort with 514 observations at 15% annual decay rate, effective_n ≈ 85-120 rather than 514. This makes `√effective_n ≈ 10` vs `√514 = 22.7` — far more appropriate.

**Impact:** Reduces cohort dominance, naturally increases company memory relative weight for tickers with fresh observations.

---

## HIGH-PRIORITY ACCURACY IMPROVEMENTS

### H1: Company Memory Priority — Raise 1.45 → 3.0

**File:** `auto_valuation/learning/_layered_calibrator.py` lines ~610-618

**Current priorities:**
```python
("company_memory", ..., 1.45, ...),
("cohort_memory",  ..., 1.20, ...),  # with n=514, cohort raw_weight = 1.20 × √514 = 27.2
("sector_memory",  ..., 0.95, ...),
("analog_memory",  ..., 0.90, ...),
("macro_memory",   ..., 0.75, ...),
("global_memory",  ..., 0.55, ...),
```

**Mathematical justification (Bühlmann-Straub credibility theory):**

Optimal company weight = `Var(sector_residuals) / (Var(sector_residuals) + Var(company_residuals))`

Company-specific residuals are **autocorrelated** (management style, capital allocation DNA persist). Sector residuals are independent. This gives company observations higher per-observation information value.

At priority=1.45 with company n≈51: `raw_weight = 1.45 × 7.1 = 10.3`  
At priority=1.45 with cohort n=514: `raw_weight = 1.20 × 22.7 = 27.2`  
Company gets **27%** of blend. Should be **~55%** for a well-covered ticker.

**Fix:**
```python
("company_memory", ..., 3.0,  ...),  # → company raw_weight = 3.0 × 7.1 = 21.3
("cohort_memory",  ..., 1.20, ...),  # → cohort raw_weight = 1.20 × 22.7 = 27.2
# After combining BUG-3 fix (effective_n): cohort effective_n ≈ 100 → raw_weight ≈ 12.0
# Combined result: company ≈ 55%, cohort ≈ 30%, others ≈ 15%
```

**After combining with BUG-3 fix (effective sample size):**
- Company: `3.0 × √51 ≈ 21.4`
- Cohort: `1.20 × √100 ≈ 12.0` (effective_n after decay)
- Normalized company weight: **≈ 55%**

---

### H2: Analog Threshold — Config Mismatch and Threshold Too High

**Files:** 
- `auto_valuation/config.py` line 265: `"min_analog_similarity": 0.75`
- `auto_valuation/learning/_layered_calibrator.py` line 493: `float(_LEARNING_CONFIG.get("min_analog_similarity", 0.82))`

**Problem:** The calibrator hardcodes a default of 0.82 but config says 0.75. Whichever wins, both are too high. In practice, `_filter_analog_memory()` returns 0–3 analogs for most companies. With 2 analogs, `√2 × 0.90 = 1.27` raw weight → analog layer weight ≈ 2-3% of blend. Effectively zero.

**Why 0.82 is wrong:** Cosine similarity on 21 features normalized in different ways. A similarity of 0.82 means 2 companies match on 17/21 features within tolerance. This exists only for near-identical companies — useless for cross-sector pattern matching.

**Fix:**
1. In `auto_valuation/config.py`: set `"min_analog_similarity": 0.65`
2. In `_layered_calibrator.py`: change default from 0.82 → 0.65
3. Apply analog vintage decay in the analog score: currently `recency_weight` is computed in `_recency_weight()` (using 12-year window) and stored on `AnalogMatch` but needs to be applied in the final `analog_score` denominator

**Expected result:** 8-15 analogs per company instead of 0-3. Analog layer rises from 2% to 8-10% of blend.

---

### H3: Pattern Library Overlays Have Zero Validation

**File:** `auto_valuation/learning/cross_industry.py` — `PATTERN_LIBRARY`

**Problem:** Patterns like `PLATFORM_FLYWHEEL` apply `{"revenue_growth_adj": 0.02, "ebit_margin_adj": 0.01}` to any company matching 4 structural conditions — without any backtesting. If historically platform flywheels showed -1% margin (due to investment mode), this overlay is directionally wrong.

The conditions are also fragile:
```python
# PLATFORM_FLYWHEEL fires when:
"revenue_cagr_3y": (0.30, None),    # growing >30%
"fcf_conversion": (None, 0.0),      # negative FCF 
"capex_intensity": (None, 0.06),    # low capex (contradicts most flywheels)
"gross_margin_ttm": (0.60, None),   # >60% gross margin
```
Low capex + high gross margin + negative FCF + 30%+ growth → this fires for SaaS (correct) but also for many high-growth companies burning cash on S&M (wrong pattern match). DISRUPTED_INCUMBENT and REGULATORY_WINDFALL conditions are even looser.

**Fix:**
1. Add `confidence_threshold: float = 0.0` to `PatternDefinition` — only apply overlay when `_pattern_score()` > threshold (e.g., 0.75 = all conditions must match, not partial)
2. Add `overlay_validated: bool = False` — overlays where `overlay_validated=False` get damped to 50% of their stated value until postmortem evidence confirms them
3. Long-term: run a retrospective on postmortem data to validate or invalidate each pattern's overlay direction

---

### H4: Structural Break Flag is Binary — Should Be Graded

**File:** `auto_valuation/learning/_layered_calibrator.py` line ~720

**Current:**
```python
structural_break_flag: bool = False  # on CalibrationObservation
```

```python
# In _build_assumption_summary:
if layer_name in {"company_memory", "cohort_memory", "sector_memory"} and structural_break.detected:
    raw_weight *= max(0.2, 1.0 - (0.60 * structural_break.score))
```

`StructuralBreakSummary.score` is already graded (0.0–1.0). But `CalibrationObservation.structural_break_flag` is boolean — so per-observation structural break context is lost when observations are aggregated.

**Current trigger in postmortem.py:**
```python
structural_break = bool(structural_break_hints) or abs(revenue_error_pct) > 25.0
```
A single 26% revenue miss = structural break = company/cohort memory penalized 60%. A company recovering from a one-time miss gets wrongly penalized for 3+ calibration cycles.

**Fix:**
1. Add `structural_break_score: float = 0.0` to `CalibrationObservation`
2. Change postmortem trigger: `structural_break_score = min(1.0, abs(revenue_error_pct) / 50.0 + consecutive_miss_count * 0.15)`
3. In `_build_assumption_summary`, use the per-observation break score for weighting rather than a binary detected/not-detected at the summary level

---

## MEDIUM-PRIORITY SIGNAL IMPROVEMENTS

### M1: Add Momentum Regime to Calibration Observations

**Highest-impact missing signal.** The feature space intentionally excludes behavioral signals (comment in `feature_space.py` line 45-50), but momentum regime should be added as a **calibration context dimension**, not a feature-similarity dimension.

**What to add to `CalibrationObservation`:**
```python
price_momentum_regime: str = "neutral"  # "strong_bull", "bull", "neutral", "bear", "strong_bear"
sector_rotation_phase: str = "neutral"  # "in_favor", "neutral", "out_of_favor"
```

These fields come from `macro_backdrop` which already includes `market_cycle_phase` in `PredictionRecord`. They should be propagated into the calibration observation so the model can learn: "EBIT margin residuals for Consumer Discretionary in bear markets average -150bps more than in bull markets."

**Why this matters:** The model currently has no way to know that a NKE prediction made in 2022 (bear market, consumer discretionary rotation out) should be calibrated differently than one made in 2021. Both years' observations get pooled with equal regime context.

---

### M2: Revenue Volatility Missing from WACC Calibration

`CalibrationObservation` has `predicted_wacc` and `actual_wacc` but no `revenue_volatility` field. The calibrator can't learn: "companies with revenue growth volatility > 20% need 150bps higher WACC adjustment."

`feature_space.py` already computes `revenue_growth_volatility` as a feature. It should be propagated to `CalibrationObservation` so the WACC layer can condition on it.

**Add to `CalibrationObservation`:**
```python
revenue_volatility: float = 0.0   # annualized std of revenue growth (from feature vector)
margin_volatility: float = 0.0    # annualized std of EBIT margin changes
```

---

### M3: `rf_rate_at_time` Is Rarely Populated

`CalibrationObservation` has `rf_rate_at_time: float | None = None` and historical replay correctly sets it via `_historical_rf(int(year))`. But this field is **not used anywhere in the calibration logic** in `_layered_calibrator.py`. It's stored but ignored.

The WACC calibration layer should condition residuals on the rate environment at time of prediction. A WACC over-estimation made in 2020 (rf=0.5%) has different implications than the same error in 2023 (rf=4.5%).

**Fix:** In `_build_assumption_summary` for the `wacc` assumption, apply a rate-era normalization:
```python
# When computing WACC residuals, normalize to current rf environment:
rate_era_adj = (current_rf - obs.rf_rate_at_time) if obs.rf_rate_at_time else 0.0
normalized_wacc_error = (obs.actual_wacc - obs.predicted_wacc) - rate_era_adj
```

---

### M4: Cohort Matching Ignores Macro Timing Alignment

`_filter_exact_cohort()` matches on `macro_regime` (string: "expansion", "contraction", etc.) but NOT on whether the analog and subject are at the **same point in their respective economic cycles**.

A Consumer Staples company in "contraction" macro in 2009 (early in a severe downturn) is being matched with one in "contraction" macro in 2023 (shallow, short recession). The macro_regime string is the same but the cohort should not be considered equivalent.

This is hard to fully fix but a useful improvement: add `macro_cycle_year` (1st year of contraction, 2nd year, etc.) as a matching dimension in `_filter_exact_cohort`.

---

### M5: CalibrationStore Has No Observation Storage — Only Priors

**File:** `auto_valuation/learning/_layered_calibrator.py` — `CalibrationStore`

**Critical architectural gap:** `CalibrationStore._ensure_schema()` creates only a `calibration_priors` table (summary statistics). Individual `CalibrationObservation` objects are NOT persisted to any database. They live in memory only (in `obs_cache.pkl` via `ObservationCache`).

This means:
- If `obs_cache.pkl` is deleted or corrupted, all calibration signal is lost
- Observations cannot be queried by assumption name, ticker, or date range for analysis
- No audit trail for which observations drove which priors

**Fix:** Add `calibration_observations` table to `CalibrationStore` and write each observation on creation. This enables per-assumption drilling, observation-level auditing, and proper debugging of calibration drift.

---

### M6: `actual_ev_mm` Sparsity Kills Market Overlay

`build_market_residual_overlay()` in `market_implied.py` requires `min_records = 5` matured predictions with EV data. But `actual_ev_mm` in `PredictionRecord` is only populated when the background runner successfully fetches it at maturity time.

For most tickers, `actual_ev_mm = None` → `compute_market_implied_snapshot()` returns None → overlay disabled. The entire market-implied feedback loop silently fails for any ticker where the background runner didn't fetch post-maturity EV.

**Fix:** In `live_evidence_bootstrap.py`, when labeling a matured prediction, explicitly fetch and store `actual_ev_mm` using current market cap + net debt data. Add a check in the background runner audit: log how many matured predictions have `actual_ev_mm = None`.

---

## LOW-PRIORITY (Quality-of-Life / Future Sessions)

### L1: Quinquennial Report Never Used for Calibration

`QuinquennialStore` (postmortem.py) generates trajectory/drift analysis every 5 years per ticker. But the output — `trajectory_analysis`, `assumption_drift_diagnosis`, `compounding_error_attribution` — is stored but **never read back into the calibration pipeline**. A 5-year drift diagnosis should update the company memory prior.

### L2: Pattern Score Is Partial Match Ratio — Should Weight Conditions

`_pattern_score()` in `cross_industry.py` returns `sum(checks) / len(checks)` where each condition check is binary (pass/fail). A company that barely passes all 4 PLATFORM_FLYWHEEL conditions scores 1.0, same as one that dramatically exceeds them. Weight conditions by how far the value exceeds the threshold.

### L3: `model_bias_signal` in Postmortem Uses a Flat ±10% Threshold

```python
def _model_bias_signal(errors: list[float]) -> str:
    if mean_error <= -10.0: return "optimistic"
    if mean_error >= 10.0:  return "pessimistic"
```

This threshold is the same for NKE (low-volatility, 5% growth) and a EM growth company (high-volatility, 30% growth). A 10% miss for NKE is huge; for the EM growth company it's noise. Scale the threshold by the sector's expected volatility.

### L4: Cross-Industry Exchange Alias Normalization Is Incomplete

`cross_industry.py` normalizes `.KS` ↔ `.KO` but not `.T` ↔ `.TYO`, `.L` ↔ `.LON`, or `AMS` ↔ `.AS`. European tickers frequently fail to find analogs because the exchange suffix doesn't match.

### L5: Analog `recency_weight` Computed But Not Applied in `analog_score`

`_recency_weight()` exists and sets `AnalogMatch.recency_weight`. But looking at how `analog_score` is computed, the recency weight may not be factored into the final score that determines which analogs are kept vs. discarded at the similarity threshold. Verify and ensure recency weight multiplies into the sorting/pruning step.

---

## IMPLEMENTATION ORDER (By Bang-for-Buck)

| # | Change | File(s) | Impact | Complexity |
|---|--------|---------|--------|------------|
| 1 | **BUG-1**: Use real WACC in historical_replay (beta + rf + erp) | `historical_replay.py` | ★★★★★ | Medium |
| 2 | **BUG-2**: Store implied WACC/TG back as CalibrationObservations | `market_implied.py` + bootstrap | ★★★★★ | Medium |
| 3 | **BUG-3**: Effective sample size in raw_weight (Kish formula) | `_layered_calibrator.py` L~700 | ★★★★☆ | Low |
| 4 | **H1**: Raise company_memory priority 1.45 → 3.0 | `_layered_calibrator.py` L614 | ★★★★☆ | Trivial |
| 5 | **H2**: Lower min_analog_similarity 0.75/0.82 → 0.65 | `config.py` + `_layered_calibrator.py` | ★★★☆☆ | Trivial |
| 6 | **H4**: Graded structural break score (0–1) instead of boolean | `_layered_calibrator.py` + `postmortem.py` | ★★★☆☆ | Medium |
| 7 | **H3**: Pattern overlay confidence threshold + damping | `cross_industry.py` | ★★★☆☆ | Low |
| 8 | **M6**: Ensure `actual_ev_mm` populated at maturity | `live_evidence_bootstrap.py` | ★★★☆☆ | Medium |
| 9 | **M3**: Use `rf_rate_at_time` for rate-era WACC normalization | `_layered_calibrator.py` | ★★★☆☆ | Medium |
| 10 | **M1**: Momentum regime on CalibrationObservation | `ledger.py` + `_layered_calibrator.py` | ★★★☆☆ | High |
| 11 | **M5**: Add observation-level storage to CalibrationStore | `_layered_calibrator.py` | ★★☆☆☆ | Medium |
| 12 | **M2**: Revenue/margin volatility on CalibrationObservation | `historical_replay.py` + ledger | ★★☆☆☆ | Low |

---

## QUICK WINS (Can Deploy Today)

These are single-line or single-constant changes:

```python
# 1. H1 — company_memory priority in _layered_calibrator.py ~L614:
("company_memory", company_memory, 3.0, "Same-symbol realised history...")

# 2. H2 — min_analog_similarity in config.py L265:
"min_analog_similarity": 0.65,  # was 0.75

# 3. Calibrator default fallback in _layered_calibrator.py L493:
min_similarity = float(_LEARNING_CONFIG.get("min_analog_similarity", 0.65))  # was 0.82
```

These three changes alone will visibly shift company memory weight from ~45-50% to ~60-65% for well-covered tickers, and double or triple the number of analog matches for most companies.

---

## WHAT GOOD LOOKS LIKE (TARGET STATE)

After all critical + high-priority fixes:

| Metric | Current | Target |
|--------|---------|--------|
| Company memory weight (NKE) | ~45-50% | 60-65% |
| Analogs per company (avg) | 0-3 | 8-15 |
| WACC calibration signal | 0 (BUG-1) | 29K+ observations |
| Market-implied WACC learning | ephemeral, forgotten | persistent, cumulative |
| Structural break false-positives | high (binary 25% trigger) | low (graded, persisted) |
| Pattern overlay reliability | unvalidated | confidence-gated |
| WACC MAPE | uncalibrated | -15-25% (estimated) |
| EV MAPE | 22% | ~18% (estimated) |

---

*All changes are backward-compatible. No schema migrations required for quick wins 1-3.*
*BUG-1 and BUG-2 require rebuilding the deployment seed after implementation to push new signal to production.*
