# Adaptive Self-Improving DCF Engine — Build Prompt

## Overview

You are building an advanced **adaptive DCF forecasting system** that augments the existing `auto_valuation/` engine. The system tracks every prediction made by the model from a company's IPO through 54 years of history, performs structured post-mortems every 5 years, learns from every error, ingests cross-industry pattern intelligence, and uses all of this to continuously narrow forecast uncertainty and improve the quality of future projections.

The system has three interlocking pillars:
1. **Temporal Self-Calibration** — rolling post-mortem engine that checks predictions against actuals
2. **Cross-Industry Pattern Intelligence** — sector-agnostic learning that detects structural analogs
3. **Adaptive Confidence & Uncertainty Quantification** — Bayesian confidence bands that tighten as the model learns

---

## System Architecture

### Module: `auto_valuation/learning/`

Create the following new submodules inside `auto_valuation/learning/`:

```
auto_valuation/learning/
    __init__.py
    ledger.py             # Immutable prediction log — every forecast ever made
    postmortem.py         # 5-year and 1-year post-mortem engine
    attribution.py        # Error decomposition: what drove the miss?
    calibrator.py         # Recalibrates assumption priors from ledger
    cross_industry.py     # Cross-sector pattern matching and analog scoring
    confidence.py         # Bayesian confidence intervals on all outputs
    online_research.py    # Structured web research integration layer
    adapter.py            # Applies learning signals back to assumptions engine
```

---

## Module 1: Prediction Ledger (`ledger.py`)

### Purpose
An **append-only, time-stamped log** of every prediction the model makes. This is the ground truth dataset that feeds all learning.

### Schema — `PredictionRecord`

```python
@dataclass
class PredictionRecord:
    # Identity
    record_id: str                    # UUID, immutable
    ticker: str
    company_name: str
    sector: str                       # GICS Level 1
    industry: str                     # GICS Level 2
    run_date: date                    # When was this prediction made?
    forecast_horizon_year: int        # Which calendar year is being predicted?
    years_since_ipo: int              # How many years post-IPO?
    data_vintage_years: int           # How many years of history were available?

    # Prediction
    predicted_revenue_mm: float
    predicted_ebit_margin: float
    predicted_ebit_mm: float
    predicted_ufcf_mm: float
    predicted_wacc: float
    predicted_terminal_growth: float
    predicted_ev_mm: float
    predicted_equity_value_mm: float
    predicted_price_per_share: float
    scenario: str                     # base / bull / bear

    # Assumption drivers at prediction time
    near_term_revenue_growth: float
    target_ebit_margin: float
    da_pct_revenue: float
    capex_pct_revenue: float
    beta: float
    erp: float
    rf_rate: float

    # Context at prediction time
    actual_price_at_prediction: float
    actual_ev_at_prediction: float
    market_cycle_phase: str           # expansion / contraction / peak / trough
    macro_backdrop: dict              # { "10y_yield": x, "cpi_yoy": x, "gdp_growth": x }

    # Actuals (populated at post-mortem time, None until then)
    actual_revenue_mm: Optional[float]
    actual_ebit_margin: Optional[float]
    actual_ufcf_mm: Optional[float]
    actual_ev_mm: Optional[float]
    actual_price_at_horizon: Optional[float]
    postmortem_date: Optional[date]
    postmortem_notes: Optional[str]
```

### Storage
- Primary store: SQLite database (`learning/db/predictions.db`), schema-versioned via Alembic-style migrations.
- Secondary: JSONL export per ticker (`learning/ledger/{ticker}.jsonl`) for portability and diff tracking.
- Ledger is **immutable** — records are never updated; post-mortem data is appended as a linked `PostmortemRecord` with a foreign key to `record_id`.
- Provide `LedgerWriter.append(record)` and `LedgerReader.query(ticker, horizon_year, scenario)`.

---

## Module 2: Post-Mortem Engine (`postmortem.py`)

### Purpose
At two cadences, compare predictions against actuals and produce a structured diagnostic:
- **Annual check**: Y+1 — compare 1-year-ahead prediction against actual FY result
- **Quinquennial review**: Y+5 — full 5-year post-mortem comparing the entire forecast arc

### Annual Check (`run_annual_postmortem(ticker, horizon_year)`)

1. Fetch actual financials for `horizon_year` from the data layer (`auto_valuation/data/fetcher.py`).
2. Load all `PredictionRecord` rows where `ticker == ticker AND forecast_horizon_year == horizon_year`.
3. For each record compute:
   - `revenue_error_pct = (actual - predicted) / predicted * 100`
   - `margin_error_bps = (actual_margin - predicted_margin) * 10_000`
   - `ev_error_pct = (actual_ev - predicted_ev) / predicted_ev * 100`
   - `price_return_error_pct` (predicted implied return vs actual)
4. Produce a `PostmortemRecord` with:
   - All error metrics
   - `primary_miss_driver` — the single metric with largest relative error
   - `surprise_flags` — list of events in the year not captured at prediction time (M&A, regulatory, macro shock)
   - `model_bias_signal` — was the model systematically optimistic or pessimistic in this year?

### Quinquennial Review (`run_5year_postmortem(ticker, base_year)`)

Runs annual post-mortems for years `base_year+1` through `base_year+5`, then adds:

1. **Trajectory analysis** — did the model predict the *direction* of margin and revenue trends correctly?
2. **Assumption drift diagnosis** — which assumption moved most from initial to final (e.g., WACC expanded 200 bps due to rate cycle)?
3. **Structural break detection** — did a structural break occur mid-period (new technology, regulatory change, competitor entry)? Use heuristic: if actual revenue diverges from predicted by >25% in any single year, flag as structural break candidate.
4. **Compounding error attribution** — decompose the cumulative EV miss into:
   - Revenue growth variance
   - Margin compression/expansion
   - Multiple re-rating (EV/EBITDA change)
   - WACC change impact
   - Terminal value sensitivity
5. **Cross-industry comparison** — did analog companies (from `cross_industry.py`) perform differently, and if so, was the signal detectable in advance?

Output format: `QuinquennialReport` dataclass with all the above, stored in `learning/db/postmortems.db`.

---

## Module 3: Error Attribution Engine (`attribution.py`)

### Purpose
Decompose *why* a prediction was wrong into structured causes.

### Attribution Categories

```python
class ErrorDriver(Enum):
    REVENUE_SURPRISE        = "revenue_surprise"       # Macro or company-specific top-line miss
    MARGIN_SURPRISE         = "margin_surprise"        # Cost structure changed
    CAPEX_CYCLE             = "capex_cycle"            # Unanticipated investment cycle
    MULTIPLE_RERATING       = "multiple_rerating"      # Market re-rated the sector
    MACRO_RATE_SHIFT        = "macro_rate_shift"       # Risk-free rate / WACC shift
    STRUCTURAL_DISRUPTION   = "structural_disruption"  # Technology, regulation, new competitor
    ACQUISITION_DILUTION    = "acquisition_dilution"   # M&A changed financials materially
    CURRENCY_IMPACT         = "currency_impact"        # FX move (>10% delta in functional currency)
    ONE_TIME_ITEM           = "one_time_item"          # Non-recurring charge/gain
    MANAGEMENT_CHANGE       = "management_change"      # CEO/CFO change with strategy pivot
    MACRO_CYCLE             = "macro_cycle"            # Business cycle (recession, expansion)
    SECTOR_ROTATION         = "sector_rotation"        # Broad sector multiple compression
    MODEL_BIAS              = "model_bias"             # Systematic model error (overfit, anchoring)
```

### Attribution Algorithm

For each `PostmortemRecord`:

1. **Variance decomposition** (Shapley-style):
   - $\Delta EV = \frac{\partial EV}{\partial g} \cdot \Delta g + \frac{\partial EV}{\partial \text{margin}} \cdot \Delta \text{margin} + \frac{\partial EV}{\partial \text{WACC}} \cdot \Delta \text{WACC} + \epsilon$
   - Use the DCF model's own sensitivity grid to compute partial derivatives.

2. **External event tagger**:
   - Check macro backdrop delta: if `10y_yield` moved >150 bps during forecast period → `MACRO_RATE_SHIFT`
   - Check revenue growth deviation: if actual YoY < predicted by >15% in any year → `REVENUE_SURPRISE`
   - Check if any peer in same GICS sub-industry had similar miss → likely `SECTOR_ROTATION`

3. **Bias detector**:
   - Accumulate signed errors across all predictions for this ticker.
   - If mean error > +10% for 3+ consecutive years → systematic optimism bias → `MODEL_BIAS`

4. Return a ranked list of `(ErrorDriver, contribution_pct)` summing to 100% of total EV error.

---

## Module 4: Assumption Calibrator (`calibrator.py`)

### Purpose
Use the ledger of post-mortems to update **prior distributions** over key assumptions, so future forecasts are better calibrated.

### Method: Empirical Bayes Prior Updating

For each assumption $\theta \in \{\text{revenue growth, EBIT margin, WACC, terminal growth, beta}\}$:

1. **Build empirical distribution** from all past prediction errors for this (sector, data_vintage_years) cohort:
   ```
   θ_error_i = actual_θ_i - predicted_θ_i   for each past record i
   ```

2. **Fit a correction factor**:
   ```
   correction_mean = mean(θ_errors)
   correction_std  = std(θ_errors)
   ```

3. **Apply correction to new forecast**:
   ```
   θ_adjusted = θ_model + correction_mean
   θ_std_band = base_std + correction_std  (widens uncertainty)
   ```

### Cohort Stratification

Calibration is stratified by:
- **Sector** (GICS Level 1): 11 sectors
- **Company maturity**: `data_vintage_years` buckets: [1–3], [4–10], [11–20], [21+]
- **Market cap regime**: nano (<$300M), small ($300M–$2B), mid ($2B–$10B), large ($10B–$100B), mega (>$100B)
- **Macro regime**: tight (10y yield >4%) vs. easy (10y yield <2%) vs. neutral

A correction factor is only applied when the cohort has ≥5 historical observations. Otherwise, use global sector average.

### Output

`CalibratedAssumptions` dataclass extending the existing `AssumptionSet`:
- All original fields from `auto_valuation/assumptions/engine.py`
- `revenue_growth_adj: float` — calibrated point estimate
- `revenue_growth_band: tuple[float, float]` — (10th pct, 90th pct) empirical range
- `ebit_margin_adj: float`
- `ebit_margin_band: tuple[float, float]`
- `wacc_adj: float`
- `wacc_band: tuple[float, float]`
- `calibration_cohort_size: int` — how many observations backed this calibration
- `calibration_confidence: float` — 0–1 score (1 = well-calibrated, 0 = thin data)

---

## Module 5: Cross-Industry Pattern Intelligence (`cross_industry.py`)

### Purpose
Find structural analogs across sectors and industries to enrich forecasts with patterns from companies that went through a similar maturity/disruption/growth phase, even if they are in a completely different sector.

### Analog Matching Algorithm

For a subject company at data vintage $v$ years post-IPO:

1. **Feature vector** (normalized, z-score):
   ```
   f = [
       revenue_cagr_3y,
       ebit_margin_ttm,
       gross_margin_ttm,
       capex_intensity,   # CapEx / Revenue
       asset_turnover,    # Revenue / Total Assets
       fcf_conversion,    # UFCF / NOPAT
       leverage_ratio,    # Net Debt / EBITDA
       reinvestment_rate, # (CapEx - D&A) / NOPAT
       revenue_growth_volatility,  # std of last 5y YoY growth
       margin_trend,      # slope of EBIT margin last 3y
   ]
   ```

2. **Search the ledger** for all historical companies where this feature vector was similar at a comparable vintage year:
   - Cosine similarity > 0.85 on the 10-dimensional feature vector
   - Vintage year within ±3 years of subject
   - Exclude same-sector companies (to get cross-industry signal only)

3. **Outcome distribution**: for matching analogs, what happened to:
   - Revenue CAGR over next 5 years
   - EBIT margin expansion/compression over 5 years
   - EV/EBITDA multiple re-rating

4. **Analog confidence score**:
   ```
   analog_score = cosine_similarity × (1 / sector_distance) × sqrt(num_analogs)
   ```
   Where `sector_distance` = 1 if same GICS level-2, 2 if same level-1, 3 if different level-1.

5. **Return**: `AnalogSet` containing top 10 analogs with their feature vectors, outcomes, and weighted outcome distribution.

### Cross-Sector Pattern Library

Pre-populate the pattern library with:

| Pattern Name | Trigger Conditions | Historical Archetype Examples |
|---|---|---|
| `PLATFORM_FLYWHEEL` | High revenue growth (>30%), negative FCF, low capex intensity, high gross margin (>60%) | Amazon 1999–2005, Salesforce 2008–2014, Shopify 2015–2019 |
| `COMMODITY_SUPERCYCLE` | Revenue growth tied to spot price, low gross margin (<30%), high capex intensity | BHP 2003–2008, VALE 2009–2011 |
| `MATURE_COMPOUNDER` | Revenue growth 5–12%, stable EBIT margin >20%, FCF yield >4%, consistent buybacks | Colgate 2000–2020, Visa 2012–2022 |
| `DISRUPTED_INCUMBENT` | Declining revenue growth, margin compression, rising capex intensity | Kodak 1990–2010, Nokia 2007–2013 |
| `CYCLICAL_RECOVERY` | Revenue rebound after >20% decline, operating leverage expansion | Ford 2009–2011, United Airlines 2021–2023 |
| `REGULATORY_WINDFALL` | Step-change margin improvement following deregulation | US Telecom 1996–2000, Energy post-2005 |
| `EMERGING_MARKET_PREMIUM` | High revenue growth in new geographies, reinvestment > earnings | Starbucks China 2012–2018 |
| `CAPITAL_LIGHT_TRANSITION` | Declining capex intensity + stable gross margin → FCF inflection | Adobe 2015–2019 (perpetual → SaaS) |

Match subject company to pattern library using feature-vector similarity. If match score > 0.7, incorporate pattern's historical outcome distribution as a Bayesian prior overlay on the calibrated assumptions.

---

## Module 6: Confidence & Uncertainty Quantification (`confidence.py`)

### Purpose
Replace point estimates with calibrated probability distributions. Every output of the model — revenue, EBIT, UFCF, EV, price — must carry a confidence interval that reflects:
- Inherent forecast uncertainty (increases with horizon)
- Data quality (fewer vintage years → wider bands)
- Model calibration quality (from calibrator cohort size)
- Analog signal quality (from cross-industry matching)

### Uncertainty Model

#### Base Uncertainty — Grows with Horizon

For year $t$ in the forecast:

$$\sigma_t^{rev} = \sigma_0^{rev} \times (1 + 0.08)^t$$

where $\sigma_0^{rev}$ is the base revenue uncertainty derived from the cohort's historical error distribution.

Default base standard deviations (if no cohort data):
- Revenue growth: $\sigma_0 = 0.06$ (6%)
- EBIT margin: $\sigma_0 = 0.025$ (250 bps)
- WACC: $\sigma_0 = 0.01$ (100 bps)

#### Vintage-Based Adjustment

The less historical data available, the wider the uncertainty:

```
vintage_multiplier = max(1.0, 2.5 - 0.15 * data_vintage_years)
```
- At 1 year vintage: multiplier = 2.35× (very wide)
- At 5 years vintage: multiplier = 1.75×
- At 10 years vintage: multiplier = 1.0× (base)

#### Calibration Confidence Adjustment

```
calibration_multiplier = 2.0 - calibration_confidence
```
- Perfect calibration (1.0): multiplier = 1.0
- No calibration data (0.0): multiplier = 2.0

#### Final Uncertainty Bands

```python
@dataclass
class ConfidenceInterval:
    p10: float   # 10th percentile
    p25: float   # 25th percentile (bear)
    p50: float   # 50th percentile (base)
    p75: float   # 75th percentile (bull)
    p90: float   # 90th percentile
    confidence_score: float  # 0–1, model's self-assessed reliability
    driving_uncertainty: str  # "macro", "model_bias", "thin_data", "structural_risk"
```

All `ForecastYear` dataclass fields should be extended to carry a `ConfidenceInterval` alongside the point estimate.

#### Model Confidence Score

A single scalar 0–1 representing how much to trust this specific forecast:

```
confidence = w1 × calibration_confidence
           + w2 × (min(data_vintage_years, 15) / 15)
           + w3 × analog_confidence
           + w4 × (1 - structural_break_risk)
           + w5 × (1 - macro_uncertainty)

weights: w1=0.30, w2=0.25, w3=0.20, w4=0.15, w5=0.10
```

---

## Module 7: Online Research Integration (`online_research.py`)

### Purpose
Augment the model with structured, targeted web research to capture developments that historical financial data cannot encode — new technologies, regulatory changes, competitive threats, industry tailwinds.

### Research Query Framework

For a subject company, generate and execute the following structured research queries:

```python
RESEARCH_QUERIES = {
    "sector_technology_trends": [
        f"latest technology disruption in {industry} sector {current_year}",
        f"AI automation impact on {sector} margins {current_year}",
        f"capital expenditure trends {industry} next 5 years",
    ],
    "competitive_landscape": [
        f"{company_name} competitive moat erosion signals {current_year}",
        f"new entrants {industry} market share {current_year}",
        f"{company_name} pricing power research {current_year}",
    ],
    "regulatory_environment": [
        f"{industry} regulatory changes {current_year} impact",
        f"{company_name} regulatory risk {current_year}",
    ],
    "macro_sensitivity": [
        f"{sector} interest rate sensitivity research",
        f"{industry} recession resilience historical analysis",
    ],
    "academic_dcf_advances": [
        f"advanced DCF methodology improvements {current_year} research",
        f"machine learning equity valuation accuracy improvement",
        f"cash flow forecasting techniques {industry} sector",
    ],
}
```

### Research Output Schema

```python
@dataclass
class ResearchInsight:
    query: str
    source_url: str
    source_credibility: float     # 0–1: academic paper=1.0, news article=0.5, blog=0.2
    insight_text: str             # 2–3 sentence summary
    assumption_impacted: str      # Which assumption does this affect?
    direction: str                # "positive", "negative", "neutral"
    magnitude_estimate: float     # Estimated % impact on that assumption
    confidence: float             # 0–1
    valid_until: date             # When should this insight expire?
```

### Integration with Assumptions

After collecting `ResearchInsight` objects, `online_research.py` produces an `ExternalSignalAdjustment`:

```python
def compute_signal_adjustments(insights: list[ResearchInsight]) -> dict[str, float]:
    """
    Returns assumption-keyed adjustment factors. Example:
    {
        "revenue_growth_adj": +0.015,      # +150 bps from positive tech tailwind
        "capex_intensity_adj": +0.02,      # +2pp from announced investment cycle
        "ebit_margin_adj": -0.01,          # -100 bps from new regulatory cost
    }
    """
```

These adjustments are **additive** on top of the calibrator's empirical priors — they represent forward-looking signal that the historical data cannot capture.

---

## Module 8: Learning Adapter (`adapter.py`)

### Purpose
The single integration point that wires all learning signals back into the `auto_valuation/assumptions/engine.py` pipeline. Every call to `build_assumptions()` should optionally pass through the adapter.

### Adapter Pipeline

```python
def adapt_assumptions(
    ticker: str,
    sector: str,
    industry: str,
    data_vintage_years: int,
    market_cap_regime: str,
    macro_regime: str,
    raw_assumptions: AssumptionSet,         # From existing engine
    research_insights: list[ResearchInsight],  # From online_research.py
) -> AdaptedAssumptionSet:

    # Step 1: Calibrate from historical prediction ledger
    calibrated = calibrator.calibrate(raw_assumptions, sector, industry,
                                       data_vintage_years, market_cap_regime, macro_regime)

    # Step 2: Overlay cross-industry analog signal
    analog_set = cross_industry.find_analogs(ticker, feature_vector)
    analog_overlay = cross_industry.compute_overlay(analog_set)
    calibrated = cross_industry.apply_overlay(calibrated, analog_overlay)

    # Step 3: Apply online research signal adjustments
    signal_adj = online_research.compute_signal_adjustments(research_insights)
    calibrated = _apply_signal_adjustments(calibrated, signal_adj)

    # Step 4: Compute confidence intervals
    ci = confidence.compute_intervals(calibrated, data_vintage_years,
                                       calibrated.calibration_confidence,
                                       analog_set.analog_confidence)

    # Step 5: Return adapted set with all metadata
    return AdaptedAssumptionSet(
        **calibrated.__dict__,
        confidence_intervals=ci,
        analog_set=analog_set,
        research_insights=research_insights,
        model_confidence_score=ci.overall_score,
    )
```

---

## Temporal Learning Schedule (IPO to Year 54)

This defines **when** the learning system fires for a given company.

```
IPO Year 0:  Initial forecast (0 historical data → maximum uncertainty bands)
             Source: sector defaults + IPO prospectus comps + analog matching only

Year 1:      Annual post-mortem run. Ledger updated. Calibrator updates priors.
             Prediction for Year 2 now uses Year-1 actuals + calibration.

Year 2:      Annual post-mortem. Calibrator now has 2 data points for this ticker.

Year 3:      Annual post-mortem. Cross-industry analogs re-scored vs. reality.

Year 4:      Annual post-mortem.

Year 5:      QUINQUENNIAL REVIEW: Full 5-year post-mortem runs.
             Attribution engine decomposes cumulative EV error.
             Calibration priors updated with 5 data points.
             Cross-industry pattern library updated.
             Confidence score re-evaluated.
             All future predictions use updated priors.

Years 6–10:  Annual + second quinquennial at Year 10.

...

Year 54:     Final quinquennial at Year 50 + 4 annual post-mortems.
             Model should have calibrated down to ±5-10% uncertainty bands
             for a mature company with 20+ cohort observations.
```

### Confidence Evolution

As years of data accumulate:
- **Year 1–3**: Wide bands (±50-80% on EV), low confidence score (0.2–0.4)
- **Year 4–10**: Moderate bands (±25-40% on EV), confidence 0.4–0.65
- **Year 11–20**: Tighter bands (±15-25% on EV), confidence 0.65–0.80
- **Year 21–54**: Narrow bands (±8-15% on EV), confidence 0.80–0.95

---

## Integration with Existing `auto_valuation/` Engine

### Changes to `auto_valuation/assumptions/engine.py`

Add an optional `learning_mode: bool = False` parameter to `build_assumptions()`. When `True`:

```python
from auto_valuation.learning.adapter import adapt_assumptions

def build_assumptions(ticker, financials, ..., learning_mode=False):
    raw = _build_raw_assumptions(ticker, financials, ...)
    if learning_mode:
        insights = online_research.fetch_insights(ticker, sector, industry)
        return adapt_assumptions(ticker, sector, industry, data_vintage_years,
                                  market_cap_regime, macro_regime, raw, insights)
    return raw
```

### Changes to `auto_valuation/forecast/dcf.py`

When `AdaptedAssumptionSet` is passed (instead of `AssumptionSet`):
- Propagate `ConfidenceInterval` through each `ForecastYear`
- In addition to single-scenario DCF, run a **Monte Carlo pass** (1000 samples) drawing from the confidence interval distributions to produce a full EV distribution:
  - P10, P25, P50 (base), P75, P90 equity value per share
  - Output these as the `monte_carlo_result` field on `DCFResult`

### Changes to `auto_valuation/output/`

#### New sheet: `LearningSheet`
Add a new Excel sheet `Learning & Attribution` with:
- **Prediction accuracy history** — table of past predictions vs. actuals for this ticker (if any)
- **Top 3 error drivers** — bar chart of attribution decomposition
- **Analog companies** — table of top-5 cross-industry analogs with similarity scores and outcome data
- **Confidence dashboard** — bar chart of P10/P25/P50/P75/P90 equity value
- **Model confidence score** — large number cell with RAG (Red/Amber/Green) conditional formatting

#### Extended `ModelSheet`
For each forecast year in the DCF schedule, add P10/P90 rows below the point estimate rows, with grey italic formatting.

---

## Database Schema (`learning/db/`)

### `predictions.db`

```sql
CREATE TABLE prediction_records (
    record_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    industry TEXT,
    run_date DATE NOT NULL,
    forecast_horizon_year INTEGER NOT NULL,
    years_since_ipo INTEGER,
    data_vintage_years INTEGER,
    predicted_revenue_mm REAL,
    predicted_ebit_margin REAL,
    predicted_ufcf_mm REAL,
    predicted_ev_mm REAL,
    predicted_price_per_share REAL,
    scenario TEXT DEFAULT 'base',
    near_term_revenue_growth REAL,
    target_ebit_margin REAL,
    wacc REAL,
    terminal_growth REAL,
    beta REAL,
    erp REAL,
    rf_rate REAL,
    market_cycle_phase TEXT,
    macro_backdrop_json TEXT,
    actual_price_at_prediction REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE postmortem_records (
    postmortem_id TEXT PRIMARY KEY,
    record_id TEXT REFERENCES prediction_records(record_id),
    postmortem_date DATE NOT NULL,
    actual_revenue_mm REAL,
    actual_ebit_margin REAL,
    actual_ufcf_mm REAL,
    actual_ev_mm REAL,
    actual_price_at_horizon REAL,
    revenue_error_pct REAL,
    margin_error_bps REAL,
    ev_error_pct REAL,
    primary_miss_driver TEXT,
    error_attribution_json TEXT,     -- JSON array of (ErrorDriver, pct) pairs
    structural_break_detected BOOLEAN,
    model_bias_signal TEXT,
    postmortem_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE calibration_priors (
    prior_id TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    industry TEXT,
    maturity_bucket TEXT NOT NULL,    -- "1-3", "4-10", "11-20", "21+"
    cap_regime TEXT NOT NULL,
    macro_regime TEXT NOT NULL,
    assumption_name TEXT NOT NULL,    -- e.g. "revenue_growth"
    correction_mean REAL NOT NULL,
    correction_std REAL NOT NULL,
    cohort_size INTEGER NOT NULL,
    last_updated DATE NOT NULL,
    UNIQUE(sector, industry, maturity_bucket, cap_regime, macro_regime, assumption_name)
);

CREATE TABLE analog_patterns (
    analog_id TEXT PRIMARY KEY,
    subject_ticker TEXT,
    analog_ticker TEXT,
    subject_vintage_year INTEGER,
    similarity_score REAL,
    feature_vector_json TEXT,
    outcome_revenue_cagr_5y REAL,
    outcome_margin_change_bps REAL,
    outcome_ev_multiple_change REAL,
    pattern_label TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Configuration (`auto_valuation/config.py` additions)

Add the following to `config.py`:

```python
LEARNING_CONFIG = {
    # Toggle learning system on/off globally
    "learning_enabled": True,

    # Minimum cohort size before applying calibration priors
    "min_calibration_observations": 5,

    # Online research
    "online_research_enabled": True,
    "research_cache_ttl_days": 7,
    "max_research_queries_per_run": 12,
    "min_source_credibility": 0.3,

    # Cross-industry analog matching
    "min_analog_similarity": 0.75,
    "max_analogs_returned": 10,
    "cross_sector_only": True,  # Exclude same-sector analogs

    # Monte Carlo
    "monte_carlo_enabled": True,
    "monte_carlo_samples": 1000,
    "monte_carlo_seed": 42,

    # Post-mortem schedule
    "annual_postmortem_enabled": True,
    "quinquennial_postmortem_enabled": True,
    "postmortem_min_data_quality_score": 0.6,

    # Confidence intervals
    "base_revenue_uncertainty": 0.06,
    "base_margin_uncertainty": 0.025,
    "base_wacc_uncertainty": 0.01,
    "uncertainty_growth_per_year": 0.08,
}
```

---

## Testing Requirements

Create `tests/test_learning_system.py` with at minimum:

1. **Ledger tests**: write a `PredictionRecord`, read it back, verify immutability
2. **Annual post-mortem test**: mock actual financials, verify error metrics computed correctly
3. **Attribution test**: inject known error pattern, verify `ErrorDriver` attributed correctly
4. **Calibrator test**: inject 10 historical records, verify correction factors computed, verify cohort-size gating at <5 observations
5. **Cross-industry analog test**: inject 3 feature vectors, verify cosine similarity computed, verify same-sector exclusion
6. **Confidence interval test**: verify band width grows with forecast horizon, verify vintage multiplier applied
7. **Adapter integration test**: run full adapter pipeline with mocked sub-components, verify output is `AdaptedAssumptionSet`
8. **Monte Carlo test**: run 100-sample MC pass, verify P10 < P50 < P90
9. **Temporal schedule test**: verify quinquennial review fires at years 5, 10, 15, 20 and not at years 2, 3, 4

All tests must pass with the existing `run_tests` task: `pytest tests/ -q --tb=short`.

---

## Implementation Priority Order

Implement in this order to maximize incremental value:

1. `ledger.py` — database + JSONL store, `PredictionRecord` schema, writer/reader
2. `postmortem.py` — annual post-mortem (quinquennial builds on this)
3. `attribution.py` — error decomposition
4. `calibrator.py` — empirical prior updating (this is the core learning signal)
5. `confidence.py` — uncertainty quantification
6. `cross_industry.py` — analog matching
7. `online_research.py` — web research integration
8. `adapter.py` — wires everything together
9. Integration into existing `engine.py`, `dcf.py`, `excel_builder.py`
10. New `LearningSheet` Excel output
11. Tests

---

## Key Design Principles

- **Non-destructive**: The learning system is entirely additive. Existing `run_valuation()` continues to work unchanged when `learning_mode=False`.
- **Calibration before confidence**: Never report a confidence score without backing data. If cohort size < 5, confidence score is capped at 0.35.
- **Decomposition discipline**: Every error must be attributed. Attribution percentages must sum to 100%.
- **Temporal integrity**: Never allow future data to leak into past predictions. Post-mortem records are timestamped and chronological ordering is enforced.
- **Graceful degradation**: If ledger is empty, calibrator returns raw assumptions unchanged. If analog search returns 0 results, adapter skips that step. If online research fails, adapter proceeds with historical signals only.
- **Auditable**: Every assumption adjustment must carry a `source` field tracing it back to either a `PostmortemRecord`, an `AnalogPattern`, or a `ResearchInsight`.
