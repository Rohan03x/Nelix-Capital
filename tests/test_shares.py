"""Tests for model/shares.py."""
import pytest
from auto_valuation.model.shares import (
    diluted_shares_tsm,
    compute_warrant_dilution,
    rollforward_basic_shares,
    rollforward_shares_forecast,
    compute_diluted_shares,
)


class TestDilutedSharesTsm:
    def test_no_dilution_when_otm(self):
        result = diluted_shares_tsm(
            basic_shares_mm=100.0,
            options_outstanding_mm=5.0,
            options_avg_strike=150.0,
            current_price=100.0,   # OTM: price < strike
        )
        assert result == pytest.approx(100.0)

    def test_dilution_when_itm(self):
        result = diluted_shares_tsm(
            basic_shares_mm=100.0,
            options_outstanding_mm=10.0,
            options_avg_strike=50.0,
            current_price=100.0,   # ITM
        )
        # TSM: net dilution = 10 * (1 - 50/100) = 5mm
        assert result == pytest.approx(105.0)

    def test_zero_options(self):
        result = diluted_shares_tsm(
            basic_shares_mm=200.0,
            options_outstanding_mm=0.0,
            options_avg_strike=50.0,
            current_price=100.0,
        )
        assert result == pytest.approx(200.0)

    def test_zero_price(self):
        result = diluted_shares_tsm(
            basic_shares_mm=100.0,
            options_outstanding_mm=5.0,
            options_avg_strike=50.0,
            current_price=0.0,
        )
        assert result == pytest.approx(100.0)   # no price → no dilution

    def test_rsus_added_at_face_value(self):
        result = diluted_shares_tsm(
            basic_shares_mm=100.0,
            restricted_stock_units_mm=3.0,
        )
        assert result == pytest.approx(103.0)


class TestComputeWarrantDilution:
    def test_basic_warrant_dilution(self):
        # Warrants ITM: 10 * (1 - 50/100) = 5mm dilution; function returns total diluted
        result = compute_warrant_dilution(
            basic_shares_mm=100.0,
            warrants_outstanding_mm=10.0,
            warrant_strike=50.0,
            current_price=100.0,
        )
        assert result == pytest.approx(105.0)

    def test_otm_warrants_no_dilution(self):
        result = compute_warrant_dilution(
            basic_shares_mm=100.0,
            warrants_outstanding_mm=5.0,
            warrant_strike=200.0,
            current_price=100.0,
        )
        assert result == pytest.approx(100.0)


class TestRollforwardBasicShares:
    def test_buyback_reduces_shares(self):
        result = rollforward_basic_shares(
            opening_shares_mm=100.0,
            buybacks_mm=5.0,
        )
        assert result == pytest.approx(95.0)

    def test_issuance_increases_shares(self):
        result = rollforward_basic_shares(
            opening_shares_mm=100.0,
            new_issuances_mm=10.0,
        )
        assert result == pytest.approx(110.0)

    def test_net_change(self):
        result = rollforward_basic_shares(
            opening_shares_mm=100.0,
            buybacks_mm=3.0,
            new_issuances_mm=2.0,
        )
        assert result == pytest.approx(99.0)

    def test_cannot_go_negative(self):
        result = rollforward_basic_shares(
            opening_shares_mm=10.0,
            buybacks_mm=100.0,
        )
        assert result == pytest.approx(0.0)


class TestRollforwardSharesForecast:
    def test_returns_list_of_correct_length(self):
        result = rollforward_shares_forecast(
            opening_basic_mm=100.0,
            forecast_years=5,
        )
        assert len(result) == 5

    def test_each_entry_has_required_keys(self):
        result = rollforward_shares_forecast(
            opening_basic_mm=100.0,
            forecast_years=3,
        )
        for entry in result:
            assert "year" in entry
            assert "basic_shares_mm" in entry
            assert "diluted_shares_mm" in entry

    def test_diluted_ge_basic(self):
        result = rollforward_shares_forecast(
            opening_basic_mm=100.0,
            forecast_years=3,
            options_mm=10.0,
            options_strike=50.0,
            current_price=100.0,
        )
        for entry in result:
            assert entry["diluted_shares_mm"] >= entry["basic_shares_mm"]


class TestComputeDilutedShares:
    def test_basic_itm_dilution(self):
        result = compute_diluted_shares(
            basic_shares_mm=100.0,
            options_outstanding_mm=10.0,
            options_avg_strike=50.0,
            current_price=100.0,
        )
        assert result > 100.0

    def test_no_dilutive_securities(self):
        result = compute_diluted_shares(
            basic_shares_mm=100.0,
            current_price=100.0,
        )
        assert result == pytest.approx(100.0)

    def test_convertibles_add_shares(self):
        result = compute_diluted_shares(
            basic_shares_mm=100.0,
            convertible_shares_mm=5.0,
            current_price=100.0,
        )
        assert result == pytest.approx(105.0)

    def test_itm_warrants_add_dilution(self):
        result = compute_diluted_shares(
            basic_shares_mm=100.0,
            warrants_outstanding_mm=10.0,
            warrant_strike=50.0,
            current_price=100.0,
        )
        # Net warrants: 10 * (1 - 50/100) = 5mm
        assert result == pytest.approx(105.0)
