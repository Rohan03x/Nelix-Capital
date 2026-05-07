"""
learning/train_regime_classifier.py — Offline training script for LightGBM regime classifier.

Trains on existing prediction_records in the ledger DB to learn which
revenue-trajectory features predict the correct terminal growth regime.

Usage:
    python -m auto_valuation.learning.train_regime_classifier
    python -m auto_valuation.learning.train_regime_classifier --db path/to/predictions.db

The trained model bundle is saved to:
    auto_valuation/learning/data/regime_classifier.pkl
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_OUTPUT_PATH = Path(__file__).parent / "data" / "regime_classifier.pkl"
_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

from auto_valuation.assumptions.headwind_table import (
    classify_revenue_regime,
    get_industry_headwind_score,
    terminal_g_prior_range,
)
from auto_valuation.learning.regime_classifier import REGIME_LABELS, _build_feature_vector
from auto_valuation.learning.storage_paths import learning_db_dir


def _load_prediction_records(db_path: Path) -> list[dict[str, Any]]:
    """Load prediction_records and join with calibration_observations for actual TG labels."""
    if not db_path.exists():
        logger.warning("Predictions DB not found at %s", db_path)
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT record_id, ticker, sector, industry, near_term_revenue_growth,
                   predicted_terminal_growth, predicted_wacc, beta, rf_rate,
                   data_vintage_years, feature_vector_json, market_cap_regime,
                   macro_regime, run_date
            FROM prediction_records
            WHERE near_term_revenue_growth IS NOT NULL
              AND scenario = 'base'
            ORDER BY created_at ASC
            """
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        records.append(dict(row))
    logger.info("Loaded %d prediction records", len(records))

    # Also load calibration_observations for actual TG lookup (by ticker + year)
    cal_db_path = db_path.parent.parent / "db" / "calibration.db"
    if not cal_db_path.exists():
        # Try sibling db directory
        cal_db_path = db_path.parent / "calibration.db"
    actual_tg_by_ticker: dict[str, list[float]] = {}
    if cal_db_path.exists():
        try:
            with sqlite3.connect(cal_db_path) as cconn:
                cconn.row_factory = sqlite3.Row
                cal_rows = cconn.execute(
                    "SELECT ticker, actual_terminal_growth FROM calibration_observations "
                    "WHERE actual_terminal_growth IS NOT NULL"
                ).fetchall()
            for row in cal_rows:
                t = str(row["ticker"] or "").upper()
                if t:
                    actual_tg_by_ticker.setdefault(t, []).append(float(row["actual_terminal_growth"]))
            logger.info("Loaded actual TG for %d tickers from calibration DB", len(actual_tg_by_ticker))
        except Exception as exc:
            logger.warning("Could not load calibration observations: %s", exc)

    # Attach best actual_terminal_growth to records
    for record in records:
        ticker = str(record.get("ticker") or "").upper()
        actuals = actual_tg_by_ticker.get(ticker)
        if actuals:
            # Use median of observed actual TG values for this ticker
            sorted_actuals = sorted(actuals)
            n = len(sorted_actuals)
            record["actual_terminal_growth"] = sorted_actuals[n // 2]
        else:
            record["actual_terminal_growth"] = None

    return records


def _label_from_actual_terminal_g(actual_tg: float | None) -> str | None:
    """Convert an actual_terminal_growth value to a regime label."""
    if actual_tg is None:
        return None
    if actual_tg < -0.04:
        return "structural_decline"
    if actual_tg < -0.01:
        return "mild_decline"
    if actual_tg < 0.03:
        return "stable"
    if actual_tg < 0.05:
        return "moderate_growth"
    return "strong_growth"


def _extract_training_row(record: dict[str, Any]) -> tuple[dict[str, float], str] | None:
    """Extract (feature_vector, label) from a prediction_record.
    Uses actual_terminal_growth (from calibration_observations) when available,
    falling back to predicted_terminal_growth as a proxy label.
    """
    # Prefer actual TG from calibration observations; fall back to predicted
    tg = record.get("actual_terminal_growth") or record.get("predicted_terminal_growth")
    label = _label_from_actual_terminal_g(tg)
    if label is None:
        return None

    # Extract features
    cagr_5 = record.get("near_term_revenue_growth")   # near-term CAGR proxy
    cagr_3 = cagr_5                                     # same field — best available
    cagr_10: float | None = None
    ntm: float | None = cagr_5                         # proxy
    mig: float | None = record.get("predicted_terminal_growth")
    break_score = 0.0

    # Try to extract from feature_vector_json for richer features
    fvj = record.get("feature_vector_json")
    if fvj:
        try:
            fv_dict = json.loads(fvj) if isinstance(fvj, str) else fvj
            cagr_3 = float(fv_dict.get("hist_cagr_3yr") or fv_dict.get("hist_cagr_5yr") or cagr_3 or 0.0)
            cagr_5 = float(fv_dict.get("hist_cagr_5yr") or cagr_5 or 0.0)
            cagr_10 = fv_dict.get("hist_cagr_10yr")
            ntm = fv_dict.get("ntm_consensus_growth") or ntm
            mig = fv_dict.get("market_implied_g") or mig
            break_score = float(fv_dict.get("structural_break_score") or 0.0)
        except Exception:
            pass

    if cagr_5 is None and cagr_3 is None:
        return None

    industry = str(record.get("industry") or "")
    headwind = get_industry_headwind_score(industry)
    wacc = record.get("predicted_wacc")

    fv = _build_feature_vector(
        cagr_3, cagr_5, cagr_10, ntm, mig,
        break_score, headwind, 0.0, 0.0, wacc=wacc,
    )
    return fv, label


def train(db_path: Path) -> None:
    """Main training entry point."""
    try:
        import numpy as np
    except ImportError:
        logger.error("numpy is required: pip install numpy")
        return
    try:
        import lightgbm as lgb
    except ImportError:
        logger.warning("LightGBM not installed. Falling back to sklearn RandomForest.")
        lgb = None

    records = _load_prediction_records(db_path)
    if not records:
        logger.error("No prediction data found; cannot train classifier")
        return

    rows: list[tuple[dict[str, float], str]] = []
    for record in records:
        result = _extract_training_row(record)
        if result is not None:
            rows.append(result)

    if len(rows) < 50:
        logger.error("Insufficient labelled rows (%d); need at least 50 to train", len(rows))
        return

    logger.info("Training on %d labelled samples", len(rows))

    feature_names = sorted(rows[0][0].keys())
    X = np.array([[r.get(f, 0.0) for f in feature_names] for r, _ in rows], dtype=float)
    y_labels = [label for _, label in rows]
    y_int = [REGIME_LABELS.index(label) if label in REGIME_LABELS else 2 for label in y_labels]
    y = np.array(y_int, dtype=int)

    # Sanity check: class distribution
    from collections import Counter
    dist = Counter(y_labels)
    logger.info("Class distribution: %s", dict(dist))

    if lgb is not None:
        model = lgb.LGBMClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            class_weight="balanced",
            random_state=42,
            verbose=-1,
        )
    else:
        try:
            from sklearn.ensemble import RandomForestClassifier
            model = RandomForestClassifier(
                n_estimators=200,
                max_depth=6,
                class_weight="balanced",
                random_state=42,
            )
            logger.info("Using RandomForest as LightGBM fallback")
        except ImportError:
            logger.error("Neither LightGBM nor scikit-learn is installed; cannot train")
            return

    # Train / eval split (80/20 time-ordered)
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    model.fit(X_train, y_train)
    val_acc = (model.predict(X_val) == y_val).mean() if len(X_val) > 0 else float("nan")
    logger.info("Validation accuracy: %.1f%%", val_acc * 100)

    bundle = {
        "model": model,
        "feature_names": feature_names,
        "labels": REGIME_LABELS,
        "n_train": len(X_train),
        "val_accuracy": float(val_acc),
    }
    with open(_OUTPUT_PATH, "wb") as fh:
        pickle.dump(bundle, fh)
    logger.info("Model saved to %s", _OUTPUT_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train regime classifier")
    parser.add_argument("--db", type=Path, default=None, help="Path to predictions.db (default: auto-detect)")
    args = parser.parse_args()

    db_path = args.db or (learning_db_dir() / "predictions.db")
    train(db_path)


if __name__ == "__main__":
    main()

