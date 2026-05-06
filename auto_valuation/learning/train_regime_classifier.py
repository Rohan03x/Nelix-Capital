"""
learning/train_regime_classifier.py — Offline training script for LightGBM regime classifier.

Trains on existing postmortem records in the ledger DB to learn which
revenue-trajectory features predict the correct terminal growth regime.

Usage:
    python -m auto_valuation.learning.train_regime_classifier
    python -m auto_valuation.learning.train_regime_classifier --db path/to/ledger.db

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


def _load_postmortems(db_path: Path) -> list[dict[str, Any]]:
    """Load all postmortem payloads from the ledger DB."""
    if not db_path.exists():
        logger.warning("Ledger DB not found at %s", db_path)
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT payload_json FROM postmortem_records ORDER BY created_at ASC"
        ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payloads.append(json.loads(row["payload_json"]))
        except Exception:
            continue
    logger.info("Loaded %d postmortem records", len(payloads))
    return payloads


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


def _extract_training_row(payload: dict[str, Any]) -> tuple[dict[str, float], str] | None:
    """
    Extract (feature_vector, label) from a postmortem payload.
    Returns None if insufficient data.
    """
    snap = payload.get("prediction_snapshot") or {}
    tg = payload.get("actual_terminal_growth") or snap.get("predicted_terminal_growth")
    label = _label_from_actual_terminal_g(tg)
    if label is None:
        return None

    # Best-effort feature extraction from postmortem snapshot
    # Fields may vary depending on what was stored
    cagr_5 = snap.get("historical_cagr") or snap.get("hist_cagr_5yr")
    cagr_3 = snap.get("historical_cagr_3yr") or cagr_5
    cagr_10 = snap.get("historical_cagr_10yr")
    ntm = snap.get("ntm_consensus_growth") or snap.get("near_term_growth")
    mig = snap.get("market_implied_g") or payload.get("actual_terminal_growth")
    break_score = float(payload.get("structural_break_score") or 0.0)
    industry = payload.get("industry") or snap.get("industry")
    headwind = get_industry_headwind_score(industry)
    rev_vol = float(snap.get("revenue_volatility") or 0.0)
    mar_vol = float(snap.get("margin_volatility") or 0.0)
    wacc = snap.get("predicted_wacc")

    if cagr_5 is None and cagr_3 is None:
        return None

    fv = _build_feature_vector(
        cagr_3, cagr_5, cagr_10, ntm, mig,
        break_score, headwind, rev_vol, mar_vol, wacc=wacc,
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

    payloads = _load_postmortems(db_path)
    if not payloads:
        logger.error("No postmortem data found; cannot train classifier")
        return

    rows: list[tuple[dict[str, float], str]] = []
    for payload in payloads:
        result = _extract_training_row(payload)
        if result is not None:
            rows.append(result)

    if len(rows) < 50:
        logger.error("Insufficient labelled rows (%d); need at least 50 to train", len(rows))
        return

    logger.info("Training on %d labelled samples", len(rows))

    feature_names = sorted(rows[0][0].keys())
    X = np.array([[r[0].get(f, 0.0) for f in feature_names] for r, _ in rows], dtype=float)
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
    parser = argparse.ArgumentParser(description="Train LightGBM regime classifier")
    parser.add_argument("--db", type=Path, default=None, help="Path to ledger.db (default: auto-detect)")
    args = parser.parse_args()

    db_path = args.db or (learning_db_dir() / "ledger.db")
    train(db_path)


if __name__ == "__main__":
    main()
