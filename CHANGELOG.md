# Changelog

All notable changes to the Automated Valuation System are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- Compliance pages and global disclaimer footer for `/terms`, `/disclosures`, and `/privacy`.
- EU suitability gate for country-detected visitors, with a one-time research-use questionnaire stored in the anonymous session.
- GDPR workflow lifecycle controls: `DELETE /api/me` purges anonymous watchlist/search/compare workflow data, and internal privacy cleanup removes stale workflow rows older than 24 months.
- Shared-brain validation harness in `auto_valuation/validation/shared_brain.py` with deterministic, time-aware benchmark cases, operational diagnostics, and acceptance summarization.
- `validate_shared_brain.py` CLI for running the benchmark and exporting an explicit acceptance verdict.
- Live EODHD dashboard payload support for `knowledge_model` outputs, including knowledge-model assumption provenance in the web app payload.
- Health-check hardening in `check.py` so the repo can validate the live dashboard contract and local learning-ledger diagnostics in one command.
- Regression coverage for the live knowledge-model payload and the shared-brain validation harness.

### Fixed
- Restored the missing `auto_valuation.learning.calibrator` module required by the learning stack.
- Aligned `webapp/data/knowledge_model.py` with the canonical shared feature-space contract used by the analog and cross-symbol learning layers.
- Fixed local shared-brain wiring defects in `refine_live_assumptions()` that were preventing the EODHD path from surfacing knowledge-model output.

### Planned
- Phase 1: Data layer (FMP, yfinance, FRED fetchers; cleaning; TTM; FX)
- Phase 2: Core financial model (UFCF, rollforwards, ratios)
- Phase 3: WACC engine
- Phase 4: DCF engine (discounting, terminal value, EV bridge)
- Phase 5: Comparable companies and precedent transactions
- Phase 6: Sensitivity analysis, scenarios, Monte Carlo
- Phase 7: Sector-specific handling (REIT, financial gate)
- Phase 8: Validation & QC (20+ checks)
- Phase 9: Excel output layer (all sheets, football field, charts)
- Phase 10: Config, CLI, batch, error recovery
- Phase 11: Testing (unit + integration)
- Phase 12: Deployment (Docker, email, PDF, webhook)

---

## [1.0.0] — 2026-04-29

### Added
- Phase 0: Project scaffold and environment
  - `auto_valuation/` package structure with all subpackage stubs
  - `config.py`: global constants, `ValuationConfig` dataclass, 4-layer config hierarchy
    (global defaults → sector defaults → per-ticker JSON → CLI overrides)
  - `SECTOR_DEFAULTS` for 9 GICS sectors
  - `main.py`: argparse CLI with `--ticker`, `--batch`, `--scenario`, `--override`,
    `--terminal-growth`, `--wacc`, `--forecast-years`, `--email`, `--webhook`, `--verbose`
  - `ValuationResult` dataclass with full output contract (Part 70.1)
  - `utils/logging_utils.py`: structured JSON-lines audit trail + coloured console output
  - `utils/error.py`: custom exceptions (`ValuationError`, `DataFetchError`,
    `DataQualityError`, `UnsupportedCompanyError`, `ConfigError`) and recovery helpers
  - `requirements.txt`: pinned dependencies (pandas, numpy, scipy, openpyxl, yfinance,
    requests, matplotlib, python-dotenv, python-dateutil)
  - `.env.example`, `.gitignore`
  - `overrides/EXAMPLE.json`: full v4 override schema (Parts 31, 66.2)
  - `tests/conftest.py`: shared pytest fixtures with synthetic NKE financial data
  - `README.md`, `CHANGELOG.md`
  - `output/`, `logs/` directories
