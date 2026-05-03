"""
tests/test_sensitivity.py — Unit tests for auto_valuation/sensitivity/analysis.py

Phase 6 — Sensitivity Analysis, Scenarios & Risk

Tests cover:
  wacc_growth_sensitivity()     : grid shape, WACC×g monotonicity
  growth_margin_sensitivity()   : grid shape, growth×margin monotonicity
  build_tornado_chart()         : bar count, ordering, delta signs
  run_scenario_analysis()       : bull > base > bear ordering
  scenario_summary_table()      : dict keys, scenario count
  compute_irr_implied_wacc()    : converges to correct WACC, edge cases
  run_monte_carlo_dcf()         : stat keys, distribution properties, seed reproducibility

No live API calls — all tests use synthetic data from conftest.py fixtures.
"""

from __future__ import annotations

import math
import pytest

from auto_valuation.sensitivity.analysis import (
    MonteCarloResult,
    TornadoBar,
    build_tornado_chart,
    compute_irr_implied_wacc,
    growth_margin_sensitivity,
    run_monte_carlo_dcf,
    run_scenario_analysis,
    scenario_summary_table,
    wacc_growth_sensitivity,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_kwargs(fake_income_statement, fake_balance_sheet, fake_cash_flow):
    """Minimal well-formed DCF kwargs using conftest fake data."""
    return dict(
        ticker="TEST",
        scenario="base",
        income_stmts=fake_income_statement,
        cash_flows=fake_cash_flow,
        balance_sheets=fake_balance_sheet,
        wacc=0.09,
        terminal_growth=0.025,
        near_term_growth=0.07,
        target_ebit_margin=0.14,
        forecast_years=5,
    )


@pytest.fixture
def base_ev(base_kwargs):
    """Pre-computed base EV for use in sensitivity tests."""
    from auto_valuation.forecast.dcf import run_dcf
    return run_dcf(**base_kwargs).enterprise_value


# ─────────────────────────────────────────────────────────────────────────────
# 1 — wacc_growth_sensitivity
# ─────────────────────────────────────────────────────────────────────────────

class TestWaccGrowthSensitivity:
    def test_returns_required_keys(self, base_kwargs):
        result = wacc_growth_sensitivity(base_kwargs)
        assert "wacc_range"   in result
        assert "growth_range" in result
        assert "ev_table"     in result
        assert "price_table"  in result

    def test_default_grid_has_entries(self, base_kwargs):
        result = wacc_growth_sensitivity(base_kwargs)
        assert len(result["ev_table"]) > 0

    def test_custom_ranges_respected(self, base_kwargs):
        wacc_r   = [0.08, 0.09, 0.10]
        growth_r = [0.02, 0.025]
        result = wacc_growth_sensitivity(
            base_kwargs, wacc_range=wacc_r, growth_range=growth_r
        )
        assert result["wacc_range"]   == wacc_r
        assert result["growth_range"] == growth_r

    def test_higher_wacc_lower_ev(self, base_kwargs):
        """EV must be strictly decreasing as WACC increases (g fixed)."""
        result = wacc_growth_sensitivity(
            base_kwargs,
            wacc_range=[0.08, 0.09, 0.10, 0.11, 0.12],
            growth_range=[0.025],
        )
        evs = [
            result["ev_table"].get((round(w, 4), 0.025))
            for w in [0.08, 0.09, 0.10, 0.11, 0.12]
        ]
        evs = [e for e in evs if e is not None]
        assert evs == sorted(evs, reverse=True), "EV should decrease as WACC rises"

    def test_higher_growth_higher_ev(self, base_kwargs):
        """EV must increase as terminal growth rises (WACC fixed)."""
        result = wacc_growth_sensitivity(
            base_kwargs,
            wacc_range=[0.09],
            growth_range=[0.015, 0.020, 0.025, 0.030],
        )
        evs = [
            result["ev_table"].get((0.09, round(g, 4)))
            for g in [0.015, 0.020, 0.025, 0.030]
        ]
        evs = [e for e in evs if e is not None]
        assert evs == sorted(evs), "EV should increase as terminal growth rises"

    def test_wacc_equal_growth_excluded(self, base_kwargs):
        """Grid cells where WACC ≤ terminal_growth must be skipped."""
        result = wacc_growth_sensitivity(
            base_kwargs,
            wacc_range=[0.025],
            growth_range=[0.025],
        )
        # (0.025, 0.025) → WACC == g → invalid; should not appear
        assert (0.025, 0.025) not in result["ev_table"]

    def test_price_table_populated_when_shares_given(self, base_kwargs):
        result = wacc_growth_sensitivity(
            base_kwargs,
            wacc_range=[0.09, 0.10],
            growth_range=[0.025],
            net_debt=5_000,
            shares_mm=500,
        )
        assert len(result["price_table"]) > 0
        for price in result["price_table"].values():
            assert price >= 0


# ─────────────────────────────────────────────────────────────────────────────
# 2 — growth_margin_sensitivity
# ─────────────────────────────────────────────────────────────────────────────

class TestGrowthMarginSensitivity:
    def test_returns_required_keys(self, base_kwargs):
        result = growth_margin_sensitivity(base_kwargs)
        for key in ("growth_range", "margin_range", "ev_table", "price_table"):
            assert key in result

    def test_custom_ranges(self, base_kwargs):
        gr = [0.04, 0.06, 0.08]
        mr = [0.10, 0.14]
        result = growth_margin_sensitivity(
            base_kwargs, growth_range=gr, margin_range=mr
        )
        assert result["growth_range"] == gr
        assert result["margin_range"] == mr
        assert len(result["ev_table"]) == len(gr) * len(mr)

    def test_higher_margin_higher_ev(self, base_kwargs):
        """EV must increase with higher EBIT margin (growth fixed)."""
        result = growth_margin_sensitivity(
            base_kwargs,
            growth_range=[0.07],
            margin_range=[0.10, 0.14, 0.18],
        )
        evs = [
            result["ev_table"].get((round(0.07, 4), round(m, 4)))
            for m in [0.10, 0.14, 0.18]
        ]
        evs = [e for e in evs if e is not None]
        assert evs == sorted(evs), "EV should increase with higher margin"

    def test_higher_growth_higher_ev(self, base_kwargs):
        """EV must increase with higher near-term growth (margin fixed)."""
        result = growth_margin_sensitivity(
            base_kwargs,
            growth_range=[0.04, 0.07, 0.10],
            margin_range=[0.14],
        )
        evs = [
            result["ev_table"].get((round(g, 4), round(0.14, 4)))
            for g in [0.04, 0.07, 0.10]
        ]
        evs = [e for e in evs if e is not None]
        assert evs == sorted(evs), "EV should increase with higher growth"


# ─────────────────────────────────────────────────────────────────────────────
# 3 — build_tornado_chart
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildTornadoChart:
    def test_returns_list_of_tornado_bars(self, base_kwargs, base_ev):
        bars = build_tornado_chart(base_kwargs, base_ev)
        assert isinstance(bars, list)
        assert all(isinstance(b, TornadoBar) for b in bars)

    def test_at_least_one_bar(self, base_kwargs, base_ev):
        bars = build_tornado_chart(base_kwargs, base_ev)
        assert len(bars) >= 1

    def test_sorted_by_swing_descending(self, base_kwargs, base_ev):
        bars = build_tornado_chart(base_kwargs, base_ev)
        swings = [abs(b.high_ev - b.low_ev) for b in bars]
        assert swings == sorted(swings, reverse=True)

    def test_each_bar_has_required_fields(self, base_kwargs, base_ev):
        bars = build_tornado_chart(base_kwargs, base_ev)
        for b in bars:
            assert b.variable  != ""
            assert b.base_ev   == pytest.approx(base_ev, rel=1e-6)
            assert b.low_ev    > 0
            assert b.high_ev   > 0

    def test_wacc_is_widest_bar(self, base_kwargs, base_ev):
        """WACC typically has the largest EV swing in a standard DCF."""
        bars = build_tornado_chart(base_kwargs, base_ev, variables=["wacc", "terminal_growth"])
        assert bars[0].variable == "wacc"

    def test_custom_variable_subset(self, base_kwargs, base_ev):
        bars = build_tornado_chart(
            base_kwargs, base_ev, variables=["wacc"]
        )
        assert len(bars) == 1
        assert bars[0].variable == "wacc"

    def test_high_assumption_greater_than_base_for_wacc(self, base_kwargs, base_ev):
        """Higher WACC → lower EV; so low_assumption > base for WACC bar."""
        bars = build_tornado_chart(base_kwargs, base_ev, variables=["wacc"])
        b = bars[0]
        # low assumption → higher EV; high assumption → lower EV
        assert b.low_assumption < b.high_assumption


# ─────────────────────────────────────────────────────────────────────────────
# 4 — run_scenario_analysis
# ─────────────────────────────────────────────────────────────────────────────

class TestRunScenarioAnalysis:
    def test_returns_three_scenarios_by_default(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        assert set(results.keys()) == {"bull", "base", "bear"}

    def test_custom_scenario_list(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs, scenarios=["base", "bear"])
        assert set(results.keys()) == {"base", "bear"}

    def test_bull_ev_greater_than_bear(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        assert results["bull"].enterprise_value > results["bear"].enterprise_value

    def test_base_between_bull_and_bear(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        bull_ev = results["bull"].enterprise_value
        base_ev = results["base"].enterprise_value
        bear_ev = results["bear"].enterprise_value
        assert bear_ev < base_ev < bull_ev

    def test_all_results_are_dcf_results(self, base_kwargs):
        from auto_valuation.forecast.dcf import DCFResult
        results = run_scenario_analysis(base_kwargs)
        for res in results.values():
            assert isinstance(res, DCFResult)

    def test_custom_overrides_applied(self, base_kwargs):
        """Custom override for 'base' scenario should replace default adjustments."""
        custom = {"base": {"wacc": 0.20}}
        results = run_scenario_analysis(base_kwargs, custom_scenario_overrides=custom)
        # With WACC = 20%, EV should be much lower than default base
        default_results = run_scenario_analysis(base_kwargs)
        assert results["base"].enterprise_value < default_results["base"].enterprise_value


# ─────────────────────────────────────────────────────────────────────────────
# 5 — scenario_summary_table
# ─────────────────────────────────────────────────────────────────────────────

class TestScenarioSummaryTable:
    def test_row_count_matches_scenarios(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        table   = scenario_summary_table(results, net_debt=5_000, shares_mm=500)
        assert len(table) == 3

    def test_required_keys_in_each_row(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        table   = scenario_summary_table(results)
        for row in table:
            for key in ("scenario", "enterprise_value", "equity_value",
                        "price_per_share", "wacc", "terminal_growth"):
                assert key in row

    def test_prices_positive_with_low_net_debt(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        table   = scenario_summary_table(results, net_debt=1_000, shares_mm=200)
        for row in table:
            assert row["price_per_share"] > 0

    def test_bull_price_higher_than_bear(self, base_kwargs):
        results = run_scenario_analysis(base_kwargs)
        table   = scenario_summary_table(results, net_debt=1_000, shares_mm=200)
        by_scenario = {r["scenario"]: r["price_per_share"] for r in table}
        assert by_scenario["bull"] > by_scenario["bear"]


# ─────────────────────────────────────────────────────────────────────────────
# 6 — compute_irr_implied_wacc
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeIrrImpliedWacc:
    def test_recovers_base_wacc(self, base_kwargs, base_ev):
        """If target_ev equals the base EV, the implied WACC should equal base WACC."""
        implied = compute_irr_implied_wacc(base_kwargs, target_ev=base_ev)
        assert implied is not None
        assert abs(implied - base_kwargs["wacc"]) < 0.001

    def test_higher_target_ev_implies_lower_wacc(self, base_kwargs, base_ev):
        """A higher target_ev (market over-values) implies a lower discount rate."""
        implied = compute_irr_implied_wacc(base_kwargs, target_ev=base_ev * 1.3)
        if implied is not None:   # may be None if target exceeds search range
            assert implied < base_kwargs["wacc"]

    def test_lower_target_ev_implies_higher_wacc(self, base_kwargs, base_ev):
        """A lower target_ev (market under-values) implies a higher discount rate."""
        implied = compute_irr_implied_wacc(base_kwargs, target_ev=base_ev * 0.7)
        if implied is not None:
            assert implied > base_kwargs["wacc"]

    def test_returns_none_when_out_of_range(self, base_kwargs):
        """Target EV = 0 is unreachable by any WACC; should return None."""
        implied = compute_irr_implied_wacc(
            base_kwargs, target_ev=0.0,
            wacc_low=0.03, wacc_high=0.40,
        )
        assert implied is None

    def test_returns_float_in_valid_range(self, base_kwargs, base_ev):
        implied = compute_irr_implied_wacc(base_kwargs, target_ev=base_ev)
        assert implied is not None
        assert 0.03 <= implied <= 0.40

    def test_convergence_tolerance(self, base_kwargs, base_ev):
        """Verify that re-running DCF at the implied WACC gives EV close to target."""
        from auto_valuation.forecast.dcf import run_dcf
        implied = compute_irr_implied_wacc(base_kwargs, target_ev=base_ev)
        assert implied is not None
        kwargs  = dict(base_kwargs, wacc=implied)
        check_ev = run_dcf(**kwargs).enterprise_value
        assert abs(check_ev - base_ev) / max(base_ev, 1) < 0.001  # within 0.1%


# ─────────────────────────────────────────────────────────────────────────────
# 7 — run_monte_carlo_dcf
# ─────────────────────────────────────────────────────────────────────────────

class TestRunMonteCarloDcf:
    _N = 200   # small N for fast tests; still statistically meaningful

    def test_returns_monte_carlo_result(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert isinstance(mc, MonteCarloResult)

    def test_n_simulations_recorded(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert mc.n_simulations == self._N

    def test_ev_samples_populated(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert len(mc.ev_samples) > self._N * 0.5   # at least 50% valid draws

    def test_all_ev_samples_positive(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert all(v > 0 for v in mc.ev_samples)

    def test_mean_positive(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert mc.ev_mean > 0

    def test_percentile_ordering(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert mc.ev_min  <= mc.ev_p10
        assert mc.ev_p10  <= mc.ev_p25
        assert mc.ev_p25  <= mc.ev_median
        assert mc.ev_median <= mc.ev_p75
        assert mc.ev_p75  <= mc.ev_p90
        assert mc.ev_p90  <= mc.ev_max

    def test_std_is_non_negative(self, base_kwargs):
        mc = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=42)
        assert mc.ev_std >= 0

    def test_price_stats_populated_when_shares_given(self, base_kwargs):
        mc = run_monte_carlo_dcf(
            base_kwargs, n_simulations=self._N, seed=42,
            net_debt=5_000, shares_mm=500,
        )
        assert mc.price_mean   is not None
        assert mc.price_median is not None
        assert mc.price_p10    is not None
        assert mc.price_p90    is not None
        assert len(mc.price_samples) > 0

    def test_price_stats_none_without_shares(self, base_kwargs):
        mc = run_monte_carlo_dcf(
            base_kwargs, n_simulations=self._N, seed=42,
            shares_mm=0.0,
        )
        assert mc.price_mean is None

    def test_seed_reproducibility(self, base_kwargs):
        mc1 = run_monte_carlo_dcf(base_kwargs, n_simulations=50, seed=99)
        mc2 = run_monte_carlo_dcf(base_kwargs, n_simulations=50, seed=99)
        assert mc1.ev_mean   == pytest.approx(mc2.ev_mean)
        assert mc1.ev_median == pytest.approx(mc2.ev_median)

    def test_different_seeds_give_different_results(self, base_kwargs):
        mc1 = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=1)
        mc2 = run_monte_carlo_dcf(base_kwargs, n_simulations=self._N, seed=2)
        assert mc1.ev_mean != pytest.approx(mc2.ev_mean)

    def test_wider_std_gives_wider_distribution(self, base_kwargs):
        """Larger sigma should produce a wider p10-p90 spread."""
        mc_narrow = run_monte_carlo_dcf(
            base_kwargs, n_simulations=self._N, seed=42,
            wacc_std=0.002, growth_std=0.002,
        )
        mc_wide   = run_monte_carlo_dcf(
            base_kwargs, n_simulations=self._N, seed=42,
            wacc_std=0.030, growth_std=0.040,
        )
        spread_narrow = mc_narrow.ev_p90 - mc_narrow.ev_p10
        spread_wide   = mc_wide.ev_p90   - mc_wide.ev_p10
        assert spread_wide > spread_narrow

    def test_mean_near_base_ev(self, base_kwargs, base_ev):
        """With symmetric draws, Monte Carlo mean should be near base EV (within 20%)."""
        mc = run_monte_carlo_dcf(
            base_kwargs, n_simulations=500, seed=42,
            wacc_std=0.005, growth_std=0.005, margin_std=0.005, terminal_g_std=0.002,
        )
        pct_diff = abs(mc.ev_mean - base_ev) / base_ev
        assert pct_diff < 0.20   # within 20% of base EV

    def test_price_percentile_ordering(self, base_kwargs):
        mc = run_monte_carlo_dcf(
            base_kwargs, n_simulations=self._N, seed=42,
            net_debt=2_000, shares_mm=300,
        )
        if mc.price_p10 is not None:
            assert mc.price_p10 <= mc.price_p25
            assert mc.price_p25 <= mc.price_p75
            assert mc.price_p75 <= mc.price_p90
