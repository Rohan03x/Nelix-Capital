"""Structured, cacheable forward-looking research insights for the learning adapter."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from auto_valuation.config import ROOT_DIR


try:
    from auto_valuation.config import LEARNING_CONFIG as _LEARNING_CONFIG
except ImportError:
    _LEARNING_CONFIG = {
        "online_research_enabled": True,
        "research_cache_ttl_days": 7,
        "max_research_queries_per_run": 12,
        "min_source_credibility": 0.3,
    }


RESEARCH_CACHE_DIR = ROOT_DIR / "learning" / "cache" / "research"


RESEARCH_QUERIES = {
    "sector_technology_trends": [
        "latest technology disruption in {industry} sector {current_year}",
        "AI automation impact on {sector} margins {current_year}",
        "capital expenditure trends {industry} next 5 years",
    ],
    "competitive_landscape": [
        "{company_name} competitive moat erosion signals {current_year}",
        "new entrants {industry} market share {current_year}",
        "{company_name} pricing power research {current_year}",
    ],
    "regulatory_environment": [
        "{industry} regulatory changes {current_year} impact",
        "{company_name} regulatory risk {current_year}",
    ],
    "macro_sensitivity": [
        "{sector} interest rate sensitivity research",
        "{industry} recession resilience historical analysis",
    ],
    "academic_dcf_advances": [
        "advanced DCF methodology improvements {current_year} research",
        "machine learning equity valuation accuracy improvement",
        "cash flow forecasting techniques {industry} sector",
    ],
}


@dataclass(frozen=True)
class ResearchInsight:
    query: str
    source_url: str
    source_credibility: float
    insight_text: str
    assumption_impacted: str
    direction: str
    magnitude_estimate: float
    confidence: float
    valid_until: date


def build_research_queries(company_name: str, sector: str, industry: str, current_year: int | None = None) -> list[str]:
    year = current_year or date.today().year
    queries: list[str] = []
    for items in RESEARCH_QUERIES.values():
        for item in items:
            queries.append(
                item.format(
                    company_name=company_name,
                    sector=sector,
                    industry=industry,
                    current_year=year,
                )
            )
    return queries


def _cache_path(ticker: str) -> Path:
    RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return RESEARCH_CACHE_DIR / f"{ticker.upper()}.json"


def _serialise(insights: list[ResearchInsight]) -> list[dict[str, Any]]:
    payload = []
    for insight in insights:
        item = asdict(insight)
        item["valid_until"] = insight.valid_until.isoformat()
        payload.append(item)
    return payload


def _deserialise(payload: list[dict[str, Any]]) -> list[ResearchInsight]:
    output = []
    for item in payload:
        output.append(
            ResearchInsight(
                query=item["query"],
                source_url=item["source_url"],
                source_credibility=float(item["source_credibility"]),
                insight_text=item["insight_text"],
                assumption_impacted=item["assumption_impacted"],
                direction=item["direction"],
                magnitude_estimate=float(item["magnitude_estimate"]),
                confidence=float(item["confidence"]),
                valid_until=date.fromisoformat(item["valid_until"]),
            )
        )
    return output


def fetch_insights(
    ticker: str,
    sector: str,
    industry: str,
    *,
    company_name: str | None = None,
    provider: Callable[[str], list[ResearchInsight] | list[dict[str, Any]] | ResearchInsight | dict[str, Any] | None] | None = None,
    current_year: int | None = None,
    cache_dir: Path | str | None = None,
    enabled: bool | None = None,
) -> list[ResearchInsight]:
    """Fetch structured research insights via an injected provider and cache them locally."""
    enabled = _LEARNING_CONFIG.get("online_research_enabled", True) if enabled is None else enabled
    cache_path = Path(cache_dir) / f"{ticker.upper()}.json" if cache_dir else _cache_path(ticker)

    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as handle:
            cached_payload = json.load(handle)
        cached_insights = _deserialise(cached_payload)
        if cached_insights and all(item.valid_until >= date.today() for item in cached_insights):
            return cached_insights

    if not enabled or provider is None:
        return []

    queries = build_research_queries(company_name or ticker, sector, industry, current_year=current_year)
    max_queries = int(_LEARNING_CONFIG.get("max_research_queries_per_run", 12))
    min_credibility = float(_LEARNING_CONFIG.get("min_source_credibility", 0.3))
    ttl_days = int(_LEARNING_CONFIG.get("research_cache_ttl_days", 7))

    insights: list[ResearchInsight] = []
    for query in queries[:max_queries]:
        result = provider(query)
        if result is None:
            continue
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, ResearchInsight):
                insight = item
            else:
                valid_until = item.get("valid_until")
                insight = ResearchInsight(
                    query=item.get("query", query),
                    source_url=item.get("source_url", ""),
                    source_credibility=float(item.get("source_credibility", 0.0)),
                    insight_text=item.get("insight_text", ""),
                    assumption_impacted=item.get("assumption_impacted", "revenue_growth_adj"),
                    direction=item.get("direction", "neutral"),
                    magnitude_estimate=float(item.get("magnitude_estimate", 0.0)),
                    confidence=float(item.get("confidence", 0.0)),
                    valid_until=(date.fromisoformat(valid_until) if isinstance(valid_until, str) else (valid_until or (date.today() + timedelta(days=ttl_days)))),
                )
            if insight.source_credibility >= min_credibility:
                insights.append(insight)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(_serialise(insights), handle, indent=2)
    return insights


def compute_signal_adjustments(insights: list[ResearchInsight]) -> dict[str, float]:
    """Aggregate research insights into additive assumption adjustments."""
    adjustments: dict[str, float] = {}
    for insight in insights:
        direction = 0.0
        if insight.direction == "positive":
            direction = 1.0
        elif insight.direction == "negative":
            direction = -1.0
        else:
            continue
        weight = insight.source_credibility * insight.confidence
        adjustments[insight.assumption_impacted] = adjustments.get(insight.assumption_impacted, 0.0) + direction * insight.magnitude_estimate * weight
    return adjustments
