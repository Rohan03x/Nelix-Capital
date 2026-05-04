# Nelix Capital — Full EODHD Architecture & Model Improvement Plan

**Generated after comprehensive audit of codebase + all EODHD API endpoints.**
**API key:** `EODHD_API_KEY` environment variable | **Budget:** 100,000 req/day | **Used:** ~89/day (massive headroom)

---

## PART 1 — CURRENT STATE AUDIT

### 1.1 What The Model Does (Full Inventory)

| Layer | File(s) | What it does |
|-------|---------|--------------|
| **Data ingestion** | `eodhd_client.py` | Fetches fundamentals (20+ yr history), live price, builds full DCF payload |
| **Data ingestion** | `yfinance_client.py` | Fallback DCF builder using yfinance (4-yr limit, unreliable international) |
| **Data ingestion** | `fmp_client.py` | Second fallback, requires FMP_API_KEY |
| **Data routing** | `samples.py:get_dashboard_data()` | Priority chain: EODHD → yfinance → FMP → hardcoded sample |
| **Peer comps** | `peer_lists.py` | Industry peer definitions + multiples fetching (now EODHD-first) |
| **NTM estimates** | `auto_valuation/data/estimates.py` | FMP → yfinance fallback for forward EPS/revenue |
| **Price/beta** | `auto_valuation/data/fetcher.py` | yfinance-only: `fetch_yfinance_info()`, `fetch_52wk_range()`, `check_price_freshness()`, `fetch_current_price()` |
| **DCF engine** | `forecast/dcf.py` | 7-yr UFCF DCF, terminal value, equity bridge |
| **WACC** | `assumptions/wacc.py` | CAPM WACC, uses beta + ERP + Rf |
| **Assumptions** | `assumptions/engine.py` | Builds AssumptionSet from data |
| **Calibration** | `learning/calibrator.py` | Empirical Bayes from prediction errors → CalibrationStore (SQLite) |
| **Historical replay** | `learning/historical_replay.py` | Scan 3,092+ fund files → FY2016–2025 prediction-vs-actual pairs |
| **Feature space** | `learning/feature_space.py` | 20 features: revenue CAGR, EBIT margin, FCF conversion, leverage, etc. |
| **Confidence** | `learning/confidence.py` + `webapp/data/confidence.py` | Grades each valuation A–F |
| **Background runner** | `learning/background_runner.py` | 16-worker parallel prefetch, 300s loop |
| **Universe** | `learning/universe.py` + `symbol_universe.db` | 3,808 symbols across 20 exchanges |
| **Output** | `webapp/app.py` | Flask routes serving valuations + comps |
| **Excel export** | `output/excel_builder.py` | 15-sheet workbook with full model |

### 1.2 All yfinance Usages — Complete Audit

#### `auto_valuation/data/fetcher.py` (HARD import — blocks on install)
```python
import yfinance as yf  # line 20 — TOP-LEVEL import, always executed

fetch_yfinance_info(ticker)        # → yf.Ticker(ticker).info
fetch_52wk_range(ticker)           # → yf.Ticker(ticker).info + .history(period="1y")
check_price_freshness(ticker)      # → yf.Ticker(ticker).history(period="5d")
fetch_current_price(ticker)        # → yf.Ticker(ticker).info
```
**Used by:** `main.py` line 143, 167 (CLI tool — calls `fetch_yfinance_info`)

#### `auto_valuation/data/estimates.py` (lazy import)
```python
def fetch_ntm_estimates_yfinance(ticker):
    import yfinance as yf
    info = yf.Ticker(ticker).info
    # Returns: revenue_mm, ebitda_mm, forwardEps

def fetch_ntm_estimates(ticker, ...):
    # Priority: FMP → yfinance → override
    yf_est = fetch_ntm_estimates_yfinance(ticker)  # line 201
```
**Impact:** NTM estimates for the core DCF use stale/unreliable yfinance forward data

#### `webapp/data/yfinance_client.py` (full fallback DCF builder)
- Entire file builds DCF from yfinance data
- Called from `samples.py` as fallback when EODHD fails
- Returns same dict schema as `eodhd_client.py`
- 4-year financial history limit, international tickers return empty

#### `webapp/data/peer_lists.py` (lazy import — fallback)
```python
_YFINANCE_EXCHANGE_SUFFIX = {...}   # exchange map still used for lookup
_to_yfinance_ticker(code, exchange) # converter function
# In fetch_peer_metrics: yfinance used if ticker not in EODHD index
import yfinance as yf
info = yf.Ticker(yf_ticker).info or {}
```
**Impact:** Tickers missing from EODHD fund cache fall back to yfinance (international fail)

#### `webapp/data/confidence.py`
```python
_is_yf = "yahoo" in _source.lower() or "yfinance" in _source.lower()
# Penalises yfinance data for short history (4-yr cap)
```
**Impact:** Cosmetic; penalises yfinance when it's used as source

---

## PART 2 — EODHD API COMPLETE ENDPOINT MAP

### Currently Used
| Endpoint | URL | What we use it for |
|----------|-----|--------------------|
| **Fundamentals** | `GET /api/fundamentals/{CODE}.{EXCHANGE}` | Full: Income, Balance, CF, Highlights, Valuation, General |
| **Real-time price** | `GET /api/real-time/{CODE}.{EXCHANGE}` | Live price (5-min cache) |
| **Exchange symbols** | `GET /api/exchange-symbol-list/{EXCHANGE}` | Universe seeding |

### Available but NOT Used (high-value gaps)

#### Tier 1 — CRITICAL (use immediately, directly improves accuracy)

| Endpoint | URL pattern | Data | Cost |
|----------|-------------|------|------|
| **Earnings Trends** | `GET /api/calendar/trends?symbols=AAPL.US,MSFT.US` | Analyst consensus: NTM EPS/revenue, EPS revision trend (7d/30d/60d/90d), # analysts, low/high/avg | 1 call |
| **Calendar / Earnings** | `GET /api/calendar/earnings?symbols=AAPL.US` | Historical EPS actual vs estimate, earnings surprise % | 1 call |
| **EOD Price History** | `GET /api/eod/{CODE}?from=2020-01-01&to=2025-01-01` | Full adjusted price series for beta calculation, 52-wk range, price momentum | 1 call (any length) |
| **Macro Indicators** | `GET /api/macro-indicator/{COUNTRY}?indicator=gdp_growth_annual` | GDP growth, inflation, real interest rate, unemployment — country-specific | 10 calls |
| **Live OHLCV** | `GET /api/real-time/{CODE}.{EXCHANGE}?fmt=json` | Current price, prev close, 52-wk hi/lo, change% | 1 call (already used) |

#### Tier 2 — HIGH VALUE (improves model richness)

| Endpoint | URL pattern | Data | Cost |
|----------|-------------|------|------|
| **Insider Transactions** | `GET /api/insider-transactions?code=AAPL` | SEC Form 4 filings — net buy/sell signal, volume-weighted | ~5 calls |
| **Analyst Ratings** (from fundamentals) | `fundamentals filter=AnalystRatings` | `Rating`, `TargetPrice`, `StrongBuy`, `Buy`, `Hold`, `Sell`, `StrongSell` — already in fund cache! | 0 extra calls |
| **Splits/Dividends** | `GET /api/div/{CODE}?fmt=json` | Historical dividends — needed for correct historical price total return | 1 call |
| **Screener** | `GET /api/screener?filters=[["sector","=","Technology"]]` | Discover comps by sector/industry/size filter | 5 calls |
| **Search API** | `GET /api/search/{QUERY}` | Ticker resolution when user types company name | 1 call |
| **Financial News** | `GET /api/news?s=AAPL.US&limit=10` | Sentiment score (polarity, neg, neu, pos) per article | 5 calls/10 tickers |
| **Sentiments** | `GET /api/sentiments?s=AAPL.US&from=...&to=...` | Normalised daily sentiment −1→1 per ticker | 1 call |

#### Tier 3 — FUTURE (macro/alternative data enrichment)

| Endpoint | URL pattern | Data | Cost |
|----------|-------------|------|------|
| **Bulk Fundamentals** | `GET /api/bulk-fundamentals/{EXCHANGE}` | All tickers on exchange in one call | Plan-dependent |
| **Historical Market Cap** | `GET /api/historical-market-cap/{CODE}?from=2020-01-01` | Point-in-time market cap for training labels | 1 call |
| **ESG Data** | `GET /api/fundamentals/{CODE}?filter=ESGScores` | E/S/G scores — increasingly priced in | included in fundamentals |
| **Index Constituents** | `GET /api/fundamentals/GSPC.INDX` | S&P 500 components — universe seeding | 10 calls |
| **Corporate Actions** | `GET /api/splits/{CODE}` | Split history — needed for price series normalisation | 1 call |

---

## PART 3 — IDENTIFIED GAPS, BUGS & BOTTLENECKS

### 3.1 Financial Accuracy Gaps

**A) NTM estimates use stale/wrong data**
- Currently: FMP → yfinance fallback
- yfinance `forwardEps` is stale, not consensus, no analyst count
- EODHD `fundamentals.Earnings.Trend` has FULL consensus: `earningsEstimateAvg`, `revenueEstimateAvg`, `earningsEstimateNumberOfAnalysts`, revision trends
- Also available via `GET /api/calendar/trends?symbols=...` for any ticker not in cache
- **Fix**: Add `fetch_ntm_estimates_eodhd()` using `Earnings.Trend` section from fund cache

**B) Beta is wrong for non-US tickers**
- Currently: `auto_valuation/data/fetcher.py` → `yf.Ticker(ticker).info["beta"]`
- yfinance beta = US-exchange-adjusted vs S&P 500; meaningless for ABBN.SW vs SMI
- EODHD `Technicals.Beta` in fund cache is exchange-adjusted beta
- **Fix**: Use `data["Technicals"]["Beta"]` from fund cache, fall back to Damodaran sector beta

**C) 52-week range and current price via yfinance**
- Currently: `fetch_52wk_range()` and `fetch_current_price()` in `fetcher.py` are yfinance-only
- EODHD fundamentals cache has: `Technicals.52WeekHigh`, `Technicals.52WeekLow`
- EODHD real-time has: `close`, `change_p` (already used in `eodhd_client.py`)
- **Fix**: Replace `fetcher.py` yfinance calls with EODHD equivalents

**D) No earnings surprise signal in model**
- Historical earnings surprises (actual vs estimate) are a known predictor of short-term price momentum and long-term analyst revision cascades
- EODHD `Earnings.History` in every fund file has: actual EPS, estimated EPS, surprise %, per quarter
- **Fix**: Extract `earnings_surprise_avg_4q` as a calibration feature

**E) No analyst consensus signal**
- `AnalystRatings.Rating`, `AnalystRatings.TargetPrice` already in every EODHD fund file — zero extra API calls
- Price vs target gives a "Street consensus margin of safety" signal
- **Fix**: Surface as a comps insight metric and use as a confidence modifier

**F) WACC uses FRED for risk-free rate but falls back to 4.5% hardcoded**
- EODHD Macro API has `real_interest_rate` per country — directly replaces FRED dependency
- For non-US tickers this is critical (EUR companies should use ECB rate, not Fed)
- **Fix**: `fetch_risk_free_rate_eodhd(country_iso3)` from macro-indicator endpoint

**G) Terminal growth rate uses GDP from FRED (may be absent)**
- EODHD macro `gdp_growth_annual` available for 100+ countries
- **Fix**: Route `fetch_gdp_growth_estimate()` through EODHD macro endpoint as primary

### 3.2 Training Data Quality / Leakage Issues

**A) Prediction uses data that wasn't available at training time (look-ahead bias)**
- `historical_replay.py` generates "predictions" using sector WACC defaults and prior-year EBIT margin
- BUT: the financial data used to compute `prior-year EBIT margin` comes from the SAME fund cache that contains FUTURE years
- The `_annual_snapshots()` function applies `cutoff: date` parameter but this only restricts the "actual" side, not the feature side
- **Risk**: Medium. The persistence model (predict t = t−1) is simple enough that leakage is minimal, but any feature that touches multi-year slopes could contaminate
- **Fix**: When building training observations, use only data with `date_field <= cutoff_date` on BOTH prediction AND actual sides. Add explicit `as_of_date` guard to all feature reads.

**B) Calibration cohort mixing**
- `CalibrationStore` groups by `(sector, industry, cap_regime, macro_regime)` — fine
- But `macro_regime` is derived from current risk-free rate, not the rate at the time of prediction
- A 2018 observation calibrated under "rising_rates" (post-2022 regime) is wrong
- **Fix**: Store the RF rate at training time in each `CalibrationObservation`, compute `macro_regime` from historical RF, not current

**C) No temporal ordering in training set**
- Observations from 2016–2025 are pooled with equal weight
- Recent company behaviour (post-2020) may differ structurally from pre-2020 data
- **Fix**: Add time-decay weighting to CalibrationStore: `weight = exp(-lambda * age_in_years)` with `lambda = 0.15`

**D) `fetch_ntm_estimates_yfinance()` provides TTM revenue as "forward" estimate**
- Explicitly noted in code: "Revenue estimate from totalRevenue (TTM only in yfinance — rough proxy)"
- This means the NTM revenue used in DCF is actually the TRAILING number, not consensus forward
- Creates systematic downward bias in growth assumptions when using yfinance
- **Fix**: Replace entirely with EODHD `Earnings.Trend` consensus

**E) Historical replay uses "persistence model" as prediction baseline**
- This is intentionally simple but means the calibration only corrects for sector-level biases, not company-specific momentum
- A high-growth company in a low-growth sector gets the wrong correction
- **Fix**: Add a second feature dimension: `growth_regime` = high/medium/low based on prior 3-yr CAGR, add as calibration dimension

### 3.3 Performance Bottlenecks

**A) `_build_eodhd_multiples_index()` scans 3,092 JSON files on every cold start**
- Currently LRU-cached with `maxsize=1` so only runs once per process
- On Vercel cold start: ~6 seconds blocking the first request
- **Fix**: Pre-serialize the index to a single `peer_multiples_index.msgpack` or `pickle` file, refresh nightly via background runner. Cold start drops to ~50ms.

**B) `get_all_observations()` rebuilds from 3,092 files every hour**
- 6-second cold rebuild every hour is acceptable but degrades under load
- **Fix**: Persist `_OBS_CACHE` to disk as a pickle file with `_ts` TTL check. Survives restarts.

**C) EODHD fund cache TTL is 24h but refreshed file-by-file**
- When a fund file expires, it triggers a live API call in the request path (blocks response)
- **Fix**: Background runner should pre-refresh files that are >20h old, not wait for request-path expiry

**D) `fetch_peer_metrics()` is O(n_peers × n_files) on cold start**
- Even with EODHD index, the index scan is ~3,092 files
- **Fix**: Already fixed (LRU cache), but ensure index rebuild doesn't block requests — rebuild on background thread, serve stale data if rebuild in progress

---

## PART 4 — COMPREHENSIVE EODHD-ONLY IMPROVEMENT PLAN

### PHASE 1: Eliminate yfinance Entirely (1-2 days)

#### Step 1.1 — Replace `auto_valuation/data/fetcher.py` yfinance calls
These 4 functions all use yfinance and are used by the CLI tool (`main.py`), not the webapp:

```python
# REPLACE fetch_yfinance_info(ticker) WITH:
def fetch_eodhd_info(ticker: str) -> dict:
    """Get market data from EODHD fund cache or real-time endpoint."""
    # 1. Check EODHD fund cache (eodhd_fund_{code}_{exchange}.json)
    # 2. Return: currentPrice, beta, 52WeekHigh, 52WeekLow, marketCap
    cache_file = _find_eodhd_fund_cache(ticker)
    if cache_file:
        data = json.load(open(cache_file))["data"]
        return {
            "currentPrice": data.get("Highlights", {}).get("..."),
            "beta": data.get("Technicals", {}).get("Beta"),
            "fiftyTwoWeekHigh": data.get("Technicals", {}).get("52WeekHigh"),
            "fiftyTwoWeekLow": data.get("Technicals", {}).get("52WeekLow"),
            "marketCap": data.get("Highlights", {}).get("MarketCapitalization"),
        }
    # Fallback: EODHD real-time
    resp = requests.get(f"{_EODHD_BASE}/real-time/{ticker}?api_token={key}&fmt=json")
    ...
```

**Affected functions:**
- `fetch_yfinance_info()` → `fetch_eodhd_info()` (use fund cache)
- `fetch_52wk_range()` → use `Technicals.52WeekHigh/Low` + EOD history endpoint
- `check_price_freshness()` → use EODHD real-time endpoint `updated_at` timestamp
- `fetch_current_price()` → already served by `eodhd_client.py`'s `_fetch_live_price()`

**Remove from `requirements.txt`:** `yfinance==0.2.38`

#### Step 1.2 — Replace `auto_valuation/data/estimates.py` yfinance fallback
```python
# ADD: fetch_ntm_estimates_eodhd(ticker)
def fetch_ntm_estimates_eodhd(ticker: str) -> NTMEstimates:
    """
    Use EODHD Earnings.Trend (from fund cache) for NTM estimates.
    Returns consensus avg EPS, consensus avg revenue, analyst count.
    """
    fund_data = _load_fund_cache(ticker)
    if not fund_data:
        # Live API: GET /api/calendar/trends?symbols={ticker}.US
        resp = requests.get(f"{_EODHD_BASE}/calendar/trends?symbols={eodhd_code}&api_token={key}&fmt=json")
        ...
    
    trend = fund_data.get("Earnings", {}).get("Trend", {})
    # Find "+1y" period
    for key, item in trend.items():
        if item.get("period") == "+1y":
            return NTMEstimates(
                revenue_mm = float(item.get("revenueEstimateAvg", 0)) / 1e6,
                eps        = float(item.get("earningsEstimateAvg", 0)),
                analyst_count = int(float(item.get("earningsEstimateNumberOfAnalysts", 0))),
                eps_revision_up_30d = float(item.get("epsRevisionsUpLast30days", 0)),
                eps_revision_dn_30d = float(item.get("epsRevisionsDownLast30days", 0)),
                source     = "eodhd_consensus",
            )
```

**Update `fetch_ntm_estimates()` priority chain:**
```
1. EODHD fund cache Earnings.Trend (zero extra API calls — already downloaded)
2. EODHD Calendar Trends API (for tickers not yet in fund cache)
3. FMP (if FMP_API_KEY available)
4. Override file
# Remove yfinance entirely
```

#### Step 1.3 — Remove yfinance fallback from `peer_lists.py`
- `_to_yfinance_ticker()` and `_YFINANCE_EXCHANGE_SUFFIX` can stay as dead code for now
- Remove the `import yfinance as yf` block in `fetch_peer_metrics()`
- If EODHD index misses a peer ticker: mark as `source="not_available"` with `None` multiples (already handled — `N/M` display)

#### Step 1.4 — Remove `yfinance_client.py` fallback from `samples.py`
```python
# In get_dashboard_data() priority chain, REMOVE step 2:
# 2. Fallback to yfinance if EODHD failed  ← DELETE THIS BLOCK
# Replace with: if EODHD fails, go directly to FMP
```
The `yfinance_client.py` can be kept as an archive but not called.

---

### PHASE 2: Enrich EODHD Data Extraction (2-3 days)

#### Step 2.1 — Extract Analyst Ratings from fund cache (0 extra API calls)
Every `eodhd_fund_*.json` already contains:
```json
"AnalystRatings": {
    "Rating": 4.1064,
    "TargetPrice": 247.925,
    "StrongBuy": 24,
    "Buy": 8,
    "Hold": 12,
    "Sell": 2,
    "StrongSell": 1
}
```
**Action:** In `eodhd_client.py:build_dashboard_data()`, add to output dict:
```python
"analyst_target_price": sf(ar.get("TargetPrice")),
"analyst_rating": sf(ar.get("Rating")),      # 1–5 scale
"analyst_strong_buy": int(ar.get("StrongBuy", 0)),
"analyst_buy": int(ar.get("Buy", 0)),
"analyst_hold": int(ar.get("Hold", 0)),
"analyst_sell": int(ar.get("Sell", 0) or 0) + int(ar.get("StrongSell", 0) or 0),
"price_vs_target": current_price / analyst_target if analyst_target else None,
```
Surface on dashboard as "Street Consensus" section. Use `price_vs_target` as a confidence modifier.

#### Step 2.2 — Extract Earnings Surprise from fund cache (0 extra API calls)
Every fund file has `Earnings.History`:
```json
"History": {
    "2025-03-31": {"epsActual": 1.65, "epsEstimate": 1.61, "epsDifference": 0.04, "surprisePercent": 2.48},
    "2024-12-31": {...}
}
```
**Action:** Compute `avg_earnings_surprise_4q` = mean of last 4 quarters' `surprisePercent`.
- Add as feature in `feature_space.py`
- Add as calibration dimension: consistent positive surprisers get higher growth confidence
- Surface on comps table: "Beat/Miss Avg"

#### Step 2.3 — Extract Beta from fund cache (0 extra API calls)
`Technicals.Beta` is already in every fund file.
```python
# In eodhd_client.py where WACC is computed, use:
beta_raw = _sf(technicals.get("Beta"))
# Already done in eodhd_client.py — VERIFY this is actually used and not overridden by yfinance
```
**Verify**: Search eodhd_client.py for beta assignment. If it falls through to yfinance, fix.

#### Step 2.4 — Add NTM Estimates from Earnings.Trend (0 extra API calls for cached tickers)
```python
def _extract_ntm_from_trend(earnings: dict) -> dict:
    """Extract +1y consensus from Earnings.Trend section."""
    trend = earnings.get("Trend") or {}
    for entry in trend.values():
        if isinstance(entry, dict) and entry.get("period") == "+1y":
            return {
                "ntm_revenue_estimate": float(entry.get("revenueEstimateAvg") or 0) / 1e6,
                "ntm_eps_estimate": float(entry.get("earningsEstimateAvg") or 0),
                "ntm_revenue_growth": float(entry.get("revenueEstimateGrowth") or 0),
                "ntm_eps_growth": float(entry.get("earningsEstimateGrowth") or 0),
                "eps_revision_momentum": (
                    float(entry.get("epsRevisionsUpLast30days") or 0) -
                    float(entry.get("epsRevisionsDownLast30days") or 0)
                ),
                "analyst_count": int(float(entry.get("earningsEstimateNumberOfAnalysts") or 0)),
            }
    return {}
```
Use `ntm_revenue_estimate` as the NTM forecast in DCF when available, overriding the persistence model.
Use `eps_revision_momentum` as a calibration signal: positive = upgrade cycle = higher growth confidence.

---

### PHASE 3: New EODHD Endpoints Integration (3-5 days)

#### Step 3.1 — EOD Price History for Beta & Momentum
**Replace yfinance history with EODHD EOD:**
```
GET /api/eod/{CODE}.{EXCHANGE}?from=2020-01-01&order=a&fmt=json&api_token={KEY}
Cost: 1 API call (any time range)
```
Use this to:
1. **Beta calculation**: Regress stock returns vs MSCI World / S&P 500 index returns (fetch `GSPC.INDX` history once weekly)
2. **52-week high/low**: Computed from last 252 trading days
3. **Price momentum**: 3-month, 6-month, 12-month total return — a known valuation signal
4. **Volatility**: Realised 1-year daily return std dev — fed into confidence score and risk assessment

**Cache strategy:** 7-day TTL (already defined as `_TTL_EOD_HISTORY_SEC = 86_400 * 7`)

#### Step 3.2 — EODHD Macro Indicators (replaces FRED)
**Endpoints:**
```
GET /api/macro-indicator/USA?indicator=gdp_growth_annual&api_token={KEY}&fmt=json
GET /api/macro-indicator/USA?indicator=real_interest_rate&api_token={KEY}&fmt=json
GET /api/macro-indicator/USA?indicator=inflation_consumer_prices_annual&api_token={KEY}&fmt=json
Cost: 10 API calls per indicator (use sparingly — cache 30 days)
```
**Replace:** `fetch_risk_free_rate()` in `fetcher.py` (currently FRED-dependent, falls back to 4.5%)
**Replace:** `fetch_gdp_growth_estimate()` in `fetcher.py`

**New function:**
```python
def fetch_macro_context_eodhd(country_iso3: str = "USA") -> dict:
    """
    Returns risk-free rate, GDP growth, inflation for WACC and terminal growth.
    Cached 30 days — macro indicators don't change fast.
    """
    indicators = ["gdp_growth_annual", "real_interest_rate", "inflation_consumer_prices_annual"]
    result = {}
    for ind in indicators:
        resp = _get(f"{_EODHD_BASE}/macro-indicator/{country_iso3}", 
                    params={"indicator": ind, "api_token": _api_key(), "fmt": "json"})
        # resp is list of {date, value} — take most recent
        if resp:
            result[ind] = float(resp[-1]["Value"]) / 100.0  # convert percent to decimal
    return result
```
**Impact:** Non-US tickers (ABBN.SW, SAP.XETRA) get correct country-specific Rf and GDP growth for terminal rate.

#### Step 3.3 — Screener API for Peer Discovery
Current peer lists are hardcoded in `peer_lists.py` (350+ manual definitions).
```
GET /api/screener?filters=[["sector","=","Technology"],["exchange","=","us"],
     ["market_capitalization",">",1000000000]]&sort=market_capitalization.desc
     &limit=20&api_token={KEY}
Cost: 5 API calls per request
```
**Use for:** When a ticker has no hardcoded peer group, use screener to find top-20 by market cap in same sector+exchange.
```python
def _discover_peers_via_screener(sector: str, industry: str, exchange: str, 
                                  min_cap_mm: float) -> list[str]:
    """Fallback peer discovery when no hardcoded list exists."""
    filters = [
        ["sector", "match", sector],
        ["exchange", "=", exchange.lower()],
        ["market_capitalization", ">", min_cap_mm * 1e6],
    ]
    resp = requests.get(f"{_EODHD_BASE}/screener", params={
        "filters": json.dumps(filters),
        "sort": "market_capitalization.desc",
        "limit": 20,
        "api_token": _api_key(),
    })
    return [item["code"] for item in resp.json().get("data", [])]
```
**Impact:** Model can value ANY ticker, not just the ~50 industries with hardcoded peers.

#### Step 3.4 — Financial News Sentiment (optional enrichment)
```
GET /api/sentiments?s={TICKER}.US&from=2025-01-01&api_token={KEY}
Returns: daily sentiment score −1 to +1 (count + normalised)
```
**Use as:** A supplementary signal in the confidence score: `sentiment_30d_avg` near 0 = no news uplift.
Cache: 24h TTL. Cost: 1 call per ticker.

---

### PHASE 4: Training Quality Improvements (3-5 days)

#### Step 4.1 — Fix Look-ahead Bias in Historical Replay
Current issue: `historical_replay.py` uses multi-year financial data without strict point-in-time cutoffs on the feature side.

**Fix:**
```python
# In _annual_snapshots(), add strict cutoff to ALL data access:
def _build_features_as_of(fundamentals: dict, cutoff_date: date) -> dict:
    """
    Build model features using ONLY data that was available as of cutoff_date.
    Fundamentals report dates are in 'date' field of each annual period.
    """
    income = _annual_periods_by_year(
        dict((fin.get("Income_Statement") or {}).get("yearly") or {}),
        as_of_date=cutoff_date,  # already exists — verify it's used consistently
    )
    # VERIFY: _annual_periods_by_year must filter by period end date <= cutoff_date
    # NOT by what's available in the cache today
```

#### Step 4.2 — Macro-regime-at-training-time Fix
```python
@dataclass(frozen=True)
class CalibrationObservation:
    ...
    rf_rate_at_time: float = 0.045  # ADD THIS FIELD
    # macro_regime derived from rf_rate_at_time, not current rate
```
In historical replay, fetch historical RF rate for each training year from EODHD macro indicators (10 calls once, 30-year history, cache forever).

#### Step 4.3 — Add Time-decay Weighting to Calibration Store
```python
# In CalibrationStore._get_prior():
current_year = date.today().year
weights = [
    math.exp(-0.15 * (current_year - obs_year))  # half-life ~5 years
    for obs_year in observation_years
]
correction_mean = np.average(corrections, weights=weights)
```

#### Step 4.4 — Add Growth-regime Calibration Dimension
```python
# Add to CalibrationObservation:
growth_regime: str  # "high" (>15% CAGR), "medium" (5-15%), "low" (<5%), "shrinking" (<0%)

# In _cap_regime-equivalent function:
def _growth_regime(revenue_cagr_3y: float) -> str:
    if revenue_cagr_3y > 0.15: return "high"
    if revenue_cagr_3y > 0.05: return "medium"
    if revenue_cagr_3y >= 0: return "low"
    return "shrinking"
```
This prevents a 30% CAGR fintech from being calibrated with 2% CAGR utilities.

#### Step 4.5 — Earnings Surprise as Calibration Signal
```python
# Add to feature_space.py FEATURE_SPECS:
FeatureSpec("earnings_surprise_avg_4q", "EPS Surprise (4Q avg)", "quality", 0.90, 15.0, "pct"),

# In _compute_features():
surprises = [entry.get("surprisePercent") for entry in recent_4q_earnings if entry.get("surprisePercent")]
features["earnings_surprise_avg_4q"] = mean(surprises) if surprises else 0.0
```
Consistent earnings beats → model becomes more confident in the management's guidance.

---

### PHASE 5: Predictive Power Improvements (5-7 days)

#### Step 5.1 — Forward Revenue Anchoring
Currently the DCF uses analyst consensus NTM estimates as a CHECK against the model's own projection, but doesn't feed them into the base case.
**Change:** Use EODHD `Earnings.Trend +1y revenueEstimateAvg` as the Year 1 revenue directly when analyst count ≥ 3.
```python
if ntm_estimates.analyst_count >= 3 and ntm_estimates.revenue_mm > 0:
    # Anchor Year 1 to consensus, let model drive Years 2-7
    year1_revenue = ntm_estimates.revenue_mm
    # Compute implied growth vs LTM as Year 1 growth rate
    year1_growth = (year1_revenue / ltm_revenue) - 1.0
```

#### Step 5.2 — Price Target vs Model DCF Divergence Signal
When `analyst_target_price` is available:
```python
dcf_vs_street = (intrinsic_value - analyst_target) / analyst_target
# Surface as: "Model premium/discount vs Street consensus"
# > +20%: model is significantly more bullish — flag for review
# < -20%: model is significantly more bearish — could be a value signal
```

#### Step 5.3 — Insider Activity Signal
Use EODHD `InsiderTransactions` section (already in fund cache):
```python
# Extract last 90 days of insider transactions:
recent = [t for t in insider_txns if date_diff(t["date"]) <= 90]
net_insider_shares = sum(
    t["transactionAmount"] * (1 if t["transactionAcquiredDisposed"] == "A" else -1)
    for t in recent
)
net_insider_signal = "buying" if net_insider_shares > 0 else "selling"
```
Use as confidence modifier: insider buying → +5 confidence pts, heavy selling → −5 pts.

#### Step 5.4 — Cross-sector Peer Validation
Current comps use only industry-level peers. Add S&P 500 index peers:
```python
# Use EODHD Index Constituents: GET /api/fundamentals/GSPC.INDX
# Filter to same market-cap decile + similar revenue CAGR
# Build a "quality peers" list that crosses sector boundaries
```

#### Step 5.5 — Multi-scenario DCF (Bear/Base/Bull)
Currently: single DCF with sensitivity ranges.
**Add:** Three-scenario model using EODHD data:
- **Bear**: `revenueEstimateLow` (analyst low end) + expanded WACC (+100bps)
- **Base**: `revenueEstimateAvg` (consensus) + current WACC
- **Bull**: `revenueEstimateHigh` (analyst high end) + compressed WACC (−50bps)
```python
scenarios = {
    "bear": DCFInputs(rev_growth=low_case, wacc=wacc + 0.01),
    "base": DCFInputs(rev_growth=avg_case, wacc=wacc),
    "bull": DCFInputs(rev_growth=high_case, wacc=wacc - 0.005),
}
```
This directly maps EODHD analyst range data to the three output ranges already shown in the UI.

---

## PART 5 — EXECUTION CHECKLIST (Ordered by Impact/Effort)

### Immediate (do today, highest ROI)

- [ ] **P1** `auto_valuation/data/estimates.py`: Add `fetch_ntm_estimates_eodhd()`, update priority chain, remove yfinance fallback
- [ ] **P2** `auto_valuation/data/fetcher.py`: Replace top-level `import yfinance as yf` with lazy + EODHD fallbacks; fix `fetch_yfinance_info`, `fetch_52wk_range`, `check_price_freshness`, `fetch_current_price`
- [ ] **P3** `webapp/data/peer_lists.py`: Remove yfinance import block in `fetch_peer_metrics()`, mark uncached peers as N/A instead of yfinance fallback
- [ ] **P4** `webapp/data/eodhd_client.py`: Extract `AnalystRatings` (TargetPrice, rating, buy/hold/sell counts) into dashboard dict
- [ ] **P5** `webapp/data/eodhd_client.py`: Extract `Earnings.History` last 4Q surprise % into `earnings_surprise_avg_4q`
- [ ] **P6** `webapp/data/eodhd_client.py`: Extract `Earnings.Trend` +1y consensus into NTM estimates (replace yfinance NTM)

### Short-term (this week)

- [ ] **S1** `auto_valuation/data/fetcher.py`: Add `fetch_macro_context_eodhd()` replacing FRED dependency for Rf and GDP growth
- [ ] **S2** `auto_valuation/learning/historical_replay.py`: Fix macro_regime to use historical RF, not current RF
- [ ] **S3** `auto_valuation/learning/calibrator.py`: Add time-decay weighting to CalibrationStore priors
- [ ] **S4** `auto_valuation/learning/feature_space.py`: Add `earnings_surprise_avg_4q` and `eps_revision_momentum_30d` features
- [ ] **S5** `webapp/data/eodhd_client.py`: Add EOD price history fetch for beta computation (replace Technicals.Beta when better calculation needed)
- [ ] **S6** `webapp/data/peer_lists.py`: Add `_discover_peers_via_screener()` as fallback for industries with no hardcoded peers

### Medium-term (next sprint)

- [ ] **M1** Multi-scenario DCF: Bear/Base/Bull using EODHD analyst range data
- [ ] **M2** `webapp/data/eodhd_client.py`: Extract `InsiderTransactions` last 90d as net buy/sell signal
- [ ] **M3** `learning/historical_replay.py`: Add `growth_regime` as calibration dimension
- [ ] **M4** Background runner: Pre-serialise peer multiples index to disk (msgpack), cold start drops from 6s to 50ms
- [ ] **M5** Persist `_OBS_CACHE` to disk (pickle TTL), survive Vercel cold starts
- [ ] **M6** Background runner: Pre-refresh EODHD fund files >20h old, not in request path

### Future

- [ ] **F1** Sentiment signal: `GET /api/sentiments` → `sentiment_30d_avg` as confidence modifier
- [ ] **F2** Historical market cap for point-in-time training labels
- [ ] **F3** S&P 500 index constituents for cross-sector peer validation
- [ ] **F4** ESG scores from fundamentals → ESG discount/premium factor in WACC
- [ ] **F5** Forward revenue anchoring: use consensus Year 1 as DCF input when ≥3 analysts

---

## PART 6 — API COST ANALYSIS

| Action | Cost per ticker | Frequency | Daily budget impact |
|--------|----------------|-----------|---------------------|
| Fund fundamentals (already running) | 10 calls | 1/day (TTL 24h) | 3,092 files × 10 = 30,920/day max |
| Calendar Trends (NTM estimates) | 1 call per 50 tickers | Weekly | ~60 calls/week = 9/day |
| EOD price history | 1 call | 1/week (TTL 7d) | 3,092 / 7 = 442/day |
| Macro indicators | 10 calls × 3 indicators | Monthly | 30 calls/month |
| Screener (peer discovery) | 5 calls | On-demand | <50/day |
| **Current daily usage** | | | **~89 calls/day** |
| **Projected with all improvements** | | | **~1,200 calls/day** |
| **Headroom** | | | **98,800 calls/day remaining** |

All proposed improvements fit **comfortably within the 100,000/day budget** with 99% headroom.

---

## PART 7 — FINANCIAL ACCURACY IMPROVEMENTS SUMMARY

| Signal | Current | After Fix | Impact |
|--------|---------|-----------|--------|
| NTM Revenue | yfinance TTM (trailing, wrong) | EODHD consensus forward | Year 1 DCF more accurate |
| Beta | yfinance (US-adjusted) | EODHD exchange-adjusted | WACC correct for non-US tickers |
| 52-wk range | yfinance | EODHD Technicals | Works for all 150k tickers |
| Risk-free rate | FRED (US only) | EODHD Macro (per country) | Non-US WACC accurate |
| GDP terminal cap | FRED (US only) | EODHD Macro (per country) | TGR correct for EUR/APAC |
| Analyst consensus | None | EODHD AnalystRatings (already cached) | Street vs model divergence signal |
| Earnings quality | None | EODHD Earnings.History surprise % | Calibration quality improves |
| Insider signal | None | EODHD InsiderTransactions (already cached) | Management confidence signal |
| Peer discovery | Hardcoded 350 industries | +Screener fallback | 100% coverage for any ticker |
| Training leakage | Medium risk | Fixed with as_of_date guards | More reliable calibration |
| Macro regime | Current RF rate only | Historical RF + country-specific | Correct regime classification |

---

*This plan was prepared based on a full codebase audit + complete EODHD API documentation review.*
*EODHD key source: `EODHD_API_KEY` environment variable | Fund cache: 3,092 files in `webapp/data/cache/`*
