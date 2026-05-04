"""
Deep historical replay — close every available annual & quarterly financial window
for all cached tickers and feed the resulting CalibrationObservations directly into
the calibration store.

Why: the existing live_evidence_bootstrap caps replay at 5-6 years per ticker and
only processes ~54 valued tickers.  This module scans all 3,781+ cached fundamentals
files, generates prediction-vs-actual pairs for FY2016-FY2025 (annual) and every
consecutive quarter, and updates the sector/industry/cap calibration priors immediately
— no prediction-maturation latency.

Design: the "prediction" for each historical period uses the persistence model:
  - predicted_revenue_growth = 0.0 (neutral; learns the sector's inherent bias)
  - predicted_ebit_margin   = prior year's EBIT margin (margin persistence)
  - predicted_wacc          = sector WACC default (unobservable → zero error contribution)
  - predicted_beta          = 1.0 (market average)
  - predicted_terminal_growth = 0.025 (default)

The "actual" values come from the next period's realised financials.  The systematic
error  (actual - predicted) becomes the calibration signal.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time as _time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from auto_valuation.config import LEARNING_CONFIG
from auto_valuation.learning._layered_calibrator import (
    CalibrationObservation,
    CalibrationPrior,
    CalibrationStore,
    maturity_bucket,
)
from auto_valuation.learning.maintenance import (
    _annual_periods_by_year,
    _optional_float,
    _parse_date_value,
)

logger = logging.getLogger(__name__)

WEBAPP_CACHE_DIR = Path(__file__).resolve().parents[2] / "webapp" / "data" / "cache"

# Sector WACC defaults used as the "predicted_wacc" baseline.
# Actual WACC is unobservable from financials alone so we use the same value for both
# → zero WACC error contribution; only revenue_growth and ebit_margin generate signal.
_SECTOR_WACC: dict[str, float] = {
    "Technology": 0.095,
    "Consumer Cyclical": 0.088,
    "Consumer Defensive": 0.082,
    "Consumer Staples": 0.082,
    "Communication Services": 0.090,
    "Healthcare": 0.086,
    "Health Care": 0.086,
    "Industrials": 0.084,
    "Energy": 0.091,
    "Materials": 0.087,
    "Financial Services": 0.093,
    "Real Estate": 0.081,
    "Utilities": 0.077,
}
_DEFAULT_WACC = 0.090
_DEFAULT_BETA = 1.0
_DEFAULT_TGR = 0.025


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _cap_regime(market_cap_usd: float | None) -> str:
    if market_cap_usd is None or market_cap_usd <= 0:
        return "mid"
    if market_cap_usd >= 10_000_000_000:
        return "large"
    if market_cap_usd >= 2_000_000_000:
        return "mid"
    return "small"


def _annual_snapshots(
    fundamentals: dict[str, Any],
    *,
    cutoff: date,
) -> dict[int, dict[str, float | None]]:
    """Return {year: {revenue_mm, ebit_margin, ufcf_margin}} from annual financials."""
    fin = dict(fundamentals.get("Financials") or {})
    income_by_yr = _annual_periods_by_year(
        dict((fin.get("Income_Statement") or {}).get("yearly") or {}),
        as_of_date=cutoff,
    )
    cf_by_yr = _annual_periods_by_year(
        dict((fin.get("Cash_Flow") or {}).get("yearly") or {}),
        as_of_date=cutoff,
    )

    snaps: dict[int, dict[str, float | None]] = {}
    for year in sorted(set(income_by_yr) | set(cf_by_yr)):
        income = dict(income_by_yr.get(year) or {})
        cf = dict(cf_by_yr.get(year) or {})
        rev = _optional_float(income.get("totalRevenue"))
        if not rev or rev <= 0:
            continue
        ebit = _optional_float(income.get("ebit")) or _optional_float(income.get("operatingIncome"))
        ebit_m = (ebit / rev) if ebit is not None else None
        fcf = _optional_float(cf.get("freeCashFlow"))
        if fcf is None:
            op_cf = _optional_float(cf.get("totalCashFromOperatingActivities"))
            capex = _optional_float(cf.get("capitalExpenditures"))
            if op_cf is not None and capex is not None:
                fcf = op_cf - abs(capex)
        ufcf_m = (fcf / rev) if (fcf is not None and rev > 0) else None
        snaps[year] = {"revenue_mm": rev / 1e6, "ebit_margin": ebit_m, "ufcf_margin": ufcf_m}
    return snaps


def _quarterly_snapshots(
    fundamentals: dict[str, Any],
    *,
    cutoff: date,
) -> list[dict[str, Any]]:
    """Return time-ordered quarterly snapshots."""
    fin = dict(fundamentals.get("Financials") or {})
    income_q = dict((fin.get("Income_Statement") or {}).get("quarterly") or {})
    cf_q = dict((fin.get("Cash_Flow") or {}).get("quarterly") or {})

    quarters: dict[str, dict[str, Any]] = {}
    for date_str, period in income_q.items():
        pd = _parse_date_value(date_str)
        if pd is None or pd > cutoff:
            continue
        period = dict(period or {})
        rev = _optional_float(period.get("totalRevenue"))
        if not rev or rev <= 0:
            continue
        ebit = _optional_float(period.get("ebit")) or _optional_float(period.get("operatingIncome"))
        quarters[date_str] = {
            "date": pd,
            "revenue_mm": rev / 1e6,
            "ebit_margin": (ebit / rev) if ebit is not None else None,
            "ufcf_margin": None,
        }
    for date_str, period in cf_q.items():
        if date_str not in quarters:
            continue
        period = dict(period or {})
        rev_mm = quarters[date_str].get("revenue_mm") or 0.0
        if rev_mm <= 0:
            continue
        fcf = _optional_float(period.get("freeCashFlow"))
        if fcf is None:
            op_cf = _optional_float(period.get("totalCashFromOperatingActivities"))
            capex = _optional_float(period.get("capitalExpenditures"))
            if op_cf is not None and capex is not None:
                fcf = op_cf - abs(capex)
        if fcf is not None:
            quarters[date_str]["ufcf_margin"] = fcf / (rev_mm * 1e6)

    return sorted(quarters.values(), key=lambda q: q["date"])


# ─── Core observation builder ─────────────────────────────────────────────────

def observations_for_ticker(
    ticker: str,
    fundamentals: dict[str, Any],
    *,
    start_year: int = 2016,
    quarterly: bool = True,
    as_of_date: date | None = None,
) -> list[CalibrationObservation]:
    """
    Build closed CalibrationObservation objects for every consecutive historical
    period pair available for this ticker.  Returns immediately — no API calls.
    """
    cutoff = as_of_date or date.today()
    general = dict(fundamentals.get("General") or {})
    sector = str(general.get("Sector") or "")
    industry = str(general.get("Industry") or "")
    # MarketCapitalization lives in Highlights, not General
    highlights = dict(fundamentals.get("Highlights") or {})
    mktcap_raw = _optional_float(str(highlights.get("MarketCapitalization") or "")) or \
                 _optional_float(str(general.get("MarketCapitalization") or ""))
    cap = _cap_regime(mktcap_raw)
    wacc0 = _SECTOR_WACC.get(sector, _DEFAULT_WACC)

    ann = _annual_snapshots(fundamentals, cutoff=cutoff)
    years = sorted(ann)
    vintage = len(years)

    obs: list[CalibrationObservation] = []

    # Annual pairs ────────────────────────────────────────────────────────────
    for i, year in enumerate(years):
        if year < start_year or i == 0:
            continue
        prev, curr = ann[years[i - 1]], ann[year]
        if prev["revenue_mm"] <= 0 or curr["revenue_mm"] <= 0:
            continue
        actual_rg = (curr["revenue_mm"] - prev["revenue_mm"]) / prev["revenue_mm"]
        actual_em = curr["ebit_margin"]
        pred_em = prev["ebit_margin"]          # persistence model
        if actual_em is None or pred_em is None:
            continue
        obs.append(CalibrationObservation(
            sector=sector,
            industry=industry,
            data_vintage_years=min(vintage, 20),
            market_cap_regime=cap,
            macro_regime="neutral",
            predicted_revenue_growth=0.0,          # neutral; signal = sector's inherent bias
            actual_revenue_growth=float(actual_rg),
            predicted_ebit_margin=float(pred_em),  # persistence model
            actual_ebit_margin=float(actual_em),
            predicted_wacc=wacc0,
            actual_wacc=wacc0,                     # unobservable → zero WACC error
            predicted_terminal_growth=_DEFAULT_TGR,
            actual_terminal_growth=_DEFAULT_TGR,
            predicted_beta=_DEFAULT_BETA,
            actual_beta=_DEFAULT_BETA,
            ticker=ticker,
            predicted_ufcf_margin=prev.get("ufcf_margin"),
            actual_ufcf_margin=curr.get("ufcf_margin"),
        ))

    # Quarterly pairs ─────────────────────────────────────────────────────────
    if quarterly:
        qs = _quarterly_snapshots(fundamentals, cutoff=cutoff)
        for i in range(1, len(qs)):
            prev_q, curr_q = qs[i - 1], qs[i]
            if curr_q["date"].year < start_year:
                continue
            pv, cv = prev_q.get("revenue_mm") or 0.0, curr_q.get("revenue_mm") or 0.0
            if pv <= 0 or cv <= 0:
                continue
            actual_rg_q = ((cv - pv) / pv) * 4.0   # annualise QoQ
            actual_em_q = curr_q.get("ebit_margin")
            pred_em_q = prev_q.get("ebit_margin")
            if actual_em_q is None or pred_em_q is None:
                continue
            obs.append(CalibrationObservation(
                sector=sector,
                industry=industry,
                data_vintage_years=min(vintage, 20),
                market_cap_regime=cap,
                macro_regime="neutral",
                predicted_revenue_growth=0.0,
                actual_revenue_growth=float(actual_rg_q),
                predicted_ebit_margin=float(pred_em_q),
                actual_ebit_margin=float(actual_em_q),
                predicted_wacc=wacc0,
                actual_wacc=wacc0,
                predicted_terminal_growth=_DEFAULT_TGR,
                actual_terminal_growth=_DEFAULT_TGR,
                predicted_beta=_DEFAULT_BETA,
                actual_beta=_DEFAULT_BETA,
                ticker=ticker,
            ))
    return obs


# ─── Calibration store updater ────────────────────────────────────────────────

def update_calibration_from_observations(
    observations: list[CalibrationObservation],
    calibration_store: CalibrationStore | None = None,
) -> int:
    """
    Compute correction_mean / correction_std per cohort bucket and save priors.
    Returns number of prior rows saved.
    """
    if not observations:
        return 0
    store = calibration_store or CalibrationStore()

    # Group by (sector, industry, maturity_bucket, cap_regime, macro_regime)
    groups: dict[tuple, list[CalibrationObservation]] = defaultdict(list)
    for o in observations:
        key = (o.sector, o.industry, maturity_bucket(o.data_vintage_years), o.market_cap_regime, o.macro_regime)
        groups[key].append(o)

    saved = 0
    today = date.today()
    specs = [
        ("revenue_growth", "actual_revenue_growth", "predicted_revenue_growth"),
        ("ebit_margin",    "actual_ebit_margin",    "predicted_ebit_margin"),
        ("ufcf_margin",    "actual_ufcf_margin",    "predicted_ufcf_margin"),
    ]
    for (sector, industry, bucket, cap, macro), cohort in groups.items():
        for assumption_name, actual_key, pred_key in specs:
            errors = [
                getattr(o, actual_key) - getattr(o, pred_key)
                for o in cohort
                if getattr(o, actual_key) is not None and getattr(o, pred_key) is not None
            ]
            if not errors:
                continue
            corr_mean = mean(errors)
            corr_std = pstdev(errors) if len(errors) >= 2 else 0.0
            store.save_prior(CalibrationPrior(
                prior_id=str(uuid.uuid4()),
                sector=sector,
                industry=industry,
                maturity_bucket=bucket,
                cap_regime=cap,
                macro_regime=macro,
                assumption_name=assumption_name,
                correction_mean=corr_mean,
                correction_std=corr_std,
                cohort_size=len(errors),
                last_updated=today,
            ))
            saved += 1
    return saved


# ─── Single-ticker replay ─────────────────────────────────────────────────────

def replay_ticker_history(
    ticker: str,
    fundamentals: dict[str, Any],
    *,
    calibration_store: CalibrationStore | None = None,
    start_year: int = 2016,
    quarterly: bool = True,
) -> dict[str, int]:
    """Generate and save calibration signal for one ticker. Returns stats dict."""
    obs = observations_for_ticker(ticker, fundamentals, start_year=start_year, quarterly=quarterly)
    store = calibration_store or CalibrationStore()
    saved = update_calibration_from_observations(obs, store)
    return {"ticker": ticker, "observations": len(obs), "priors_saved": saved}


# ─── Full universe scan ───────────────────────────────────────────────────────

def _fundamentals_from_cache_file(path: Path) -> tuple[str, dict[str, Any]] | None:
    """Load ticker + fundamentals from an eodhd_fund_*.json cache file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(data, dict):
        return None
    general = data.get("General")
    if not isinstance(general, dict):
        return None
    code = str(general.get("Code") or "").strip().upper()
    exchange = str(general.get("Exchange") or "").strip().upper()
    if not code:
        return None
    # US exchanges: use bare code to match the live model's ticker format.
    # International exchanges: use code.exchange (e.g. "ASML.AS", "005930.KO").
    _US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "NYSE MKT", "NYSE ARCA", "BATS", "OTC", "CBOE", "US", "PINK"}
    ticker = code if exchange in _US_EXCHANGES else (f"{code}.{exchange}" if exchange and "." not in code else code)
    return ticker, data


def run_full_universe_replay(
    *,
    max_tickers: int | None = None,
    start_year: int = 2016,
    quarterly: bool = True,
    checkpoint_every: int = 10,
    max_workers: int | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Scan all cached fundamentals files and replay historical periods for every
    cached ticker.  Uses a ThreadPoolExecutor sized to os.cpu_count() to process
    multiple tickers in parallel (pure computation — no API calls).

    Args:
        max_tickers: cap the number of tickers processed (None = all cached).
        start_year: earliest fiscal year to include (default 2016).
        quarterly: also generate QoQ observations (default True).
        checkpoint_every: save priors to SQLite after this many tickers.
        max_workers: thread pool size (default os.cpu_count()).
        cache_dir: override WEBAPP_CACHE_DIR (for testing).

    Returns a summary dict with total counts.
    """
    scan_dir = cache_dir or WEBAPP_CACHE_DIR
    if not scan_dir.exists():
        return {"enabled": False, "reason": "cache_dir_missing", "cache_dir": str(scan_dir)}

    fund_files = sorted(
        scan_dir.glob("eodhd_fund_*.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    if max_tickers and max_tickers > 0:
        fund_files = fund_files[:max_tickers]

    if not fund_files:
        return {"enabled": True, "reason": "no_cache_files", "tickers_processed": 0}

    store = CalibrationStore()
    workers = max(1, min(max_workers or (os.cpu_count() or 4), 16))
    started_at = datetime.now(timezone.utc)

    total_obs = 0
    total_saved = 0
    total_tickers = 0
    errors: list[dict] = []

    # Collect all observations, flush to store in batches
    batch_obs: list[CalibrationObservation] = []

    def process_file(path: Path) -> tuple[str, list[CalibrationObservation]] | None:
        result = _fundamentals_from_cache_file(path)
        if result is None:
            return None
        ticker, fundamentals = result
        try:
            obs = observations_for_ticker(
                ticker, fundamentals,
                start_year=start_year,
                quarterly=quarterly,
            )
        except Exception as exc:
            logger.debug("historical_replay: %s failed: %s", ticker, exc)
            return None
        return ticker, obs

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_file, f): f for f in fund_files}
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            if result is None:
                continue
            ticker, obs = result
            batch_obs.extend(obs)
            total_obs += len(obs)
            total_tickers += 1

            # Checkpoint: flush observations to the calibration store
            if total_tickers % checkpoint_every == 0 and batch_obs:
                saved = update_calibration_from_observations(batch_obs, store)
                total_saved += saved
                batch_obs.clear()
                logger.info(
                    "historical_replay checkpoint: %d tickers, %d obs, %d priors",
                    total_tickers, total_obs, total_saved,
                )

    # Final flush
    if batch_obs:
        saved = update_calibration_from_observations(batch_obs, store)
        total_saved += saved

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    result = {
        "enabled": True,
        "ran": True,
        "tickers_processed": total_tickers,
        "observations_generated": total_obs,
        "priors_saved": total_saved,
        "elapsed_seconds": round(elapsed, 1),
        "workers": workers,
        "start_year": start_year,
        "quarterly": quarterly,
        "errors": len(errors),
    }
    logger.info("historical_replay complete: %s", result)
    return result


# ─── In-process cache for get_all_observations() ─────────────────────────────

_OBS_CACHE: list[CalibrationObservation] = []
_OBS_CACHE_TS: float = 0.0
_OBS_CACHE_TTL: float = 3600.0  # rebuild at most once per hour


def get_all_observations(
    *,
    start_year: int = 2016,
    quarterly: bool = True,
    cache_dir: Path | None = None,
    max_workers: int | None = None,
    force_refresh: bool = False,
) -> list[CalibrationObservation]:
    """Return all historical CalibrationObservation objects from every cached
    fundamentals file.  Results are kept in module-level memory for
    *_OBS_CACHE_TTL* seconds so that repeated calls within a single process
    pay the I/O cost only once.

    Observations use ``data_vintage_years = min(available_annual_periods, 20)``
    which aligns with the live model's ``len(revenues)`` window.
    """
    global _OBS_CACHE, _OBS_CACHE_TS  # noqa: PLW0603

    now = _time.monotonic()
    if not force_refresh and _OBS_CACHE and (now - _OBS_CACHE_TS) < _OBS_CACHE_TTL:
        return list(_OBS_CACHE)

    scan_dir = cache_dir or WEBAPP_CACHE_DIR
    if not scan_dir.exists():
        return []

    fund_files = sorted(scan_dir.glob("eodhd_fund_*.json"))
    if not fund_files:
        return []

    workers = max(1, min(max_workers or (os.cpu_count() or 4), 16))
    all_obs: list[CalibrationObservation] = []

    def _process(path: Path) -> list[CalibrationObservation]:
        result = _fundamentals_from_cache_file(path)
        if result is None:
            return []
        ticker, fundamentals = result
        try:
            return observations_for_ticker(
                ticker, fundamentals, start_year=start_year, quarterly=quarterly
            )
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch in pool.map(_process, fund_files):
            all_obs.extend(batch)

    _OBS_CACHE = all_obs
    _OBS_CACHE_TS = now
    logger.info(
        "historical_replay.get_all_observations: %d observations from %d files",
        len(all_obs),
        len(fund_files),
    )
    return list(all_obs)


__all__ = [
    "get_all_observations",
    "observations_for_ticker",
    "replay_ticker_history",
    "run_full_universe_replay",
    "update_calibration_from_observations",
]
