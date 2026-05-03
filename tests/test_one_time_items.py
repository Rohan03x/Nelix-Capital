"""Tests for assumptions/one_time_items.py."""
import pytest
from auto_valuation.assumptions.one_time_items import (
    detect_restructuring,
    detect_impairments,
    normalize_ebit,
    build_normalized_history,
)


class TestDetectRestructuring:
    def test_above_threshold_flagged(self):
        stmts = [{
            "date": "2022-12-31",
            "revenue": 394_000,
            "restructuringCharges": 10_000,   # 2.54% > 2% threshold
        }]
        result = detect_restructuring(stmts)
        assert len(result) == 1
        assert result[0]["flagged"] is True

    def test_below_threshold_not_flagged(self):
        stmts = [{
            "date": "2022-12-31",
            "revenue": 394_000,
            "restructuringCharges": 100,   # 0.025% < threshold
        }]
        result = detect_restructuring(stmts)
        assert result[0]["flagged"] is False

    def test_zero_restructuring(self):
        stmts = [{"date": "2022-12-31", "revenue": 394_000, "restructuringCharges": 0}]
        result = detect_restructuring(stmts)
        assert result[0]["flagged"] is False

    def test_zero_revenue_no_crash(self):
        stmts = [{"date": "2022-12-31", "revenue": 0, "restructuringCharges": 100}]
        result = detect_restructuring(stmts)
        assert isinstance(result, list)

    def test_multi_year(self):
        stmts = [
            {"date": "2021-12-31", "revenue": 365_817, "restructuringCharges": 5_000},
            {"date": "2022-12-31", "revenue": 394_000, "restructuringCharges": 0},
        ]
        result = detect_restructuring(stmts)
        assert len(result) == 2


class TestDetectImpairments:
    def test_goodwill_impairment_flagged(self):
        stmts = [{
            "date": "2022-12-31",
            "revenue": 394_000,
            "goodwillImpairmentLosses": 25_000,  # 6.3% > 5% threshold
        }]
        result = detect_impairments(stmts)
        assert result[0]["flagged"] is True
        assert result[0]["goodwill_impairment_mm"] == pytest.approx(25_000)

    def test_asset_impairment_flagged(self):
        stmts = [{
            "date": "2022-12-31",
            "revenue": 394_000,
            "assetImpairmentCharges": 25_000,
        }]
        result = detect_impairments(stmts)
        assert result[0]["flagged"] is True

    def test_combined_impairments(self):
        stmts = [{
            "date": "2022-12-31",
            "revenue": 394_000,
            "goodwillImpairmentLosses": 3_000,
            "impairmentOfIntangibles": 2_000,
        }]
        result = detect_impairments(stmts)
        assert result[0]["total_impairment_mm"] == pytest.approx(5_000)

    def test_no_impairment(self):
        stmts = [{"date": "2022-12-31", "revenue": 394_000}]
        result = detect_impairments(stmts)
        assert result[0]["flagged"] is False


class TestNormalizeEbit:
    def test_addback_restructuring(self):
        normalized = normalize_ebit(ebit=119_000, restructuring_mm=5_000)
        assert normalized == pytest.approx(124_000)

    def test_no_charges_unchanged(self):
        normalized = normalize_ebit(ebit=119_000)
        assert normalized == pytest.approx(119_000)

    def test_combined_addbacks(self):
        normalized = normalize_ebit(
            ebit=119_000,
            restructuring_mm=5_000,
            impairment_mm=10_000,
            legal_settlement_mm=2_000,
        )
        assert normalized == pytest.approx(136_000)

    def test_gain_on_sale_deducted(self):
        normalized = normalize_ebit(ebit=119_000, gain_on_sale_mm=3_000)
        assert normalized == pytest.approx(116_000)


class TestBuildNormalizedHistory:
    def test_adds_ebit_normalized_field(self):
        stmts = [
            {"date": "2022-12-31", "revenue": 394_000, "ebit": 119_000,
             "restructuringCharges": 5_000, "calendarYear": "2022"},
        ]
        restr = detect_restructuring(stmts)
        result = build_normalized_history(stmts, restructuring_results=restr)
        assert "ebit_normalized" in result[0]

    def test_normalized_higher_than_reported(self):
        stmts = [
            {"date": "2022-12-31", "revenue": 394_000, "ebit": 119_000,
             "restructuringCharges": 5_000, "calendarYear": "2022"},
        ]
        restr = detect_restructuring(stmts)
        result = build_normalized_history(stmts, restructuring_results=restr)
        assert result[0]["ebit_normalized"] >= result[0]["ebit"]

    def test_no_adjustments_ebit_unchanged(self):
        stmts = [
            {"date": "2022-12-31", "revenue": 394_000, "ebit": 119_000, "calendarYear": "2022"},
        ]
        result = build_normalized_history(stmts)
        assert result[0]["ebit_normalized"] == pytest.approx(119_000)
