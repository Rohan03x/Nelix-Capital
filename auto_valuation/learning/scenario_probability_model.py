"""
learning/scenario_probability_model.py — ML model for scenario probability prediction.

Trains a multinomial LogisticRegression that predicts p(bull) / p(base) / p(bear)
from base-case DCF parameters + regime signals.  Two training sources:

  1. prediction_records (predictions.db) — 33k+ rows going back to each ticker's IPO.
     Label derived from actual_price_at_horizon / predicted_price_per_share:
       > 1.30 → bull,  < 0.70 → bear,  else → base.
     Bootstraps the model immediately on first run.

  2. scenario_outcomes (scenario_outcomes.db) — explicit quarterly/annual labels from
     the ScenarioCalibrator (quarterly_winner / annual_winner).  Takes precedence over
     the bootstrap when ≥ 30 labeled rows exist.

Architecture
------------
* Input features (9, FEATURE_NAMES_V2): all available at both training AND inference time
  — wacc_level, rev_growth, ebit_margin, terminal_growth, iv_discount (log IV/price),
    years_since_ipo, macro_enc, mktcap_enc, data_vintage.
* Output:  softmax(3) → p_base, p_bull, p_bear  (sum to 1.0)
* Model:   LogisticRegression(multi_class='multinomial', solver='lbfgs', C=1.0)
           with StandardScaler.  Consistent sklearn pattern with CAGR Ridge predictor.
* Min samples before activating: _MIN_TRAIN_SAMPLES (30).
* Model file:  auto_valuation/learning/data/scenario_probability_model.pkl

Training cadence:  every 12 h via background_runner._train_scenario_probability_model()
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

# V2 feature set — uses only base-case DCF values available at both training and
# inference time.  Compatible with prediction_records AND scenario_outcomes sources.
FEATURE_NAMES_V2 = [
    "wacc_level",       # base WACC (decimal, e.g. 0.116)
    "rev_growth",       # near-term revenue growth (decimal, e.g. 0.05)
    "ebit_margin",      # target EBIT margin (decimal, e.g. 0.10)
    "terminal_growth",  # terminal growth rate (decimal, e.g. 0.03)
    "iv_discount",      # log(base_iv / market_price) — negative = market >> DCF
    "years_since_ipo",  # company maturity, capped at 30
    "macro_enc",        # contraction=-1, neutral=0, expansion=1
    "mktcap_enc",       # micro=-2 … mega=2
    "data_vintage",     # years of financial history available, capped at 20
]
# Keep old name as alias for any remaining references
FEATURE_NAMES = FEATURE_NAMES_V2

# Label thresholds for deriving bull/base/bear from actual vs predicted price
_BULL_THRESHOLD = 1.30   # actual > 130% of predicted → bull outcome
_BEAR_THRESHOLD = 0.70   # actual <  70% of predicted → bear outcome


def _safe_log_ratio(a: float, b: float) -> float:
    """log(a/b) clamped to [-3, 3], handling zero/negative values safely."""
    if a <= 0 or b <= 0:
        return 0.0
    ratio = a / b
    if ratio <= 0:
        return 0.0
    return max(-3.0, min(3.0, math.log(ratio)))


def extract_features_from_scenario_row(row: dict[str, Any]) -> list[float] | None:
    """Extract 9-feature V2 vector from a scenario_outcomes row dict.

    Uses base-case values only.  Compatible with train_from_prediction_records.
    Returns None if essential values are missing or invalid.
    """
    try:
        base_iv = float(row.get("base_iv") or 0)
        price_at_pred = float(row.get("price_at_prediction") or 0)
        base_wacc = float(row.get("base_wacc") or 0)
        if base_iv <= 0 or base_wacc <= 0:
            return None

        rev_growth = float(row.get("base_rev_growth") or 0)
        ebit_margin = float(row.get("base_margin") or 0) / 100.0  # stored as pct
        terminal_growth = float(row.get("base_g") or 0) / 100.0  # stored as pct
        macro = str(row.get("macro_regime") or "neutral").lower()
        mktcap = str(row.get("market_cap_regime") or "mid").lower()
        iv_discount = _safe_log_ratio(base_iv, price_at_pred) if price_at_pred > 0 else 0.0

        return [
            float(base_wacc),
            float(rev_growth),
            float(ebit_margin),
            float(terminal_growth),
            float(iv_discount),
            0.0,                                           # years_since_ipo unknown
            float(_MACRO_ENC.get(macro, 0)),
            float(_MKTCAP_ENC.get(mktcap, 0)),
            0.0,                                           # data_vintage unknown
        ]
    except Exception as exc:
        logger.debug("Feature extraction failed for scenario row: %s", exc)
        return None


def extract_features_from_prediction_record(row: dict[str, Any]) -> list[float] | None:
    """Extract 9-feature V2 vector from a prediction_records row dict.

    Uses actual_price_at_prediction to compute iv_discount.  This is the primary
    training source — 33k+ rows going back to each ticker's IPO.
    Returns None if essential values are missing or invalid.
    """
    try:
        predicted_price = float(row.get("predicted_price_per_share") or 0)
        actual_at_pred = float(row.get("actual_price_at_prediction") or 0)
        wacc = float(row.get("predicted_wacc") or 0)
        if predicted_price <= 0 or wacc <= 0:
            return None

        rev_growth = float(row.get("near_term_revenue_growth") or 0)
        ebit_margin = float(row.get("target_ebit_margin") or 0)
        terminal_growth = float(row.get("predicted_terminal_growth") or 0)
        years_since_ipo = min(float(row.get("years_since_ipo") or 0), 30.0)
        data_vintage = min(float(row.get("data_vintage_years") or 0), 20.0)
        macro = str(row.get("macro_regime") or "neutral").lower()
        mktcap = str(row.get("market_cap_regime") or "mid").lower()
        iv_discount = _safe_log_ratio(predicted_price, actual_at_pred) if actual_at_pred > 0 else 0.0

        return [
            float(wacc),
            float(rev_growth),
            float(ebit_margin),
            float(terminal_growth),
            float(iv_discount),
            float(years_since_ipo),
            float(_MACRO_ENC.get(macro, 0)),
            float(_MKTCAP_ENC.get(mktcap, 0)),
            float(data_vintage),
        ]
    except Exception as exc:
        logger.debug("Feature extraction failed for prediction record: %s", exc)
        return None


def extract_features_for_prediction_v2(
    *,
    wacc: float,
    rev_growth: float,
    ebit_margin: float,
    terminal_growth: float,
    base_iv: float,
    market_price: float,
    years_since_ipo: int = 10,
    macro_regime: str = "neutral",
    market_cap_regime: str = "mid",
    data_vintage: int = 5,
) -> list[float] | None:
    """Extract 9-feature V2 vector for live inference.

    All parameters are available at dashboard render time.  ``iv_discount`` is
    log(base_iv / market_price) — deeply negative for richly-valued stocks like TSLA,
    which is the strongest predictor of a bull outcome.
    """
    if wacc <= 0 or base_iv <= 0:
        return None
    try:
        iv_discount = _safe_log_ratio(base_iv, market_price) if market_price > 0 else 0.0
        return [
            float(wacc),
            float(rev_growth),
            float(ebit_margin),
            float(terminal_growth),
            float(iv_discount),
            float(min(years_since_ipo, 30)),
            float(_MACRO_ENC.get(str(macro_regime or "neutral").lower(), 0)),
            float(_MKTCAP_ENC.get(str(market_cap_regime or "mid").lower(), 0)),
            float(min(data_vintage, 20)),
        ]
    except Exception as exc:
        logger.debug("Feature extraction v2 failed: %s", exc)
        return None


def extract_features_for_prediction(
    *,
    bull_wacc: float = 0.0,
    bear_wacc: float = 0.0,
    base_wacc: float,
    bull_g: float = 0.0,
    bear_g: float = 0.0,
    bull_iv: float = 0.0,
    bear_iv: float = 0.0,
    base_iv: float,
    bull_rev_growth: float = 0.0,
    bear_rev_growth: float = 0.0,
    bull_margin: float = 0.0,
    bear_margin: float = 0.0,
    heuristic_bull: float = 0.25,
    heuristic_bear: float = 0.25,
    macro_regime: str = "neutral",
    revenue_regime: str = "stable",
    market_cap_regime: str = "mid",
    market_price: float = 0.0,
    years_since_ipo: int = 10,
    data_vintage: int = 5,
    rev_growth: float = 0.0,
    ebit_margin: float = 0.0,
    terminal_growth: float = 0.0,
) -> list[float] | None:
    """Backward-compatible wrapper — delegates to extract_features_for_prediction_v2.

    Callers should migrate to extract_features_for_prediction_v2 directly.
    """
    return extract_features_for_prediction_v2(
        wacc=base_wacc,
        rev_growth=rev_growth or bull_rev_growth,
        ebit_margin=ebit_margin,
        terminal_growth=terminal_growth or (bull_g + bear_g) / 2.0 if (bull_g or bear_g) else 0.025,
        base_iv=base_iv,
        market_price=market_price,
        years_since_ipo=years_since_ipo,
        macro_regime=macro_regime,
        market_cap_regime=market_cap_regime,
        data_vintage=data_vintage,
    )


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
                "feature_names": FEATURE_NAMES_V2,
                "feature_version": "v2",
                "training_source": "scenario_outcomes",
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

    def train_from_prediction_records(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Train from prediction_records where actual_price_at_horizon is known.

        Derives label from actual_price_at_horizon / predicted_price_per_share:
          > _BULL_THRESHOLD (1.30) → 'bull'
          < _BEAR_THRESHOLD (0.70) → 'bear'
          else                     → 'base'

        This bootstraps the model from 33k+ historical IPO-to-today records
        immediately, rather than waiting for scenario_outcomes to accumulate.

        Parameters
        ----------
        rows : dicts from prediction_records, must have actual_price_at_horizon > 0

        Returns
        -------
        Summary dict with status, n_samples, accuracy, class_counts.
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
        skipped = 0

        for row in rows:
            actual_price = float(row.get("actual_price_at_horizon") or 0)
            predicted_price = float(row.get("predicted_price_per_share") or 0)
            if actual_price <= 0 or predicted_price <= 0:
                skipped += 1
                continue

            ratio = actual_price / predicted_price
            if ratio > _BULL_THRESHOLD:
                label = "bull"
            elif ratio < _BEAR_THRESHOLD:
                label = "bear"
            else:
                label = "base"

            features = extract_features_from_prediction_record(row)
            if features is None:
                skipped += 1
                continue

            xs.append(features)
            ys.append(_LABEL_ENC[label])

        n = len(xs)
        logger.info(
            "ScenarioProbabilityModel bootstrap: %d usable rows, %d skipped",
            n, skipped,
        )

        if n < _MIN_TRAIN_SAMPLES:
            return {"status": "insufficient_data", "n_samples": n, "n_required": _MIN_TRAIN_SAMPLES}

        unique_classes = set(ys)
        if len(unique_classes) < 2:
            return {"status": "single_class", "n_samples": n}

        try:
            x_arr = np.array(xs, dtype=float)
            y_arr = np.array(ys, dtype=int)

            model = make_pipeline(
                StandardScaler(),
                LogisticRegression(
                    solver="lbfgs",
                    C=1.0,
                    max_iter=500,
                    class_weight="balanced",
                ),
            )
            model.fit(x_arr, y_arr)

            preds = model.predict(x_arr)
            accuracy = float((preds == y_arr).mean())
            class_counts = {SCENARIO_LABELS[k]: int((y_arr == k).sum()) for k in range(3)}

            bundle = {
                "model": model,
                "n_samples": n,
                "n_classes": len(unique_classes),
                "accuracy": accuracy,
                "class_counts": class_counts,
                "feature_names": FEATURE_NAMES_V2,
                "feature_version": "v2",
                "training_source": "prediction_records_bootstrap",
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("wb") as fh:
                pickle.dump(bundle, fh, protocol=pickle.HIGHEST_PROTOCOL)
            self._bundle = bundle

            logger.info(
                "ScenarioProbabilityModel bootstrap trained: %d samples, acc=%.3f, "
                "classes=%s (bull=%d base=%d bear=%d)",
                n, accuracy, class_counts,
                class_counts.get("bull", 0),
                class_counts.get("base", 0),
                class_counts.get("bear", 0),
            )
            return {
                "status": "trained",
                "training_source": "prediction_records_bootstrap",
                "n_samples": n,
                "accuracy": round(accuracy, 3),
                "class_counts": class_counts,
            }
        except Exception as exc:
            logger.warning("ScenarioProbabilityModel.train_from_prediction_records failed: %s", exc)
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
        "feature_version": bundle.get("feature_version", "v1"),
        "training_source": bundle.get("training_source", "unknown"),
    }
