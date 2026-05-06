"""Cross-industry analog matching and explainable global overlays."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable

from .feature_space import (
    FEATURE_NAMES,
    FEATURE_NORMALIZERS,
    FEATURE_SPECS,
    FEATURE_WEIGHTS,
    SymbolFeatures,
    coerce_feature_map,
    coerce_symbol_features,
)


try:
    from auto_valuation.config import LEARNING_CONFIG as _LEARNING_CONFIG
except ImportError:
    _LEARNING_CONFIG = {
        "min_analog_similarity": 0.60,
        "max_analogs_returned": 10,
        "cross_sector_only": True,
    }


_COMPANY_NAME_TOKEN_RE = re.compile(r"[^A-Z0-9]+")
_COMPANY_NAME_STOPWORDS = frozenset(
    {
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LIMITED",
        "LTD",
        "PLC",
        "AG",
        "SA",
        "SE",
        "NV",
        "BV",
        "AB",
        "ASA",
        "ADR",
        "ADS",
        "GDR",
        "HOLDING",
        "HOLDINGS",
        "GROUP",
        "CLASS",
        "CL",
        "ORD",
        "ORDINARY",
        "SHARES",
        "SHARE",
    }
)


@dataclass(frozen=True)
class AnalogObservation:
    ticker: str
    sector: str
    industry: str
    vintage_year: int
    company_name: str = ""
    feature_vector: tuple[float, ...] | None = None
    outcome_revenue_cagr_5y: float = 0.0
    outcome_margin_change_bps: float = 0.0
    outcome_ev_multiple_change: float = 0.0
    pattern_label: str | None = None
    feature_map: dict[str, float] = field(default_factory=dict)
    market_cap_regime: str = ""
    macro_regime: str = "neutral"
    maturity_stage: str = ""
    valuation_regime: str = ""
    volatility_regime: str = ""
    data_quality_score: float = 0.6
    sample_size: int = 1
    predictive_usefulness: float = 0.5
    as_of_year: int | None = None

    def __post_init__(self) -> None:
        normalized = coerce_symbol_features(
            self.feature_map or self.feature_vector,
            ticker=self.ticker,
            sector=self.sector,
            industry=self.industry,
            market_cap_regime=self.market_cap_regime or "mid",
            macro_regime=self.macro_regime or "neutral",
            data_quality_score=self.data_quality_score,
            sample_size=self.sample_size,
            predictive_usefulness=self.predictive_usefulness,
            as_of_year=self.as_of_year or self.vintage_year,
        )
        object.__setattr__(self, "feature_map", dict(normalized.feature_map))
        object.__setattr__(self, "feature_vector", normalized.vector)
        object.__setattr__(self, "market_cap_regime", self.market_cap_regime or normalized.market_cap_regime)
        object.__setattr__(self, "macro_regime", self.macro_regime or normalized.macro_regime)
        object.__setattr__(self, "maturity_stage", self.maturity_stage or normalized.maturity_stage)
        object.__setattr__(self, "valuation_regime", self.valuation_regime or normalized.valuation_regime)
        object.__setattr__(self, "volatility_regime", self.volatility_regime or normalized.volatility_regime)
        object.__setattr__(self, "data_quality_score", normalized.data_quality_score)
        object.__setattr__(self, "sample_size", normalized.sample_size)
        object.__setattr__(self, "predictive_usefulness", normalized.predictive_usefulness)
        object.__setattr__(self, "as_of_year", normalized.as_of_year)
        if not self.pattern_label:
            pattern_name, _, _ = match_pattern_library(normalized)
            object.__setattr__(self, "pattern_label", pattern_name)


@dataclass(frozen=True)
class AnalogMatch:
    analog: AnalogObservation
    similarity_score: float
    sector_distance: int
    analog_score: float
    static_similarity: float = 0.0
    regime_similarity: float = 0.0
    recency_weight: float = 1.0
    quality_weight: float = 1.0
    sample_weight: float = 1.0
    usefulness_weight: float = 1.0
    evidence: tuple[dict[str, Any], ...] = ()
    industry_fit_score: float = 1.0  # 1.0 = known industry; <1 = blank/Other penalty


@dataclass(frozen=True)
class AnalogCohort:
    label: str
    members: tuple[str, ...]
    score: float
    explanation: str


@dataclass
class AnalogSet:
    subject_ticker: str
    analogs: list[AnalogMatch] = field(default_factory=list)
    weighted_outcomes: dict[str, float] = field(default_factory=dict)
    analog_confidence: float = 0.0
    pattern_match: str | None = None
    pattern_match_score: float = 0.0
    subject_features: SymbolFeatures | None = None
    cohorts: list[AnalogCohort] = field(default_factory=list)
    overlay: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PatternDefinition:
    name: str
    conditions: dict[str, tuple[float | None, float | None]]
    overlay: dict[str, float]
    archetypes: tuple[str, ...]
    # BRAIN_IMPROVEMENT_PLAN.md (H3) — confidence gate + validation flag
    confidence_threshold: float = 0.75  # pattern_score must reach this to apply
    overlay_validated: bool = False     # if False, overlay is damped to 50%


PATTERN_LIBRARY = [
    PatternDefinition(
        name="PLATFORM_FLYWHEEL",
        conditions={
            "revenue_cagr_3y": (0.30, None),
            "fcf_conversion": (None, 0.0),
            "capex_intensity": (None, 0.06),
            "gross_margin_ttm": (0.60, None),
        },
        overlay={"revenue_growth_adj": 0.02, "ebit_margin_adj": 0.01},
        archetypes=("Amazon 1999-2005", "Salesforce 2008-2014", "Shopify 2015-2019"),
    ),
    PatternDefinition(
        name="COMMODITY_SUPERCYCLE",
        conditions={
            "gross_margin_ttm": (None, 0.30),
            "capex_intensity": (0.08, None),
        },
        overlay={"revenue_growth_adj": 0.015, "wacc_adj": 0.005},
        archetypes=("BHP 2003-2008", "VALE 2009-2011"),
    ),
    PatternDefinition(
        name="MATURE_COMPOUNDER",
        conditions={
            "revenue_cagr_3y": (0.05, 0.12),
            "ebit_margin_ttm": (0.20, None),
            "fcf_conversion": (0.70, None),
        },
        overlay={"ebit_margin_adj": 0.005, "wacc_adj": -0.0025},
        archetypes=("Colgate 2000-2020", "Visa 2012-2022"),
    ),
    PatternDefinition(
        name="DISRUPTED_INCUMBENT",
        conditions={
            "revenue_cagr_3y": (None, 0.02),
            "margin_trend": (None, 0.0),
            "capex_intensity": (0.05, None),
        },
        overlay={"revenue_growth_adj": -0.02, "ebit_margin_adj": -0.01},
        archetypes=("Kodak 1990-2010", "Nokia 2007-2013"),
    ),
    PatternDefinition(
        name="CYCLICAL_RECOVERY",
        conditions={
            "revenue_growth_volatility": (0.20, None),
            "margin_trend": (0.0, None),
        },
        overlay={"revenue_growth_adj": 0.01, "ebit_margin_adj": 0.015},
        archetypes=("Ford 2009-2011", "United Airlines 2021-2023"),
    ),
    PatternDefinition(
        name="REGULATORY_WINDFALL",
        conditions={
            "margin_trend": (0.03, None),
            "gross_margin_ttm": (0.30, None),
        },
        overlay={"ebit_margin_adj": 0.02},
        archetypes=("US Telecom 1996-2000", "Energy post-2005"),
    ),
    PatternDefinition(
        name="EMERGING_MARKET_PREMIUM",
        conditions={
            "reinvestment_rate": (1.0, None),
            "revenue_cagr_3y": (0.15, None),
        },
        overlay={"revenue_growth_adj": 0.015, "wacc_adj": 0.0025},
        archetypes=("Starbucks China 2012-2018",),
    ),
    PatternDefinition(
        name="CAPITAL_LIGHT_TRANSITION",
        conditions={
            "capex_intensity": (None, 0.04),
            "gross_margin_ttm": (0.50, None),
            "fcf_conversion": (0.60, None),
        },
        overlay={"ebit_margin_adj": 0.01, "revenue_growth_adj": 0.005},
        archetypes=("Adobe 2015-2019",),
    ),
]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _coerce_subject_features(
    ticker: str,
    features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    *,
    subject_sector: str,
    subject_industry: str,
    subject_market_cap_regime: str,
    subject_macro_regime: str,
    subject_vintage_year: int,
    observation_year: int | None,
) -> SymbolFeatures:
    return coerce_symbol_features(
        features,
        ticker=ticker,
        sector=subject_sector,
        industry=subject_industry,
        market_cap_regime=subject_market_cap_regime or "mid",
        macro_regime=subject_macro_regime or "neutral",
        sample_size=max(1, subject_vintage_year or 1),
        as_of_year=observation_year or subject_vintage_year or None,
    )


def cosine_similarity(
    left: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    right: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
) -> float:
    left_map = coerce_feature_map(left)
    right_map = coerce_feature_map(right)
    left_values = [left_map[name] * FEATURE_WEIGHTS[name] for name in FEATURE_NAMES]
    right_values = [right_map[name] * FEATURE_WEIGHTS[name] for name in FEATURE_NAMES]
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _feature_vec(feature_map: dict[str, float]) -> list[float]:
    """Return weighted feature vector for a coerced feature map."""
    return [feature_map[name] * FEATURE_WEIGHTS[name] for name in FEATURE_NAMES]


def _batch_cosine_similarities(
    query_vec: list[float],
    candidate_vecs: list[list[float]],
) -> list[float]:
    """Compute cosine similarities between query and all candidates.

    Uses torch (CPU) for batches of 50+, falls back to pure Python otherwise.
    """
    n = len(candidate_vecs)
    if n == 0:
        return []

    # Try torch path for large batches
    if n >= 50:
        try:
            import torch  # noqa: PLC0415

            q = torch.tensor(query_vec, dtype=torch.float32)
            m = torch.tensor(candidate_vecs, dtype=torch.float32)  # (n, d)
            q_norm = q / (q.norm() + 1e-9)
            m_norm = m / (m.norm(dim=1, keepdim=True) + 1e-9)
            sims = (m_norm @ q_norm).tolist()
            return sims  # type: ignore[return-value]
        except Exception:
            pass  # fall through to pure Python

    # Pure Python path
    q_norm_sq = sum(v * v for v in query_vec)
    q_norm = math.sqrt(q_norm_sq) if q_norm_sq > 0 else 0.0
    results: list[float] = []
    for vec in candidate_vecs:
        dot = sum(a * b for a, b in zip(query_vec, vec))
        v_norm = math.sqrt(sum(v * v for v in vec))
        if q_norm == 0.0 or v_norm == 0.0:
            results.append(0.0)
        else:
            results.append(dot / (q_norm * v_norm))
    return results


def _normalize_company_name(value: str) -> str:
    tokens = [
        token
        for token in _COMPANY_NAME_TOKEN_RE.sub(" ", str(value or "").upper()).split()
        if token and token not in _COMPANY_NAME_STOPWORDS
    ]
    return " ".join(tokens)


def _normalized_listing_ticker(ticker: str) -> str:
    ticker_text = str(ticker or "").strip().upper()
    if not ticker_text:
        return ""
    try:
        from webapp.data.eodhd_client import normalize_requested_ticker

        return str(normalize_requested_ticker(ticker_text) or ticker_text).strip().upper()
    except Exception:
        return ticker_text


def _analog_identity_keys(ticker: str, company_name: str = "") -> tuple[str, ...]:
    keys: list[str] = []
    company_key = _normalize_company_name(company_name)
    if company_key:
        keys.append(f"name:{company_key}")
    ticker_key = _normalized_listing_ticker(ticker)
    if ticker_key:
        keys.append(f"ticker:{ticker_key}")
    return tuple(keys)


def _same_issuer_bridge_bonus(subject_identity_keys: set[str], candidate: AnalogObservation) -> float:
    if not subject_identity_keys:
        return 0.0
    candidate_keys = set(_analog_identity_keys(candidate.ticker, candidate.company_name))
    if not candidate_keys:
        return 0.0
    if any(key.startswith("name:") and key in subject_identity_keys for key in candidate_keys):
        return 0.14
    if any(key.startswith("ticker:") and key in subject_identity_keys for key in candidate_keys):
        return 0.08
    return 0.0


def _analog_candidate_dedupe_key(candidate: AnalogObservation) -> str:
    company_key = _normalize_company_name(candidate.company_name)
    if company_key:
        return f"name:{company_key}"
    ticker_key = _normalized_listing_ticker(candidate.ticker)
    return f"ticker:{ticker_key or str(candidate.ticker or '').strip().upper()}"


def sector_distance(subject_sector: str, analog_sector: str, subject_industry: str = "", analog_industry: str = "") -> int:
    if subject_industry and analog_industry and subject_industry == analog_industry:
        return 1
    if subject_sector and analog_sector and subject_sector == analog_sector:
        return 2
    return 3


def _pattern_score(pattern: PatternDefinition, features: dict[str, float]) -> float:
    checks: list[float] = []
    for key, (minimum, maximum) in pattern.conditions.items():
        value = float(features.get(key, 0.0))
        passed = True
        if minimum is not None:
            passed = passed and value >= minimum
        if maximum is not None:
            passed = passed and value <= maximum
        checks.append(1.0 if passed else 0.0)
    return sum(checks) / len(checks) if checks else 0.0


def match_pattern_library(
    features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
) -> tuple[str | None, float, dict[str, float]]:
    feature_map = coerce_feature_map(features)
    best_name = None
    best_score = 0.0
    best_overlay: dict[str, float] = {}
    for pattern in PATTERN_LIBRARY:
        score = _pattern_score(pattern, feature_map)
        if score > best_score:
            best_name = pattern.name
            best_score = score
            best_overlay = dict(pattern.overlay)
    return best_name, best_score, best_overlay


def _regime_similarity(subject: SymbolFeatures, analog: SymbolFeatures, subject_vintage_year: int, analog_vintage_year: int) -> float:
    checks = [
        1.0 if subject.market_cap_regime == analog.market_cap_regime else 0.45,
        1.0 if subject.maturity_stage == analog.maturity_stage else 0.55,
        1.0 if subject.valuation_regime == analog.valuation_regime else 0.60,
        1.0 if subject.volatility_regime == analog.volatility_regime else 0.65,
        1.0 if subject.macro_regime == analog.macro_regime else 0.70,
    ]
    if subject_vintage_year and analog_vintage_year:
        checks.append(_clamp(1.0 - abs(subject_vintage_year - analog_vintage_year) / 6.0, 0.35, 1.0))
    return sum(checks) / len(checks)


def _recency_weight(subject_year: int | None, analog_year: int | None) -> float:
    if not subject_year or not analog_year:
        return 0.90
    return _clamp(1.0 - abs(subject_year - analog_year) / 12.0, 0.45, 1.0)


def _sample_weight(sample_size: int) -> float:
    return _clamp(0.55 + 0.10 * min(max(sample_size, 0), 6), 0.55, 1.0)


def _feature_similarity(subject: SymbolFeatures, analog: SymbolFeatures, name: str) -> float:
    left_value = float(subject.feature_map.get(name, 0.0))
    right_value = float(analog.feature_map.get(name, 0.0))
    normalizer = FEATURE_NORMALIZERS.get(name, 1.0)
    return _clamp(1.0 - abs(left_value - right_value) / max(normalizer, 1e-6), 0.0, 1.0)


def _top_evidence(subject: SymbolFeatures, analog: SymbolFeatures, limit: int = 4) -> tuple[dict[str, Any], ...]:
    ranked = []
    for spec in FEATURE_SPECS:
        closeness = _feature_similarity(subject, analog, spec.name)
        ranked.append((closeness * spec.weight, spec, closeness))
    ranked.sort(key=lambda item: item[0], reverse=True)
    evidence: list[dict[str, Any]] = []
    for _, spec, closeness in ranked[:limit]:
        subject_dimension = next((dimension for dimension in subject.dimensions if dimension.name == spec.name), None)
        analog_dimension = next((dimension for dimension in analog.dimensions if dimension.name == spec.name), None)
        evidence.append(
            {
                "dimension": spec.name,
                "label": spec.label,
                "similarity": round(closeness, 3),
                "subject": subject_dimension.display_value if subject_dimension else f"{subject.feature_map.get(spec.name, 0.0):.3f}",
                "analog": analog_dimension.display_value if analog_dimension else f"{analog.feature_map.get(spec.name, 0.0):.3f}",
                "bucket": analog_dimension.bucket if analog_dimension else "mid",
            }
        )
    return tuple(evidence)


def _cohort_key(analog: AnalogObservation) -> str:
    return f"{analog.maturity_stage}:{analog.valuation_regime}:{analog.volatility_regime}"


def _build_cohorts(matches: list[AnalogMatch]) -> list[AnalogCohort]:
    grouped: dict[str, list[AnalogMatch]] = {}
    for match in matches:
        grouped.setdefault(_cohort_key(match.analog), []).append(match)
    cohorts: list[AnalogCohort] = []
    for key, cohort_matches in grouped.items():
        cohort_score = sum(match.analog_score for match in cohort_matches) / len(cohort_matches)
        maturity_stage, valuation_regime, volatility_regime = key.split(":")
        cohorts.append(
            AnalogCohort(
                label=key,
                members=tuple(match.analog.ticker for match in cohort_matches[:5]),
                score=cohort_score,
                explanation=(
                    f"{maturity_stage.title()} {valuation_regime} / {volatility_regime} cohort "
                    f"with {len(cohort_matches)} analog(s)."
                ),
            )
        )
    cohorts.sort(key=lambda cohort: cohort.score, reverse=True)
    return cohorts[:3]


def build_analog_observations(records: Iterable[Any]) -> list[AnalogObservation]:
    observations: list[AnalogObservation] = []
    for record in records:
        feature_input = getattr(record, "feature_vector", None) or getattr(record, "feature_map", None)
        if not feature_input:
            continue
        actual_margin = getattr(record, "actual_ebit_margin", None)
        actual_revenue = getattr(record, "actual_revenue_mm", None)
        predicted_revenue = float(getattr(record, "predicted_revenue_mm", 0.0) or 0.0)
        predicted_margin = float(getattr(record, "predicted_ebit_margin", 0.0) or 0.0)
        predicted_ev = float(getattr(record, "predicted_ev_mm", 0.0) or 0.0)
        actual_ev = getattr(record, "actual_ev_mm", None)
        if actual_margin is None and actual_revenue is None and actual_ev is None:
            continue

        error_terms: list[float] = []
        if actual_revenue is not None and predicted_revenue:
            error_terms.append(abs(actual_revenue - predicted_revenue) / max(abs(predicted_revenue), 1.0))
        if actual_margin is not None:
            error_terms.append(abs(actual_margin - predicted_margin) / 0.20)
        if actual_ev is not None and predicted_ev:
            error_terms.append(abs(actual_ev - predicted_ev) / max(abs(predicted_ev), 1.0))
        usefulness = _clamp(1.0 - (sum(error_terms) / len(error_terms) if error_terms else 0.5), 0.25, 1.0)
        run_date = getattr(record, "run_date", None)
        as_of_year = run_date.year if isinstance(run_date, date) else None
        normalized = coerce_symbol_features(
            feature_input,
            ticker=getattr(record, "ticker", ""),
            sector=getattr(record, "sector", ""),
            industry=getattr(record, "industry", ""),
            market_cap_regime=getattr(record, "market_cap_regime", "") or "mid",
            macro_regime=getattr(record, "macro_regime", "neutral") or "neutral",
            data_quality_score=_clamp(0.35 + 0.08 * max(int(getattr(record, "data_vintage_years", 0) or 0), 1), 0.35, 0.95),
            sample_size=max(int(getattr(record, "data_vintage_years", 0) or 0), 1),
            predictive_usefulness=usefulness,
            as_of_year=as_of_year,
        )
        observations.append(
            AnalogObservation(
                ticker=getattr(record, "ticker", ""),
                company_name=getattr(record, "company_name", ""),
                sector=getattr(record, "sector", ""),
                industry=getattr(record, "industry", ""),
                vintage_year=max(int(getattr(record, "data_vintage_years", 0) or 0), 1),
                feature_vector=normalized.vector,
                feature_map=dict(normalized.feature_map),
                outcome_revenue_cagr_5y=(
                    (float(actual_revenue) - predicted_revenue) / max(abs(predicted_revenue), 1.0)
                    if actual_revenue is not None and predicted_revenue
                    else 0.0
                ),
                outcome_margin_change_bps=(
                    (float(actual_margin) - predicted_margin) * 10_000.0 if actual_margin is not None else 0.0
                ),
                outcome_ev_multiple_change=(
                    (float(actual_ev) - predicted_ev) / max(abs(predicted_revenue), 1.0)
                    if actual_ev is not None and predicted_revenue
                    else 0.0
                ),
                market_cap_regime=normalized.market_cap_regime,
                macro_regime=normalized.macro_regime,
                maturity_stage=normalized.maturity_stage,
                valuation_regime=normalized.valuation_regime,
                volatility_regime=normalized.volatility_regime,
                data_quality_score=normalized.data_quality_score,
                sample_size=normalized.sample_size,
                predictive_usefulness=normalized.predictive_usefulness,
                as_of_year=normalized.as_of_year,
            )
        )
    return observations


def compute_global_overlay(analog_set: AnalogSet) -> dict[str, Any]:
    if not analog_set.analogs:
        return {
            "enabled": False,
            "scope": None,
            "confidence": 0.0,
            "analog_count": 0,
            "cohort_size": 0,
            "sector_span": 0,
            "revenue_growth_adj_pp": 0.0,
            "ebit_margin_adj_pp": 0.0,
            "valuation_multiple_adj": 0.0,
            "wacc_adj_pp": 0.0,
            "terminal_growth_adj_pp": 0.0,
            "beta_adj": 0.0,
            "top_analogs": [],
            "note": None,
        }

    confidence = _clamp(analog_set.analog_confidence, 0.0, 1.0)
    damping = _clamp(0.12 + 0.28 * confidence, 0.12, 0.40)
    sector_span = len({match.analog.sector for match in analog_set.analogs if match.analog.sector})
    revenue_growth_adj_pp = round(_clamp(analog_set.weighted_outcomes.get("outcome_revenue_cagr_5y", 0.0) * 100.0 * damping, -5.0, 5.0), 1)
    ebit_margin_adj_pp = round(_clamp((analog_set.weighted_outcomes.get("outcome_margin_change_bps", 0.0) / 100.0) * damping, -4.0, 4.0), 1)
    valuation_multiple_adj = round(analog_set.weighted_outcomes.get("outcome_ev_multiple_change", 0.0) * damping, 2)
    wacc_adj_pp = round(_clamp(-valuation_multiple_adj * 0.18, -0.8, 0.8), 1)
    terminal_growth_adj_pp = round(_clamp(revenue_growth_adj_pp * 0.08, -0.5, 0.5), 1)
    beta_adj = round(_clamp(-wacc_adj_pp / 6.0, -0.20, 0.20), 2)
    top_analogs = [
        {
            "ticker": match.analog.ticker,
            "score": round(match.analog_score, 3),
            "similarity": round(match.similarity_score, 3),
            "sector": match.analog.sector or "—",
            "industry": match.analog.industry or "—",
            "industry_fit_score": round(match.industry_fit_score, 3),
            "maturity_stage": match.analog.maturity_stage,
            "valuation_regime": match.analog.valuation_regime,
        }
        for match in analog_set.analogs[:5]
    ]
    # Analogs labeled as cross-sector structural matches
    weak_industry_count = sum(1 for m in analog_set.analogs if m.industry_fit_score < 1.0)
    analog_label = "cross-sector operating analogs" if sector_span > 1 else "same-sector operating analogs"
    note_text = (
        f"Cross-symbol overlay from {len(analog_set.analogs)} {analog_label} across {sector_span} sectors."
    )
    if weak_industry_count > 0:
        note_text += (
            f" {weak_industry_count} analog(s) had missing/Other industry metadata"
            " and received a reduced weight."
        )
    return {
        "enabled": True,
        "scope": "analog-network",
        "confidence": round(confidence, 2),
        "analog_count": len(analog_set.analogs),
        "cohort_size": len(analog_set.analogs),
        "sector_span": sector_span,
        "revenue_growth_adj_pp": revenue_growth_adj_pp,
        "ebit_margin_adj_pp": ebit_margin_adj_pp,
        "valuation_multiple_adj": valuation_multiple_adj,
        "wacc_adj_pp": wacc_adj_pp,
        "terminal_growth_adj_pp": terminal_growth_adj_pp,
        "beta_adj": beta_adj,
        "top_analogs": top_analogs,
        "note": note_text,
    }


def find_analogs(
    ticker: str,
    feature_vector: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    candidates: list[AnalogObservation],
    *,
    subject_company_name: str = "",
    subject_sector: str = "",
    subject_industry: str = "",
    subject_vintage_year: int = 0,
    subject_market_cap_regime: str = "",
    subject_macro_regime: str = "neutral",
    observation_year: int | None = None,
    min_similarity: float | None = None,
    max_results: int | None = None,
    cross_sector_only: bool | None = None,
) -> AnalogSet:
    min_similarity = float(min_similarity or _LEARNING_CONFIG.get("min_analog_similarity", 0.75))
    max_results = int(max_results or _LEARNING_CONFIG.get("max_analogs_returned", 10))
    cross_sector_only = bool(_LEARNING_CONFIG.get("cross_sector_only", True) if cross_sector_only is None else cross_sector_only)

    subject_features = _coerce_subject_features(
        ticker,
        feature_vector,
        subject_sector=subject_sector,
        subject_industry=subject_industry,
        subject_market_cap_regime=subject_market_cap_regime,
        subject_macro_regime=subject_macro_regime,
        subject_vintage_year=subject_vintage_year,
        observation_year=observation_year,
    )
    pattern_name, pattern_score, _ = match_pattern_library(subject_features)
    subject_identity_keys = set(_analog_identity_keys(ticker, subject_company_name))

    matches: list[AnalogMatch] = []
    for candidate in candidates:
        if candidate.ticker.upper() == ticker.upper():
            continue
        if subject_vintage_year and abs(candidate.vintage_year - subject_vintage_year) > 5:
            continue
        if cross_sector_only and candidate.sector == subject_sector:
            continue

        analog_features = coerce_symbol_features(
            candidate.feature_map,
            ticker=candidate.ticker,
            sector=candidate.sector,
            industry=candidate.industry,
            market_cap_regime=candidate.market_cap_regime or "mid",
            macro_regime=candidate.macro_regime or "neutral",
            data_quality_score=candidate.data_quality_score,
            sample_size=candidate.sample_size,
            predictive_usefulness=candidate.predictive_usefulness,
            as_of_year=candidate.as_of_year,
        )
        static_similarity = cosine_similarity(subject_features, analog_features)
        regime_similarity = _regime_similarity(subject_features, analog_features, subject_vintage_year, candidate.vintage_year)
        similarity = 0.72 * static_similarity + 0.28 * regime_similarity
        identity_bonus = _same_issuer_bridge_bonus(subject_identity_keys, candidate)
        if similarity + identity_bonus < min_similarity:
            continue
        recency_weight = _recency_weight(subject_features.as_of_year, candidate.as_of_year)
        quality_weight = _clamp(candidate.data_quality_score or analog_features.data_quality_score, 0.35, 1.0)
        sample_weight = _sample_weight(candidate.sample_size or analog_features.sample_size)
        usefulness_weight = _clamp(candidate.predictive_usefulness or analog_features.predictive_usefulness, 0.25, 1.0)
        distance = sector_distance(subject_sector, candidate.sector, subject_industry, candidate.industry)
        evidence = _top_evidence(subject_features, analog_features)

        # Industry-fit penalty: analogs with blank or "Other" industry/sector
        # carry less weight because we cannot verify economic relatedness.
        analog_industry = str(candidate.industry or "").strip()
        analog_sector = str(candidate.sector or "").strip()
        if not analog_industry or analog_industry.lower() in {"other", "n/a", "unknown", ""}:
            industry_fit_score = 0.60  # blank industry — structural match only
        elif not analog_sector or analog_sector.lower() in {"other", "n/a", "unknown", ""}:
            industry_fit_score = 0.80  # blank sector — mild penalty
        else:
            industry_fit_score = 1.0

        if identity_bonus > 0:
            evidence = (
                {
                    "dimension": "issuer_identity",
                    "label": "Issuer mapping",
                    "similarity": 1.0,
                    "subject": subject_company_name or ticker,
                    "analog": candidate.company_name or candidate.ticker,
                    "bucket": "same_issuer",
                },
            ) + evidence
        analog_score = (
            (similarity + identity_bonus)
            * recency_weight
            * quality_weight
            * sample_weight
            * usefulness_weight
            * (1.0 / distance)
            * industry_fit_score   # apply industry penalty
        )
        matches.append(
            AnalogMatch(
                analog=candidate,
                similarity_score=similarity,
                sector_distance=distance,
                analog_score=analog_score,
                static_similarity=static_similarity,
                regime_similarity=regime_similarity,
                recency_weight=recency_weight,
                quality_weight=quality_weight,
                sample_weight=sample_weight,
                usefulness_weight=usefulness_weight,
                evidence=evidence,
                industry_fit_score=industry_fit_score,
            )
        )

    matches.sort(key=lambda item: item.analog_score, reverse=True)
    deduped_matches: dict[str, AnalogMatch] = {}
    for match in matches:
        dedupe_key = _analog_candidate_dedupe_key(match.analog)
        current = deduped_matches.get(dedupe_key)
        if current is None or match.analog_score > current.analog_score:
            deduped_matches[dedupe_key] = match

    matches = sorted(deduped_matches.values(), key=lambda item: item.analog_score, reverse=True)[:max_results]
    weighted_outcomes = {
        "outcome_revenue_cagr_5y": 0.0,
        "outcome_margin_change_bps": 0.0,
        "outcome_ev_multiple_change": 0.0,
    }
    total_weight = sum(match.analog_score for match in matches)
    if total_weight > 0:
        for key in weighted_outcomes:
            weighted_outcomes[key] = sum(getattr(match.analog, key) * match.analog_score for match in matches) / total_weight

    average_similarity = sum(match.similarity_score for match in matches) / len(matches) if matches else 0.0
    average_quality = sum(match.quality_weight * match.usefulness_weight for match in matches) / len(matches) if matches else 0.0
    analog_confidence = _clamp(average_similarity * average_quality * min(1.0, math.sqrt(len(matches)) / 2.5), 0.0, 1.0)
    analog_set = AnalogSet(
        subject_ticker=ticker,
        analogs=matches,
        weighted_outcomes=weighted_outcomes,
        analog_confidence=analog_confidence,
        pattern_match=pattern_name,
        pattern_match_score=pattern_score,
        subject_features=subject_features,
        cohorts=_build_cohorts(matches),
    )
    analog_set.overlay = compute_global_overlay(analog_set)
    return analog_set


def form_cohorts(
    ticker: str,
    feature_vector: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    candidates: list[AnalogObservation],
    **kwargs: Any,
) -> list[AnalogCohort]:
    return find_analogs(ticker, feature_vector, candidates, **kwargs).cohorts


def compute_overlay(analog_set: AnalogSet) -> dict[str, float]:
    overlay = dict(analog_set.overlay or compute_global_overlay(analog_set))
    result = {
        "revenue_growth_adj": overlay.get("revenue_growth_adj_pp", 0.0) / 100.0,
        "ebit_margin_adj": overlay.get("ebit_margin_adj_pp", 0.0) / 100.0,
        "ev_multiple_adj": float(overlay.get("valuation_multiple_adj", 0.0) or 0.0),
        "wacc_adj": overlay.get("wacc_adj_pp", 0.0) / 100.0,
        "terminal_growth_adj": overlay.get("terminal_growth_adj_pp", 0.0) / 100.0,
        "analog_confidence": analog_set.analog_confidence,
    }
    if analog_set.pattern_match_score > 0.7 and analog_set.pattern_match:
        # BRAIN_IMPROVEMENT_PLAN.md (H3) — apply confidence_threshold and damp unvalidated overlays
        matched_pattern = next(
            (p for p in PATTERN_LIBRARY if p.name == analog_set.pattern_match), None
        )
        if matched_pattern is not None and analog_set.pattern_match_score >= matched_pattern.confidence_threshold:
            _, _, pattern_overlay = match_pattern_library(analog_set.subject_features or {})
            damp = 1.0 if matched_pattern.overlay_validated else 0.50
            for key, value in pattern_overlay.items():
                result[key] = result.get(key, 0.0) + value * damp
    if analog_set.pattern_match:
        result["pattern_match_score"] = analog_set.pattern_match_score
    return result


def apply_overlay(calibrated: Any, analog_overlay: dict[str, float]) -> Any:
    if not analog_overlay:
        return calibrated

    revenue_shift = float(analog_overlay.get("revenue_growth_adj", 0.0))
    margin_shift = float(analog_overlay.get("ebit_margin_adj", 0.0))
    wacc_shift = float(analog_overlay.get("wacc_adj", 0.0))
    terminal_growth_shift = float(analog_overlay.get("terminal_growth_adj", 0.0))
    source_text = f"analog_overlay:{analog_overlay.get('analog_confidence', 0.0):.2f}"

    updated_sources = dict(getattr(calibrated, "calibration_sources", {}))
    updated_sources["revenue_growth_adj"] = source_text
    updated_sources["ebit_margin_adj"] = source_text
    if wacc_shift:
        updated_sources["wacc_adj"] = source_text
    if terminal_growth_shift:
        updated_sources["terminal_growth_adj"] = source_text

    return replace(
        calibrated,
        near_term_growth=getattr(calibrated, "near_term_growth", 0.0) + revenue_shift,
        revenue_growth_adj=getattr(calibrated, "revenue_growth_adj", 0.0) + revenue_shift,
        revenue_growth_band=(
            getattr(calibrated, "revenue_growth_band", (0.0, 0.0))[0] + revenue_shift,
            getattr(calibrated, "revenue_growth_band", (0.0, 0.0))[1] + revenue_shift,
        ),
        revenue_growth_rates=[value + revenue_shift for value in getattr(calibrated, "revenue_growth_rates", [])],
        ebit_margin_current=getattr(calibrated, "ebit_margin_current", 0.0) + margin_shift,
        ebit_margin_terminal=getattr(calibrated, "ebit_margin_terminal", 0.0) + margin_shift,
        ebit_margin_adj=getattr(calibrated, "ebit_margin_adj", 0.0) + margin_shift,
        ebit_margin_band=(
            getattr(calibrated, "ebit_margin_band", (0.0, 0.0))[0] + margin_shift,
            getattr(calibrated, "ebit_margin_band", (0.0, 0.0))[1] + margin_shift,
        ),
        ebit_margin_schedule=[value + margin_shift for value in getattr(calibrated, "ebit_margin_schedule", [])],
        wacc_adj=getattr(calibrated, "wacc_adj", 0.0) + wacc_shift,
        wacc_band=(
            getattr(calibrated, "wacc_band", (0.0, 0.0))[0] + wacc_shift,
            getattr(calibrated, "wacc_band", (0.0, 0.0))[1] + wacc_shift,
        ),
        long_run_growth=getattr(calibrated, "long_run_growth", 0.0) + terminal_growth_shift,
        terminal_growth_adj=getattr(calibrated, "terminal_growth_adj", 0.0) + terminal_growth_shift,
        terminal_growth_band=(
            getattr(calibrated, "terminal_growth_band", (0.0, 0.0))[0] + terminal_growth_shift,
            getattr(calibrated, "terminal_growth_band", (0.0, 0.0))[1] + terminal_growth_shift,
        ),
        calibration_sources=updated_sources,
    )


__all__ = [
    "AnalogCohort",
    "AnalogMatch",
    "AnalogObservation",
    "AnalogSet",
    "FEATURE_NAMES",
    "PATTERN_LIBRARY",
    "apply_overlay",
    "build_analog_observations",
    "compute_global_overlay",
    "compute_overlay",
    "cosine_similarity",
    "_batch_cosine_similarities",
    "_feature_vec",
    "find_analogs",
    "form_cohorts",
    "match_pattern_library",
    "sector_distance",
]