"""
auto_valuation/learning/scenario_calibrator.py
───────────────────────────────────────────────
Scenario outcome calibration — the "which scenario won?" learning layer.

Architecture:
  1. RECORD  — when a valuation is produced, store base/bull/bear IVs + params
               in `scenario_outcomes` table with a future labeling date
  2. LABEL   — background runner checks matured outcomes (quarterly 91d, annual 365d)
               and records which scenario was closest to realized price
  3. BUILD   — aggregate labeled outcomes into `scenario_calibration_priors` per cohort
  4. QUERY   — `get_scenario_prior()` returns probability weights + recommended
               base-case scenario shift for a given cohort

The labeled outcomes feed two signals:
  A. Scenario probability weights (p_bear, p_base, p_bull) per cohort
  B. Base-case scenario shift: if bear fires 70% in a cohort, shift base toward bear

DB: auto_valuation/learning/db/scenario_outcomes.db (separate to avoid locking predictions.db)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from auto_valuation.learning.storage_paths import learning_db_dir

logger = logging.getLogger(__name__)

# ── DB setup ──────────────────────────────────────────────────────────────────

_DB_NAME = "scenario_outcomes.db"


def _scenario_db_path() -> Path:
    return learning_db_dir() / _DB_NAME


@contextmanager
def _get_conn(path: Path | None = None, timeout: float = 10.0) -> Generator[sqlite3.Connection, None, None]:
    db_path = path or _scenario_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_db(path: Path | None = None) -> None:
    with _get_conn(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scenario_outcomes (
                outcome_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT    NOT NULL,
                prediction_date     TEXT    NOT NULL,
                quarterly_label_date TEXT   NOT NULL,
                annual_label_date   TEXT    NOT NULL,

                -- Scenario IVs at prediction time
                base_iv             REAL,
                bull_iv             REAL,
                bear_iv             REAL,

                -- Scenario assumptions
                base_g              REAL,
                bull_g              REAL,
                bear_g              REAL,
                base_wacc           REAL,
                bull_wacc           REAL,
                bear_wacc           REAL,
                base_rev_growth     REAL,
                bull_rev_growth     REAL,
                bear_rev_growth     REAL,
                base_margin         REAL,
                bull_margin         REAL,
                bear_margin         REAL,
                base_probability    REAL,
                bull_probability    REAL,
                bear_probability    REAL,

                -- Market state at prediction time
                price_at_prediction REAL,

                -- Cohort dimensions for grouping
                sector              TEXT,
                industry            TEXT,
                macro_regime        TEXT,
                revenue_regime      TEXT,
                market_cap_regime   TEXT,

                -- Quarterly outcome (filled after 91 days)
                quarterly_realized_price  REAL,
                quarterly_winner          TEXT,   -- 'base','bull','bear','none'
                quarterly_base_err_pct    REAL,
                quarterly_bull_err_pct    REAL,
                quarterly_bear_err_pct    REAL,
                quarterly_labeled_at      TEXT,

                -- Annual outcome (filled after 365 days)
                annual_realized_price     REAL,
                annual_winner             TEXT,
                annual_base_err_pct       REAL,
                annual_bull_err_pct       REAL,
                annual_bear_err_pct       REAL,
                annual_labeled_at         TEXT,

                created_at          TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE INDEX IF NOT EXISTS idx_so_ticker           ON scenario_outcomes(ticker);
            CREATE INDEX IF NOT EXISTS idx_so_prediction_date  ON scenario_outcomes(prediction_date);
            CREATE INDEX IF NOT EXISTS idx_so_qlabel           ON scenario_outcomes(quarterly_label_date);
            CREATE INDEX IF NOT EXISTS idx_so_alabel           ON scenario_outcomes(annual_label_date);
            CREATE INDEX IF NOT EXISTS idx_so_sector_industry  ON scenario_outcomes(sector, industry);
            CREATE INDEX IF NOT EXISTS idx_so_q_winner         ON scenario_outcomes(quarterly_winner);
            CREATE INDEX IF NOT EXISTS idx_so_a_winner         ON scenario_outcomes(annual_winner);

            CREATE TABLE IF NOT EXISTS scenario_calibration_priors (
                prior_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cohort_key          TEXT    NOT NULL UNIQUE,
                horizon             TEXT    NOT NULL,   -- 'quarterly' or 'annual'
                sector              TEXT,
                industry            TEXT,
                macro_regime        TEXT,
                revenue_regime      TEXT,
                market_cap_regime   TEXT,

                -- Probability that each scenario won
                p_bear              REAL    DEFAULT 0.25,
                p_base              REAL    DEFAULT 0.50,
                p_bull              REAL    DEFAULT 0.25,

                -- Mean absolute price error per scenario
                mean_base_err_pct   REAL,
                mean_bull_err_pct   REAL,
                mean_bear_err_pct   REAL,

                -- Recommended probability shifts vs naive (p-0.333 for each)
                base_shift          REAL    DEFAULT 0.0,
                bull_shift          REAL    DEFAULT 0.0,
                bear_shift          REAL    DEFAULT 0.0,

                -- Bear bias pp: positive = bear fires more than 33%; negative = less
                bear_bias_pp        REAL    DEFAULT 0.0,

                n_observations      INTEGER DEFAULT 0,
                updated_at          TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE INDEX IF NOT EXISTS idx_scp_cohort  ON scenario_calibration_priors(cohort_key);
            CREATE INDEX IF NOT EXISTS idx_scp_horizon ON scenario_calibration_priors(horizon);
            CREATE INDEX IF NOT EXISTS idx_scp_sector  ON scenario_calibration_priors(sector, industry);
        """)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ScenarioPrior:
    """Learned scenario probability weights for a cohort."""
    p_bear: float
    p_base: float
    p_bull: float
    bear_bias_pp: float      # how much more/less bear fires vs 33%
    n_observations: int
    horizon: str             # 'quarterly' or 'annual'
    cohort_key: str
    # Recommended multiplier for scenario width: >1 = widen, <1 = narrow
    scenario_width_adj: float = 1.0
    # Recommended base-case probability adj for weighted expected upside calc
    base_shift: float = 0.0
    bull_shift: float = 0.0
    bear_shift: float = 0.0

    @property
    def confident(self) -> bool:
        return self.n_observations >= 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_bear": round(self.p_bear, 3),
            "p_base": round(self.p_base, 3),
            "p_bull": round(self.p_bull, 3),
            "bear_bias_pp": round(self.bear_bias_pp, 1),
            "n_observations": self.n_observations,
            "horizon": self.horizon,
            "cohort_key": self.cohort_key,
            "scenario_width_adj": round(self.scenario_width_adj, 3),
            "base_shift": round(self.base_shift, 3),
            "bull_shift": round(self.bull_shift, 3),
            "bear_shift": round(self.bear_shift, 3),
            "confident": self.confident,
        }


_NEUTRAL_PRIOR = ScenarioPrior(
    p_bear=0.25, p_base=0.50, p_bull=0.25,
    bear_bias_pp=0.0, n_observations=0,
    horizon="quarterly", cohort_key="neutral",
)


# ── Record a scenario prediction ─────────────────────────────────────────────

def record_scenario_prediction(
    *,
    ticker: str,
    base_iv: float,
    bull_iv: float,
    bear_iv: float,
    base_g: float,
    bull_g: float,
    bear_g: float,
    base_wacc: float,
    bull_wacc: float,
    bear_wacc: float,
    base_rev_growth: float,
    bull_rev_growth: float,
    bear_rev_growth: float,
    base_margin: float,
    bull_margin: float,
    bear_margin: float,
    base_probability: float,
    bull_probability: float,
    bear_probability: float,
    price_at_prediction: float,
    sector: str = "",
    industry: str = "",
    macro_regime: str = "neutral",
    revenue_regime: str = "stable",
    market_cap_regime: str = "mid_cap",
    db_path: Path | None = None,
) -> int | None:
    """
    Store a scenario prediction for future labeling.

    Returns the outcome_id or None on failure.
    """
    try:
        _migrate_db(db_path)
        now = datetime.now(timezone.utc)
        q_label = (now + timedelta(days=91)).date().isoformat()
        a_label = (now + timedelta(days=365)).date().isoformat()
        with _get_conn(db_path) as conn:
            cur = conn.execute("""
                INSERT INTO scenario_outcomes (
                    ticker, prediction_date, quarterly_label_date, annual_label_date,
                    base_iv, bull_iv, bear_iv,
                    base_g, bull_g, bear_g,
                    base_wacc, bull_wacc, bear_wacc,
                    base_rev_growth, bull_rev_growth, bear_rev_growth,
                    base_margin, bull_margin, bear_margin,
                    base_probability, bull_probability, bear_probability,
                    price_at_prediction,
                    sector, industry, macro_regime, revenue_regime, market_cap_regime
                ) VALUES (
                    ?,?,?,?,
                    ?,?,?,
                    ?,?,?,
                    ?,?,?,
                    ?,?,?,
                    ?,?,?,
                    ?,?,?,
                    ?,
                    ?,?,?,?,?
                )
            """, (
                ticker.upper(), now.date().isoformat(), q_label, a_label,
                base_iv, bull_iv, bear_iv,
                base_g, bull_g, bear_g,
                base_wacc, bull_wacc, bear_wacc,
                base_rev_growth, bull_rev_growth, bear_rev_growth,
                base_margin, bull_margin, bear_margin,
                base_probability, bull_probability, bear_probability,
                price_at_prediction,
                sector, industry, macro_regime, revenue_regime, market_cap_regime,
            ))
            return cur.lastrowid
    except Exception as exc:
        logger.debug("record_scenario_prediction failed for %s: %s", ticker, exc)
        return None


# ── Label matured outcomes ────────────────────────────────────────────────────

def _winner(base_iv: float, bull_iv: float, bear_iv: float, realized: float) -> tuple[str, float, float, float]:
    """Return (winner, base_err_pct, bull_err_pct, bear_err_pct)."""
    if realized <= 0:
        return "none", 0.0, 0.0, 0.0
    base_err = abs(base_iv - realized) / realized * 100
    bull_err = abs(bull_iv - realized) / realized * 100
    bear_err = abs(bear_iv - realized) / realized * 100
    min_err = min(base_err, bull_err, bear_err)
    if min_err == base_err:
        winner = "base"
    elif min_err == bull_err:
        winner = "bull"
    else:
        winner = "bear"
    return winner, base_err, bull_err, bear_err


def label_matured_outcomes(
    price_fetcher: Any,
    *,
    max_labels: int = 200,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Scan for unlabeled outcomes whose horizon date has passed and fetch
    the current price to label them.

    `price_fetcher(ticker) -> float | None` should return the current price
    in the appropriate currency. Pass the EODHD fetcher or a simple yfinance
    wrapper — the function only needs to return a float.

    Returns summary dict with counts labeled.
    """
    try:
        _migrate_db(db_path)
    except Exception as exc:
        return {"error": str(exc), "quarterly_labeled": 0, "annual_labeled": 0}

    today = datetime.now(timezone.utc).date().isoformat()
    q_labeled = 0
    a_labeled = 0
    errors = 0

    try:
        with _get_conn(db_path) as conn:
            # Quarterly — horizon passed, not yet labeled
            pending_q = conn.execute("""
                SELECT outcome_id, ticker,
                       base_iv, bull_iv, bear_iv
                FROM   scenario_outcomes
                WHERE  quarterly_label_date <= ?
                  AND  quarterly_winner IS NULL
                LIMIT  ?
            """, (today, max_labels)).fetchall()

        for row in pending_q:
            oid = row["outcome_id"]
            ticker = row["ticker"]
            try:
                price = price_fetcher(ticker)
                if price is None or price <= 0:
                    continue
                w, be, ble, bre = _winner(
                    row["base_iv"], row["bull_iv"], row["bear_iv"], price
                )
                with _get_conn(db_path) as conn:
                    conn.execute("""
                        UPDATE scenario_outcomes
                        SET    quarterly_realized_price = ?,
                               quarterly_winner         = ?,
                               quarterly_base_err_pct   = ?,
                               quarterly_bull_err_pct   = ?,
                               quarterly_bear_err_pct   = ?,
                               quarterly_labeled_at     = ?
                        WHERE  outcome_id = ?
                    """, (price, w, be, ble, bre, today, oid))
                q_labeled += 1
            except Exception as exc:
                logger.debug("quarterly label failed %s: %s", ticker, exc)
                errors += 1
            time.sleep(0.02)   # tiny throttle

        with _get_conn(db_path) as conn:
            pending_a = conn.execute("""
                SELECT outcome_id, ticker,
                       base_iv, bull_iv, bear_iv
                FROM   scenario_outcomes
                WHERE  annual_label_date <= ?
                  AND  annual_winner IS NULL
                LIMIT  ?
            """, (today, max_labels)).fetchall()

        for row in pending_a:
            oid = row["outcome_id"]
            ticker = row["ticker"]
            try:
                price = price_fetcher(ticker)
                if price is None or price <= 0:
                    continue
                w, be, ble, bre = _winner(
                    row["base_iv"], row["bull_iv"], row["bear_iv"], price
                )
                with _get_conn(db_path) as conn:
                    conn.execute("""
                        UPDATE scenario_outcomes
                        SET    annual_realized_price = ?,
                               annual_winner         = ?,
                               annual_base_err_pct   = ?,
                               annual_bull_err_pct   = ?,
                               annual_bear_err_pct   = ?,
                               annual_labeled_at     = ?
                        WHERE  outcome_id = ?
                    """, (price, w, be, ble, bre, today, oid))
                a_labeled += 1
            except Exception as exc:
                logger.debug("annual label failed %s: %s", ticker, exc)
                errors += 1
            time.sleep(0.02)

    except Exception as exc:
        return {"error": str(exc), "quarterly_labeled": 0, "annual_labeled": 0}

    return {
        "quarterly_labeled": q_labeled,
        "annual_labeled": a_labeled,
        "errors": errors,
    }


# ── Build scenario calibration priors ────────────────────────────────────────

def _cohort_key(
    horizon: str,
    sector: str,
    industry: str,
    macro_regime: str,
    revenue_regime: str,
    market_cap_regime: str,
) -> str:
    return f"{horizon}|{sector}|{industry}|{macro_regime}|{revenue_regime}|{market_cap_regime}"


def build_scenario_priors(
    *,
    min_observations: int = 10,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Aggregate labeled outcomes into scenario_calibration_priors.

    Runs through all labeled quarterly AND annual outcomes and groups by
    (horizon × sector × industry × macro_regime × revenue_regime × market_cap_regime).

    Falls back to broader cohorts (sector+macro only) when narrow cohort has
    fewer than min_observations entries.

    Returns summary dict.
    """
    try:
        _migrate_db(db_path)
    except Exception as exc:
        return {"error": str(exc), "cohorts_updated": 0}

    cohorts_updated = 0
    rows_processed = 0

    for horizon_col, winner_col, label_col in [
        ("quarterly_winner", "quarterly_winner", "quarterly_base_err_pct"),
        ("annual_winner",    "annual_winner",    "annual_base_err_pct"),
    ]:
        horizon = "quarterly" if "quarterly" in horizon_col else "annual"
        base_err_col  = f"{horizon}_base_err_pct"
        bull_err_col  = f"{horizon}_bull_err_pct"
        bear_err_col  = f"{horizon}_bear_err_pct"

        try:
            with _get_conn(db_path) as conn:
                rows = conn.execute(f"""
                    SELECT sector, industry, macro_regime, revenue_regime, market_cap_regime,
                           {winner_col}     AS winner,
                           {base_err_col}   AS base_err,
                           {bull_err_col}   AS bull_err,
                           {bear_err_col}   AS bear_err
                    FROM   scenario_outcomes
                    WHERE  {winner_col} IS NOT NULL
                      AND  {winner_col} != 'none'
                """).fetchall()
        except Exception as exc:
            logger.warning("build_scenario_priors query failed (%s): %s", horizon, exc)
            continue

        # Group by full cohort key first
        cohort_data: dict[str, list[dict]] = {}
        for r in rows:
            key = _cohort_key(
                horizon,
                r["sector"] or "",
                r["industry"] or "",
                r["macro_regime"] or "neutral",
                r["revenue_regime"] or "stable",
                r["market_cap_regime"] or "mid_cap",
            )
            cohort_data.setdefault(key, []).append({
                "winner": r["winner"],
                "base_err": r["base_err"] or 0.0,
                "bull_err": r["bull_err"] or 0.0,
                "bear_err": r["bear_err"] or 0.0,
                "sector": r["sector"] or "",
                "industry": r["industry"] or "",
                "macro_regime": r["macro_regime"] or "neutral",
                "revenue_regime": r["revenue_regime"] or "stable",
                "market_cap_regime": r["market_cap_regime"] or "mid_cap",
            })
            rows_processed += 1

        # Write or merge priors for each cohort
        for key, entries in cohort_data.items():
            n = len(entries)
            winners = [e["winner"] for e in entries]
            n_bear = winners.count("bear")
            n_base = winners.count("base")
            n_bull = winners.count("bull")
            total = max(n_bear + n_base + n_bull, 1)
            p_bear = n_bear / total
            p_base = n_base / total
            p_bull = n_bull / total
            bear_bias_pp = (p_bear - 0.333) * 100
            mean_base_err = sum(e["base_err"] for e in entries) / n
            mean_bull_err = sum(e["bull_err"] for e in entries) / n
            mean_bear_err = sum(e["bear_err"] for e in entries) / n
            # Probability shifts vs uniform 33%
            base_shift = p_base - 0.333
            bull_shift = p_bull - 0.333
            bear_shift = p_bear - 0.333
            # scenario_width_adj: if bear fires a lot, widen the spread
            # if bull fires a lot, also widen but differently  
            # base case → tighten (market agrees with model)
            if p_bear >= 0.50:
                width_adj = 1.0 + (p_bear - 0.50) * 1.5  # up to +0.75 wider
            elif p_bull >= 0.50:
                width_adj = 1.0 + (p_bull - 0.50) * 1.2
            else:
                width_adj = max(0.75, 1.0 - (p_base - 0.333) * 0.8)

            meta = entries[0]
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                with _get_conn(db_path) as conn:
                    conn.execute("""
                        INSERT INTO scenario_calibration_priors
                            (cohort_key, horizon, sector, industry,
                             macro_regime, revenue_regime, market_cap_regime,
                             p_bear, p_base, p_bull,
                             mean_base_err_pct, mean_bull_err_pct, mean_bear_err_pct,
                             base_shift, bull_shift, bear_shift,
                             bear_bias_pp, n_observations, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(cohort_key) DO UPDATE SET
                            p_bear             = excluded.p_bear,
                            p_base             = excluded.p_base,
                            p_bull             = excluded.p_bull,
                            mean_base_err_pct  = excluded.mean_base_err_pct,
                            mean_bull_err_pct  = excluded.mean_bull_err_pct,
                            mean_bear_err_pct  = excluded.mean_bear_err_pct,
                            base_shift         = excluded.base_shift,
                            bull_shift         = excluded.bull_shift,
                            bear_shift         = excluded.bear_shift,
                            bear_bias_pp       = excluded.bear_bias_pp,
                            n_observations     = excluded.n_observations,
                            updated_at         = excluded.updated_at
                    """, (
                        key, horizon,
                        meta["sector"], meta["industry"],
                        meta["macro_regime"], meta["revenue_regime"], meta["market_cap_regime"],
                        round(p_bear, 4), round(p_base, 4), round(p_bull, 4),
                        round(mean_base_err, 2), round(mean_bull_err, 2), round(mean_bear_err, 2),
                        round(base_shift, 4), round(bull_shift, 4), round(bear_shift, 4),
                        round(bear_bias_pp, 2), n, now_iso,
                    ))
                cohorts_updated += 1
            except Exception as exc:
                logger.debug("prior upsert failed %s: %s", key, exc)

    return {
        "cohorts_updated": cohorts_updated,
        "rows_processed": rows_processed,
    }


# ── Query scenario priors ─────────────────────────────────────────────────────

def get_scenario_prior(
    *,
    sector: str = "",
    industry: str = "",
    macro_regime: str = "neutral",
    revenue_regime: str = "stable",
    market_cap_regime: str = "mid_cap",
    horizon: str = "quarterly",
    min_observations: int = 10,
    db_path: Path | None = None,
) -> ScenarioPrior:
    """
    Return scenario probability priors for a given cohort.

    Lookup cascade (most specific → least specific):
      1. full key: horizon|sector|industry|macro|revenue_regime|market_cap
      2. sector+macro+revenue_regime (drop industry + market_cap)
      3. sector+revenue_regime only
      4. revenue_regime only
      5. neutral (return default)
    """
    try:
        _migrate_db(db_path)
    except Exception:
        return _NEUTRAL_PRIOR

    def _fetch(key: str) -> ScenarioPrior | None:
        try:
            with _get_conn(db_path) as conn:
                row = conn.execute("""
                    SELECT * FROM scenario_calibration_priors
                    WHERE cohort_key = ? AND horizon = ? AND n_observations >= ?
                """, (key, horizon, min_observations)).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        width_adj = 1.0
        p_bear = float(row["p_bear"] or 0.25)
        p_base = float(row["p_base"] or 0.50)
        p_bull = float(row["p_bull"] or 0.25)
        if p_bear >= 0.50:
            width_adj = 1.0 + (p_bear - 0.50) * 1.5
        elif p_bull >= 0.50:
            width_adj = 1.0 + (p_bull - 0.50) * 1.2
        else:
            width_adj = max(0.75, 1.0 - (p_base - 0.333) * 0.8)
        return ScenarioPrior(
            p_bear=p_bear,
            p_base=p_base,
            p_bull=p_bull,
            bear_bias_pp=float(row["bear_bias_pp"] or 0.0),
            n_observations=int(row["n_observations"] or 0),
            horizon=horizon,
            cohort_key=str(row["cohort_key"]),
            scenario_width_adj=round(width_adj, 3),
            base_shift=float(row["base_shift"] or 0.0),
            bull_shift=float(row["bull_shift"] or 0.0),
            bear_shift=float(row["bear_shift"] or 0.0),
        )

    # cascade lookups
    full_key = _cohort_key(horizon, sector, industry, macro_regime, revenue_regime, market_cap_regime)
    broad1 = _cohort_key(horizon, sector, "", macro_regime, revenue_regime, "")
    broad2 = _cohort_key(horizon, sector, "", "", revenue_regime, "")
    broad3 = _cohort_key(horizon, "", "", "", revenue_regime, "")

    for key in (full_key, broad1, broad2, broad3):
        result = _fetch(key)
        if result is not None:
            return result
    return _NEUTRAL_PRIOR


# ── Summary stats ─────────────────────────────────────────────────────────────

def get_scenario_calibration_summary(db_path: Path | None = None) -> dict[str, Any]:
    """Return overview stats for the scenario calibration system."""
    try:
        _migrate_db(db_path)
        with _get_conn(db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM scenario_outcomes").fetchone()[0]
            q_labeled = conn.execute(
                "SELECT COUNT(*) FROM scenario_outcomes WHERE quarterly_winner IS NOT NULL"
            ).fetchone()[0]
            a_labeled = conn.execute(
                "SELECT COUNT(*) FROM scenario_outcomes WHERE annual_winner IS NOT NULL"
            ).fetchone()[0]
            q_pending = conn.execute(
                "SELECT COUNT(*) FROM scenario_outcomes WHERE quarterly_winner IS NULL AND quarterly_label_date <= date('now')"
            ).fetchone()[0]
            a_pending = conn.execute(
                "SELECT COUNT(*) FROM scenario_outcomes WHERE annual_winner IS NULL AND annual_label_date <= date('now')"
            ).fetchone()[0]
            n_priors = conn.execute("SELECT COUNT(*) FROM scenario_calibration_priors").fetchone()[0]

            # winner distribution across quarterly
            q_dist = {r[0]: r[1] for r in conn.execute("""
                SELECT quarterly_winner, COUNT(*) FROM scenario_outcomes
                WHERE quarterly_winner IS NOT NULL GROUP BY quarterly_winner
            """).fetchall()}
            a_dist = {r[0]: r[1] for r in conn.execute("""
                SELECT annual_winner, COUNT(*) FROM scenario_outcomes
                WHERE annual_winner IS NOT NULL GROUP BY annual_winner
            """).fetchall()}
        return {
            "total_predictions_recorded": total,
            "quarterly_labeled": q_labeled,
            "annual_labeled": a_labeled,
            "quarterly_pending_labeling": q_pending,
            "annual_pending_labeling": a_pending,
            "calibration_priors_built": n_priors,
            "quarterly_winner_distribution": q_dist,
            "annual_winner_distribution": a_dist,
        }
    except Exception as exc:
        return {"error": str(exc)}


__all__ = [
    "record_scenario_prediction",
    "label_matured_outcomes",
    "build_scenario_priors",
    "get_scenario_prior",
    "get_scenario_calibration_summary",
    "ScenarioPrior",
]
