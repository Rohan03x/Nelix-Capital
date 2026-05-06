"""
learning/near_term_cagr_predictor.py — Layer F Tier 2: per-regime Ridge regression
for near-term revenue CAGR prediction.

Each of the five revenue regimes has its own Ridge regression model trained on
historical postmortem records. Falls back to the best available heuristic when
models are not yet trained or when the model file is absent.

Regimes: structural_decline, mild_decline, stable, moderate_growth, strong_growth

Training is performed offline via train_near_term_cagr_predictor.py (or by calling
NearTermCagrPredictor.train()).  Saved to:
  auto_valuation/learning/data/near_term_cagr_models.pkl

Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md — Layer F Tier 2.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CAGR_MODEL_PATH = Path(__file__).parent / "data" / "near_term_cagr_models.pkl"

# Features used per regime (subset of the full feature vector)
_REGIME_FEATURES: dict[str, list[str]] = {
    "structural_decline": [
        "cagr_3yr",
        "cagr_1yr",
        "gross_margin_trend",
        "industry_headwind_score",
        "market_implied_g",
    ],
    "mild_decline": [
        "cagr_3yr",
        "cagr_5yr",
        "ntm_growth",
        "margin_trend",
        "industry_headwind_score",
    ],
    "stable": [
        "cagr_3yr",
        "cagr_5yr",
        "ntm_growth",
        "margin_trend",
        "structural_break_score",
    ],
    "moderate_growth": [
        "cagr_3yr",
        "cagr_5yr",
        "ntm_growth",
        "margin_trend",
        "structural_break_score",
        "industry_headwind_score",
    ],
    "strong_growth": [
        "cagr_3yr",
        "cagr_5yr",
        "ntm_growth",
        "margin_trend",
        "market_implied_g",
        "structural_break_score",
    ],
}

# Fallback CAGR estimate keys (tried in order)
_FALLBACK_FEATURE_KEYS = ("ntm_growth", "cagr_3yr", "cagr_5yr")


def _extract_features(
    regime: str,
    feature_vector: dict[str, float],
) -> list[float]:
    """Extract per-regime feature subset from a full feature vector."""
    keys = _REGIME_FEATURES.get(regime, list(_REGIME_FEATURES["stable"]))
    return [float(feature_vector.get(k, 0.0)) for k in keys]


def _fallback_cagr(feature_vector: dict[str, float]) -> float:
    """Return best available heuristic CAGR when model is unavailable."""
    for key in _FALLBACK_FEATURE_KEYS:
        value = feature_vector.get(key)
        if value is not None and value == value:  # not NaN
            return float(value)
    return 0.05  # absolute fallback: 5% nominal growth


class NearTermCagrPredictor:
    """Per-regime Ridge regression predictor for near-term revenue CAGR.

    Usage:
        predictor = NearTermCagrPredictor()
        cagr = predictor.predict(regime="stable", feature_vector={...})

    To train:
        predictor.train(postmortem_records)  # saves to disk
    """

    def __init__(self, model_path: Path | str | None = None) -> None:
        self._path = Path(model_path) if model_path else _CAGR_MODEL_PATH
        self._models: dict[str, Any] | None = None

    def _load_models(self) -> dict[str, Any] | None:
        if self._models is not None:
            return self._models
        if not self._path.exists():
            return None
        try:
            with open(self._path, "rb") as fh:
                bundle = pickle.load(fh)
            self._models = bundle.get("models", {})
            return self._models
        except Exception as exc:
            logger.warning("Failed to load near-term CAGR models from %s: %s", self._path, exc)
            return None

    def predict(
        self,
        regime: str,
        feature_vector: dict[str, float],
    ) -> float:
        """Predict near-term revenue CAGR for a given regime and feature vector.

        Falls back to heuristic if the per-regime model is unavailable.

        Parameters
        ----------
        regime : one of the five REGIME_LABELS strings
        feature_vector : dict[str, float] as produced by regime_classifier._build_feature_vector()

        Returns
        -------
        float : predicted near-term CAGR (e.g. 0.08 = 8%)
        """
        models = self._load_models()
        if models and regime in models:
            try:
                import numpy as np
                model = models[regime]
                x = np.array([_extract_features(regime, feature_vector)], dtype=float)
                prediction = float(model.predict(x)[0])
                # Clamp to [-0.50, +1.00] — extreme values indicate model failure
                return max(-0.50, min(prediction, 1.00))
            except Exception as exc:
                logger.debug("Ridge model predict failed for regime %s: %s", regime, exc)

        return _fallback_cagr(feature_vector)

    def train(
        self,
        postmortem_records: list[Any],
        alpha: float = 1.0,
    ) -> dict[str, int]:
        """Fit one Ridge regression per regime from postmortem records.

        Parameters
        ----------
        postmortem_records : list of PostmortemRecord-like objects or dicts
        alpha : Ridge regularisation strength

        Returns
        -------
        dict mapping regime → number of training samples used
        """
        try:
            import numpy as np
            from sklearn.linear_model import Ridge
        except ImportError:
            logger.error("scikit-learn is required to train CAGR models. Install sklearn.")
            return {}

        from auto_valuation.learning.regime_classifier import REGIME_LABELS, classify_regime

        regime_data: dict[str, tuple[list[list[float]], list[float]]] = {
            r: ([], []) for r in REGIME_LABELS
        }

        for record in postmortem_records:
            _get = (
                (lambda r, k: r.get(k))
                if isinstance(record, dict)
                else (lambda r, k: getattr(r, k, None))
            )
            actual_rg = _get(record, "actual_revenue_growth")
            if actual_rg is None:
                continue

            fv_raw = _get(record, "feature_vector") or {}
            if not fv_raw:
                continue
            fv = dict(fv_raw) if isinstance(fv_raw, dict) else {}
            if not fv:
                continue

            # Determine the regime from the feature vector
            try:
                rc = classify_regime(
                    cagr_3yr=fv.get("cagr_3yr"),
                    cagr_5yr=fv.get("cagr_5yr"),
                    cagr_10yr=fv.get("cagr_10yr"),
                    ntm_growth=fv.get("ntm_growth"),
                    market_implied_g=fv.get("market_implied_g"),
                    structural_break_score=fv.get("structural_break_score", 0.0),
                    revenue_volatility=fv.get("revenue_volatility", 0.0),
                    margin_volatility=fv.get("margin_volatility", 0.0),
                    prefer_model=False,
                )
                regime = rc.regime
            except Exception:
                continue

            features = _extract_features(regime, fv)
            regime_data[regime][0].append(features)
            regime_data[regime][1].append(float(actual_rg))

        models: dict[str, Any] = {}
        sample_counts: dict[str, int] = {}
        for regime, (xs, ys) in regime_data.items():
            n = len(xs)
            sample_counts[regime] = n
            if n < 5:
                logger.debug("Skipping Ridge model for %s: only %d samples", regime, n)
                continue
            x_arr = np.array(xs, dtype=float)
            y_arr = np.array(ys, dtype=float)
            model = Ridge(alpha=alpha)
            model.fit(x_arr, y_arr)
            models[regime] = model
            logger.info("Trained Ridge CAGR model for %s with %d samples", regime, n)

        if models:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "wb") as fh:
                pickle.dump({"models": models, "regime_features": _REGIME_FEATURES}, fh)
            logger.info("Saved %d regime CAGR models to %s", len(models), self._path)

        self._models = models
        return sample_counts


# Module-level singleton for convenience
_predictor: NearTermCagrPredictor | None = None


def predict_near_term_cagr(
    regime: str,
    feature_vector: dict[str, float],
) -> float:
    """Predict near-term CAGR using the module-level singleton predictor.

    Parameters
    ----------
    regime : regime label (e.g. "stable", "moderate_growth")
    feature_vector : dict produced by regime_classifier._build_feature_vector()

    Returns
    -------
    float : predicted CAGR
    """
    global _predictor
    if _predictor is None:
        _predictor = NearTermCagrPredictor()
    return _predictor.predict(regime, feature_vector)


__all__ = [
    "NearTermCagrPredictor",
    "predict_near_term_cagr",
    "REGIME_FEATURES",
]

# Make REGIME_FEATURES accessible as module-level alias
REGIME_FEATURES = _REGIME_FEATURES
