"""
learning/scenario_probability_model.py — ML model for scenario probability prediction.

Trains a multinomial LogisticRegression that predicts p(bull) / p(base) / p(bear)
from scenario parameters and regime signals. Learns *from labeled historical outcomes*
(quarterly_winner / annual_winner in scenario_outcomes.db) so the weights adapt over
time as more predictions mature.

Architecture
------------
* Input features (13):  derived entirely from columns already stored in scenario_outcomes
  — WACC spread, IV skew/upside/downside, growth spread, margin spread, revenue growth
    spread, heuristic probabilities as input (lets model learn systematic bias), and
    ordinal regime encodings.
* Output:  softmax(3) → p_base, p_bull, p_bear  (sum to 1.0)
* Model:   LogisticRegression(multi_class='multinomial', solver='lbfgs', C=1.0)
           with StandardScaler.  Consistent sklearn pattern with CAGR Ridge predictor.
* Min samples before activating: _MIN_TRAIN_SAMPLES (30) — well-calibrated Dirichlet
  estimate below that.
* Model file:  auto_valuation/learning/data/scenario_probability_model.pkl

Training cadence:  every 12 h via background_runner._train_scenario_probability_model()
Re-training is incremental (re-fit from full labeled set each cycle, cheap for <10k rows).

Reference: ADAPTIVE_DCF_IMPROVEMENT_PLAN.md — Layer G: Scenario Probability Learning.
"""

from __future__ import annotations

import logging
import math
import pickle
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).parent / "data" / "scenario_probability_model.pkl"

# Minimum labeled outcomes required before the model replaces the heuristic.
_MIN_TRAIN_SAMPLES = 30

# Classes: must match label encoding used throughout.
SCENARIO_LABELS = ("base", "bull", "bear")  # indices 0, 1, 2
_LABEL_ENC = {label: idx for idx, label in enumerate(SCENARIO_LABELS)}

# Ordinal encodings for categorical regime fields
_MACRO_ENC = {"contraction": -1, "neutral": 0, "expansion": 1}
_REVENUE_ENC = {
    "structural_decline": -2,
    "mild_decline": -1,
    "stable": 0,
    "moderate_growth": 1,
    "strong_growth": 2,
}
_MKTCAP_ENC = {
    "micro": -2,
    "small": -1,
    "mid_cap": 0,
    "mid": 0,
    "large": 1,
    "mega": 2,
    "large_cap": 1,
    "small_cap": -1,
}

FEATURE_NAMES = [
    "wacc_spread",         # bull_wacc - bear_wacc (scenario WACC width)
    "growth_spread",       # bull_g - bear_g (terminal growth uncertainty)
    "iv_skew",             # log(bull_iv / bear_iv) — pos = bull-skewed IV
    "iv_bull_upside",      # bull_iv / base_iv - 1
    "iv_bear_downside",    # 1 - bear_iv / base_iv
    "rev_growth_spread",   # bull_rev_growth - bear_rev_growth
    "margin_spread",       # bull_margin - bear_margin
    "heuristic_bull",      # heuristic p_bull (let model learn systematic bias)
    "heuristic_bear",      # heuristic p_bear
    "macro_enc",           # contraction=-1, neutral=0, expansion=1
    "revenue_enc",         # structural_decline=-2 … strong_growth=2
    "mktcap_enc",          # micro=-2 … mega=2
    "wacc_level",          # base WACC level (absolute, not spread)
]


def _safe_log_ratio(a: float, b: float) -> float:
    """log(a/b) clamped to [-3, 3], handling zero/negative values safely."""
    if a <= 0 or b <= 0:
        return 0.0
    ratio = a / b
    if ratio <= 0:
        return 0.0
    return max(-3.0, min(3.0, math.log(ratio)))


def extract_features_from_scenario_row(row: dict[str, Any]) -> list[float] | None:
    """Extract ML feature vector from a scenario_outcomes row dict.

    Returns None if essential values are missing or invalid.
    """
    try:
        base_iv = float(row.get("base_iv") or 0)
        bull_iv = float(row.get("bull_iv") or 0)
        bear_iv = float(row.get("bear_iv") or 0)
        if base_iv <= 0 or bull_iv <= 0 or bear_iv <= 0:
            return None

        bull_wacc = float(row.get("bull_wacc") or 0)
        bear_wacc = float(row.get("bear_wacc") or 0)
        base_wacc = float(row.get("base_wacc") or (bull_wacc + bear_wacc) / 2)
        bull_g = float(row.get("bull_g") or 0)
        bear_g = float(row.get("bear_g") or 0)
        bull_rev = float(row.get("bull_rev_growth") or 0)
        bear_rev = float(row.get("bear_rev_growth") or 0)
        bull_margin = float(row.get("bull_margin") or 0)
        bear_margin = float(row.get("bear_margin") or 0)
        p_bull = float(row.get("bull_probability") or 0.25)
        p_bear = float(row.get("bear_probability") or 0.25)
        macro = str(row.get("macro_regime") or "neutral").lower()
        revenue_reg = str(row.get("revenue_regime") or "stable").lower()
        mktcap = str(row.get("market_cap_regime") or "mid").lower()

        return [
            bull_wacc - bear_wacc,                         # wacc_spread
            bull_g - bear_g,                               # growth_spread
            _safe_log_ratio(bull_iv, bear_iv),             # iv_skew
            bull_iv / base_iv - 1.0,                       # iv_bull_upside
            1.0 - bear_iv / base_iv,                       # iv_bear_downside
            bull_rev - bear_rev,                           # rev_growth_spread
            bull_margin - bear_margin,                     # margin_spread
            p_bull,                                        # heuristic_bull
            p_bear,                                        # heuristic_bear
            float(_MACRO_ENC.get(macro, 0)),               # macro_enc
            float(_REVENUE_ENC.get(revenue_reg, 0)),       # revenue_enc
            float(_MKTCAP_ENC.get(mktcap, 0)),             # mktcap_enc
            base_wacc,                                     # wacc_level
        ]
    except Exception as exc:
        logger.debug("Feature extraction failed for scenario row: %s", exc)
        return None


def extract_features_for_prediction(
    *,
    bull_wacc: float,
    bear_wacc: float,
    base_wacc: float,
    bull_g: float,
    bear_g: float,
    bull_iv: float,
    bear_iv: float,
    base_iv: float,
    bull_rev_growth: float,
    bear_rev_growth: float,
    bull_margin: float,
    bear_margin: float,
    heuristic_bull: float,
    heuristic_bear: float,
    macro_regime: str = "neutral",
    revenue_regime: str = "stable",
    market_cap_regime: str = "mid",
) -> list[float] | None:
    """Extract feature vector from live prediction parameters."""
    if base_iv <= 0 or bull_iv <= 0 or bear_iv <= 0:
        return None
    try:
        return [
            bull_wacc - bear_wacc,
            bull_g - bear_g,
            _safe_log_ratio(bull_iv, bear_iv),
            bull_iv / base_iv - 1.0,
            1.0 - bear_iv / base_iv,
            bull_rev_growth - bear_rev_growth,
            bull_margin - bear_margin,
            float(heuristic_bull),
            float(heuristic_bear),
            float(_MACRO_ENC.get(str(macro_regime or "neutral").lower(), 0)),
            float(_REVENUE_ENC.get(str(revenue_regime or "stable").lower(), 0)),
            float(_MKTCAP_ENC.get(str(market_cap_regime or "mid").lower(), 0)),
            float(base_wacc),
        ]
    except Exception as exc:
        logger.debug("Feature extraction failed for prediction: %s", exc)
        return None


class ScenarioProbabilityModel:
    """Multinomial Logistic Regression for scenario probability prediction.

    Learns from labeled scenario_outcomes (quarterly_winner / annual_winner).
    Falls back to heuristic when not enough labeled data exists.

    Usage:
        model = ScenarioProbabilityModel()
        probs = model.predict(features)  # returns {"base": 0.50, "bull": 0.25, "bear": 0.25}
        model.train(labeled_rows)        # saves to disk
    """

    def __init__(self, model_path: Path | str | None = None) -> None:
        self._path = Path(model_path) if model_path else _MODEL_PATH
        self._bundle: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any] | None:
        if self._bundle is not None:
            return self._bundle
        if not self._path.exists():
            return None
        try:
            with self._path.open("rb") as fh:
                self._bundle = pickle.load(fh)
            return self._bundle
        except Exception as exc:
            logger.warning("Failed to load scenario probability model: %s", exc)
            return None

    @property
    def is_trained(self) -> bool:
        bundle = self._load()
        return bool(bundle and bundle.get("model") and bundle.get("n_samples", 0) >= _MIN_TRAIN_SAMPLES)

    @property
    def n_samples(self) -> int:
        bundle = self._load()
        return int((bundle or {}).get("n_samples", 0))

    def predict(
        self,
        features: list[float],
    ) -> dict[str, float] | None:
        """Predict scenario probabilities.

        Returns dict with keys 'base', 'bull', 'bear' summing to 1.0.
        Returns None if model is not trained or features are invalid.
        """
        bundle = self._load()
        if not bundle:
            return None
        model = bundle.get("model")
        if model is None or bundle.get("n_samples", 0) < _MIN_TRAIN_SAMPLES:
            return None
        try:
            import numpy as np
            x = np.array([features], dtype=float)
            proba = model.predict_proba(x)[0]  # shape (3,) for classes 0=base,1=bull,2=bear
            classes = model.classes_  # the LogisticRegression class indices
            # Map model class indices to label names
            probs: dict[str, float] = {"base": 0.0, "bull": 0.0, "bear": 0.0}
            for class_idx, p in zip(classes, proba):
                label = SCENARIO_LABELS[int(class_idx)]
                probs[label] = float(p)
            # Normalise to ensure sum = 1.0 (floating point safety)
            total = sum(probs.values())
            if total > 0:
                probs = {k: v / total for k, v in probs.items()}
            return probs
        except Exception as exc:
            logger.debug("ScenarioProbabilityModel.predict failed: %s", exc)
            return None

    def train(
        self,
        labeled_rows: list[dict[str, Any]],
        prefer_horizon: str = "quarterly",
    ) -> dict[str, Any]:
        """Fit multinomial LogisticRegression from labeled scenario_outcomes rows.

        Parameters
        ----------
        labeled_rows : rows from scenario_outcomes with winner fields set
        prefer_horizon : 'quarterly' or 'annual' — if both are set, prefer this one

        Returns
        -------
        Summary dict with n_samples, n_classes, status.
        """
        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            logger.error("scikit-learn required to train ScenarioProbabilityModel.")
            return {"status": "sklearn_missing", "n_samples": 0}

        xs: list[list[float]] = []
        ys: list[int] = []

        for row in labeled_rows:
            # Determine winner label — prefer quarterly since it matures faster
            winner = None
            if prefer_horizon == "quarterly":
                winner = row.get("quarterly_winner") or row.get("annual_winner")
            else:
                winner = row.get("annual_winner") or row.get("quarterly_winner")
            if not winner:
                continue
            winner = str(winner).lower().strip()
            if winner not in _LABEL_ENC:
                continue

            features = extract_features_from_scenario_row(row)
            if features is None:
                continue

            xs.append(features)
            ys.append(_LABEL_ENC[winner])

        n = len(xs)
        if n < _MIN_TRAIN_SAMPLES:
            logger.info(
                "ScenarioProbabilityModel: only %d labeled rows (need %d) — skipping fit",
                n, _MIN_TRAIN_SAMPLES,
            )
            return {"status": "insufficient_data", "n_samples": n, "n_required": _MIN_TRAIN_SAMPLES}

        # Need at least 2 classes represented
        unique_classes = set(ys)
        if len(unique_classes) < 2:
            logger.info("ScenarioProbabilityModel: only %d class(es) in data — skipping", len(unique_classes))
            return {"status": "single_class", "n_samples": n}

        try:
            import numpy as np
            x_arr = np.array(xs, dtype=float)
            y_arr = np.array(ys, dtype=int)

            model = make_pipeline(
                StandardScaler(),
                LogisticRegression(
                    multi_class="multinomial",
                    solver="lbfgs",
                    C=1.0,
                    max_iter=500,
                    class_weight="balanced",  # handles class imbalance gracefully
                ),
            )
            model.fit(x_arr, y_arr)

            # Compute training accuracy
            preds = model.predict(x_arr)
            accuracy = float((preds == y_arr).mean())
            class_counts = {SCENARIO_LABELS[k]: int((y_arr == k).sum()) for k in range(3)}

            bundle = {
                "model": model,
                "n_samples": n,
                "n_classes": len(unique_classes),
                "accuracy": accuracy,
                "class_counts": class_counts,
                "feature_names": FEATURE_NAMES,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("wb") as fh:
                pickle.dump(bundle, fh, protocol=pickle.HIGHEST_PROTOCOL)
            self._bundle = bundle

            logger.info(
                "ScenarioProbabilityModel trained: %d samples, acc=%.3f, classes=%s",
                n, accuracy, class_counts,
            )
            return {
                "status": "trained",
                "n_samples": n,
                "accuracy": round(accuracy, 3),
                "class_counts": class_counts,
            }
        except Exception as exc:
            logger.warning("ScenarioProbabilityModel.train failed: %s", exc)
            return {"status": "error", "error": str(exc), "n_samples": n}


# ── Module-level singleton ────────────────────────────────────────────────────

_model_singleton: ScenarioProbabilityModel | None = None


def _get_model() -> ScenarioProbabilityModel:
    global _model_singleton  # noqa: PLW0603
    if _model_singleton is None:
        _model_singleton = ScenarioProbabilityModel()
    return _model_singleton


def predict_scenario_probabilities(
    features: list[float],
) -> dict[str, float] | None:
    """Predict p_base / p_bull / p_bear using the trained model singleton.

    Returns None if model not trained or prediction fails — caller must fall
    back to heuristic.
    """
    return _get_model().predict(features)


def get_model_info() -> dict[str, Any]:
    """Return metadata about the current model (for dashboards/diagnostics)."""
    model = _get_model()
    bundle = model._load()
    if not bundle:
        return {"trained": False, "n_samples": 0}
    return {
        "trained": model.is_trained,
        "n_samples": bundle.get("n_samples", 0),
        "accuracy": bundle.get("accuracy"),
        "class_counts": bundle.get("class_counts", {}),
    }
