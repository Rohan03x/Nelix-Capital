"""Tests for model/balance_sheet.py rollforward functions (new additions)."""
import pytest
from auto_valuation.model.balance_sheet import (
    rollforward_deferred_tax,
    rollforward_goodwill,
    rollforward_intangibles,
    rollforward_retained_earnings,
    build_equity_section,
)


class TestRollforwardDeferredTax:
    def test_tax_depreciation_exceeds_book_creates_dtl(self):
        # da_tax > da_book → creates DTL (positive closing_dt)
        result = rollforward_deferred_tax(
            opening_dt_net=0,
            da_book=100,
            da_tax=150,
            tax_rate=0.21,
        )
        # ΔDTL = (150-100) * 0.21 = 10.5
        assert result == pytest.approx(10.5)

    def test_book_exceeds_tax_creates_dta(self):
        # da_book > da_tax → creates DTA (negative closing_dt)
        result = rollforward_deferred_tax(
            opening_dt_net=0,
            da_book=150,
            da_tax=100,
            tax_rate=0.21,
        )
        # ΔDTL = (100-150) * 0.21 = -10.5 → closing = -10.5 (net DTA)
        assert result == pytest.approx(-10.5)

    def test_opening_balance_rolls_forward(self):
        result = rollforward_deferred_tax(
            opening_dt_net=50,
            da_book=100,
            da_tax=150,
            tax_rate=0.21,
        )
        # 50 + 10.5 = 60.5
        assert result == pytest.approx(60.5)

    def test_equal_depreciation_no_change(self):
        result = rollforward_deferred_tax(
            opening_dt_net=100,
            da_book=200,
            da_tax=200,
            tax_rate=0.25,
        )
        assert result == pytest.approx(100)


class TestRollforwardGoodwill:
    def test_basic_rollforward(self):
        result = rollforward_goodwill(
            opening_goodwill=1000,
            acquisitions_mm=200,
            impairment_mm=50,
        )
        assert result == pytest.approx(1150)

    def test_no_activity(self):
        result = rollforward_goodwill(opening_goodwill=500)
        assert result == pytest.approx(500)

    def test_floor_at_zero(self):
        result = rollforward_goodwill(
            opening_goodwill=100,
            impairment_mm=500,   # impairment > opening
        )
        assert result == pytest.approx(0)

    def test_fx_adjustment(self):
        result = rollforward_goodwill(
            opening_goodwill=1000,
            fx_adjustment_mm=-50,
        )
        assert result == pytest.approx(950)


class TestRollforwardIntangibles:
    def test_amortisation_reduces(self):
        result = rollforward_intangibles(
            opening_intangibles=1000,
            amortisation_mm=100,
        )
        assert result == pytest.approx(900)

    def test_auto_amortisation(self):
        """When amortisation_mm=0 and amort_years=10, auto-computes 1/10 of opening."""
        result = rollforward_intangibles(
            opening_intangibles=1000,
            amortisation_mm=0,
            amort_years=10,
        )
        assert result == pytest.approx(900)

    def test_floor_at_zero(self):
        result = rollforward_intangibles(
            opening_intangibles=100,
            amortisation_mm=500,
        )
        assert result == pytest.approx(0)

    def test_new_intangibles_added(self):
        result = rollforward_intangibles(
            opening_intangibles=1000,
            new_intangibles_mm=200,
            amortisation_mm=100,
        )
        assert result == pytest.approx(1100)


class TestRollforwardRetainedEarnings:
    def test_basic_rollforward(self):
        result = rollforward_retained_earnings(
            opening_re=500,
            net_income=200,
            dividends=50,
            buybacks=30,
        )
        assert result == pytest.approx(620)

    def test_no_distributions(self):
        result = rollforward_retained_earnings(
            opening_re=500,
            net_income=200,
        )
        assert result == pytest.approx(700)

    def test_net_loss(self):
        result = rollforward_retained_earnings(
            opening_re=500,
            net_income=-100,
        )
        assert result == pytest.approx(400)


class TestBuildEquitySection:
    def test_basic_equity_section(self):
        result = build_equity_section(
            opening_common_equity=1000,
            opening_re=400,
            opening_apic=600,
            net_income=200,
            dividends=50,
            sbc_expense=30,
        )
        assert isinstance(result, dict)
        assert "closing_equity" in result
        assert "closing_retained_earnings" in result
        assert "closing_apic" in result

    def test_equity_components_sum(self):
        result = build_equity_section(
            opening_common_equity=1000,
            opening_re=400,
            opening_apic=600,
            net_income=200,
            dividends=50,
            sbc_expense=30,
            other_ci=10,
        )
        # closing_equity = APIC + RE + OCI
        expected_apic = 600 + 30         # opening_apic + sbc
        expected_re   = 400 + 200 - 50   # opening_re + NI - dividends
        expected_equity = expected_apic + expected_re + 10
        assert result["closing_equity"] == pytest.approx(expected_equity)

    def test_sbc_flows_through_apic(self):
        result = build_equity_section(
            opening_common_equity=1000,
            opening_re=400,
            opening_apic=600,
            net_income=200,
            dividends=0,
            sbc_expense=50,
        )
        assert result["closing_apic"] == pytest.approx(650)   # 600 + 50

    def test_buybacks_reduce_equity(self):
        result_no_buyback = build_equity_section(
            opening_common_equity=1000, opening_re=400, opening_apic=600,
            net_income=200, buybacks=0,
        )
        result_with_buyback = build_equity_section(
            opening_common_equity=1000, opening_re=400, opening_apic=600,
            net_income=200, buybacks=100,
        )
        assert result_with_buyback["closing_equity"] < result_no_buyback["closing_equity"]
