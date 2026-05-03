"""Shared multi-symbol feature engineering for cross-symbol learning."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    label: str
    group: str
    weight: float
    normalizer: float
    display_kind: str = "ratio"


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("revenue_cagr_3y", "Revenue CAGR (3Y)", "growth", 1.30, 0.25, "pct"),
    FeatureSpec("ebit_margin_ttm", "EBIT Margin", "margin", 1.20, 0.20, "pct"),
    FeatureSpec("gross_margin_ttm", "Gross Margin", "margin", 0.85, 0.20, "pct"),
    FeatureSpec("capex_intensity", "Capex Intensity", "reinvestment", 1.00, 0.12, "pct"),
    FeatureSpec("asset_turnover", "Asset Turnover", "capital", 0.95, 0.90, "turns"),
    FeatureSpec("fcf_conversion", "FCF Conversion", "cash", 1.05, 1.00, "turns"),
    FeatureSpec("leverage_ratio", "Leverage", "risk", 1.00, 1.50, "turns"),
    FeatureSpec("reinvestment_rate", "Reinvestment Rate", "reinvestment", 1.00, 1.20, "turns"),
    FeatureSpec("revenue_growth_volatility", "Growth Volatility", "risk", 0.95, 0.20, "pct"),
    FeatureSpec("margin_trend", "Margin Trend", "margin", 0.90, 0.08, "pct"),
    FeatureSpec("revenue_cagr_5y", "Revenue CAGR (5Y)", "growth", 0.80, 0.22, "pct"),
    FeatureSpec("growth_acceleration", "Growth Acceleration", "growth", 0.75, 0.16, "pct"),
    FeatureSpec("cash_conversion", "Cash Conversion", "cash", 0.85, 1.00, "turns"),
    FeatureSpec("capital_intensity", "Capital Intensity", "capital", 0.85, 1.50, "turns"),
    FeatureSpec("dilution_rate", "Dilution Rate", "capital", 0.80, 0.08, "pct"),
    FeatureSpec("size_scale", "Size Scale", "size", 0.60, 0.25, "score"),
    FeatureSpec("cyclicality_score", "Cyclicality", "risk", 0.75, 0.65, "score"),
    FeatureSpec("maturity_score", "Maturity", "stage", 0.75, 0.55, "score"),
    FeatureSpec("valuation_regime_score", "Valuation Regime", "valuation", 0.70, 0.70, "score"),
    FeatureSpec("volatility_score", "Volatility", "risk", 0.70, 0.60, "score"),
)

FEATURE_NAMES = [spec.name for spec in FEATURE_SPECS]
FEATURE_WEIGHTS = {spec.name: spec.weight for spec in FEATURE_SPECS}
FEATURE_NORMALIZERS = {spec.name: spec.normalizer for spec in FEATURE_SPECS}


@dataclass(frozen=True)
class FeatureObservation:
    name: str
    label: str
    group: str
    value: float
    weight: float
    display_value: str
    bucket: str
    evidence: str


@dataclass(frozen=True)
class SymbolFeatures:
    ticker: str = ""
    sector: str = ""
    industry: str = ""
    market_cap_regime: str = ""
    macro_regime: str = "neutral"
    maturity_stage: str = "mature"
    valuation_regime: str = "market"
    volatility_regime: str = "steady"
    data_quality_score: float = 0.0
    sample_size: int = 0
    predictive_usefulness: float = 0.5
    as_of_year: int | None = None
    feature_map: dict[str, float] = field(default_factory=dict)
    dimensions: tuple[FeatureObservation, ...] = ()
    summary: str = ""

    @property
    def vector(self) -> tuple[float, ...]:
        return tuple(float(self.feature_map.get(name, 0.0)) for name in FEATURE_NAMES)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "sector": self.sector,
            "industry": self.industry,
            "market_cap_regime": self.market_cap_regime,
            "macro_regime": self.macro_regime,
            "maturity_stage": self.maturity_stage,
            "valuation_regime": self.valuation_regime,
            "volatility_regime": self.volatility_regime,
            "data_quality_score": round(self.data_quality_score, 2),
            "sample_size": self.sample_size,
            "predictive_usefulness": round(self.predictive_usefulness, 2),
            "as_of_year": self.as_of_year,
            "summary": self.summary,
            "feature_map": {name: round(value, 6) for name, value in self.feature_map.items()},
            "feature_vector": [round(value, 6) for value in self.vector],
            "dimensions": [
                {
                    "name": dimension.name,
                    "label": dimension.label,
                    "group": dimension.group,
                    "value": round(dimension.value, 6),
                    "weight": round(dimension.weight, 2),
                    "display_value": dimension.display_value,
                    "bucket": dimension.bucket,
                    "evidence": dimension.evidence,
                }
                for dimension in self.dimensions
            ],
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _safe_mean(values: Sequence[float]) -> float:
    cleaned = [float(value) for value in values if value is not None]
    return statistics.mean(cleaned) if cleaned else 0.0


def _safe_pstdev(values: Sequence[float]) -> float:
    cleaned = [float(value) for value in values if value is not None]
    return statistics.pstdev(cleaned) if len(cleaned) >= 2 else 0.0


def _clean_series(values: Sequence[float] | None) -> list[float]:
    if not values:
        return []
    return [float(value) for value in values if value is not None]


def growth_rates(revenues: Sequence[float] | None) -> list[float]:
    cleaned = _clean_series(revenues)
    rates: list[float] = []
    for idx in range(1, len(cleaned)):
        prev = cleaned[idx - 1]
        curr = cleaned[idx]
        if prev and prev > 0:
            rates.append(curr / prev - 1.0)
    return rates


def rolling_cagr(values: Sequence[float] | None, years: int = 5) -> float:
    cleaned = _clean_series(values)
    if len(cleaned) < 2:
        return 0.0
    usable = cleaned[-(years + 1):] if len(cleaned) > years else cleaned
    if len(usable) < 2 or usable[0] <= 0 or usable[-1] <= 0:
        return 0.0
    periods = len(usable) - 1
    return (usable[-1] / usable[0]) ** (1.0 / periods) - 1.0


def infer_market_cap_regime(market_cap: float) -> str:
    market_cap = float(market_cap or 0.0)
    if market_cap < 2_000:
        return "small"
    if market_cap < 10_000:
        return "mid"
    return "large"


def _format_feature_value(spec: FeatureSpec, value: float) -> str:
    if spec.display_kind == "pct":
        return f"{value * 100:.1f}%"
    if spec.display_kind in {"turns", "multiple"}:
        return f"{value:.2f}x"
    return f"{value:.2f}"


def _label_from_thresholds(value: float, thresholds: Sequence[tuple[float, str]], fallback: str) -> str:
    for limit, label in thresholds:
        if value <= limit:
            return label
    return fallback


def _bucket_feature(spec: FeatureSpec, value: float) -> str:
    if spec.name in {"revenue_cagr_3y", "revenue_cagr_5y"}:
        return _label_from_thresholds(value, ((0.00, "contracting"), (0.06, "low"), (0.15, "mid"), (0.30, "high")), "hyper")
    if spec.name in {"ebit_margin_ttm", "gross_margin_ttm"}:
        return _label_from_thresholds(value, ((0.00, "negative"), (0.10, "thin"), (0.20, "solid"), (0.35, "strong")), "elite")
    if spec.name in {"margin_trend", "growth_acceleration"}:
        return _label_from_thresholds(value, ((-0.03, "falling"), (0.00, "flat"), (0.03, "improving")), "surging")
    if spec.name in {"fcf_conversion", "cash_conversion"}:
        return _label_from_thresholds(value, ((0.00, "negative"), (0.50, "weak"), (1.00, "healthy")), "strong")
    if spec.name in {"leverage_ratio", "capital_intensity"}:
        return _label_from_thresholds(value, ((0.50, "light"), (1.25, "moderate"), (2.00, "heavy")), "very-heavy")
    if spec.name == "dilution_rate":
        return _label_from_thresholds(value, ((0.00, "shrinking"), (0.02, "stable"), (0.05, "rising")), "dilutive")
    if spec.name in {"size_scale", "maturity_score", "valuation_regime_score", "volatility_score", "cyclicality_score"}:
        return _label_from_thresholds(value, ((0.50, "low"), (1.00, "mid"), (1.50, "high")), "extreme")
    return _label_from_thresholds(value, ((0.50, "low"), (1.00, "mid"), (1.50, "high")), "extreme")


def _maturity_stage(feature_map: dict[str, float]) -> str:
    growth = float(feature_map.get("revenue_cagr_3y", 0.0))
    margin = float(feature_map.get("ebit_margin_ttm", 0.0))
    cash_conversion = float(feature_map.get("cash_conversion", feature_map.get("fcf_conversion", 0.0)))
    reinvestment = float(feature_map.get("reinvestment_rate", 0.0))
    margin_trend = float(feature_map.get("margin_trend", 0.0))
    if growth >= 0.18 and cash_conversion < 0.55:
        return "emerging"
    if growth >= 0.12 or reinvestment >= 1.0:
        return "scaling"
    if growth >= 0.05 and margin >= 0.15 and cash_conversion >= 0.80:
        return "compounder"
    if growth <= 0.02 or margin_trend <= -0.02:
        return "challenged"
    return "mature"


def _valuation_regime(feature_map: dict[str, float]) -> str:
    score = float(feature_map.get("valuation_regime_score", 0.0))
    leverage = float(feature_map.get("leverage_ratio", 0.0))
    margin = float(feature_map.get("ebit_margin_ttm", 0.0))
    if leverage >= 1.50 or margin < 0.05:
        return "stressed"
    if score >= 1.25:
        return "premium"
    if score <= 0.40:
        return "value"
    return "market"


def _volatility_regime(feature_map: dict[str, float]) -> str:
    volatility = float(feature_map.get("volatility_score", 0.0))
    cyclicality = float(feature_map.get("cyclicality_score", 0.0))
    score = max(volatility, cyclicality)
    if score >= 1.25:
        return "volatile"
    if score >= 0.65:
        return "swing"
    return "steady"


def _dimension_evidence(spec: FeatureSpec, sample_size: int) -> str:
    years = max(int(sample_size or 0), 1)
    return f"Computed from trailing information available at forecast time over {years} observation(s)."


def _build_dimensions(feature_map: dict[str, float], sample_size: int) -> tuple[FeatureObservation, ...]:
    return tuple(
        FeatureObservation(
            name=spec.name,
            label=spec.label,
            group=spec.group,
            value=float(feature_map.get(spec.name, 0.0)),
            weight=spec.weight,
            display_value=_format_feature_value(spec, float(feature_map.get(spec.name, 0.0))),
            bucket=_bucket_feature(spec, float(feature_map.get(spec.name, 0.0))),
            evidence=_dimension_evidence(spec, sample_size),
        )
        for spec in FEATURE_SPECS
    )


def _build_summary(feature_map: dict[str, float], maturity_stage: str, valuation_regime: str) -> str:
    growth = feature_map.get("revenue_cagr_3y", 0.0)
    margin = feature_map.get("ebit_margin_ttm", 0.0)
    cash_conversion = feature_map.get("cash_conversion", feature_map.get("fcf_conversion", 0.0))
    return (
        f"{maturity_stage.title()} {valuation_regime} profile with "
        f"{growth * 100:.1f}% 3Y revenue CAGR, {margin * 100:.1f}% EBIT margin, "
        f"and {cash_conversion:.2f}x cash conversion."
    )


def coerce_feature_map(features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float] | None) -> dict[str, float]:
    if isinstance(features, SymbolFeatures):
        return dict(features.feature_map)
    if features is None:
        return {name: 0.0 for name in FEATURE_NAMES}
    if isinstance(features, dict):
        return {name: float(features.get(name, 0.0) or 0.0) for name in FEATURE_NAMES}
    values = [float(value) for value in features]
    padded = values[: len(FEATURE_NAMES)] + [0.0] * max(0, len(FEATURE_NAMES) - len(values))
    return dict(zip(FEATURE_NAMES, padded))


def coerce_symbol_features(
    features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float] | None,
    *,
    ticker: str = "",
    sector: str = "",
    industry: str = "",
    market_cap_regime: str = "",
    macro_regime: str = "neutral",
    data_quality_score: float | None = None,
    sample_size: int | None = None,
    predictive_usefulness: float = 0.5,
    as_of_year: int | None = None,
) -> SymbolFeatures:
    if isinstance(features, SymbolFeatures):
        return features

    feature_map = coerce_feature_map(features)
    effective_sample_size = int(sample_size if sample_size is not None else max(1, int(feature_map.get("size_scale", 0.0) * 6)))
    maturity_stage = _maturity_stage(feature_map)
    valuation_regime = _valuation_regime(feature_map)
    volatility_regime = _volatility_regime(feature_map)
    if not market_cap_regime:
        market_cap_regime = "mid"
    quality = float(data_quality_score) if data_quality_score is not None else _clamp(0.35 + 0.08 * effective_sample_size, 0.35, 0.90)
    dimensions = _build_dimensions(feature_map, effective_sample_size)
    return SymbolFeatures(
        ticker=ticker,
        sector=sector,
        industry=industry,
        market_cap_regime=market_cap_regime,
        macro_regime=macro_regime,
        maturity_stage=maturity_stage,
        valuation_regime=valuation_regime,
        volatility_regime=volatility_regime,
        data_quality_score=quality,
        sample_size=effective_sample_size,
        predictive_usefulness=_clamp(predictive_usefulness, 0.25, 1.0),
        as_of_year=as_of_year,
        feature_map=feature_map,
        dimensions=dimensions,
        summary=_build_summary(feature_map, maturity_stage, valuation_regime),
    )


def build_symbol_features(
    *,
    ticker: str = "",
    sector: str = "",
    industry: str = "",
    revenues: Sequence[float] | None = None,
    ebit_margins: Sequence[float] | None = None,
    gross_margin_base_pct: float = 0.0,
    capex_pct: float = 0.0,
    total_assets: float = 0.0,
    total_debt: float = 0.0,
    revenue_base: float = 0.0,
    operating_cf: float = 0.0,
    fcf: float = 0.0,
    da_pct: float = 0.0,
    tax_rate_pct: float = 0.0,
    market_cap: float = 0.0,
    enterprise_value: float | None = None,
    share_counts: Sequence[float] | None = None,
    market_cap_regime: str = "",
    macro_regime: str = "neutral",
    observation_year: int | None = None,
    predictive_usefulness: float = 0.5,
) -> SymbolFeatures:
    revenues = _clean_series(revenues)
    ebit_margins = _clean_series(ebit_margins)
    share_counts = [value for value in _clean_series(share_counts) if value > 0]

    revenue_base = float(revenue_base or (revenues[-1] if revenues else 0.0))
    enterprise_value = float(enterprise_value if enterprise_value is not None else market_cap)
    gross_margin = _clamp(float(gross_margin_base_pct or 0.0) / 100.0, 0.0, 1.0)
    capex_intensity = _clamp(float(capex_pct or 0.0) / 100.0, 0.0, 0.5)
    recent_margin = _safe_mean([value / 100.0 for value in ebit_margins[-2:]])
    prior_margin = _safe_mean([value / 100.0 for value in ebit_margins[-4:-2]]) or recent_margin
    margin_trend = recent_margin - prior_margin
    growth_lookback = growth_rates(revenues[-6:])
    recent_growth = _safe_mean(growth_lookback[-2:])
    prior_growth = _safe_mean(growth_lookback[-4:-2]) if len(growth_lookback) >= 4 else _safe_mean(growth_lookback[:-2])
    nopat = revenue_base * max(recent_margin, 0.0) * max(0.0, 1.0 - float(tax_rate_pct or 0.0) / 100.0)
    reinvestment = max(float(capex_pct or 0.0) - float(da_pct or 0.0), 0.0) / 100.0 * revenue_base
    margin_volatility = _safe_pstdev([value / 100.0 for value in ebit_margins[-5:]])
    share_dilution = rolling_cagr(share_counts, 3) if len(share_counts) >= 2 else 0.0
    size_scale = _clamp(math.log10(max(float(market_cap or 0.0), 1.0)) / 4.0 - 0.75, 0.0, 2.0)
    revenue_growth_volatility = _safe_pstdev(growth_lookback)
    cash_conversion = operating_cf / max(abs(nopat), 1.0)
    fcf_conversion = fcf / max(abs(operating_cf), 1.0)
    capital_intensity = total_assets / max(revenue_base, 1.0)
    valuation_multiple = enterprise_value / max(revenue_base, 1.0) if revenue_base else 0.0
    maturity_score = _clamp(0.55 + recent_margin * 2.0 + max(cash_conversion, 0.0) * 0.25 - recent_growth * 1.15 + size_scale * 0.20, 0.0, 2.0)
    volatility_score = _clamp((revenue_growth_volatility / 0.22) * 0.60 + (margin_volatility / 0.12) * 0.40, 0.0, 2.0)
    cyclicality_score = _clamp((revenue_growth_volatility / 0.20) * 0.70 + (margin_volatility / 0.10) * 0.30, 0.0, 2.0)
    market_cap_regime = market_cap_regime or infer_market_cap_regime(float(market_cap or 0.0))
    sample_size = max(len(revenues), len(ebit_margins), len(share_counts))
    history_factor = _clamp(max(len(revenues) - 1, 0) / 5.0, 0.0, 1.0)
    coverage = _safe_mean(
        [
            1.0 if revenues else 0.0,
            1.0 if ebit_margins else 0.0,
            1.0 if total_assets else 0.0,
            1.0 if market_cap else 0.0,
            1.0 if operating_cf or fcf else 0.0,
            1.0 if share_counts else 0.0,
        ]
    )
    data_quality_score = round(_clamp(0.20 + 0.45 * coverage + 0.35 * history_factor, 0.20, 1.0), 2)
    feature_map = {
        "revenue_cagr_3y": rolling_cagr(revenues, 3),
        "ebit_margin_ttm": _clamp((ebit_margins[-1] / 100.0) if ebit_margins else 0.0, -0.5, 1.0),
        "gross_margin_ttm": gross_margin,
        "capex_intensity": capex_intensity,
        "asset_turnover": revenue_base / max(total_assets, 1.0),
        "fcf_conversion": _clamp(fcf_conversion, -2.0, 2.0),
        "leverage_ratio": total_debt / max(revenue_base, 1.0),
        "reinvestment_rate": _clamp(reinvestment / max(abs(nopat), 1.0), -1.0, 3.0),
        "revenue_growth_volatility": _clamp(revenue_growth_volatility, 0.0, 1.0),
        "margin_trend": _clamp(margin_trend, -0.5, 0.5),
        "revenue_cagr_5y": rolling_cagr(revenues, 5),
        "growth_acceleration": _clamp(recent_growth - prior_growth, -0.5, 0.5),
        "cash_conversion": _clamp(cash_conversion, -2.0, 3.0),
        "capital_intensity": _clamp(capital_intensity, 0.0, 8.0),
        "dilution_rate": _clamp(share_dilution, -0.5, 0.5),
        "size_scale": size_scale,
        "cyclicality_score": cyclicality_score,
        "maturity_score": maturity_score,
        "valuation_regime_score": _clamp(valuation_multiple / 4.0, 0.0, 2.0),
        "volatility_score": volatility_score,
    }
    return coerce_symbol_features(
        feature_map,
        ticker=ticker,
        sector=sector,
        industry=industry,
        market_cap_regime=market_cap_regime,
        macro_regime=macro_regime,
        data_quality_score=data_quality_score,
        sample_size=sample_size,
        predictive_usefulness=max(0.35, predictive_usefulness),
        as_of_year=observation_year,
    )


def build_feature_map(**kwargs: Any) -> dict[str, float]:
    return build_symbol_features(**kwargs).feature_map


__all__ = [
    "FEATURE_NAMES",
    "FEATURE_NORMALIZERS",
    "FEATURE_SPECS",
    "FEATURE_WEIGHTS",
    "FeatureObservation",
    "FeatureSpec",
    "SymbolFeatures",
    "build_feature_map",
    "build_symbol_features",
    "coerce_feature_map",
    "coerce_symbol_features",
    "growth_rates",
    "infer_market_cap_regime",
    "rolling_cagr",
]