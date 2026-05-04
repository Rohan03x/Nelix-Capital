"""Relationship-graph memory for cross-symbol learning."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from .cross_industry import AnalogMatch, AnalogSet, cosine_similarity
from .feature_space import SymbolFeatures


_ROLE_STYLES = {
    "subject": {"fill": "#0f172a", "stroke": "#38bdf8", "text": "#f8fafc"},
    "analog": {"fill": "#115e59", "stroke": "#5eead4", "text": "#ecfeff"},
    "realized-peer": {"fill": "#9a3412", "stroke": "#fdba74", "text": "#fff7ed"},
    "other": {"fill": "#334155", "stroke": "#cbd5e1", "text": "#f8fafc"},
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _score_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.65:
        return "moderate"
    if score >= 0.5:
        return "guarded"
    return "low"


def _feature_vector(item: Any) -> Any:
    vector = _get(item, "feature_vector")
    if vector:
        return vector
    feature_map = _get(item, "feature_map")
    if feature_map:
        return feature_map
    return None


def _safe_similarity(left: Any, right: Any) -> float:
    if left is None or right is None:
        return 0.0
    try:
        return _clamp(float(cosine_similarity(left, right)), 0.0, 1.0)
    except Exception:
        return 0.0


def _role_style(role: str) -> dict[str, str]:
    return dict(_ROLE_STYLES.get(role, _ROLE_STYLES["other"]))


def _short_label(ticker: str) -> str:
    label = str(ticker or "").strip().upper()
    if not label:
        return "?"
    core = label.split(".", 1)[0]
    if len(core) <= 8:
        return core
    return core[:7]


def _empty_visualization() -> dict[str, Any]:
    return {
        "width": 560,
        "height": 340,
        "view_box": "0 0 560 340",
        "nodes": [],
        "edges": [],
        "legend": [
            {"role": "subject", "label": "Subject ticker", **_role_style("subject")},
            {"role": "analog", "label": "Analog memory", **_role_style("analog")},
            {"role": "realized-peer", "label": "Realized peer", **_role_style("realized-peer")},
        ],
    }


def _arc_positions(
    count: int,
    *,
    start_degrees: float,
    end_degrees: float,
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
) -> list[tuple[float, float]]:
    if count <= 0:
        return []
    if count == 1:
        angles = [math.radians((start_degrees + end_degrees) / 2.0)]
    else:
        step = (end_degrees - start_degrees) / max(count - 1, 1)
        angles = [math.radians(start_degrees + step * index) for index in range(count)]
    return [
        (
            center_x + math.cos(angle) * radius_x,
            center_y + math.sin(angle) * radius_y,
        )
        for angle in angles
    ]


def _build_visualization(subject_ticker: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    width = 560
    height = 340
    center_x = 280.0
    center_y = 170.0

    subject_node = next((node for node in nodes if str(node.get("role") or "") == "subject"), None)
    other_nodes = [node for node in nodes if node is not subject_node]
    analog_nodes = sorted(
        [node for node in other_nodes if str(node.get("role") or "") == "analog"],
        key=lambda item: (float(item.get("score") or 0.0), str(item.get("ticker") or "")),
        reverse=True,
    )
    realized_nodes = sorted(
        [node for node in other_nodes if str(node.get("role") or "") == "realized-peer"],
        key=lambda item: (float(item.get("score") or 0.0), str(item.get("ticker") or "")),
        reverse=True,
    )
    extra_nodes = sorted(
        [node for node in other_nodes if node not in analog_nodes and node not in realized_nodes],
        key=lambda item: (float(item.get("score") or 0.0), str(item.get("ticker") or "")),
        reverse=True,
    )

    positioned: dict[str, dict[str, Any]] = {}
    if subject_node is not None:
        style = _role_style("subject")
        positioned[str(subject_node.get("ticker") or subject_ticker)] = {
            **subject_node,
            **style,
            "x": round(center_x, 1),
            "y": round(center_y, 1),
            "radius": 26.0,
            "short_label": _short_label(str(subject_node.get("ticker") or subject_ticker)),
            "label_y": round(center_y + 42.0, 1),
            "label_position": "below",
        }

    grouped_positions = [
        (
            analog_nodes,
            _arc_positions(
                len(analog_nodes),
                start_degrees=-160,
                end_degrees=-20,
                center_x=center_x,
                center_y=center_y,
                radius_x=198.0,
                radius_y=118.0,
            ),
        ),
        (
            realized_nodes,
            _arc_positions(
                len(realized_nodes),
                start_degrees=20,
                end_degrees=160,
                center_x=center_x,
                center_y=center_y,
                radius_x=198.0,
                radius_y=118.0,
            ),
        ),
        (
            extra_nodes,
            _arc_positions(
                len(extra_nodes),
                start_degrees=170,
                end_degrees=350,
                center_x=center_x,
                center_y=center_y,
                radius_x=228.0,
                radius_y=138.0,
            ),
        ),
    ]

    for node_group, positions in grouped_positions:
        for node, (x_pos, y_pos) in zip(node_group, positions):
            ticker = str(node.get("ticker") or "")
            if not ticker:
                continue
            score = _clamp(float(node.get("score") or 0.0), 0.0, 1.0)
            style = _role_style(str(node.get("role") or "other"))
            label_position = "above" if y_pos > center_y else "below"
            label_y = y_pos - (18.0 + score * 8.0) if label_position == "above" else y_pos + (26.0 + score * 8.0)
            positioned[ticker] = {
                **node,
                **style,
                "x": round(x_pos, 1),
                "y": round(y_pos, 1),
                "radius": round(15.0 + score * 10.0, 1),
                "short_label": _short_label(ticker),
                "label_y": round(label_y, 1),
                "label_position": label_position,
            }

    visual_edges: list[dict[str, Any]] = []
    for edge in edges:
        source = positioned.get(str(edge.get("source") or ""))
        target = positioned.get(str(edge.get("target") or ""))
        if source is None or target is None:
            continue
        weight = _clamp(float(edge.get("weight") or 0.0), 0.0, 1.0)
        visual_edges.append(
            {
                **edge,
                "x1": source["x"],
                "y1": source["y"],
                "x2": target["x"],
                "y2": target["y"],
                "stroke_width": round(1.2 + 3.8 * weight, 1),
                "opacity": round(_clamp(0.18 + 0.68 * weight, 0.18, 0.92), 2),
            }
        )

    return {
        "width": width,
        "height": height,
        "view_box": f"0 0 {width} {height}",
        "nodes": list(positioned.values()),
        "edges": visual_edges,
        "legend": [
            {"role": "subject", "label": "Subject ticker", **_role_style("subject")},
            {"role": "analog", "label": "Analog memory", **_role_style("analog")},
            {"role": "realized-peer", "label": "Realized peer", **_role_style("realized-peer")},
        ],
    }


@dataclass(frozen=True)
class _GraphSignal:
    ticker: str
    sector: str
    industry: str
    role: str
    feature_vector: Any
    similarity: float
    strength: float
    predictive_usefulness: float
    revenue_delta: float
    margin_delta_pp: float
    valuation_delta: float
    rationale: str


def _signal_from_match(match: AnalogMatch) -> _GraphSignal:
    usefulness = _clamp(float(match.usefulness_weight or match.analog.predictive_usefulness or 0.5), 0.25, 1.0)
    evidence_labels = [
        str(row.get("label") or row.get("dimension") or "signal")
        for row in list(match.evidence or ())[:2]
        if isinstance(row, dict)
    ]
    rationale = (
        f"Analog fingerprint aligned on {', '.join(evidence_labels)}."
        if evidence_labels
        else "Analog fingerprint aligned on operating and regime similarity."
    )
    return _GraphSignal(
        ticker=match.analog.ticker,
        sector=match.analog.sector,
        industry=match.analog.industry,
        role="analog",
        feature_vector=match.analog.feature_vector or match.analog.feature_map,
        similarity=_clamp(float(match.similarity_score or 0.0), 0.0, 1.0),
        strength=_clamp(float(match.analog_score or 0.0), 0.0, 1.0),
        predictive_usefulness=usefulness,
        revenue_delta=float(match.analog.outcome_revenue_cagr_5y or 0.0),
        margin_delta_pp=float(match.analog.outcome_margin_change_bps or 0.0) / 100.0,
        valuation_delta=float(match.analog.outcome_ev_multiple_change or 0.0),
        rationale=rationale,
    )


def _signal_from_observation(
    observation: Any,
    *,
    subject_vector: Any,
) -> _GraphSignal | None:
    ticker = str(_get(observation, "ticker", "") or "").upper()
    if not ticker:
        return None
    feature_vector = _feature_vector(observation)
    similarity = _safe_similarity(subject_vector, feature_vector)
    if similarity <= 0:
        return None

    predicted_revenue = _get(observation, "predicted_revenue_growth")
    actual_revenue = _get(observation, "actual_revenue_growth")
    predicted_margin = _get(observation, "predicted_ebit_margin")
    actual_margin = _get(observation, "actual_ebit_margin")
    predicted_wacc = _get(observation, "predicted_wacc")
    actual_wacc = _get(observation, "actual_wacc")
    predicted_terminal_growth = _get(observation, "predicted_terminal_growth")
    actual_terminal_growth = _get(observation, "actual_terminal_growth")

    revenue_delta = 0.0
    if actual_revenue is not None and predicted_revenue is not None:
        revenue_delta = float(actual_revenue) - float(predicted_revenue)

    margin_delta_pp = 0.0
    if actual_margin is not None and predicted_margin is not None:
        margin_delta_pp = (float(actual_margin) - float(predicted_margin)) * 100.0

    valuation_delta = 0.0
    if actual_wacc is not None and predicted_wacc is not None:
        valuation_delta += (float(predicted_wacc) - float(actual_wacc)) * 2.0
    if actual_terminal_growth is not None and predicted_terminal_growth is not None:
        valuation_delta += (float(actual_terminal_growth) - float(predicted_terminal_growth)) * 3.0

    usefulness = _clamp(0.35 + 0.65 * similarity, 0.25, 1.0)
    structural_break = bool(_get(observation, "structural_break_flag", False) or _get(observation, "structural_break_detected", False))
    strength = similarity * usefulness * (0.82 if structural_break else 1.0)
    if structural_break:
        rationale = "Realized peer remains relevant, but structural-break hints keep the edge soft."
    else:
        rationale = "Realized peer reinforces the subject through similar fingerprint and regime behavior."

    return _GraphSignal(
        ticker=ticker,
        sector=str(_get(observation, "sector", "") or ""),
        industry=str(_get(observation, "industry", "") or ""),
        role="realized-peer",
        feature_vector=feature_vector,
        similarity=similarity,
        strength=_clamp(strength, 0.0, 1.0),
        predictive_usefulness=usefulness,
        revenue_delta=revenue_delta,
        margin_delta_pp=margin_delta_pp,
        valuation_delta=valuation_delta,
        rationale=rationale,
    )


def _disabled_graph() -> dict[str, Any]:
    return {
        "enabled": False,
        "confidence": 0.0,
        "node_count": 0,
        "edge_count": 0,
        "sector_span": 0,
        "density": 0.0,
        "candidate_pool_size": 0,
        "analog_pool_size": 0,
        "realized_candidate_count": 0,
        "role_counts": {"analog": 0, "realized_peer": 0},
        "connected_tickers": [],
        "central_nodes": [],
        "communities": [],
        "pathways": [],
        "nodes": [],
        "edges": [],
        "visualization": _empty_visualization(),
        "overlay": {
            "enabled": False,
            "revenue_growth_adj_pp": 0.0,
            "ebit_margin_adj_pp": 0.0,
            "wacc_adj_pp": 0.0,
            "terminal_growth_adj_pp": 0.0,
            "beta_adj": 0.0,
        },
        "summary": "Relationship graph is inactive until enough connected analog or realized peers are available.",
        "note": "Relationship graph is inactive until enough connected analog or realized peers are available.",
    }


def build_relationship_graph(
    *,
    ticker: str,
    subject_features: SymbolFeatures | dict[str, float] | tuple[float, ...] | list[float],
    analog_set: AnalogSet,
    observations: Iterable[Any],
    sector: str = "",
    industry: str = "",
    max_neighbors: int = 6,
    min_similarity: float = 0.58,
) -> dict[str, Any]:
    subject_ticker = str(ticker or "").upper()
    subject_vector = getattr(subject_features, "vector", None) or subject_features
    max_neighbors = max(3, int(max_neighbors or 6))
    observation_rows = list(observations or [])

    analog_pool = [_signal_from_match(match) for match in list(analog_set.analogs or [])]
    seen_tickers = {subject_ticker}
    seen_tickers.update(signal.ticker.upper() for signal in analog_pool if signal.ticker)

    realized_pool: list[_GraphSignal] = []
    for observation in observation_rows:
        signal = _signal_from_observation(observation, subject_vector=subject_vector)
        if signal is None or signal.ticker.upper() in seen_tickers:
            continue
        if signal.similarity < min_similarity:
            continue
        seen_tickers.add(signal.ticker.upper())
        realized_pool.append(signal)

    realized_pool.sort(key=lambda signal: (signal.strength, signal.similarity), reverse=True)
    if analog_pool and realized_pool:
        analog_budget = min(len(analog_pool), max(2, int(round(max_neighbors * 0.6))))
        analog_budget = min(analog_budget, max_neighbors - 1)
        realized_budget = min(len(realized_pool), max_neighbors - analog_budget)
    elif analog_pool:
        analog_budget = min(len(analog_pool), max_neighbors)
        realized_budget = 0
    else:
        analog_budget = 0
        realized_budget = min(len(realized_pool), max_neighbors)

    selected_analog_signals = analog_pool[:analog_budget]
    selected_realized_signals = realized_pool[:realized_budget]
    signals = list(selected_analog_signals)
    signals.extend(selected_realized_signals)
    if len(signals) < max_neighbors:
        for signal in analog_pool[analog_budget:]:
            signals.append(signal)
            if len(signals) >= max_neighbors:
                break
    if len(signals) < max_neighbors:
        for signal in realized_pool[realized_budget:]:
            signals.append(signal)
            if len(signals) >= max_neighbors:
                break
    if not signals:
        return _disabled_graph()

    def _industry_fit(sig_sector: str, sig_industry: str) -> float:
        """Return 1.0 when sector/industry is populated, lower for blank/Other."""
        industry_val = str(sig_industry or "").strip()
        sector_val = str(sig_sector or "").strip()
        if not industry_val or industry_val.lower() in {"other", "n/a", "unknown"}:
            return 0.60
        if not sector_val or sector_val.lower() in {"other", "n/a", "unknown"}:
            return 0.80
        return 1.0

    nodes = [
        {
            "ticker": subject_ticker,
            "sector": sector,
            "industry": industry,
            "role": "subject",
            "score": 1.0,
            "industry_fit": 1.0,
            "label": "Subject ticker",
        }
    ]
    for signal in signals:
        fit = _industry_fit(signal.sector, signal.industry)
        nodes.append(
            {
                "ticker": signal.ticker,
                "sector": signal.sector,
                "industry": signal.industry,
                "role": signal.role,
                "score": round(signal.strength, 3),
                "similarity": round(signal.similarity, 3),
                "usefulness": round(signal.predictive_usefulness, 2),
                "industry_fit": round(fit, 3),
                "peer_classification": (
                    "operating-analog" if signal.role == "analog" else "realized-spillover"
                ),
                "rationale": signal.rationale,
            }
        )

    edges: list[dict[str, Any]] = []
    for signal in signals:
        fit = _industry_fit(signal.sector, signal.industry)
        # Reduce edge weight for nodes with blank/Other industry metadata.
        edge_weight = _clamp(
            (0.55 * signal.similarity + 0.45 * signal.strength) * fit,
            0.0,
            1.0,
        )
        relationship = "analog-fingerprint" if signal.role == "analog" else "realized-spillover"
        edges.append(
            {
                "source": subject_ticker,
                "target": signal.ticker,
                "weight": round(edge_weight, 3),
                "relationship": relationship,
                "industry_fit": round(fit, 3),
                "rationale": signal.rationale,
            }
        )

    for left_index, left_signal in enumerate(signals):
        for right_signal in signals[left_index + 1 :]:
            pair_similarity = _safe_similarity(left_signal.feature_vector, right_signal.feature_vector)
            if pair_similarity < 0.72:
                continue
            pair_fit = min(
                _industry_fit(left_signal.sector, left_signal.industry),
                _industry_fit(right_signal.sector, right_signal.industry),
            )
            pair_weight = _clamp(
                pair_similarity
                * (0.55 + 0.25 * min(left_signal.predictive_usefulness, right_signal.predictive_usefulness))
                * pair_fit,
                0.0,
                1.0,
            )
            edges.append(
                {
                    "source": left_signal.ticker,
                    "target": right_signal.ticker,
                    "weight": round(pair_weight, 3),
                    "relationship": "peer-cluster",
                    "industry_fit": round(pair_fit, 3),
                    "rationale": "Connected through a similar operating fingerprint inside the broader symbol graph.",
                }
            )

    node_count = len(nodes)
    edge_count = len(edges)
    possible_edges = max((node_count * (node_count - 1)) / 2.0, 1.0)
    density = _clamp(edge_count / possible_edges, 0.0, 1.0)
    sector_span = len({signal.sector for signal in signals if signal.sector})
    average_strength = sum(signal.strength for signal in signals) / len(signals)
    average_similarity = sum(signal.similarity for signal in signals) / len(signals)
    confidence = _clamp(
        0.50 * average_strength
        + 0.20 * average_similarity
        + 0.15 * min(len(signals) / max_neighbors, 1.0)
        + 0.10 * density
        + 0.05 * min(sector_span / 3.0, 1.0),
        0.0,
        1.0,
    )

    weighted_signals = [
        signal.strength * signal.predictive_usefulness
        for signal in signals
    ]
    total_weight = sum(weighted_signals)
    if total_weight > 0:
        revenue_delta = sum(signal.revenue_delta * weight for signal, weight in zip(signals, weighted_signals)) / total_weight
        margin_delta_pp = sum(signal.margin_delta_pp * weight for signal, weight in zip(signals, weighted_signals)) / total_weight
        valuation_delta = sum(signal.valuation_delta * weight for signal, weight in zip(signals, weighted_signals)) / total_weight
    else:
        revenue_delta = 0.0
        margin_delta_pp = 0.0
        valuation_delta = 0.0

    damping = _clamp(0.08 + 0.16 * confidence, 0.08, 0.24)
    overlay = {
        "enabled": True,
        "revenue_growth_adj_pp": round(_clamp(revenue_delta * 100.0 * damping, -2.5, 2.5), 1),
        "ebit_margin_adj_pp": round(_clamp(margin_delta_pp * damping, -2.0, 2.0), 1),
        "wacc_adj_pp": round(_clamp(-valuation_delta * damping * 0.22, -0.6, 0.6), 1),
        "terminal_growth_adj_pp": round(_clamp(revenue_delta * 100.0 * damping * 0.06, -0.3, 0.3), 1),
        "beta_adj": round(_clamp(valuation_delta * damping * 0.04, -0.12, 0.12), 2),
    }

    communities: list[dict[str, Any]] = []
    grouped: dict[str, list[_GraphSignal]] = {}
    for signal in signals:
        label = signal.sector or signal.role
        grouped.setdefault(label, []).append(signal)
    for label, members in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)[:3]:
        communities.append(
            {
                "label": label,
                "count": len(members),
                "members": [member.ticker for member in members[:5]],
            }
        )

    central_nodes = [
        {
            "ticker": signal.ticker,
            "role": signal.role,
            "score": round(signal.strength, 3),
            "similarity": round(signal.similarity, 3),
            "label": _score_label(signal.strength),
        }
        for signal in sorted(signals, key=lambda item: (item.strength, item.similarity), reverse=True)[:4]
    ]
    pathways = [
        {
            "ticker": signal.ticker,
            "role": signal.role,
            "score": round(signal.strength, 3),
            "impact": (
                f"Growth {overlay['revenue_growth_adj_pp']:+.1f}pp, margin {overlay['ebit_margin_adj_pp']:+.1f}pp"
            ),
            "rationale": signal.rationale,
        }
        for signal in sorted(signals, key=lambda item: (item.strength, item.similarity), reverse=True)[:3]
    ]

    connected_tickers = [signal.ticker for signal in signals]
    role_counts = {
        "analog": sum(1 for signal in signals if signal.role == "analog"),
        "realized_peer": sum(1 for signal in signals if signal.role == "realized-peer"),
    }
    summary = (
        f"Relationship graph linked {len(signals)} connected symbol(s) across {sector_span or 1} sector(s) "
        f"with {_score_label(confidence)} confidence."
    )

    return {
        "enabled": True,
        "confidence": round(confidence, 2),
        "node_count": node_count,
        "edge_count": edge_count,
        "sector_span": sector_span,
        "density": round(density, 2),
        "candidate_pool_size": len(observation_rows),
        "analog_pool_size": len(analog_pool),
        "realized_candidate_count": len(realized_pool),
        "role_counts": role_counts,
        "connected_tickers": connected_tickers,
        "central_nodes": central_nodes,
        "communities": communities,
        "pathways": pathways,
        "nodes": nodes,
        "edges": edges,
        "visualization": _build_visualization(subject_ticker, nodes, edges),
        "overlay": overlay,
        "summary": summary,
        "note": summary,
    }


__all__ = ["build_relationship_graph"]