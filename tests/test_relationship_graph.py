from __future__ import annotations

from auto_valuation.learning.confidence import build_ranked_confidence_model
from auto_valuation.learning.cross_industry import AnalogMatch, AnalogObservation, AnalogSet, find_analogs
from auto_valuation.learning.feature_space import build_symbol_features
from auto_valuation.learning.relationship_graph import build_relationship_graph


def _subject_features():
    return build_symbol_features(
        ticker="SUBJ",
        sector="Industrials",
        industry="Machinery",
        revenues=[520.0, 560.0, 605.0, 655.0, 710.0, 770.0],
        ebit_margins=[8.5, 9.0, 9.4, 10.1, 10.8, 11.2],
        gross_margin_base_pct=39.0,
        capex_pct=3.2,
        total_assets=980.0,
        total_debt=120.0,
        revenue_base=770.0,
        operating_cf=155.0,
        fcf=102.0,
        da_pct=1.7,
        tax_rate_pct=22.0,
        market_cap=8_200.0,
        observation_year=2024,
    )


def test_build_relationship_graph_links_analogs_and_realized_peers():
    subject = _subject_features()
    analog = AnalogObservation(
        ticker="GRAPH",
        sector="Industrials",
        industry="Machinery",
        vintage_year=6,
        feature_map=dict(subject.feature_map),
        outcome_revenue_cagr_5y=0.09,
        outcome_margin_change_bps=120.0,
        outcome_ev_multiple_change=0.6,
        predictive_usefulness=0.87,
        as_of_year=2024,
    )
    analog_set = AnalogSet(
        subject_ticker="SUBJ",
        subject_features=subject,
        analogs=[
            AnalogMatch(
                analog=analog,
                similarity_score=0.93,
                sector_distance=1,
                analog_score=0.91,
                static_similarity=0.92,
                regime_similarity=0.88,
                usefulness_weight=0.87,
                evidence=(
                    {"label": "Gross Margin", "similarity": 0.94},
                    {"label": "FCF Conversion", "similarity": 0.91},
                ),
            )
        ],
        analog_confidence=0.82,
    )
    observations = [
        {
            "ticker": "PAIR",
            "sector": "Industrials",
            "industry": "Machinery",
            "feature_vector": tuple(subject.vector),
            "predicted_revenue_growth": 0.05,
            "actual_revenue_growth": 0.07,
            "predicted_ebit_margin": 0.11,
            "actual_ebit_margin": 0.12,
            "predicted_wacc": 0.09,
            "actual_wacc": 0.087,
            "predicted_terminal_growth": 0.025,
            "actual_terminal_growth": 0.026,
        }
    ]

    graph = build_relationship_graph(
        ticker="SUBJ",
        subject_features=subject,
        analog_set=analog_set,
        observations=observations,
        sector="Industrials",
        industry="Machinery",
    )

    assert graph["enabled"] is True
    assert graph["node_count"] >= 3
    assert graph["edge_count"] >= 2
    assert graph["overlay"]["enabled"] is True
    assert "GRAPH" in graph["connected_tickers"]
    assert graph["visualization"]["nodes"]
    assert graph["pathways"][0]["ticker"] in {"GRAPH", "PAIR"}


def test_relationship_graph_keeps_realized_peers_visible_when_analog_pool_is_large():
    subject = _subject_features()
    analogs = []
    for index in range(6):
        analog_obs = AnalogObservation(
            ticker=f"AN{index}",
            sector="Industrials",
            industry="Machinery",
            vintage_year=6,
            feature_map=dict(subject.feature_map),
            outcome_revenue_cagr_5y=0.04 + (index * 0.002),
            outcome_margin_change_bps=80.0 + (index * 5.0),
            outcome_ev_multiple_change=0.3 + (index * 0.02),
            predictive_usefulness=0.8,
            as_of_year=2024,
        )
        analogs.append(
            AnalogMatch(
                analog=analog_obs,
                similarity_score=0.9 - (index * 0.01),
                sector_distance=1,
                analog_score=0.88 - (index * 0.03),
                static_similarity=0.9 - (index * 0.01),
                regime_similarity=0.86,
                usefulness_weight=0.82,
            )
        )

    analog_set = AnalogSet(
        subject_ticker="SUBJ",
        subject_features=subject,
        analogs=analogs,
        analog_confidence=0.8,
    )
    observations = [
        {
            "ticker": f"RP{index}",
            "sector": "Industrials",
            "industry": "Machinery",
            "feature_vector": tuple(subject.vector),
            "predicted_revenue_growth": 0.05,
            "actual_revenue_growth": 0.06 + (index * 0.003),
            "predicted_ebit_margin": 0.11,
            "actual_ebit_margin": 0.12,
            "predicted_wacc": 0.09,
            "actual_wacc": 0.088,
            "predicted_terminal_growth": 0.025,
            "actual_terminal_growth": 0.026,
        }
        for index in range(4)
    ]

    graph = build_relationship_graph(
        ticker="SUBJ",
        subject_features=subject,
        analog_set=analog_set,
        observations=observations,
        sector="Industrials",
        industry="Machinery",
        max_neighbors=6,
    )

    assert graph["role_counts"]["analog"] >= 3
    assert graph["role_counts"]["realized_peer"] >= 1
    assert graph["candidate_pool_size"] == 4
    assert len(graph["visualization"]["nodes"]) == graph["node_count"]
    assert all(0 <= node["x"] <= graph["visualization"]["width"] for node in graph["visualization"]["nodes"])
    assert all(0 <= node["y"] <= graph["visualization"]["height"] for node in graph["visualization"]["nodes"])


def test_confidence_model_benefits_from_relationship_graph():
    base_payload = {
        "calibration_confidence": 0.62,
        "learning_confidence": 0.60,
        "calibration_cohort_size": 7,
        "history_window_years": 5,
        "pattern_match_score": 0.72,
        "scenario_width_multiplier": 1.1,
        "wacc": 9.1,
        "terminal_growth": 2.5,
        "global_learning": {"confidence": 0.58, "cohort_size": 8, "sector_span": 3},
        "analogs": {
            "count": 2,
            "pattern_match_score": 0.72,
            "items": [
                {"score": 0.82, "similarity": 0.87},
                {"score": 0.77, "similarity": 0.81},
            ],
        },
        "layered_learning": {
            "layer_mix": {
                "company_memory": {"records": 1},
                "cohort_memory": {"records": 7},
                "sector_memory": {"records": 9},
                "analog_memory": {"confidence": 0.72},
            },
            "uncertainty": {"weak_evidence": False, "conflict_score": 0.009, "scenario_width_multiplier": 1.1},
            "structural_break": {"score": 0.05},
        },
        "explainability": {
            "company_memory": {"history_window_years": 5, "completed_years": 6, "review_due": False},
            "forecast_layers": [
                {"company_anchor": 6.1, "sector_anchor": 5.6, "learned_adjustment": 0.4},
            ],
        },
    }

    with_graph = build_ranked_confidence_model(
        {
            **base_payload,
            "relationship_graph": {"enabled": True, "confidence": 0.8, "node_count": 5, "edge_count": 7, "sector_span": 3},
        }
    )
    without_graph = build_ranked_confidence_model(
        {
            **base_payload,
            "relationship_graph": {"enabled": False, "confidence": 0.0, "node_count": 0, "edge_count": 0, "sector_span": 0},
        }
    )

    labels = [component["label"] for component in with_graph["components"]]

    assert "Relational memory" in labels
    assert with_graph["assumption_confidence"]["score"] > without_graph["assumption_confidence"]["score"]
    assert with_graph["ranking_signal"] >= without_graph["ranking_signal"]


def test_find_analogs_deduplicates_same_ticker_candidates():
    subject = _subject_features()
    candidates = [
        AnalogObservation(
            ticker="DUPL",
            sector="Industrials",
            industry="Machinery",
            vintage_year=5,
            feature_map=dict(subject.feature_map),
            outcome_revenue_cagr_5y=0.08,
            outcome_margin_change_bps=90.0,
            outcome_ev_multiple_change=0.4,
            predictive_usefulness=0.88,
            as_of_year=2024,
        ),
        AnalogObservation(
            ticker="DUPL",
            sector="Industrials",
            industry="Machinery",
            vintage_year=6,
            feature_map=dict(subject.feature_map),
            outcome_revenue_cagr_5y=0.07,
            outcome_margin_change_bps=80.0,
            outcome_ev_multiple_change=0.35,
            predictive_usefulness=0.84,
            as_of_year=2024,
        ),
        AnalogObservation(
            ticker="UNIQ",
            sector="Industrials",
            industry="Machinery",
            vintage_year=6,
            feature_map=dict(subject.feature_map),
            outcome_revenue_cagr_5y=0.06,
            outcome_margin_change_bps=70.0,
            outcome_ev_multiple_change=0.3,
            predictive_usefulness=0.8,
            as_of_year=2024,
        ),
    ]

    analog_set = find_analogs(
        "SUBJ",
        subject,
        candidates,
        subject_sector="Industrials",
        subject_industry="Machinery",
        subject_vintage_year=6,
        subject_market_cap_regime="mid",
        subject_macro_regime="neutral",
        observation_year=2024,
        min_similarity=0.5,
        max_results=5,
        cross_sector_only=False,
    )

    tickers = [match.analog.ticker for match in analog_set.analogs]

    assert tickers.count("DUPL") == 1
    assert set(tickers) == {"DUPL", "UNIQ"}