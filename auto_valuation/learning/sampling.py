"""Quality-weighted stratified sampling for learning evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from .quality import LearningObservationQuality, assess_prediction_record


@dataclass(frozen=True)
class StratifiedSampleResult:
    records: list[Any]
    diagnostics: dict[str, Any]


def _run_date(record: Any) -> date:
    value = getattr(record, "run_date", None)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return date.min


def _bucket(record: Any, quality: LearningObservationQuality) -> tuple[str, str, str, str, str]:
    return (
        str(getattr(record, "sector", "") or "Unknown"),
        str(getattr(record, "market_cap_regime", "") or "unknown"),
        str(getattr(record, "macro_regime", "") or "unknown"),
        quality.observation_type,
        quality.quality_tier,
    )


def stratified_sample_records(
    records: Iterable[Any],
    *,
    max_records: int,
    target: str = "full_dcf",
) -> StratifiedSampleResult:
    eligible_buckets: dict[tuple[str, str, str, str, str], list[tuple[Any, LearningObservationQuality]]] = defaultdict(list)
    exclusion_reasons: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    observation_type_counts: Counter[str] = Counter()
    total_rows = 0
    quality_sum = 0.0

    for record in records:
        total_rows += 1
        quality = assess_prediction_record(record)
        observation_type_counts[quality.observation_type] += 1
        quality_sum += float(quality.quality_score)
        for target_name, is_eligible in quality.target_eligibility.items():
            if is_eligible:
                target_counts[target_name] += 1
        if not quality.eligible(target):
            for reason in quality.hard_exclusion_reasons or [f"not_eligible_for_{target}"]:
                exclusion_reasons[reason] += 1
            continue
        eligible_buckets[_bucket(record, quality)].append((record, quality))

    for bucket_records in eligible_buckets.values():
        bucket_records.sort(key=lambda item: (item[1].quality_score, _run_date(item[0])), reverse=True)

    selected: list[Any] = []
    bucket_keys = sorted(eligible_buckets, key=lambda key: len(eligible_buckets[key]), reverse=True)
    while len(selected) < max_records and bucket_keys:
        next_keys: list[tuple[str, str, str, str, str]] = []
        for key in bucket_keys:
            if len(selected) >= max_records:
                break
            rows = eligible_buckets[key]
            if not rows:
                continue
            selected.append(rows.pop(0)[0])
            if rows:
                next_keys.append(key)
        bucket_keys = next_keys

    remaining = sum(len(rows) for rows in eligible_buckets.values())
    selected_quality = [assess_prediction_record(record).quality_score for record in selected]
    eligible_rows = remaining + len(selected)
    diagnostics = {
        "enabled": True,
        "target": target,
        "candidate_rows": total_rows,
        "selected_rows": len(selected),
        "eligible_rows": eligible_rows,
        "excluded_rows": max(0, total_rows - eligible_rows),
        "average_quality_score": round((sum(selected_quality) / len(selected_quality)) if selected_quality else 0.0, 3),
        "candidate_average_quality_score": round(quality_sum / total_rows, 3) if total_rows else 0.0,
        "bucket_count": len(eligible_buckets),
        "target_eligibility_counts": dict(target_counts),
        "observation_type_counts": dict(observation_type_counts),
        "top_exclusion_reasons": dict(exclusion_reasons.most_common(10)),
    }
    return StratifiedSampleResult(records=selected, diagnostics=diagnostics)
