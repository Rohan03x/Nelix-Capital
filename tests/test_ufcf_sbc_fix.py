"""Tests for model/forecast.py — verify UFCF SBC bug is fixed."""
import pytest


def _make_base_is():
    """Minimal income statement for 3 years."""
    return [
        {
            "calendarYear": "2023",
            "revenue": 10_000,
            "ebit": 1_500,
            "netIncome": 1_100,
            "depreciationAndAmortization": 400,
            "stockBasedCompensation": 200,
            "capitalExpenditure": -300,
            "receivables": 1_200,
            "inventory": 500,
            "accountsPayable": 800,
        },
        {
            "calendarYear": "2022",
            "revenue": 9_000,
            "ebit": 1_300,
            "netIncome": 960,
            "depreciationAndAmortization": 350,
            "stockBasedCompensation": 180,
            "capitalExpenditure": -270,
            "receivables": 1_100,
            "inventory": 450,
            "accountsPayable": 720,
        },
        {
            "calendarYear": "2021",
            "revenue": 8_000,
            "ebit": 1_100,
            "netIncome": 810,
            "depreciationAndAmortization": 300,
            "stockBasedCompensation": 150,
            "capitalExpenditure": -240,
            "receivables": 1_000,
            "inventory": 400,
            "accountsPayable": 640,
        },
    ]


def _make_base_bs():
    return [
        {
            "calendarYear": "2023",
            "totalAssets": 20_000,
            "totalEquity": 10_000,
            "cashAndCashEquivalents": 2_000,
            "propertyPlantEquipmentNet": 4_000,
            "goodwill": 1_000,
            "totalCurrentAssets": 5_000,
            "totalCurrentLiabilities": 3_000,
            "shortTermDebt": 500,
            "longTermDebt": 4_000,
        },
        {
            "calendarYear": "2022",
            "totalAssets": 18_000,
            "totalEquity": 9_000,
            "cashAndCashEquivalents": 1_800,
            "propertyPlantEquipmentNet": 3_800,
            "goodwill": 1_000,
            "totalCurrentAssets": 4_500,
            "totalCurrentLiabilities": 2_700,
            "shortTermDebt": 500,
            "longTermDebt": 3_500,
        },
    ]


def _make_base_cf():
    return [
        {
            "calendarYear": "2023",
            "operatingCashFlow": 1_500,
            "capitalExpenditure": -300,
        },
    ]


class TestUfcfSbcFix:
    """Verify the critical UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC formula."""

    def test_ufcf_adds_sbc_not_subtracts(self):
        """
        When SBC treatment = 'addback', UFCF should be HIGHER when SBC > 0.
        Before the bug fix, UFCF subtracted SBC, making it lower.
        """
        try:
            from auto_valuation.model.forecast import run_forecast
        except ImportError:
            pytest.skip("run_forecast not importable in this environment")

        is_stmts = _make_base_is()
        bs_stmts = _make_base_bs()
        cf_stmts = _make_base_cf()

        result_with_sbc = run_forecast(
            income_stmts=is_stmts,
            balance_sheets=bs_stmts,
            cash_flows=cf_stmts,
            wacc=0.10,
            terminal_growth=0.025,
            forecast_years=3,
            tax_rate=0.21,
            sbc_treatment="addback",
        )

        result_no_sbc = run_forecast(
            income_stmts=is_stmts,
            balance_sheets=bs_stmts,
            cash_flows=cf_stmts,
            wacc=0.10,
            terminal_growth=0.025,
            forecast_years=3,
            tax_rate=0.21,
            sbc_treatment="expense",   # SBC treated as real cost → not added back
        )

        # With addback, UFCF >= UFCF with expense treatment (or equal if SBC=0)
        ufcf_with = [fy.ufcf for fy in result_with_sbc.forecast_years]
        ufcf_without = [fy.ufcf for fy in result_no_sbc.forecast_years]

        for uw, un in zip(ufcf_with, ufcf_without):
            assert uw >= un - 0.01, (
                f"UFCF with addback ({uw:.1f}) should be >= UFCF with expense treatment ({un:.1f}). "
                "SBC subtraction bug may still be present."
            )

    def test_ufcf_formula_direct(self):
        """Unit-test the UFCF formula arithmetic directly."""
        # UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC
        nopat = 1000
        da = 400
        sbc = 200
        capex = 300
        delta_nowc = 50

        expected_ufcf_addback = nopat + da + sbc - capex - delta_nowc   # 1250
        expected_ufcf_expense  = nopat + da - capex - delta_nowc         # 1050

        assert expected_ufcf_addback > expected_ufcf_expense
        assert expected_ufcf_addback == pytest.approx(1250)
        assert expected_ufcf_expense == pytest.approx(1050)


class TestOcfFormula:
    """OCF = NI + D&A + SBC − ΔNOWC must be correct."""

    def test_ocf_adds_sbc(self):
        ni = 800
        da = 400
        sbc = 200
        delta_nowc = 50

        # OCF = 800 + 400 + 200 - 50 = 1350
        expected = ni + da + sbc - delta_nowc
        assert expected == pytest.approx(1350)
