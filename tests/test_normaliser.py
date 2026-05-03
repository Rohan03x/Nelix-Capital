"""Tests for data/normaliser.py."""
import pytest
from auto_valuation.data.normaliser import (
    detect_units,
    detect_units_scale,
    normalize_units,
    check_unit_consistency,
    UNIT_ANOMALY_THRESHOLD,
)


class TestDetectUnits:
    def test_millions_range(self):
        stmts = [{"revenue": 394_000}]   # 394,000 → millions
        assert detect_units(stmts) == "millions"

    def test_billions_range(self):
        stmts = [{"revenue": 394.0}]   # 394 → billions
        assert detect_units(stmts) == "billions"

    def test_thousands_range(self):
        stmts = [{"revenue": 394_000_000}]  # 394,000,000 → thousands
        result = detect_units(stmts)
        assert result == "thousands"

    def test_empty_returns_unknown(self):
        assert detect_units([]) == "unknown"

    def test_none_revenue_falls_to_unknown(self):
        stmts = [{"revenue": None, "totalRevenue": None}]
        result = detect_units(stmts)
        assert result == "unknown"


class TestDetectUnitsScale:
    def test_millions_scale(self):
        stmts = [{"revenue": 394_000}]
        scale = detect_units_scale(stmts)
        assert scale == pytest.approx(1.0)

    def test_billions_scale(self):
        stmts = [{"revenue": 394.0}]
        scale = detect_units_scale(stmts)
        assert scale == pytest.approx(1000.0)

    def test_thousands_scale(self):
        stmts = [{"revenue": 394_000_000}]
        scale = detect_units_scale(stmts)
        assert scale == pytest.approx(0.001)


class TestNormalizeUnits:
    def test_passthrough_millions(self):
        stmts = [{"revenue": 394_000, "netIncome": 99_803, "date": "2023-12-31"}]
        result = normalize_units(stmts)
        # Already in millions → scale=1.0 → same list returned
        assert result[0]["revenue"] == pytest.approx(394_000)

    def test_scales_down_thousands(self):
        stmts = [{"revenue": 394_000_000}]  # in thousands
        result = normalize_units(stmts, scale=0.001)
        assert result[0]["revenue"] == pytest.approx(394_000)

    def test_non_numeric_preserved(self):
        stmts = [{"revenue": 394_000, "date": "2023-12-31", "symbol": "AAPL"}]
        result = normalize_units(stmts, scale=1.0)
        assert result[0]["date"] == "2023-12-31"
        assert result[0]["symbol"] == "AAPL"

    def test_empty_returns_empty(self):
        result = normalize_units([])
        assert result == []


class TestCheckUnitConsistency:
    def test_consistent_records_no_anomaly(self):
        stmts = [
            {"date": "2021-12-31", "revenue": 380_000},
            {"date": "2022-12-31", "revenue": 394_000},
            {"date": "2023-12-31", "revenue": 383_285},
        ]
        results = check_unit_consistency(stmts)
        assert isinstance(results, list)
        assert not any(r["anomaly"] for r in results)

    def test_anomalous_jump_flagged(self):
        stmts = [
            {"date": "2021-12-31", "revenue": 383_285},
            {"date": "2022-12-31", "revenue": 383_285_000},  # 1000x jump
        ]
        results = check_unit_consistency(stmts)
        assert any(r["anomaly"] for r in results)

    def test_returns_list_of_dicts(self):
        stmts = [
            {"date": "2022-12-31", "revenue": 100_000},
            {"date": "2023-12-31", "revenue": 110_000},
        ]
        results = check_unit_consistency(stmts)
        for r in results:
            assert "from_year" in r
            assert "to_year" in r
            assert "ratio" in r
            assert "anomaly" in r

    def test_single_record_returns_empty(self):
        stmts = [{"date": "2023-12-31", "revenue": 394_000}]
        results = check_unit_consistency(stmts)
        assert results == []
