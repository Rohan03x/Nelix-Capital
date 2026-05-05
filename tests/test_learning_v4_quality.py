from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from auto_valuation.learning.datasets import build_learning_datasets
from auto_valuation.learning.ledger import PredictionRecord
from auto_valuation.learning.market_implied import build_market_residual_overlay, compute_market_implied_snapshot
from auto_valuation.learning.quality import assess_prediction_record
from auto_valuation.learning.sampling import stratified_sample_records


def _record(**overrides) -> PredictionRecord:
    payload = {
        "record_id": "pred-1",
        "ticker": "ACME",
        "company_name": "Acme Corp",
        "sector": "Technology",
        "industry": "Software",
        "run_date": date(2024, 1, 15),
        "forecast_horizon_year": 2025,
        "years_since_ipo": 6,
        "data_vintage_years": 6,
        "predicted_revenue_mm": 100.0,
        "predicted_ebit_margin": 0.15,
        "predicted_ebit_mm": 15.0,
        "predicted_ufcf_mm": 10.0,
        "predicted_wacc": 0.09,
        "predicted_terminal_growth": 0.025,
        "predicted_ev_mm": 100.0,
        "predicted_equity_value_mm": 90.0,
        "predicted_price_per_share": 9.0,
        "scenario": "base",
        "near_term_revenue_growth": 0.08,
        "target_ebit_margin": 0.18,
        "da_pct_revenue": 0.03,
        "capex_pct_revenue": 0.02,
        "beta": 1.1,
        "erp": 0.055,
        "rf_rate": 0.04,
        "actual_price_at_prediction": 10.0,
        "actual_ev_at_prediction": 95.0,
        "market_cycle_phase": "neutral",
        "macro_backdrop": {"rf_rate": 0.04},
        "actual_revenue_mm": 112.0,
        "actual_ebit_margin": 0.17,
        "actual_ufcf_mm": 12.0,
        "actual_ev_mm": 150.0,
        "actual_price_at_horizon": 12.0,
        "market_cap_regime": "mid",
        "macro_regime": "neutral",
        "feature_vector": (0.2, 0.15, 0.6),
        "prediction_context": {"source": "webapp_live_dashboard", "data_source": "eodhd"},
    }
    payload.update(overrides)
    return PredictionRecord(**payload)


def test_quality_excludes_invalid_valuation_rows_from_full_dcf():
    record = _record(predicted_ev_mm=0.0, predicted_price_per_share=0.0)

    quality = assess_prediction_record(record)

    assert quality.eligible("revenue") is True
    assert quality.eligible("full_dcf") is False
    assert quality.eligible("valuation_ev") is False
    assert "predicted_ev_too_small" in quality.hard_exclusion_reasons
    assert "predicted_price_too_small" in quality.hard_exclusion_reasons


def test_quarterly_rows_train_revenue_but_not_valuation():
    record = _record(
        record_id="ACME-2024-Q2025-03-31-base",
        predicted_ev_mm=0.0,
        predicted_equity_value_mm=0.0,
        predicted_price_per_share=0.0,
        predicted_ebit_margin=0.0,
        actual_ebit_margin=None,
        predicted_ufcf_mm=0.0,
        actual_ufcf_mm=None,
        actual_ev_mm=None,
        actual_price_at_horizon=None,
        prediction_context={"source": "webapp_live_dashboard_quarterly", "quarter_end": "2025-03-31"},
    )

    quality = assess_prediction_record(record)

    assert quality.observation_type == "quarterly_revenue"
    assert quality.eligible("revenue") is True
    assert quality.eligible("valuation_ev") is False
    assert quality.eligible("valuation_price") is False
    assert "quarterly_record_not_valuation_eligible" in quality.hard_exclusion_reasons


def test_target_separated_datasets_keep_quarterly_rows_out_of_valuation():
    annual = _record(record_id="annual")
    quarterly = _record(
        record_id="quarterly-Q2025-03-31",
        predicted_ev_mm=0.0,
        predicted_equity_value_mm=0.0,
        predicted_price_per_share=0.0,
        predicted_ebit_margin=0.0,
        actual_ebit_margin=None,
        predicted_ufcf_mm=0.0,
        actual_ufcf_mm=None,
        actual_ev_mm=None,
        actual_price_at_horizon=None,
        prediction_context={"source": "webapp_live_dashboard_quarterly", "quarter_end": "2025-03-31"},
    )

    datasets = build_learning_datasets([annual, quarterly])

    assert datasets["operating_revenue"].diagnostics["rows"] == 2
    assert datasets["valuation_ev"].diagnostics["rows"] == 1
    assert datasets["valuation_price"].diagnostics["rows"] == 1
    assert datasets["valuation_ev"].labels[0].record_id == "annual"


def test_market_implied_snapshot_and_overlay_use_quality_gated_ev_labels():
    records = [replace(_record(record_id=f"pred-{idx}"), actual_ev_mm=150.0 + idx) for idx in range(6)]

    snapshot = compute_market_implied_snapshot(records[0])
    overlay = build_market_residual_overlay(
        records,
        ticker="ACME",
        sector="Technology",
        industry="Software",
        market_cap_regime="mid",
        macro_regime="neutral",
    )

    assert snapshot is not None
    assert snapshot.valuation_residual_pct == pytest.approx(0.5)
    assert overlay["enabled"] is True
    assert overlay["cohort_size"] == 6
    assert overlay["applied_adjustment_decimal"] > 0


def test_market_overlay_rejects_extreme_ev_labels():
    record = _record(actual_ev_mm=900.0)

    assert compute_market_implied_snapshot(record) is None

    overlay = build_market_residual_overlay(
        [replace(_record(record_id=f"bad-{idx}"), actual_ev_mm=900.0) for idx in range(6)],
        ticker="ACME",
        sector="Technology",
        industry="Software",
        market_cap_regime="mid",
        macro_regime="neutral",
    )
    assert overlay["enabled"] is False
    assert overlay["reason"] == "insufficient_market_residual_evidence"


def test_market_overlay_can_apply_stronger_negative_optimism_correction():
    records = [replace(_record(record_id=f"over-{idx}"), actual_ev_mm=30.0) for idx in range(30)]

    overlay = build_market_residual_overlay(
        records,
        ticker="ACME",
        sector="Technology",
        industry="Software",
        market_cap_regime="mid",
        macro_regime="neutral",
    )

    assert overlay["enabled"] is True
    assert overlay["optimism_bias_signal"] is True
    assert overlay["applied_adjustment_decimal"] < -0.25


def test_stratified_sampler_does_not_drop_smaller_valid_segments():
    tech = [_record(record_id=f"tech-{idx}", ticker=f"TECH{idx}", sector="Technology") for idx in range(8)]
    health = [_record(record_id=f"health-{idx}", ticker=f"HLTH{idx}", sector="Healthcare") for idx in range(2)]

    sample = stratified_sample_records([*tech, *health], max_records=4, target="full_dcf")
    selected_sectors = {record.sector for record in sample.records}

    assert len(sample.records) == 4
    assert "Healthcare" in selected_sectors
    assert sample.diagnostics["selected_rows"] == 4
