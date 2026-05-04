# Nelix Capital — V2 Improvement Plan
**Theme:** Close model + data + UI gaps to make Nelix the best-in-class fintech valuation app.
**Source data:** EODHD via `EODHD_API_KEY`, 100k req/day budget, 3,092 cached funds.
**Status of prior plan:** [ADAPTIVE_DCF_IMPROVEMENT_PLAN.md](ADAPTIVE_DCF_IMPROVEMENT_PLAN.md) P1–M6 + F1/F5 complete (1,595 tests passing).
**Author note:** This plan is the agent-execution prompt. Each item is independently shippable, has a file target, an acceptance test, and a measurable accuracy or UX impact.

---

## EXECUTIVE GAP ANALYSIS

A full-codebase audit (DCF engine, EODHD client, learning system, dashboard UI, performance, tests) revealed five clusters of gaps that, if closed, will move Nelix from a strong DIY DCF tool to an institutional-grade research platform.

| # | Cluster | Severity | Headline gap |
|---|---|---|---|
| A | **Cost of capital** | Critical | CRP=0 for non-US; FX-of-Rf hard-coded to GS10; no Damodaran ERP-by-country; no size premium ladder |
| B | **DCF realism** | Critical | No reinvestment-rate (g ≤ ROIC×RR) check; rigid 7-yr/3-flat schedule; no ROIC fade; no Insurance/Utility templates; cyclicals get permanent peak margins |
| C | **EODHD signal coverage** | High | ESG, Holders, Officers, Valuation, Technicals (vol, MA-distance), Calendar/IPO, Bonds/yields, Options, Bulk-EOD, dividend history are extracted-but-ignored or never called |
| D | **Learning integrity** | High | Survivorship bias (delisted tickers absent), priors lack time-decay, regime via calendar year not actual Rf, no structural-break triggers, weakly tested |
| E | **UI / story-telling** | High | No tornado, no driver attribution, no historical multiples band, no insider time-series, no ownership panel, mobile tables overflow, no thesis diff vs prior run |

The plan below addresses each cluster with numbered, implementable workstreams (W1–W7), then breaks each workstream into atomic tasks (e.g., `A1.1`, `A1.2`).

---

## WORKSTREAM W1 — COST OF CAPITAL OVERHAUL (Cluster A)

### Why it matters
A 50bps WACC error on an 8% discount rate moves intrinsic value 8–12% on a 7-year DCF with terminal-heavy profile. Today's bugs:
- [auto_valuation/config.py](auto_valuation/config.py) `ERP_DEFAULT = 0.055` global — wrong for India (~8.5%), Brazil (~9%), Japan (~5%).
- `SIZE_PREMIUM_DEFAULT = 0.0` — micro-caps under-discounted by 200–400bps (Duff & Phelps 2024).
- Country-risk premium tables defined in `ValuationConfig` but never plumbed.
- [auto_valuation/data/fetcher.py](auto_valuation/data/fetcher.py) `_yf()` lazy yfinance fallback still imported — beta from US-adjusted index.
- Cost of debt = `Rf + 3%` flat — ignores actual debt curve, credit rating, or sovereign spread.

### Tasks
- **W1.1 — Damodaran ERP table by country (88 countries).** Add `auto_valuation/assumptions/erp_country.py` with the public Damodaran "Country Default Spreads & ERPs" table (Jan 2025); resolve via [General.AddressData.Country] in fundamentals. Fallback chain: country → developed-market median (5.6%) → global (5.5%). Acceptance: `test_wacc_country_erp.py` asserts US=5.5%, IND=8.7%, BRA=9.0%.
- **W1.2 — Country-risk premium plumbing.** Wire `ValuationConfig.country_risk_premium` into `auto_valuation/assumptions/wacc.py:cost_of_equity()` so `Ke = Rf + β·ERP + Sp + CRP`. Treat ADRs (US listing, foreign HQ) by HQ country, not listing.
- **W1.3 — Local-currency risk-free rates.** Replace blanket GS10 with EODHD `/macro-indicator` (10Y yield endpoints already wired in S1). Map: USD→GS10, EUR→DE-Bund10, GBP→UK-Gilt10, JPY→JGB10, AUD→AGB10, INR→IGB10, CAD→GoC10. Currency derived from `General.CurrencyCode`. Cache 30 days. Acceptance: AAPL still 4.4%, BABA (HKD) uses CNY 10Y.
- **W1.4 — Size premium ladder (Duff & Phelps 2024).** Add table in [auto_valuation/config.py](auto_valuation/config.py): mega ≥$50B = 0bps, large $10–50B = 60bps, mid $2–10B = 110bps, small $300M–2B = 175bps, micro <$300M = 380bps. Apply in `cost_of_equity()`.
- **W1.5 — Real cost-of-debt from interest expense.** Compute `Kd = interest_expense / avg_total_debt` per the latest 2 fiscal years; floor at `Rf + 0.5%`, cap at `Rf + 8%`. Replace [auto_valuation/assumptions/wacc.py](auto_valuation/assumptions/wacc.py) flat 3% spread. Add interest-expense extractor in [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py) (already in IS but not surfaced).
- **W1.6 — Beta from EOD prices, 3-year rolling.** Build `fetch_eodhd_beta()` already exists; wire into [auto_valuation/assumptions/wacc.py](auto_valuation/assumptions/wacc.py) ahead of `Technicals.Beta` and yfinance. Use `^GSPC` for US, `^STOXX50E` for EU, `^N225` for JP, etc. (benchmark map by country). Cache 7 days.
- **W1.7 — Remove yfinance entirely.** Delete `_yf()` lazy import in [auto_valuation/data/fetcher.py](auto_valuation/data/fetcher.py); remove `yfinance` from `requirements.txt`; replace any remaining call-sites with EODHD equivalents. Acceptance: `grep -r yfinance auto_valuation webapp` returns 0 hits.

**Expected accuracy lift:** ±150bps WACC precision for non-US tickers, +5–8% IV swing on emerging-market names; eliminates a top-3 failure mode in calibrator residuals.

---

## WORKSTREAM W2 — DCF REALISM (Cluster B)

### Why it matters
[auto_valuation/model/income_statement.py](auto_valuation/model/income_statement.py:31) `build_revenue_forecast()` enforces 3-year flat then 4-year linear fade — this is *neat* but unrealistic for: (a) hyper-growth (NVDA, ANET → should fade faster), (b) recovery (delta after recession → S-curve), (c) cyclicals (steel, auto → mid-cycle revert). [auto_valuation/model/ratios.py](auto_valuation/model/ratios.py:133) `validate_reinvestment_consistency()` only emits warnings — never halts. No Insurance/Utility-specific templates.

### Tasks
- **W2.1 — Reinvestment-rate hard guard.** In [auto_valuation/forecast/dcf.py](auto_valuation/forecast/dcf.py), after assumptions resolve, compute `implied_RR = terminal_g / ROIC`. If RR > 1.0 or RR < 0, raise `AssumptionError` and force re-derive: cap terminal_g at `min(terminal_g, ROIC × 0.8)`. Surface to dashboard as "terminal_growth_capped" warning.
- **W2.2 — Dynamic fade schedule by life-cycle.** Replace fixed 3-flat-then-fade with regime-aware path:
  - hyper (g₀ > 25%): 2-flat then 5-fade
  - growth (10–25%): 3-flat then 4-fade (current)
  - mature (3–10%): 1-flat then 6-fade
  - decline (<3%): immediate fade to terminal
  Implement in [auto_valuation/model/income_statement.py](auto_valuation/model/income_statement.py); add unit test for each regime.
- **W2.3 — ROIC fade model.** Add `auto_valuation/forecast/roic_fade.py`. Compute current ROIC from latest fundamentals; assume linear fade to industry-median ROIC (cohort lookup) over `FORECAST_YEARS`; compute implied EBIT margin path consistent with declining ROIC × IC growth. Use as alternative to current margin-fade when sector is "cyclical" (energy, materials, semis). Toggle via config `ROIC_FADE_ENABLED_FOR_CYCLICALS = True`.
- **W2.4 — Insurance template (combined-ratio model).** Add `auto_valuation/model/insurance.py`. Use float × investment yield − underwriting loss ratio. Gate via [auto_valuation/model/sector.py](auto_valuation/model/sector.py): GICS 4030 (Insurance) → route to insurance pipeline. Acceptance: `test_insurance_combined_ratio.py` validates Berkshire-style float math.
- **W2.5 — Utility regulated-asset-base template.** Add `auto_valuation/model/utility.py`. Allowed-return × rate base + non-regulated EBIT. Gate GICS 5510. Cap WACC swing tightly (regulated returns are bounded).
- **W2.6 — Cyclical mid-cycle margin.** Detect cyclicality (β > 1.4 OR sector ∈ {Energy, Materials, Auto, Semis Memory}). Margin target = average of trough+peak rather than 75th percentile. Implement in [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py:236) `_derive_ebit_margin_target()`.
- **W2.7 — Operating-lease right-of-use adjustment.** For RETAIL / AIRLINE today, EBITDAR-add-back exists but lease liability doesn't flow to net debt. Add ROU asset/liability from `Financials.Balance_Sheet.nonCurrentLiabilities-other` to net debt. Acceptance: AMZN net debt reduces by ~$70B → IV up ~3%.
- **W2.8 — Multi-stage growth API.** Expose `near_term_years`, `fade_years`, `terminal_growth` as overridable per-ticker via `overrides/<TICKER>.json` (file already supported). Document schema.
- **W2.9 — Pension & deferred-tax tax-shield.** Extend [auto_valuation/model/itax_shield.py](auto_valuation/model/itax_shield.py) to capitalise pension under-funding × tax_rate and deferred-tax-asset balances. Sources: `Balance_Sheet.deferredLongTermLiab`, `pensionAndOtherPostRetirementBenefits`.

**Expected accuracy lift:** Eliminates impossible valuations (g > ROIC), gives sector-correct templates for ~12% of S&P 500 (banks were already gated; insurance + utilities + REIT now equivalent), reduces over-valuation on hyper-growth names by 5–10%.

---

## WORKSTREAM W3 — EODHD SIGNAL EXPANSION (Cluster C)

### Why it matters
EODHD already serves the vast majority of needed data. Audit shows ~40% of available fields are extracted but never surfaced, and several high-signal endpoints are unused. New endpoints fit comfortably in the 100k/day budget (currently ~1,200/day used).

### Tasks
- **W3.1 — ESG scores extraction & WACC modifier.** [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py) → extract `ESGScores.{Environment, Social, Governance, ESG_Score, ControversiesLevel}`. Surface in dashboard as 0–100 gauges. Apply small WACC adjustment: ControversiesLevel ≥ 4 → +25bps; ESG_Score ≥ 80 → −10bps. Add config `ESG_WACC_ADJUSTMENT_ENABLED`.
- **W3.2 — Holders & Officers panels.** Extract top-10 institutional + top-10 fund holders from `Holders.Institutions` & `Holders.Funds`; officers + age + compensation from `General.Officers`. Render as table on dashboard.
- **W3.3 — Valuation history (P/E, EV/EBITDA bands).** From `Valuation` section + 5-year EOD prices, build trailing P/E, EV/EBITDA, P/S, EV/Sales monthly history. Render as percentile-band chart on dashboard ("trading at 23rd %ile of 5Y P/E"). New module `auto_valuation/data/valuation_history.py`.
- **W3.4 — Technicals: volatility & MA distance.** Surface `Technicals.{50DayMA, 200DayMA, 52WeekHigh, 52WeekLow, Beta}` already partially available. New: compute realized 60-day volatility from EOD prices → display next to beta.
- **W3.5 — Earnings calendar (next 4Q).** Use `/calendar/earnings?symbols=...` for upcoming report dates + consensus EPS. Already partially in fundamentals.Earnings.Trend but lacking confirmed dates. Show on dashboard "Next earnings: Aug 1, consensus $2.18".
- **W3.6 — Insider time-series chart.** Already extract net 90-day; extend to 24-month time-series (monthly aggregation of buys/sells). Render as bar chart on dashboard. Highlight months >2σ above mean.
- **W3.7 — Dividend history & DDM crosscheck.** `/div/{CODE}` for dividend payment history; surface as supplementary chart for income stocks. For dividend-yield > 4% names, run [auto_valuation/forecast/ddm.py](auto_valuation/forecast/ddm.py) (currently never called) and display as a third intrinsic value alongside DCF.
- **W3.8 — Bond yields for credit spread.** EODHD Bonds endpoint → fetch latest yield for the issuer's outstanding bonds where available. Use spread-over-treasury as direct `Kd` input (more accurate than W1.5 fallback).
- **W3.9 — Macro indicators panel (per country).** Already cached via S1. Surface country GDP growth (real + nominal), inflation, unemployment as a small "Macro Context" card on dashboard.
- **W3.10 — Bulk-EOD prefetch for cohort tickers.** Use `/eod-bulk-last-day/US?date=...` to cache yesterday's prices for all 7,000 US tickers in 1 call instead of 7,000. Slot into background runner. Cuts daily price-refresh API usage by 99%.
- **W3.11 — Search endpoint for ticker resolution.** `/search/{QUERY}` — wire into dashboard search bar so user can type "Apple" and get AAPL.US dropdown. Cache 30 days per query.
- **W3.12 — Currency-aware FX caching.** [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py:651) `_get_fx_rate()` not cached. Add 24h TTL cache for forex pairs.
- **W3.13 — Historical analyst consensus stream.** Persist each fetch of `Earnings.Trend` snapshot to `learning/db/analyst_history.sqlite` with timestamp. Over time, build "consensus revision velocity" feature → input to confidence score and learning cohort.

**Expected impact:** Dashboard becomes 3× more informative; new ESG/Holders/Macro panels match Bloomberg/Tikr feel; bulk-EOD cuts API load by 5,000+ calls/day enabling more aggressive prefetch.

---

## WORKSTREAM W4 — LEARNING SYSTEM HARDENING (Cluster D)

### Why it matters
Calibration is Nelix's competitive moat — but [auto_valuation/learning/historical_replay.py](auto_valuation/learning/historical_replay.py:99) classifies macro regime by *calendar year* not actual rates; survivorship bias is unmitigated; [auto_valuation/learning/adapter.py](auto_valuation/learning/adapter.py:80) priors don't decay with staleness; structural-break detection isn't wired to live inference.

### Tasks
- **W4.1 — Survivorship-bias correction.** Add `learning/db/delisted_tickers.json` — bootstrap from EODHD `/exchanges-list` + `/exchange-symbol-list/{EX}?delisted=1`. Include delisted tickers in historical_replay cohort scan when point-in-time mcap > threshold. Mark with `is_delisted=True` flag in observation.
- **W4.2 — Macro regime from actual Rf, not year.** Replace [auto_valuation/learning/historical_replay.py](auto_valuation/learning/historical_replay.py:99) calendar-year mapping with `_historical_rf(year)` lookup (already added in S2) → bucket: Rf<2% low, 2-4% neutral, ≥4% rising.
- **W4.3 — Time-decay on priors.** [auto_valuation/learning/adapter.py](auto_valuation/learning/adapter.py:80) — multiply applied residual by `exp(-age_days / 90)` so 90-day-old prior has half-weight. Add `last_calibrated_at` field already in DB.
- **W4.4 — Structural-break detector live integration.** Run `_structural_break_score()` (already exists) on every fetch; if score ≥ 0.7, halve calibration weight and emit dashboard warning "Recent structural break detected — calibration confidence reduced".
- **W4.5 — Quarterly calibration refresh job.** Add `auto_valuation/learning/recalibrate.py` — designed to run weekly via Vercel cron / GitHub Action. Re-scans last 12mo of new EODHD evidence; updates SQLite. Currently calibration only updates on background runner cycles.
- **W4.6 — Out-of-sample validation harness expansion.** Add 2026Q1 and 2026Q2 holdouts to [validate_shared_brain.py](validate_shared_brain.py); track baseline_MAE over time; auto-fail CI if MAE degrades >5% vs prior month.
- **W4.7 — Test coverage for learning modules.** Add unit tests for: `confidence.py` (currently 0% direct coverage), `relationship_graph.py`, `cross_industry.py:discover_analogs()`, `_structural_break_score()`. Target 80% line coverage in learning/.
- **W4.8 — Backfill idempotency test.** Add `test_backfill_idempotent.py` ensuring `_backfill_learning_actuals()` running 2× produces identical SQLite state.
- **W4.9 — Cohort confidence intervals.** Replace point-estimate residual with 80% CI (using Student-t with cohort_n-1 df). Show on dashboard as "Calibration adjustment: −1.8% ±0.7%".
- **W4.10 — Cross-industry analog *exclusion list*.** When a target ticker is mid-bankruptcy or under-investigation (Altman-Z<1.8), exclude from analog pool to prevent contaminating peer cohort.

**Expected impact:** MAE drops by 8–15% on out-of-sample window; correctly attributes ±2% calibration intervals; eliminates the four pre-existing test failures by addressing root causes (time-aware harness, sparse-data fallback).

---

## WORKSTREAM W5 — UI / DASHBOARD ELEVATION (Cluster E)

### Why it matters
Today's [webapp/templates/dashboard.html](webapp/templates/dashboard.html) renders a competent valuation card. Bloomberg / Tikr / Simply Wall St beat us on: tornado charts, multi-year multiples bands, peer grids with weighted blend, mobile-first tables, thesis diff, and live-news feed. None of these require new data sources we don't already have.

### Tasks
- **W5.1 — Tornado chart of value drivers.** New component `webapp/static/js/tornado.js`. Vertical bars showing IV change for ±1σ shock to: revenue_growth_y1, ebit_margin_target, wacc, terminal_growth, capex_pct, tax_rate. Backend: extend [auto_valuation/sensitivity/analysis.py](auto_valuation/sensitivity/analysis.py) `tornado_bars()` (already exists!) — surface JSON in `/api/dashboard/{ticker}` response, render with Chart.js horizontal bar.
- **W5.2 — Historical multiples band.** From W3.3, render shaded P/E and EV/EBITDA bands (5-year mean ±1σ) with current value plotted. Color: green if below mean, red if above 1σ.
- **W5.3 — Driver attribution narrative.** Generate "If revenue growth is 3pp above consensus AND margins hold at 22%, IV is $X (+12%)." Use `apply_scenario` outputs to detect which driver moves IV most. Render as 3 bullets above scenario cards.
- **W5.4 — Mobile-responsive tables.** Forecast + Comps + Sensitivity tables — wrap in `<div class="table-scroll">` with sticky first column on <768px screens. Add card-fallback for sensitivity (collapse 5x5 grid into 5 cards on mobile).
- **W5.5 — Thesis diff vs prior run.** Persist last valuation result per ticker in `webapp/data/cache/thesis_history/<TICKER>.json`. On new run, diff: "IV moved from $182 to $194 (+6.6%); driven by +8% Year-1 revenue (analyst upgrade) and -15bps WACC (Rf decline)." Render at top of dashboard.
- **W5.6 — Peer comp table v2.** Expand to: peer ticker | mcap | EV/Sales TTM/NTM | EV/EBITDA TTM/NTM | P/E TTM/NTM | Sales growth | EBIT margin | ROIC | similarity score. Highlight current ticker row. Add sector-median + 25-/75-pct bands at bottom.
- **W5.7 — Ownership composition donut.** Insiders %, Institutions %, Funds %, Float %. From W3.2 Holders extraction.
- **W5.8 — Insider history bar chart.** From W3.6 — 24-month insider net-buy time series. 60px tall, embedded in confidence section.
- **W5.9 — Macro context strip.** Compact horizontal strip with country flag + Rf + GDP growth + inflation + ERP. From W3.9.
- **W5.10 — News feed (right rail).** `/news?s={CODE}&limit=10` — last 10 headlines with sentiment labels. Already have sentiment endpoint. Polls every 5min on dashboard.
- **W5.11 — ESG gauge cards.** From W3.1. Three small radial gauges (E/S/G) above confidence panel.
- **W5.12 — Earnings calendar widget.** From W3.5. "Next earnings: 2026-08-01, consensus EPS $2.18 (1Y growth +12%)" pill.
- **W5.13 — Skeleton-loader states.** Replace blocking spinners with skeleton cards so first paint is instant — perceived latency drops 40%.
- **W5.14 — Dark/light theme switch.** Currently dark-only. Add CSS variable theme; persist choice in localStorage.
- **W5.15 — Export to PDF / Excel.** "Download Research Report" → server-side ReportLab PDF + openpyxl model export. Reuse existing [auto_valuation/output/](auto_valuation/output/) builders.
- **W5.16 — URL-shareable scenario state.** `?bull_g=0.18&base_g=0.12&bear_g=0.05&...` so users can share their custom assumption set.
- **W5.17 — Search bar + watchlist.** Top-bar search from W3.11; localStorage-backed watchlist of last 10 tickers, surfaced on home page.
- **W5.18 — Accessibility pass.** WCAG AA: keyboard nav for all charts, ARIA labels on KPIs, color-blind friendly palette (Okabe-Ito).

**Expected impact:** UX parity with Tikr / Simply Wall St; mobile session length 2–3×; share-link feature drives organic acquisition.

---

## WORKSTREAM W6 — PERFORMANCE & RELIABILITY

### Why it matters
Vercel cold-starts > 5s lose users. Current bottlenecks: per-request beta recompute (300ms), no HTTP connection pooling, FX uncached, screener loop sequential.

### Tasks
- **W6.1 — `requests.Session` with connection pool.** [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py) — replace top-level `requests.get` with module-singleton `Session` (HTTPAdapter pool_maxsize=20). Cuts ~40ms per call.
- **W6.2 — Beta cache (W1.6 already adds 7-day TTL).** Confirm beta isn't recomputed per request.
- **W6.3 — FX cache (24h).** Already in W3.12.
- **W6.4 — Screener parallelism.** [webapp/data/peer_lists.py](webapp/data/peer_lists.py) `fetch_screener_peers` calls — issue concurrent requests via existing background-runner ThreadPool when querying multiple sectors.
- **W6.5 — Vercel warm-up cron.** Add `vercel.json` cron: hit `/healthz` every 4min during business hours to keep lambda warm.
- **W6.6 — Client-side bundling.** Concatenate + minify JS/CSS at build; serve from `webapp/static/dist/`. Cuts first-paint by 200–400ms.
- **W6.7 — Sentry instrumentation.** Add `sentry-sdk` to webapp; capture API errors + slow-request traces. Use existing `mcp_sentry_*` infra.
- **W6.8 — Healthz endpoint with depth.** Extend [check.py](check.py) → expose `/healthz?deep=1` reporting EODHD reachability, learning DB row count, cache size, last calibration time.
- **W6.9 — Request-level circuit breaker.** If EODHD latency p95 > 3s in last 60s, fall back to disk cache only (skip live calls); auto-recover after 30s.

---

## WORKSTREAM W7 — TESTING, OBSERVABILITY, DOCS

### Tasks
- **W7.1 — Coverage report in CI.** Add `pytest-cov`; fail PR if coverage drops below 75%.
- **W7.2 — End-to-end smoke for 10 reference tickers** (AAPL, MSFT, NVDA, V, JPM, BRK-B, XOM, T, KO, BABA). Run nightly; assert IV within ±20% of prior run.
- **W7.3 — Property-based tests** (`hypothesis`) for DCF: monotonicity (higher growth → higher IV), boundedness (terminal_g < WACC).
- **W7.4 — Architecture decision records (ADRs).** New folder `docs/adr/`; document each major design choice (DCF horizon, calibration cohort grouping, etc.).
- **W7.5 — User-facing API docs.** OpenAPI 3 spec for `/api/dashboard/*`, served at `/docs`.
- **W7.6 — Data-quality dashboard.** Internal `/admin/data-quality` page: per-ticker last-fetch age, calibration confidence, structural-break flags, insider-data lag.

---

## EXECUTION ORDER (RECOMMENDED)

| Phase | Items | Est. Calendar | Outcome |
|---|---|---|---|
| **Phase 1 — Quick Wins** | W1.1, W1.4, W2.1, W3.1, W3.4, W3.9, W5.1, W5.4, W6.1 | Sprint 1 | Visible UX + accuracy bump; tornado live; non-US WACC fixed |
| **Phase 2 — Data Depth** | W1.2/3/5/6/7, W3.2/3/5/6/10/11, W5.2/6/7/8/9/12 | Sprint 2 | Full EODHD coverage; institutional UI parity |
| **Phase 3 — DCF Sophistication** | W2.2–W2.9 | Sprint 3 | Insurance + Utility templates; cyclical handling; ROIC fade |
| **Phase 4 — Learning Hardening** | W4.1–W4.10 | Sprint 4 | Bias-corrected calibration; CI safety nets |
| **Phase 5 — Polish & Ops** | W5.10–W5.18, W6.2–W6.9, W7.1–W7.6 | Sprint 5 | Performance, reliability, docs, accessibility |

---

## ACCEPTANCE METRICS

| Metric | Today | Target |
|---|---|---|
| Out-of-sample MAE (revenue growth) | 0.205 | 0.180 (-12%) |
| WACC error vs Bloomberg (non-US) | ±150bps | ±50bps |
| Test count (passing) | 1,595 | 1,750+ |
| Test coverage (learning/) | ~55% | 80% |
| Cold-start p95 | ~5s | <2s |
| Mobile usability score | 65 | 90+ |
| EODHD endpoints used | 5 | 12+ |
| Daily API calls | ~1,200 | ~5,000 (still 95% headroom) |
| Sectors with first-class templates | 3 (REIT, Retail, Airline) | 6 (+ Insurance, Utility, Cyclical) |

---

## RISKS & MITIGATIONS

| Risk | Mitigation |
|---|---|
| EODHD field-schema changes break extractors | Add JSON-schema validators in [auto_valuation/validation/](auto_valuation/validation/); contract tests against fixtures |
| Damodaran ERP table goes stale | Annual refresh in CI; warn if >13 months old |
| Learning regressions from W4 changes | W7.2 nightly tests + W4.6 MAE gate |
| UI churn breaks bookmarks | URL versioning `/v2/dashboard/*`; redirect from v1 |
| Vercel function size limit (50MB) | Already lazy-importing; audit `webapp/static/dist/` post-bundling |

---

## OUT-OF-SCOPE (intentionally deferred)

- Options chain data (EODHD Options endpoint) — niche for retail valuation
- Short interest (separate paid feed)
- Real-time streaming quotes (websocket)
- LLM-generated narrative (current rule-based commentary is sufficient and deterministic; LLM adds cost + non-determinism)

---

*Source: full codebase audit performed 2026-05-05; EODHD docs reviewed; Damodaran 2025 ERP / size-premium tables referenced; Duff & Phelps 2024 cost-of-capital handbook.*
*Prior plan reference: [ADAPTIVE_DCF_IMPROVEMENT_PLAN.md](ADAPTIVE_DCF_IMPROVEMENT_PLAN.md) — items P1–M6, F1, F5 complete.*
