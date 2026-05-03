"""Persistent industry taxonomy helpers for peer relevance and universe ranking."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
INDUSTRY_TAXONOMY_PATH = PACKAGE_ROOT / "data" / "industry_taxonomy.json"

_STOPWORDS = {
    "and",
    "general",
    "goods",
    "group",
    "holdings",
    "manufacturing",
    "products",
    "retail",
    "services",
    "specialty",
}


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize_label(value).split()
        if token and token not in _STOPWORDS
    }


@lru_cache(maxsize=1)
def load_industry_taxonomy() -> dict[str, Any]:
    try:
        return json.loads(INDUSTRY_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"clusters": []}


@lru_cache(maxsize=1)
def _taxonomy_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    taxonomy = load_industry_taxonomy()
    for cluster in list(taxonomy.get("clusters") or []):
        entry = {
            "id": str(cluster.get("id") or ""),
            "family": str(cluster.get("family") or ""),
            "canonical_industry": str(cluster.get("canonical_industry") or ""),
            "sector": str(cluster.get("sector") or ""),
            "aliases": [str(item) for item in list(cluster.get("aliases") or []) if str(item or "").strip()],
            "related_industries": [str(item) for item in list(cluster.get("related_industries") or []) if str(item or "").strip()],
            "keywords": [str(item) for item in list(cluster.get("keywords") or []) if str(item or "").strip()],
        }
        labels = [entry["canonical_industry"], *entry["aliases"]]
        for label in labels:
            normalized = _normalize_label(label)
            if normalized:
                index[normalized] = entry
    return index


def resolve_industry_taxonomy(industry: str, sector: str = "") -> dict[str, Any]:
    normalized = _normalize_label(industry)
    index = _taxonomy_index()
    cluster = index.get(normalized)
    if cluster is None:
        subject_tokens = _tokens(industry)
        best_cluster: dict[str, Any] | None = None
        best_score = 0.0
        for candidate in {id(entry): entry for entry in index.values()}.values():
            candidate_tokens = _tokens(candidate.get("canonical_industry") or "")
            candidate_tokens.update(_tokens(" ".join(candidate.get("aliases") or [])))
            candidate_tokens.update(_tokens(" ".join(candidate.get("keywords") or [])))
            if not subject_tokens or not candidate_tokens:
                continue
            overlap = len(subject_tokens & candidate_tokens)
            if not overlap:
                continue
            score = overlap / len(subject_tokens | candidate_tokens)
            if score > best_score:
                best_cluster = candidate
                best_score = score
        if best_cluster is not None and best_score >= 0.45:
            cluster = best_cluster

    if cluster is None:
        canonical_industry = str(industry or "").strip()
        canonical_sector = str(sector or "").strip()
        cluster_id = _normalize_label(canonical_industry) or _normalize_label(canonical_sector) or "unknown"
        return {
            "cluster_id": cluster_id,
            "family": _normalize_label(canonical_sector) or "unknown",
            "canonical_industry": canonical_industry,
            "canonical_sector": canonical_sector,
            "related_industries": [],
            "keywords": sorted(_tokens(industry)),
        }

    canonical_industry = str(cluster.get("canonical_industry") or "").strip()
    canonical_sector = str(cluster.get("sector") or sector or "").strip()
    return {
        "cluster_id": str(cluster.get("id") or _normalize_label(canonical_industry) or "unknown"),
        "family": str(cluster.get("family") or _normalize_label(canonical_sector) or "unknown"),
        "canonical_industry": canonical_industry,
        "canonical_sector": canonical_sector,
        "related_industries": list(cluster.get("related_industries") or []),
        "keywords": [str(item) for item in list(cluster.get("keywords") or [])],
    }


def industry_similarity(
    subject_industry: str,
    candidate_industry: str,
    *,
    subject_sector: str = "",
    candidate_sector: str = "",
) -> float:
    subject = resolve_industry_taxonomy(subject_industry, subject_sector)
    candidate = resolve_industry_taxonomy(candidate_industry, candidate_sector)

    if subject["canonical_industry"] and subject["canonical_industry"] == candidate["canonical_industry"]:
        return 1.0
    if candidate["canonical_industry"] in set(subject.get("related_industries") or []):
        return 0.72
    if subject["canonical_industry"] in set(candidate.get("related_industries") or []):
        return 0.72
    if subject["family"] and subject["family"] == candidate["family"]:
        return 0.6

    subject_tokens = _tokens(subject_industry)
    candidate_tokens = _tokens(candidate_industry)
    if subject_tokens and candidate_tokens:
        overlap = len(subject_tokens & candidate_tokens) / len(subject_tokens | candidate_tokens)
    else:
        overlap = 0.0

    if _normalize_label(subject_sector) and _normalize_label(subject_sector) == _normalize_label(candidate_sector):
        return round(max(overlap, 0.35 if overlap > 0 else 0.0), 4)
    return round(overlap * 0.45, 4)


def related_industries(industry: str, sector: str = "") -> list[str]:
    resolved = resolve_industry_taxonomy(industry, sector)
    items = [resolved.get("canonical_industry") or "", *list(resolved.get("related_industries") or [])]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


__all__ = [
    "INDUSTRY_TAXONOMY_PATH",
    "industry_similarity",
    "load_industry_taxonomy",
    "related_industries",
    "resolve_industry_taxonomy",
]