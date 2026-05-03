"""Tests for data/validator.py — strict FMP data quality gate."""
import pytest
from auto_valuation.data.validator import (
    DataQualityError,
    DataCheck,
    validate_fmp_data_strict,
    get_data_quality_warnings,
)


def _make_is(revenue=5000, ebit=500, net_income=400, year="2023"):
    return {
        "calendarYear": year,
        "revenue": revenue,
        "ebit": ebit,
        "netIncome": net_income,
        "grossProfit": revenue * 0.40,
        "depreciationAndAmortization": 100,
        "stockBasedCompensation": 50,
        "incomeTaxExpense": 100,
    }


def _make_bs(year="2023"):
    return {
        "calendarYear": year,
        "totalAssets": 10000,
        "totalEquity": 5000,
        "totalLiabilitiesAndStockholdersEquity": 10000,
        "cashAndCashEquivalents": 1000,
        "totalCurrentAssets": 3000,
        "totalCurrentLiabilities": 2000,
        "shortTermDebt": 500,
        "longTermDebt": 2500,
    }


def _make_cf(year="2023"):
    return {
        "calendarYear": year,
        "operatingCashFlow": 600,
        "capitalExpenditure": -200,
    }


def _make_3yr_data():
    years = ["2023", "2022", "2021"]
    is_ = [_make_is(year=y) for y in years]
    bs_ = [_make_bs(year=y) for y in years]
    cf_ = [_make_cf(year=y) for y in years]
    return is_, bs_, cf_


class TestValidateFmpDataStrict:
    def test_valid_data_passes(self):
        is_, bs_, cf_ = _make_3yr_data()
        checks = validate_fmp_data_strict(is_, bs_, cf_)
        assert isinstance(checks, list)
        # All checks should be passing (no HALT)
        halts = [c for c in checks if c.severity == "HALT" and not c.passed]
        assert len(halts) == 0

    def test_negative_revenue_raises(self):
        is_, bs_, cf_ = _make_3yr_data()
        is_[0]["revenue"] = -1000
        with pytest.raises(DataQualityError) as exc_info:
            validate_fmp_data_strict(is_, bs_, cf_)
        assert "revenue" in str(exc_info.value).lower() or "negative" in str(exc_info.value).lower()

    def test_zero_revenue_raises(self):
        is_, bs_, cf_ = _make_3yr_data()
        is_[0]["revenue"] = 0
        with pytest.raises(DataQualityError):
            validate_fmp_data_strict(is_, bs_, cf_)

    def test_insufficient_is_years_raises(self):
        is_ = [_make_is(year="2023")]   # only 1 year
        bs_ = [_make_bs(year=y) for y in ["2023", "2022"]]
        cf_ = [_make_cf(year=y) for y in ["2023", "2022"]]
        with pytest.raises(DataQualityError):
            validate_fmp_data_strict(is_, bs_, cf_, min_is_years=3)

    def test_missing_critical_is_field_raises(self):
        is_, bs_, cf_ = _make_3yr_data()
        for stmt in is_:
            stmt["ebit"] = None
        with pytest.raises(DataQualityError):
            validate_fmp_data_strict(is_, bs_, cf_)

    def test_missing_critical_bs_field_raises(self):
        is_, bs_, cf_ = _make_3yr_data()
        for bs in bs_:
            bs["totalAssets"] = None
        with pytest.raises(DataQualityError):
            validate_fmp_data_strict(is_, bs_, cf_)

    def test_missing_critical_cf_field_raises(self):
        is_, bs_, cf_ = _make_3yr_data()
        for cf in cf_:
            cf["operatingCashFlow"] = None
        with pytest.raises(DataQualityError):
            validate_fmp_data_strict(is_, bs_, cf_)

    def test_returns_datacheck_objects(self):
        is_, bs_, cf_ = _make_3yr_data()
        checks = validate_fmp_data_strict(is_, bs_, cf_)
        for c in checks:
            assert isinstance(c, DataCheck)
            assert c.severity in ("HALT", "WARN", "INFO")

    def test_dataqualityerror_contains_checks(self):
        is_, bs_, cf_ = _make_3yr_data()
        is_[0]["revenue"] = -1000
        try:
            validate_fmp_data_strict(is_, bs_, cf_)
            assert False, "Should have raised"
        except DataQualityError as e:
            assert isinstance(e.checks, list)
            assert len(e.checks) >= 1


class TestGetDataQualityWarnings:
    def test_returns_strings(self):
        checks = [
            DataCheck("OK_REVENUE", True, "INFO", ""),
            DataCheck("WARN_FIELD", True, "WARN", "Some field missing"),
        ]
        warnings = get_data_quality_warnings(checks)
        assert isinstance(warnings, list)
        assert "Some field missing" in warnings

    def test_filters_non_warn(self):
        checks = [
            DataCheck("OK_REVENUE", True, "INFO", "info message"),
            DataCheck("WARN_FIELD", True, "WARN", "warn message"),
        ]
        warnings = get_data_quality_warnings(checks)
        assert "info message" not in warnings
        assert "warn message" in warnings

    def test_empty_input(self):
        warnings = get_data_quality_warnings([])
        assert warnings == []
