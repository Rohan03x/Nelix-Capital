"""Target-separated in-memory datasets for learning labels."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .labels import LearningLabel, build_labels


DATASET_SCOPES = {
    "operating_revenue": {"operating_revenue"},
    "operating_margin": {"operating_margin"},
    "cashflow": {"cashflow"},
    "valuation_ev": {"valuation_ev"},
    "valuation_price": {"valuation_price"},
    "market_implied": {"market_implied"},
    "direction": {"direction"},
}


@dataclass(frozen=True)
class LearningDataset:
    name: str
    labels: list[LearningLabel]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "labels": [label.to_dict() for label in self.labels],
            "diagnostics": dict(self.diagnostics),
        }


def _diagnostics(labels: list[LearningLabel]) -> dict[str, Any]:
    observation_types = Counter(label.observation_type for label in labels)
    targets = Counter(label.target_name for label in labels)
    quality_scores = [float(label.quality_score) for label in labels]
    return {
        "rows": len(labels),
        "target_counts": dict(targets),
        "observation_type_counts": dict(observation_types),
        "average_quality_score": round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else 0.0,
    }


def build_learning_datasets(records: Iterable[Any]) -> dict[str, LearningDataset]:
    labels = build_labels(records)
    datasets: dict[str, LearningDataset] = {}
    for name, scopes in DATASET_SCOPES.items():
        scoped = [label for label in labels if label.eligibility_scope in scopes]
        datasets[name] = LearningDataset(name=name, labels=scoped, diagnostics=_diagnostics(scoped))
    return datasets
