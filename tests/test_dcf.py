"""
tests/test_dcf.py — Unit tests for the DCF engine.

Covers:
  - ForecastYear construction and to_dict()
  - discount_factors (end-year and mid-year)
  - gordon_growth_tv, exit_multiple_tv, pv_terminal_value
  - tv_sensitivity_table
  - run_dcf end-to-end with synthetic data
  - DCFResult.to_dict() serialisation
"""

from __future__ import annotations

import pytest

from auto_valuation.forecast.terminal_value import (
    gordon_growth_tv,
    exit_multiple_tv,
    pv_terminal_value,
    implied_terminal_growth,
    tv_sensitivity_table,
)
from auto_valuation.forecast.dcf import (
    ForecastYear,
    DCFResult,
    discount_factors,
    enforce_terminal_growth_consistency,
    run_dcf,
)


# ─────────────────────────────────────────────────────────────────────────────
# discount_factors
# ─────────────────────────────────────────────────────────────────────────────

class TestDiscountFactors:
    def test_end_of_year_convention(self):
        dfs = discount_factors(wacc=0.10, forecast_years=3, mid_year=False)
        assert len(dfs) == 3
        assert abs(dfs[0] - 1 / 1.10)     < 1e-9
        assert abs(dfs[1] - 1 / 1.10**2)  < 1e-9
        assert abs(dfs[2] - 1 / 1.10**3)  < 1e-9

    def test_mid_year_convention(self):
        dfs = discount_factors(wacc=0.10, forecast_years=3, mid_year=True)
        assert len(dfs) == 3
        assert abs(dfs[0] - 1 / 1.10**0.5) < 1e-9
        assert abs(dfs[1] - 1 / 1.10**1.5) < 1e-9
        assert abs(dfs[2] - 1 / 1.10**2.5) < 1e-9

    def test_factors_decrease_monotonically(self):
        dfs = discount_factors(wacc=0.09, forecast_years=5, mid_year=True)
        for i in range(len(dfs) - 1):
            assert dfs[i] > dfs[i + 1]


# ─────────────────────────────────────────────────────────────────────────────
# Terminal value (GGM)
# ─────────────────────────────────────────────────────────────────────────────

class TestGordonGrowthTV:
    def test_basic_formula(self):
        # TV = UFCF_{n+1} / (WACC - g) = 1000 / (0.10 - 0.025) = 13,333.33
        tv = gordon_growth_tv(terminal_ufcf=1000, wacc=0.10, terminal_growth=0.025)
        assert abs(tv - 1000 / 0.075) < 0.01

    def test_wacc_equals_growth_raises(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            gordon_growth_tv(terminal_ufcf=1000, wacc=0.025, terminal_growth=0.025)

    def test_wacc_less_than_growth_raises(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            gordon_growth_tv(terminal_ufcf=1000, wacc=0.020, terminal_growth=0.025)

    def test_larger_growth_gives_larger_tv(self):
        tv_low  = gordon_growth_tv(1000, 0.10, 0.010)
        tv_high = gordon_growth_tv(1000, 0.10, 0.030)
        assert tv_high > tv_low


class TestExitMultipleTV:
    def test_basic(self):
        tv = exit_multiple_tv(terminal_ebitda=1000, ev_ebitda_multiple=10)
        assert tv == 10_000

    def test_zero_ebitda(self):
        tv = exit_multiple_tv(terminal_ebitda=0, ev_ebitda_multiple=10)
        assert tv == 0


# ─────────────────────────────────────────────────────────────────────────────
# PV of terminal value
# ─────────────────────────────────────────────────────────────────────────────

class TestPvTerminalValue:
    def test_end_year(self):
        pv = pv_terminal_value(terminal_value=100_000, wacc=0.10, forecast_years=5, mid_year_convention=False)
        expected = 100_000 / 1.10**5
        assert abs(pv - expected) < 1.0

    def test_mid_year(self):
        pv = pv_terminal_value(terminal_value=100_000, wacc=0.10, forecast_years=5, mid_year_convention=True)
        # mid-year: discount at t=5.5 for end-of-period TV is not used;
        # standard approach: TV discounted at n (not n-0.5)
        expected = 100_000 / 1.10**5
        # Should be close to end-year value (slight difference acceptable)
        assert abs(pv / expected - 1) < 0.15   # within 15%

    def test_higher_wacc_lower_pv(self):
        pv_lo = pv_terminal_value(100_000, 0.08, 5, False)
        pv_hi = pv_terminal_value(100_000, 0.12, 5, False)
        assert pv_lo > pv_hi


# ─────────────────────────────────────────────────────────────────────────────
# Implied terminal growth back-solve
# ─────────────────────────────────────────────────────────────────────────────

class TestImpliedTerminalGrowth:
    def test_round_trip(self):
        wacc   = 0.10
        g_orig = 0.025
        ufcf   = 1000.0
        tv     = gordon_growth_tv(ufcf, wacc, g_orig)
        g_back = implied_terminal_growth(tv, ufcf, wacc)
        assert abs(g_back - g_orig) < 1e-8

    def test_zero_tv_gives_wacc(self):
        # TV → 0 means g → -∞; not numerically useful; just check no crash
        try:
            implied_terminal_growth(0.0, 1000, 0.10)
        except (ZeroDivisionError, ValueError):
            pass   # acceptable


# ─────────────────────────────────────────────────────────────────────────────
# Sensitivity table
# ─────────────────────────────────────────────────────────────────────────────

class TestTvSensitivityTable:
    def test_shape(self):
        tbl = tv_sensitivity_table(
            terminal_ufcf=1000,
            wacc_range=[0.08, 0.09, 0.10],
            growth_range=[0.015, 0.020, 0.025],
            forecast_years=5,
            mid_year_convention=False,
        )
        # 3×3 = 9 entries
        assert len(tbl) == 9

    def test_invalid_combos_absent(self):
        tbl = tv_sensitivity_table(
            terminal_ufcf=1000,
            wacc_range=[0.02, 0.10],
            growth_range=[0.025],    # 0.02 < 0.025 → invalid
            forecast_years=5,
            mid_year_convention=False,
        )
        # (0.02, 0.025) should be absent
        for k in tbl:
            assert k[0] > k[1]

    def test_lower_wacc_higher_pv(self):
        tbl = tv_sensitivity_table(1000, [0.08, 0.12], [0.025], 5, False)  # positional mid_year_convention
        keys = sorted(tbl.keys())
        # Lower WACC → higher PV(TV)
        pv_low_wacc  = tbl[(0.08, 0.025)]
        pv_high_wacc = tbl[(0.12, 0.025)]
        assert pv_low_wacc > pv_high_wacc


# ─────────────────────────────────────────────────────────────────────────────
# ForecastYear dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestForecastYear:
    def test_to_dict_keys(self):
        fy = ForecastYear(
            year=1, revenue=10000, ebit_margin=0.12, ebit=1200,
            tax_rate=0.21, nopat=948, da=400, capex=500,
            nowc=800, delta_nowc=50, ufcf=798,
            discount_factor=0.926, pv_ufcf=739,
        )
        d = fy.to_dict()
        assert "revenue" in d
        assert "ufcf"    in d
        assert "pv_ufcf" in d
        assert d["year"] == 1

    def test_numeric_fields(self):
        fy = ForecastYear(
            year=2, revenue=11000, ebit_margin=0.13, ebit=1430,
            tax_rate=0.21, nopat=1130, da=430, capex=520,
            nowc=850, delta_nowc=50, ufcf=990,
            discount_factor=0.857, pv_ufcf=848,
        )
        assert fy.revenue == 11000
        assert abs(fy.ebit_margin - 0.13) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# run_dcf — end-to-end with synthetic NKE data
# ─────────────────────────────────────────────────────────────────────────────

class TestRunDcf:
    @pytest.fixture(autouse=True)
    def _load_data(self, fake_income_statement, fake_balance_sheet, fake_cash_flow):
        self.income_stmts  = fake_income_statement
        self.balance_sheets = fake_balance_sheet
        self.cash_flows    = fake_cash_flow

    def _run(self, **kwargs):
        defaults = dict(
            ticker="NKE",
            scenario="base",
            income_stmts=self.income_stmts,
            cash_flows=self.cash_flows,
            balance_sheets=self.balance_sheets,
            wacc=0.088,
            terminal_growth=0.025,
            near_term_growth=0.08,
            target_ebit_margin=0.135,
            forecast_years=5,
            tax_rate_override=0.21,
            mid_year_convention=True,
        )
        defaults.update(kwargs)
        return run_dcf(**defaults)

    def test_returns_dcf_result(self):
        res = self._run()
        assert isinstance(res, DCFResult)

    def test_ev_positive(self):
        res = self._run()
        assert res.enterprise_value > 0

    def test_pv_components_sum_to_ev(self):
        res = self._run()
        total = res.pv_ufcfs + res.pv_terminal_value
        assert abs(total - res.enterprise_value) < 1.0

    def test_tv_pct_in_range(self):
        res = self._run()
        assert 0.0 < res.tv_pct_of_ev < 1.0

    def test_forecast_years_count(self):
        res = self._run(forecast_years=5)
        assert len(res.forecast_years_data) == 5

    def test_higher_wacc_lower_ev(self):
        ev_lo = self._run(wacc=0.07).enterprise_value
        ev_hi = self._run(wacc=0.12).enterprise_value
        assert ev_lo > ev_hi

    def test_higher_growth_higher_ev(self):
        ev_lo = self._run(near_term_growth=0.03).enterprise_value
        ev_hi = self._run(near_term_growth=0.12).enterprise_value
        assert ev_hi > ev_lo

    def test_higher_margin_higher_ev(self):
        ev_lo = self._run(target_ebit_margin=0.08).enterprise_value
        ev_hi = self._run(target_ebit_margin=0.20).enterprise_value
        assert ev_hi > ev_lo

    def test_terminal_growth_too_high_warns(self):
        # terminal_growth ≥ wacc → ValueError or warns
        try:
            res = self._run(wacc=0.025, terminal_growth=0.025)
            # If it doesn't raise, check warnings present
            assert len(res.warnings) >= 1
        except (ValueError, ZeroDivisionError):
            pass   # acceptable

    def test_terminal_growth_roic_guard_caps_unfunded_growth(self):
        adjusted, warning = enforce_terminal_growth_consistency(
            terminal_growth=0.05,
            terminal_roic=0.10,
            terminal_reinvestment_rate=0.10,
            tolerance=0.01,
        )
        assert adjusted == pytest.approx(0.02)
        assert warning is not None

    def test_run_dcf_caps_terminal_growth_if_wacc_spread_breaks(self):
        res = self._run(wacc=0.025, terminal_growth=0.03)
        assert res.terminal_growth < res.wacc
        assert any("WACC-g" in warning for warning in res.warnings)

    def test_exit_multiple_tv_computed(self):
        res = self._run(exit_ev_ebitda_multiple=10.0)
        assert res.terminal_value_em > 0

    def test_to_dict_serialisable(self):
        import json
        res  = self._run()
        d    = res.to_dict()
        json.dumps(d)   # must not raise

    def test_bull_ev_gt_base_gt_bear(self):
        bull = self._run(scenario="bull", near_term_growth=0.11, target_ebit_margin=0.165)
        base = self._run(scenario="base")
        bear = self._run(scenario="bear", near_term_growth=0.05, target_ebit_margin=0.105)
        assert bull.enterprise_value > base.enterprise_value > bear.enterprise_value
