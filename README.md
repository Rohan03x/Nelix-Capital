# Automated Valuation System

A Python-based, IB-grade DCF + Comparable Companies + Football Field valuation system for public equities.
Outputs a fully formatted Excel workbook with all standard investment banking analysis sheets.

---

## Features

- **DCF with mid-year convention** — 5-year UFCF forecast + Gordon Growth / exit-multiple terminal value
- **Comparable companies** — automated peer screening, LTM & NTM multiples, pro forma adjustments
- **Precedent transactions** — deal-level EV/EBITDA and EV/Revenue multiples
- **Football field chart** — DCF + Comps + Transactions + 52-week trading range
- **Sensitivity grid** — 12×9 WACC × terminal growth grid
- **Scenario analysis** — bull / base / bear side-by-side
- **Monte Carlo DCF** — 10,000-simulation equity-value distribution
- **20+ validation checks** — auto-flagged in a dedicated Excel sheet
- **Batch mode** — run across a list of tickers from a CSV or JSON file

---

## Requirements

- Python 3.10+
- FMP (Financial Modeling Prep) API key — [get a free key](https://financialmodelingprep.com)
- FRED API key (optional, for live risk-free rate) — [get a free key](https://fred.stlouisfed.org/docs/api/api_key.html)

---

## Installation

```bash
# 1. Clone or download
cd auto_valuation_system

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API keys
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
# Then edit .env and add your FMP_API_KEY
```

---

## Usage

```bash
# Single ticker — base case
python main.py --ticker AAPL

# Bull scenario with a custom override file
python main.py --ticker NKE --scenario bull --override overrides/NKE.json

# Override terminal growth and forecast years on the CLI
python main.py --ticker MSFT --terminal-growth 0.03 --forecast-years 7

# Batch mode — run all tickers in a file
python main.py --batch tickers.csv

# Send completed workbook by email
python main.py --ticker AAPL --email analyst@firm.com

# Verbose debug output
python main.py --ticker AAPL --verbose
```

Output Excel files are written to `output/TICKER_YYYY-MM-DD_v1.0.xlsx`.

---

## Override File

Copy `overrides/EXAMPLE.json` to `overrides/TICKER.json` and customise any assumptions.
The override file supports per-ticker terminal growth, tax rate, peer list, precedent transactions,
and pro forma adjustments. See the file for full schema documentation.

---

## Project Structure

```
auto_valuation/
├── config.py              — Constants and 4-layer config system
├── main.py                — CLI entry point and ValuationResult
├── data/                  — FMP / yfinance / FRED data fetching and cleaning
├── model/                 — UFCF, rollforwards, DCF, EV bridge, ratios
├── assumptions/           — WACC computation
├── forecast/              — Revenue and reinvestment forecasting
├── sensitivity/           — Sensitivity grid, tornado, Monte Carlo
├── validation/            — Post-model quality checks
├── output/                — Excel workbook writer and charts
├── utils/                 — Logging, error handling
├── overrides/             — Per-ticker JSON override files
├── tests/                 — pytest unit and integration tests
├── output/                — Generated Excel / PDF files (gitignored)
└── logs/                  — Audit trail JSON-lines (gitignored)
```

---

## Architecture

Full architecture specification: `Automated Valuation System - Architecture Plan.docx`
(Parts 1–80, ~250 KB). All implementation decisions trace back to a specific Part.

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

## Shared-Brain Validation

The live Flask dashboard now surfaces a `knowledge_model` block for EODHD-backed payloads, and the repository ships with two verification entry points for the shared-brain layer.

```bash
python validate_shared_brain.py --no-diagnostics
python check.py --strict-learning
```

`validate_shared_brain.py` runs a deterministic, time-aware out-of-sample benchmark against the baseline model and reports MAE deltas for revenue growth, EBIT margin, UFCF error, and valuation error, along with an acceptance verdict.

`check.py` now validates both the live dashboard payload contract and the local learning/maintenance diagnostics. It reports hard failures separately from honest warnings such as thin calibration evidence or missing postmortems.

The latest recorded benchmark and operational snapshot are documented in `SHARED_BRAIN_VALIDATION.md`. The repo keeps the acceptance verdict conservative: the packaged benchmark is now provisionally better than baseline, but full acceptance still depends on thicker live postmortem evidence.

---

## Free Deployment

This repository is now wired for deployment on Vercel Hobby, which is free for a personal or small internal app and supports Flask directly.

### What was added

- Root `app.py` entrypoint so Vercel can detect the Flask app
- `build.py` to copy `webapp/static/` into `public/static/` for CDN serving
- `vercel.json` to run the build step automatically
- `.vercelignore` to keep local artifacts, tests, and generated files out of the deployment bundle

### Deploy steps

1. Push this repository to GitHub.
2. Create a free Vercel account and import the repository.
3. In Vercel project settings, add these environment variables:
	- `FLASK_SECRET` — required so Flask sessions remain stable across requests
	- `EODHD_API_KEY` — optional override; the app has a fallback key in code today
	- `FMP_API_KEY` — optional, enables the FMP fallback client
	- `FRED_API_KEY` — optional, enables live macro data
	- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` — only if you use email delivery features
4. Deploy. Vercel will install `requirements.txt`, run `python build.py`, and publish the Flask app.

### Notes

- Static files are served from `public/static/` on Vercel, while templates continue to render normally from Flask.
- Runtime cache writes may not persist on serverless instances. The web app still works because cache writes already fail gracefully.
- The free Hobby tier is suitable for demos, internal sharing, and light usage. Heavy always-on production traffic should use a paid host.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unhandled error |
| 2 | Data fetch failure |
| 3 | Data quality failure |
| 4 | Unsupported company type (Financials, Mining) |
| 5 | Configuration error (missing API key, bad override file) |

---

## Supported Companies

DCF valuation works for: non-financial, non-mining, non-early-stage public companies
with at least 3 years of reported financials. See Architecture Plan Part 10 for full scope.

**Not supported** (returns exit code 4):
- Banks, insurance, diversified financials (GICS sector 40)
- Mining / resource extraction companies
- SPACs, shell companies, pre-revenue biotech

---

## License

Internal use only. Not for redistribution.
