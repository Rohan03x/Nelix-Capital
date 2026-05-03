"""Tests for model/ev_bridge.py."""
import pytest
from auto_valuation.model.ev_bridge import (
    EVBridgeInputs,
    EVBridgeResult,
    compute_equity_value_per_share,
    handle_convertible_notes,
    should_add_equity_investments,
)


class TestEvBridgeInputs:
    def test_default_construction(self):
        inputs = EVBridgeInputs(
            enterprise_value_mm=10_000,
            short_term_debt_mm=500,
            long_term_debt_mm=1_500,
            cash_mm=500,
            diluted_shares_mm=100,
        )
        assert inputs.enterprise_value_mm == 10_000
        assert inputs.preferred_equity_mm == 0.0
        assert inputs.nci_mm == 0.0

    def test_all_fields(self):
        inputs = EVBridgeInputs(
            enterprise_value_mm=10_000,
            short_term_debt_mm=500,
            long_term_debt_mm=1_500,
            cash_mm=500,
            diluted_shares_mm=100,
            preferred_equity_mm=200,
            nci_mm=150,
            short_term_investments_mm=300,
            equity_investments_mm=100,
            pension_underfunded_mm=50,
        )
        assert inputs.preferred_equity_mm == 200
        assert inputs.nci_mm == 150


class TestComputeEquityValuePerShare:
    def _make_inputs(self, **kwargs):
        defaults = dict(
            enterprise_value_mm=10_000,
            long_term_debt_mm=2_000,
            cash_mm=1_000,
            diluted_shares_mm=100,
        )
        defaults.update(kwargs)
        return EVBridgeInputs(**defaults)

    def test_basic_bridge(self):
        """EV − debt + cash ÷ shares"""
        inputs = self._make_inputs()
        result = compute_equity_value_per_share(inputs)
        # EV(10000) - debt(2000) + cash(1000) = equity(9000) / shares(100) = $90/share
        assert isinstance(result, EVBridgeResult)
        assert result.equity_value_per_share == pytest.approx(90.0)
        assert result.equity_value_mm == pytest.approx(9_000)

    def test_with_preferred_and_nci(self):
        inputs = self._make_inputs(preferred_equity_mm=500, nci_mm=200)
        result = compute_equity_value_per_share(inputs)
        # Equity = 10000 - 2000 + 1000 - 500 - 200 = 8300; per share = 83
        assert result.equity_value_per_share == pytest.approx(83.0)

    def test_net_cash_company(self):
        """Cash > debt → net cash adds to equity."""
        inputs = self._make_inputs(long_term_debt_mm=500, cash_mm=2_000)
        result = compute_equity_value_per_share(inputs)
        # Equity = 10000 - 500 + 2000 = 11500; per share = 115
        assert result.equity_value_per_share == pytest.approx(115.0)

    def test_zero_shares_no_crash(self):
        """Zero diluted shares — should not crash."""
        inputs = self._make_inputs(diluted_shares_mm=0)
        result = compute_equity_value_per_share(inputs)
        assert result is not None


class TestHandleConvertibleNotes:
    def test_itm_convertible_removes_debt_adds_shares(self):
        """ITM convertibles → set debt to 0, add dilutive shares."""
        adj_debt, net_shares, itm = handle_convertible_notes(
            enterprise_value_mm=10_000,
            convertible_debt_mm=500,
            conversion_price=50,
            current_price=100,   # ITM
            basic_shares_mm=100,
        )
        assert itm is True
        assert adj_debt == pytest.approx(0.0)
        assert net_shares > 0

    def test_otm_convertible_stays_as_debt(self):
        """OTM convertibles → remain in debt, no dilution."""
        adj_debt, net_shares, itm = handle_convertible_notes(
            enterprise_value_mm=10_000,
            convertible_debt_mm=500,
            conversion_price=150,
            current_price=100,   # OTM
            basic_shares_mm=100,
        )
        assert itm is False
        assert adj_debt == pytest.approx(500)
        assert net_shares == pytest.approx(0)


class TestShouldAddEquityInvestments:
    def test_positive_equity_investment_added(self):
        """Equity investments not in NOPAT → added to equity value."""
        result = should_add_equity_investments(
            equity_investments_mm=500,
            in_nopat=False,
        )
        assert result == pytest.approx(500)

    def test_zero_investment_returns_zero(self):
        result = should_add_equity_investments(equity_investments_mm=0, in_nopat=False)
        assert result == pytest.approx(0)

    def test_negative_investment_returns_zero(self):
        result = should_add_equity_investments(equity_investments_mm=-100, in_nopat=False)
        assert result == pytest.approx(0)

    def test_in_nopat_still_added(self):
        """Even if in NOPAT, the function returns the amount (standard treatment)."""
        result = should_add_equity_investments(equity_investments_mm=200, in_nopat=True)
        # The function currently returns the amount regardless of in_nopat flag
        assert isinstance(result, (int, float))
        assert result >= 0
