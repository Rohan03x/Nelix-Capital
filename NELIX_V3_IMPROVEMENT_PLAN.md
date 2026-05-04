# Nelix Capital — V3 Improvement Plan (Gap Expansion)
**Theme:** Extends [NELIX_V2_IMPROVEMENT_PLAN.md](NELIX_V2_IMPROVEMENT_PLAN.md) (W1–W7) with **42 newly-identified gaps** uncovered by a deeper, second-pass codebase audit (architecture, data integrity, quant depth, risk, security, ops, domain coverage, UX, compliance, engineering velocity).
**Status:** V2 closes the *front-of-house* gaps (cost of capital, DCF realism, EODHD breadth, learning, dashboard polish, perf, testing). V3 closes the *back-of-house* gaps that determine whether Nelix can scale safely from a personal research tool into a production-grade platform.
**Source:** Full second-pass audit of `auto_valuation/`, `webapp/`, `tests/`, `requirements.txt`, `vercel.json` (May 2026). Each item is independently shippable, has a target file, an acceptance test, and a measurable risk-reduction or capability-uplift impact.

---

## EXECUTIVE GAP ANALYSIS (V3)

V2 made the *model* world-class. V3 makes the *system* world-class. The 42 gaps fall into ten clusters:

| # | Cluster | Severity | Headline gap |
|---|---|---|---|
| F | **Architecture & Code Quality** | High | God-modules (`eodhd_client.py` >3k LOC, `samples.py` >1.6k LOC); circular `validation.shared_brain ↔ webapp.knowledge_model`; config sprawl across 5 sites; three independent `TICKER.EXCHANGE` parsers |
| G | **Data Integrity** | Critical | No point-in-time correctness on historical replay; restated earnings silently overwrite prior years; no JSON-Schema contract on EODHD payloads; FX-normalised statements not consistency-checked |
| H | **Quant Depth (beyond W2)** | High | Monte Carlo module exists but unused; no real-options layer; SBC dilution naive; NWC seasonality flat; NOL forward-usage uncapped; SOTP, dual-class, hybrid securities under-modelled |
| I | **Risk & Governance** | High | No reproducibility (no `run_uuid`, no seed pinning per-run); requirements unpinned (no lockfile); no kill-switch when ≥2 CRITICAL validation flags fire; no rollback ledger of prior IVs |
| J | **Security** | Critical | EODHD API key hard-coded in repo; no input-size limits on overrides JSON (DoS); no CSRF on POST endpoints; no rate-limit; no `safety`/`bandit` in CI |
| K | **Deployment & Ops** | Medium | Secrets management undocumented; no canary / blue-green; no feature flags wired live; no A/B harness; serverless logs lack `traceparent` propagation |
| L | **Domain Coverage** | Medium | ADR↔home-listing not normalised; dual-class & controlled-co. discount missing; SPACs not gated; recent IPOs (<2y) crash cohort calibration; no SOTP for conglomerates; FX-hedging effects ignored |
| M | **User-Facing (beyond W5)** | Low–Med | No portfolio / multi-ticker view; no IV-vs-price alert pill; watchlist localStorage-only (lost on cache wipe); no screener UI; no comparison view; no notes/diary; no batch upload |
| N | **Compliance** | Critical (EU) | No legal disclaimer on dashboard; no MiFID-II suitability gating for EU; no GDPR data-deletion endpoint; no privacy/terms pages |
| O | **Engineering Velocity** | Medium | No pre-commit hooks; no enforced type-check (pyright strict); no formatter (`black`) in CI; no internal API versioning; no benchmark gate |

The plan below addresses each cluster with workstreams **W8–W17**, picking up the numbering from V2.

---

## WORKSTREAM W8 — ARCHITECTURE & CODE QUALITY (Cluster F)

### Why it matters
[webapp/data/eodhd_client.py](webapp/data/eodhd_client.py) is now a 3,000-line god-module mixing HTTP, caching, FX, fundamentals normalisation, and dashboard helpers. [webapp/data/samples.py](webapp/data/samples.py) is 1,600+ lines mixing demo fixtures with the live dashboard assembly path. These modules dominate cold-start parsing time and make every change risky.

### Tasks
- **W8.1 — Split `eodhd_client.py` (3 files).** Create `webapp/data/eodhd/` package: `fundamentals.py`, `pricing.py`, `macro.py`, plus `_session.py` for the shared `requests.Session`. Re-export the existing public surface from `webapp/data/eodhd_client.py` to preserve imports. Acceptance: each new file <800 LOC; `from webapp.data.eodhd_client import *` still works.
- **W8.2 — Split `samples.py`.** Extract dashboard assembly to `webapp/data/dashboard_builder.py`; demo fixtures to `webapp/data/fixtures.py`; override resolution to `webapp/data/override_resolver.py`. Acceptance: `samples.py` reduced to thin façade <300 LOC.
- **W8.3 — Break `validation.shared_brain ↔ webapp.knowledge_model` cycle.** Move shared contracts (the dataclasses currently defined in [webapp/knowledge_model.py](webapp/knowledge_model.py)) into a neutral `auto_valuation/learning/contracts.py`. Both sides import from contracts; no cross-tier imports. Acceptance: `pydeps auto_valuation webapp` shows no cycles.
- **W8.4 — Unify ticker parsing.** Create `auto_valuation/data/ticker.py` with `class Ticker` (`parse()`, `symbol`, `exchange`, `currency_default`). Replace the three independent parsers in `eodhd_client.py`, `ticker_search.py`, `yfinance_client.py`. Acceptance: `grep -r "split('.')" webapp/data/` returns ≤1 hit (in `Ticker.parse` itself).
- **W8.5 — Config package.** Replace flat `auto_valuation/config.py` with `auto_valuation/config/` package: `core.py`, `sector_profiles.py`, `learning_config.py`, `ui_config.py`, `feature_flags.py`. Provide back-compat re-exports. Acceptance: import surface unchanged; new code grouped by domain.
- **W8.6 — Merge duplicated forecast schedule logic.** [auto_valuation/assumptions/engine.py](auto_valuation/assumptions/engine.py) and [auto_valuation/model/forecast.py](auto_valuation/model/forecast.py) both build year-by-year fade schedules. Consolidate into `auto_valuation/model/forecast.ForecastSchedule`; have `assumptions.engine` delegate. Acceptance: only one place computes `years × growth_path`.
- **W8.7 — Pyright strict on data layer.** Annotate every public function in `auto_valuation/data/` with explicit return types. Add `[tool.pyright] strict = true` for that subpath in `pyproject.toml`. Acceptance: `pyright auto_valuation/data` returns 0 errors.
- **W8.8 — `CircularReferenceError` for circular WACC↔EV iteration.** [auto_valuation/config.py#L107](auto_valuation/config.py#L107) sets `CIRCULAR_REF_MAX_ITER=50` but silently exits if not converged. Raise typed exception; log iteration count; expose to dashboard as warning. Acceptance: `test_circular_ref_convergence_failure.py`.

**Expected impact:** −40% cold-start parse time (smaller modules load lazily), drastically reduced merge-conflict surface, type errors caught at CI before runtime.

---

## WORKSTREAM W9 — DATA INTEGRITY (Cluster G)

### Why it matters
Calibration MAE depends on faithful historical data. Today, replays are *not* point-in-time; restated earnings silently overwrite prior periods; FX conversion isn't end-to-end validated; and we have no schema contract with EODHD, so a silent payload change ships bad numbers.

### Tasks
- **W9.1 — Point-in-time `DataSnapshot` wrapper.** New `auto_valuation/data/snapshot.py::DataSnapshot(report_date, fetched_at, vintage_days, payload)`. Cache key in `webapp/data/cache.py` extends with `as_of_date`. Historical replay must request snapshots ≤ as-of date. Acceptance: `test_pit_replay.py` proves a 2020 replay never reads 2021 data.
- **W9.2 — Restatement labelling.** [auto_valuation/data/cleaner.py#L146](auto_valuation/data/cleaner.py#L146) currently keeps higher-revenue duplicate without labelling. Add `restated_in_year`; in calibration, halve weight of pre-restatement observations. Acceptance: `test_restatement_weighting.py`.
- **W9.3 — JSON-Schema contracts on EODHD.** Add `auto_valuation/validation/schemas/eodhd_fundamentals.schema.json`, `eodhd_eod.schema.json`, `eodhd_macro.schema.json`. Validate every payload via `jsonschema` (already available); reject + alert on violation. Acceptance: feeding a stripped fixture raises `SchemaValidationError`.
- **W9.4 — End-to-end currency consistency check.** New `validate_currency_consistency(fundamentals, quote_currency, reporting_currency)` in [auto_valuation/validation/checks.py](auto_valuation/validation/checks.py). Rejects mixed-currency revenue / EBIT / net debt. Acceptance: BHP.ADR (USD quote, AUD reporting) passes; deliberately corrupted payload fails.
- **W9.5 — Split-adjusted insider series.** Insider trade volumes / holdings counts are not split-adjusted. Build `auto_valuation/data/historical_adjustment.py::SplitAdjuster`; rebase shares on each split. Acceptance: a test ticker with a 2:1 split shows continuous holdings.
- **W9.6 — Vintage hash on every cache write.** Extend [webapp/data/cache.py](webapp/data/cache.py) entries with `payload_sha1`. Log mismatch when same-day re-fetch returns different hash; surface as data-quality metric. Acceptance: `test_cache_vintage.py`.

**Expected impact:** Eliminates the largest source of replay bias; turns silent EODHD schema drift into loud CI failures; restores faith in historical MAE numbers.

---

## WORKSTREAM W10 — QUANT DEPTH BEYOND W2 (Cluster H)

### Why it matters
V2's W2 fixed the *deterministic* DCF (life-cycle fade, ROIC fade, sector templates). V3 adds the *stochastic* and *capital-structure* layers most retail tools ignore.

### Tasks
- **W10.1 — Wire Monte Carlo to IV output.** [auto_valuation/model/monte_carlo.py](auto_valuation/model/monte_carlo.py) exists but its result is dropped before reaching the dashboard. Surface `IV_p10 / IV_p50 / IV_p90` and a histogram. Acceptance: dashboard shows a percentile band; `test_monte_carlo_surfaced.py`.
- **W10.2 — Real-options layer.** New `auto_valuation/model/real_options.py::BinomialAbandonmentOption` for biotech / capex-heavy tech. Toggle by sector. Acceptance: a Vertex-style test ticker shows IV uplift in [10%, 30%].
- **W10.3 — Implied vol from option chain.** Pull EODHD options endpoint (per V2 W3, this was deferred). Compute IV from put prices; compare to DCF-implied vol; flag divergence on dashboard. Acceptance: `test_option_iv_extraction.py` on AAPL chain.
- **W10.4 — Forecast SBC dilution.** [auto_valuation/model/shares.py#L66](auto_valuation/model/shares.py#L66) extrapolates last year's net new shares flat. Replace with model: `forecast_sbc_dilution(headcount_growth, comp_inflation, vesting_schedule)`. Acceptance: a Snowflake-style test ticker shows accelerating dilution.
- **W10.5 — NWC seasonality.** [auto_valuation/model/working_capital.py](auto_valuation/model/working_capital.py) averages 3y; add quarterly seasonality factor for retail / auto. Acceptance: WMT test shows Q4 inventory build.
- **W10.6 — NOL forward-usage cap.** Cap NOL benefit by min(NOL, 3y forward taxable income); add Sec-382 ownership-change scenario. Acceptance: `test_nol_cap.py`.
- **W10.7 — Hybrid securities (converts) two-scenario.** Base = no conversion; bull = full conversion. Blend at scenario weights. Acceptance: `test_convertibles_scenarios.py`.
- **W10.8 — Minority interest materiality check.** If NCI > 20% of equity, raise warning and switch to affiliate-adjusted UFCF. Acceptance: `test_nci_materiality.py`.
- **W10.9 — R&D capitalisation amortisation schedule.** [auto_valuation/model/sector.py#L15](auto_valuation/model/sector.py#L15) has the toggle but no amortisation logic. Add `auto_valuation/model/rd_capitalization.py::capitalize_rd()`. Acceptance: Eli Lilly test shows IV uplift consistent with Damodaran's published method.
- **W10.10 — SOTP for conglomerates.** New `auto_valuation/model/sotp.py::run_sotp(segments)`. Required for BRK, BABA, conglomerate energy majors. Acceptance: XOM test produces upstream/downstream/chemicals stack matching segment disclosures.

**Expected impact:** Closes the second-largest accuracy gap after W1+W2; produces probability-weighted IVs (industry-standard); enables credible coverage of conglomerates, biotech, and SBC-heavy tech.

---

## WORKSTREAM W11 — RISK & GOVERNANCE (Cluster I)

### Tasks
- **W11.1 — Run reproducibility envelope.** Every valuation gets a `run_uuid`, captured `random_seed`, and `data_vintage_timestamp`. Persist to `logs/<TICKER>_<DATE>.jsonl`. Acceptance: re-running a `run_uuid` reproduces IV bit-exact.
- **W11.2 — Lockfile.** Add `pip-tools`; commit `requirements.lock`; CI installs from lock. Acceptance: `pip install -r requirements.lock` produces identical pip-freeze on two machines.
- **W11.3 — Adversarial input tests.** New `tests/test_adversarial_inputs.py`: terminal_g > WACC, zero revenue, negative EBIT, 100% debt, FX collapse. Each must fail loudly (not return garbage IV).
- **W11.4 — Kill-switch on CRITICAL flags.** [auto_valuation/validation/checks.py](auto_valuation/validation/checks.py) currently warns. Add `halt_on_critical=True` mode; ≥2 CRITICAL → raise `DataQualityError`; UI shows red banner instead of valuation.
- **W11.5 — Structural-break alerts.** New `auto_valuation/monitoring/alerts.py`; if `_structural_break_score()` ≥ 0.7 OR Rf moves >100bps, fire webhook (Vercel → Sentry/Slack). Wires to V2 W4.4.
- **W11.6 — Valuation rollback ledger.** Tag every IV with commit SHA + run_uuid; keep last 10 versions per ticker; `GET /api/valuation/<ticker>/history` returns them. Acceptance: a deliberately-broken commit can be diffed against prior IV.

**Expected impact:** Production-grade reproducibility; safe rollback; auditors / power users can verify any output.

---

## WORKSTREAM W12 — SECURITY HARDENING (Cluster J)

### Why it matters
The EODHD API key is currently hard-coded in [webapp/data/eodhd_client.py](webapp/data/eodhd_client.py) and visible in the public repo. This is a P0.

### Tasks
- **W12.1 — Remove hard-coded API keys.** Delete the legacy hard-coded default API-key literal; require `EODHD_API_KEY` env var; fail fast if missing in production. Rotate the existing key (it is now considered burnt). Add `detect-secrets` pre-commit hook. Acceptance: secret-prefix grep returns 0 hits.
- **W12.2 — Override JSON DoS guard.** [auto_valuation/assumptions/overrides.py](auto_valuation/assumptions/overrides.py): enforce max nesting=10, max keys=100, max value len=1000 chars. Acceptance: `test_override_dos.py`.
- **W12.3 — Jinja autoescape audit.** Confirm `app.jinja_env.autoescape=True` for all templates; add OWASP-payload test that asserts injected `<script>` is escaped on dashboard.
- **W12.4 — CSRF on POST routes.** Wrap [webapp/app.py](webapp/app.py) with `Flask-WTF CSRFProtect`; exempt `/api/internal/*` cron endpoints behind shared-secret. Acceptance: `test_csrf_required.py`.
- **W12.5 — Rate limiting.** `Flask-Limiter`: 10 req/min/IP on `/api/dashboard`, 1 req/min on `/valuate`, 60 req/min on `/api/search`. Acceptance: 11th request in a minute returns 429.
- **W12.6 — CVE scanning in CI.** Add `safety check` and `bandit -r auto_valuation webapp` to GitHub Actions; fail PR if score ≥ 5.

**Expected impact:** Eliminates the largest security debt; makes Nelix safe to expose publicly.

---

## WORKSTREAM W13 — DEPLOYMENT & OPS (Cluster K)

### Tasks
- **W13.1 — `docs/DEPLOYMENT.md`.** Document Vercel env-var setup, secret rotation cadence, who has access, how to roll back.
- **W13.2 — Canary deploys.** Use Vercel preview branches + 10% traffic split for 5 min before promoting to production.
- **W13.3 — Bundle slimming.** `vercel.json` `excludeFiles` for `logs/`, `tests/`, `__replay_test__/`, `.venv/`, `*.txt` debug dumps. Measure cold-start delta.
- **W13.4 — Live feature flags.** New `auto_valuation/config/feature_flags.py` backed by Vercel KV; flags: `ENABLE_W4_TIME_DECAY`, `USE_DAMODARAN_ERP`, `ENABLE_SOTP`. Toggleable without redeploy.
- **W13.5 — A/B harness for assumption changes.** `auto_valuation/testing/ab_test.py::ABTestConfig`; deterministic per-ticker hash → variant; track IV-error delta in learning DB. Acceptance: a test variant on 10% of cohort produces a measurable MAE diff.
- **W13.6 — Trace context propagation.** Add OpenTelemetry `traceparent` to every log line; integrate with Sentry traces (V2 W6.7). Acceptance: a single request traceable across `/api/dashboard` → `eodhd_client` → `dcf` → cache writes.

---

## WORKSTREAM W14 — DOMAIN COVERAGE (Cluster L)

### Tasks
- **W14.1 — ADR ↔ home-listing resolver.** New `auto_valuation/data/adr_resolver.py`: BHP (NYSE ADR) maps to BHP.AX; convert per-ADR financials to home shares via ADR ratio (from EODHD General). Acceptance: `test_adr_normalisation.py`.
- **W14.2 — Dual-class share handling.** [auto_valuation/model/shares.py](auto_valuation/model/shares.py): if multiple share classes, compute per-class IV (Class A vs B/C). Surface both. Acceptance: GOOG vs GOOGL test produces consistent EV.
- **W14.3 — Controlled-company discount.** Detect founder/insider voting control >50% (EODHD Officers + Holders); apply 10–20% governance discount or flag. Acceptance: `test_controlled_company_discount.py`.
- **W14.4 — SPAC gate.** Detect pre-merger SPAC (no revenue 2y + sponsor warrants); halt DCF; show comps-only mode. Acceptance: `test_spac_gate.py`.
- **W14.5 — Recent-IPO fallback.** If `ipoDate` < 2y, swap own-history cohort for industry-peer cohort; lower confidence flag. Acceptance: `test_recent_ipo.py`.
- **W14.6 — FX hedging scenario.** If FX-hedge disclosure present in notes, render hedged vs unhedged IV side-by-side. Acceptance: `test_fx_hedge_scenarios.py`.
- **W14.7 — Segment-level forecasting.** New `auto_valuation/model/segment_analysis.py`; extract segment revenue / EBIT from EODHD; produce segment-blended forecast. Required input for W10.10 SOTP.
- **W14.8 — Family-holding-company warning.** Detect multi-tier ownership (Roche, BMW, Porsche-VW); warn that consolidated DCF may double-count.

---

## WORKSTREAM W15 — USER-FACING (BEYOND W5) (Cluster M)

### Tasks
- **W15.1 — Portfolio view.** `/portfolio?tickers=AAPL,MSFT,NVDA`; correlation matrix, weighted IV, risk parity weights.
- **W15.2 — IV-vs-price alert pill** on home page: "12 stocks trading >20% below IV." Click → filtered watchlist.
- **W15.3 — Cloud watchlist.** Move beyond localStorage; SQLite + session token; sync across devices (no signup required, anonymous tokens).
- **W15.4 — Screener UI.** `/screener` with filters (P/E, EV/Sales, ROIC, div yield, β, mcap, country, sector). Paginated 50/page. Reuses cohort cache from V2 W3.10.
- **W15.5 — Side-by-side compare view.** `/compare/<a>/<b>`: dashboards horizontally; diffs highlighted; verdict line ("A trades at richer EV/EBITDA, but better ROIC + faster growth").
- **W15.6 — Notes / valuation diary.** `/api/notes/<ticker>`; markdown notes per ticker; rendered below summary.
- **W15.7 — Batch CSV upload.** `/upload/tickers` accepts CSV ≤500 tickers; returns zipped Excel pack from `auto_valuation/output/`.
- **W15.8 — Earnings → DCF link.** Earnings calendar widget click → modal showing `NTM consensus EPS vs DCF NTM EPS`; surprise-risk badge.
- **W15.9 — Keyboard shortcuts.** `mousetrap.js`: `/` search, `n` notes, `c` compare, `w` watchlist, `?` legend.
- **W15.10 — Search bar autocomplete.** Reuses V2 W3.11 search endpoint; debounced 200ms; arrow-key navigable dropdown.

---

## WORKSTREAM W16 — COMPLIANCE (Cluster N)

### Tasks
- **W16.1 — Disclaimer footer + `/terms` + `/disclosures` pages.** Required before any public promotion.
- **W16.2 — EU MiFID-II suitability gate.** IP-geolocate; if EU, present a brief risk-tolerance questionnaire on first visit; persist to anonymous session.
- **W16.3 — Region gating.** OFAC sanctions check (block sanctioned tickers / IPs); China-mainland gating per local rules; surface clear "not available in your region" page.
- **W16.4 — GDPR data lifecycle.** `/privacy` page; cron deletes cached PII (notes, watchlists) older than 24 months; `DELETE /api/me` purges all user data.

### Current Status
- **Completed W16.1 page/footer slice:** global disclaimer footer plus `/terms`, `/disclosures`, and `/privacy` pages are implemented and tested.
- **Completed W16.2 EU suitability slice:** EU/EEA/UK country headers trigger a one-time questionnaire before dashboard access; completion is persisted in the anonymous Flask session and API calls return `suitability_required` until acknowledged.
- **Completed W16.4 anonymous workflow lifecycle slice:** `DELETE /api/me` clears server-side watchlist, search-impression, manual-compare, and derived compare-relationship workflow data; the daily internal learning cron also runs a 24-month stale workflow cleanup. Future notes/diary storage should register with the same lifecycle path when W15.6 lands.
- **Pending:** W16.3 region/sanctions gating.

---

## WORKSTREAM W17 — ENGINEERING VELOCITY (Cluster O)

### Tasks
- **W17.1 — `.pre-commit-config.yaml`** with `black`, `isort`, `flake8`, `bandit`, `detect-secrets`, `end-of-file-fixer`, `trailing-whitespace`.
- **W17.2 — Pyright strict in CI.** `pyproject.toml` `[tool.pyright] strict = true` for `auto_valuation/data/`, `auto_valuation/model/`, `auto_valuation/forecast/`.
- **W17.3 — `black --line-length 100`** enforced in CI; format-check on every PR.
- **W17.4 — Decoupling layer `api/`.** New top-level `api/` package exposing dataclass contracts; `webapp/` imports only from `api/`, never from `auto_valuation/` internals. Enables future microservice split.
- **W17.5 — API versioning.** `/api/v1/dashboard/<ticker>`; keep v1 alive for 90 days after v2 ships. Document in `docs/api_reference.md`.
- **W17.6 — Centralised `docs/`.** `architecture.md` (data-flow diagram), `api_reference.md` (auto-gen from OpenAPI), `deployment.md`, `glossary.md`, `adr/` (per V2 W7.4).
- **W17.7 — `flasgger` OpenAPI.** Auto-generate Swagger UI at `/docs`; spec-first development for new endpoints.
- **W17.8 — Test fixture isolation.** [tests/conftest.py](tests/conftest.py) — switch all mutable fixtures to `scope="function"` + `deepcopy`. Acceptance: `pytest --randomly-seed=last` is stable.
- **W17.9 — `pytest-benchmark` perf gate.** Benchmark 10 reference tickers; fail PR if p95 latency regresses >10%.

---

## EXECUTION ORDER (RECOMMENDED V3 PHASING)

| Phase | Workstreams | Outcome |
|---|---|---|
| **Phase 6 — P0 Risk Mitigation** | W12.1 (key rotation), W12.4–5 (CSRF, rate-limit), W11.4 (kill-switch), W16.1 (disclaimer) | Eliminates production-blocking security & legal risk |
| **Phase 7 — Foundations** | W8.1–8.5 (split god-modules, config package), W17.1 (pre-commit), W17.2–3 (typing & format), W11.2 (lockfile) | Future changes are safe to merge |
| **Phase 8 — Data Trust** | W9.1–9.6 (PIT, restatements, schema, FX, splits, vintage) | Calibration MAE numbers regain credibility |
| **Phase 9 — Quant Depth** | W10.1–10.10 | Probability-weighted IVs, real options, SOTP, hybrids |
| **Phase 10 — Domain Breadth** | W14.1–14.8 | ADRs, dual-class, SPACs, IPOs, FX hedge, segments |
| **Phase 11 — Ops & Observability** | W11.1, W11.5–6, W13.1–13.6 | Reproducibility, canary, feature flags, traces |
| **Phase 12 — User Experience** | W15.1–15.10, W16.2–16.4 | Portfolio, screener, compare, notes, EU gating |

---

## ACCEPTANCE METRICS (V3 ADDITIONS TO V2)

| Metric | Today | V3 Target |
|---|---|---|
| Hard-coded secrets in repo | 1 (EODHD key) | 0 |
| God-modules (>1.5k LOC) | 2 | 0 |
| Lockfile installed in CI | No | Yes |
| Type-checked subpackages (pyright strict) | 0 | 4 |
| Pre-commit hooks | 0 | 7 |
| Endpoints with rate limit | 0 | All `/api/*` |
| Endpoints with CSRF | 0 | All POST |
| JSON-Schema-validated payloads | 0 | EODHD fundamentals + EOD + macro |
| Probability-weighted IV surfaced | No | Yes (p10/p50/p90) |
| Sectors/forms covered | Equities only | + SPACs gated, ADRs normalised, dual-class, conglomerate SOTP |
| Reproducibility (`run_uuid` per IV) | No | Yes |
| Compliance pages | 0 | Disclaimer + Terms + Privacy + Disclosures |
| Cold-start p95 (post-W8.1+W13.3) | ~5s | <1.5s |

---

## RISKS & MITIGATIONS (V3-SPECIFIC)

| Risk | Mitigation |
|---|---|
| Splitting god-modules breaks imports | Re-export public names from old paths; deprecation period 60 days |
| Rotating EODHD key breaks live cache | Coordinate rotation outside US-market hours; pre-warm cache on new key |
| Kill-switch (W11.4) blocks valid valuations during data outages | Tier severities; only true CRITICAL halts; warnings still pass |
| Lockfile drift vs `requirements.txt` | CI step regenerates lock; PR fails if uncommitted diff |
| GDPR / EU gating breaks ADR users in EU | Geofence by capability, not blanket block; offer EU-compliant alternative path |

---

## OUT-OF-SCOPE FOR V3 (deferred to V4+)

- LLM-narrated thesis (cost + non-determinism — V2 already deferred this)
- Real-time streaming quotes (websocket infra)
- Mobile native apps (PWA via W5.4 / W15.* sufficient near-term)
- User accounts with payment (free tier first; monetisation post product-market fit)
- Multi-currency portfolio attribution (after W15.1 lands)

---

*Source: second-pass codebase audit performed 2026-05-05 by an `Explore` subagent against [auto_valuation/](auto_valuation/), [webapp/](webapp/), [tests/](tests/), [requirements.txt](requirements.txt), [vercel.json](vercel.json). 42 distinct gaps identified that are not addressed by [NELIX_V2_IMPROVEMENT_PLAN.md](NELIX_V2_IMPROVEMENT_PLAN.md).*
