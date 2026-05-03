"""
tests/test_session14_gaps.py — Session 14 gap tests.

Covers:
  Gap A: delta_deferred_tax parameter added to compute_ufcf()
  Gap E: model/lbo.py — full LBO module
  Gap G: ev_ufcf_ltm added to compute_peer_multiples()
"""

from __future__ import annotations

import math
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Gap A — delta_deferred_tax in compute_ufcf
# ─────────────────────────────────────────────────────────────────────────────

from auto_valuation.model.income_statement import compute_ufcf, compute_nopat


class TestDeferredTaxInUFCF:
    """compute_ufcf now accepts delta_deferred_tax as a non-cash add-back."""

    BASE_PARAMS = dict(ebit=100.0, tax_rate=0.25, da=20.0, capex=15.0, delta_nowc=5.0, sbc=3.0)

    def test_backward_compat_zero_default(self):
        """default delta_deferred_tax=0 must reproduce old result."""
        result_new = compute_ufcf(**self.BASE_PARAMS)
        result_old = compute_ufcf(**self.BASE_PARAMS, delta_deferred_tax=0.0)
        assert result_new == result_old

    def test_positive_deferred_tax_increases_ufcf(self):
        """Positive delta_deferred_tax (DTL increase) is a non-cash add-back → higher UFCF."""
        base = compute_ufcf(**self.BASE_PARAMS)
        with_dt = compute_ufcf(**self.BASE_PARAMS, delta_deferred_tax=5.0)
        assert with_dt == pytest.approx(base + 5.0)

    def test_negative_deferred_tax_decreases_ufcf(self):
        """Negative delta_deferred_tax (DTA increase) reduces UFCF."""
        base = compute_ufcf(**self.BASE_PARAMS)
        with_dt = compute_ufcf(**self.BASE_PARAMS, delta_deferred_tax=-3.0)
        assert with_dt == pytest.approx(base - 3.0)

    def test_formula_correct(self):
        """UFCF = NOPAT + DA + SBC + delta_dt − CapEx − ΔNOWC."""
        ebit, t, da, capex, nowc, sbc, dt = 200.0, 0.21, 30.0, 25.0, 8.0, 5.0, 7.0
        nopat = ebit * (1 - t)
        expected = nopat + da + sbc + dt - capex - nowc
        assert compute_ufcf(ebit, t, da, capex, nowc, sbc, dt) == pytest.approx(expected)

    def test_zero_ebit_zero_deferred_tax(self):
        """Edge: zero EBIT, zero deferred tax."""
        result = compute_ufcf(0.0, 0.25, 10.0, 10.0, 0.0, 0.0, 0.0)
        assert result == pytest.approx(0.0)

    def test_large_deferred_tax(self):
        """Large deferred tax still computed correctly."""
        result = compute_ufcf(100.0, 0.3, 20.0, 10.0, 5.0, 2.0, 50.0)
        nopat = 100.0 * 0.7
        expected = nopat + 20.0 + 2.0 + 50.0 - 10.0 - 5.0
        assert result == pytest.approx(expected)

    def test_sbc_still_works_with_dt(self):
        """SBC and delta_deferred_tax are both independent add-backs."""
        no_extras = compute_ufcf(100.0, 0.25, 20.0, 15.0, 5.0, 0.0, 0.0)
        with_both = compute_ufcf(100.0, 0.25, 20.0, 15.0, 5.0, 4.0, 6.0)
        assert with_both == pytest.approx(no_extras + 4.0 + 6.0)

    def test_nopat_unchanged_by_deferred_tax(self):
        """compute_nopat does NOT change — deferred tax only affects UFCF."""
        assert compute_nopat(100.0, 0.25) == pytest.approx(75.0)


# ─────────────────────────────────────────────────────────────────────────────
# Gap E — LBO module
# ─────────────────────────────────────────────────────────────────────────────

from auto_valuation.model.lbo import (
    compute_lbo_entry_ev,
    compute_lbo_equity_investment,
    build_lbo_debt_schedule,
    compute_lbo_exit_ev,
    compute_lbo_equity_at_exit,
    compute_cash_on_cash,
    compute_lbo_irr,
    compute_lbo_irr_cashflows,
    run_lbo_analysis,
    LBOResult,
    LBODebtYear,
)


class TestLBOEntryExit:
    def test_entry_ev_basic(self):
        assert compute_lbo_entry_ev(100.0, 10.0) == pytest.approx(1000.0)

    def test_entry_ev_fractional_multiple(self):
        assert compute_lbo_entry_ev(50.0, 8.5) == pytest.approx(425.0)

    def test_entry_ev_raises_zero_multiple(self):
        with pytest.raises(ValueError):
            compute_lbo_entry_ev(100.0, 0.0)

    def test_entry_ev_raises_negative_multiple(self):
        with pytest.raises(ValueError):
            compute_lbo_entry_ev(100.0, -1.0)

    def test_equity_investment_basic(self):
        ev = compute_lbo_entry_ev(100.0, 10.0)  # 1000
        equity = compute_lbo_equity_investment(ev, 600.0)
        assert equity == pytest.approx(400.0)

    def test_equity_investment_raises_if_debt_exceeds_ev(self):
        with pytest.raises(ValueError):
            compute_lbo_equity_investment(1000.0, 1100.0)

    def test_exit_ev_basic(self):
        assert compute_lbo_exit_ev(150.0, 9.0) == pytest.approx(1350.0)

    def test_exit_ev_raises_negative_multiple(self):
        with pytest.raises(ValueError):
            compute_lbo_exit_ev(100.0, -5.0)

    def test_equity_at_exit(self):
        assert compute_lbo_equity_at_exit(1200.0, 400.0) == pytest.approx(800.0)

    def test_equity_at_exit_can_be_negative(self):
        # Distressed scenario
        assert compute_lbo_equity_at_exit(300.0, 700.0) == pytest.approx(-400.0)


class TestLBOReturns:
    def test_cash_on_cash_basic(self):
        assert compute_cash_on_cash(800.0, 400.0) == pytest.approx(2.0)

    def test_cash_on_cash_zero_entry(self):
        assert compute_cash_on_cash(100.0, 0.0) == pytest.approx(0.0)

    def test_cash_on_cash_loss_scenario(self):
        assert compute_cash_on_cash(100.0, 400.0) == pytest.approx(0.25)

    def test_irr_simple_2x_5yr(self):
        # 2× in 5 years: r = 2^(1/5) - 1 ≈ 14.87%
        irr = compute_lbo_irr(100.0, 200.0, 5)
        expected = 2.0 ** (1 / 5) - 1
        assert irr == pytest.approx(expected, rel=1e-6)

    def test_irr_simple_3x_5yr(self):
        irr = compute_lbo_irr(100.0, 300.0, 5)
        expected = 3.0 ** 0.2 - 1
        assert irr == pytest.approx(expected, rel=1e-6)

    def test_irr_raises_zero_entry(self):
        with pytest.raises(ValueError):
            compute_lbo_irr(0.0, 200.0, 5)

    def test_irr_raises_zero_years(self):
        with pytest.raises(ValueError):
            compute_lbo_irr(100.0, 200.0, 0)

    def test_irr_total_loss(self):
        irr = compute_lbo_irr(100.0, 0.0, 5)
        assert irr == pytest.approx(-1.0)

    def test_irr_cashflows_simple(self):
        # -100 today, +121 in 2 years → IRR = 10%
        irr = compute_lbo_irr_cashflows([-100.0, 0.0, 121.0])
        assert irr == pytest.approx(0.10, abs=1e-6)

    def test_irr_cashflows_consistent_with_simple_irr(self):
        # 2 cash-flow case should agree with compute_lbo_irr
        cf_irr = compute_lbo_irr_cashflows([-100.0, 0.0, 0.0, 0.0, 0.0, 200.0])
        simple_irr = compute_lbo_irr(100.0, 200.0, 5)
        assert cf_irr == pytest.approx(simple_irr, abs=1e-5)

    def test_irr_cashflows_raises_no_sign_change(self):
        with pytest.raises(ValueError):
            compute_lbo_irr_cashflows([100.0, 200.0, 300.0])  # all positive

    def test_irr_cashflows_negative_result(self):
        # More paid than received
        irr = compute_lbo_irr_cashflows([-200.0, 50.0])
        assert irr < 0.0

    def test_irr_cashflows_intermediate_distributions(self):
        # -100, +30, +30, +80
        irr = compute_lbo_irr_cashflows([-100.0, 30.0, 30.0, 80.0])
        # Verify NPV at computed IRR ≈ 0
        npv = sum(cf / (1 + irr) ** t for t, cf in enumerate([-100.0, 30.0, 30.0, 80.0]))
        assert abs(npv) < 1e-4


class TestLBODebtSchedule:
    def test_debt_declines_each_year(self):
        ebitda_schedule = [120.0, 130.0, 140.0, 150.0, 160.0]
        schedule = build_lbo_debt_schedule(
            opening_debt=700.0,
            ebitda_schedule=ebitda_schedule,
            interest_rate=0.065,
            mandatory_amort_pct=0.05,
            cash_sweep_pct=0.50,
        )
        for row in schedule:
            assert row.closing_debt <= row.opening_debt

    def test_schedule_length_matches_ebitda(self):
        ebitda_schedule = [100.0, 110.0, 120.0]
        schedule = build_lbo_debt_schedule(700.0, ebitda_schedule, 0.065)
        assert len(schedule) == 3

    def test_debt_never_negative(self):
        ebitda_schedule = [500.0] * 5  # very high EBITDA → lots of sweep
        schedule = build_lbo_debt_schedule(700.0, ebitda_schedule, 0.065, cash_sweep_pct=1.0)
        for row in schedule:
            assert row.closing_debt >= 0.0

    def test_schedule_year_numbers_sequential(self):
        schedule = build_lbo_debt_schedule(500.0, [100.0, 110.0, 120.0], 0.06)
        assert [r.year for r in schedule] == [1, 2, 3]

    def test_net_leverage_computed(self):
        schedule = build_lbo_debt_schedule(600.0, [100.0, 110.0], 0.065)
        for row in schedule:
            expected_lev = row.closing_debt / row.ebitda
            assert row.net_leverage == pytest.approx(expected_lev)

    def test_invalid_interest_rate_raises(self):
        with pytest.raises(ValueError):
            build_lbo_debt_schedule(500.0, [100.0], interest_rate=1.5)

    def test_invalid_amort_pct_raises(self):
        with pytest.raises(ValueError):
            build_lbo_debt_schedule(500.0, [100.0], interest_rate=0.06, mandatory_amort_pct=1.5)


class TestRunLBOAnalysis:
    """Integration tests for the full LBO analysis."""

    DEFAULT_KWARGS = dict(
        ebitda_entry=100.0,
        entry_multiple=10.0,
        debt_pct_ev=0.60,
        ebitda_growth_rate=0.05,
        exit_multiple=9.0,
        holding_years=5,
        interest_rate=0.065,
        mandatory_amort_pct=0.05,
        cash_sweep_pct=0.50,
        cash_tax_rate=0.25,
        capex_pct_ebitda=0.10,
    )

    def test_returns_lbo_result(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert isinstance(result, LBOResult)

    def test_entry_ev_correct(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.entry_ev == pytest.approx(1000.0)

    def test_total_debt_entry_correct(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.total_debt_entry == pytest.approx(600.0)

    def test_equity_entry_correct(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.equity_entry == pytest.approx(400.0)

    def test_exit_ebitda_grows(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        expected = 100.0 * (1.05 ** 5)
        assert result.exit_ebitda == pytest.approx(expected)

    def test_exit_ev_uses_exit_multiple(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.exit_ev == pytest.approx(result.exit_ebitda * 9.0)

    def test_irr_positive_growing_ebitda(self):
        """With EBITDA growth and debt paydown, IRR should be positive."""
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.irr > 0.0

    def test_cash_on_cash_positive(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.cash_on_cash > 0.0

    def test_debt_schedule_length(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert len(result.debt_schedule) == 5

    def test_debt_paydown_reduces_balance(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert result.net_debt_exit < result.total_debt_entry

    def test_irr_consistent_with_cash_on_cash(self):
        """MoM ≈ (1 + IRR)^years."""
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        implied_mom = (1 + result.irr) ** result.holding_years
        assert result.cash_on_cash == pytest.approx(implied_mom, rel=0.15)

    def test_invalid_debt_pct_raises(self):
        kw = dict(self.DEFAULT_KWARGS)
        kw["debt_pct_ev"] = 1.5
        with pytest.raises(ValueError):
            run_lbo_analysis(**kw)

    def test_high_leverage_warning(self):
        """Entry leverage > 8× should produce a warning."""
        kw = dict(self.DEFAULT_KWARGS)
        kw["debt_pct_ev"] = 0.90  # very high leverage
        result = run_lbo_analysis(**kw)
        assert any("leverage" in w.lower() for w in result.warnings)

    def test_holding_years_affects_exit_ebitda(self):
        """Longer holding period produces higher exit EBITDA (more growth years)."""
        short = run_lbo_analysis(**{**self.DEFAULT_KWARGS, "holding_years": 3})
        long = run_lbo_analysis(**{**self.DEFAULT_KWARGS, "holding_years": 7})
        assert long.exit_ebitda > short.exit_ebitda

    def test_higher_exit_multiple_higher_irr(self):
        low_exit = run_lbo_analysis(**{**self.DEFAULT_KWARGS, "exit_multiple": 7.0})
        high_exit = run_lbo_analysis(**{**self.DEFAULT_KWARGS, "exit_multiple": 12.0})
        assert high_exit.irr > low_exit.irr

    def test_entry_leverage_computed(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        expected_lev = result.total_debt_entry / 100.0  # ebitda_entry=100
        assert result.entry_leverage == pytest.approx(expected_lev)

    def test_warnings_list_is_list(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        assert isinstance(result.warnings, list)

    def test_lbo_result_fields_present(self):
        result = run_lbo_analysis(**self.DEFAULT_KWARGS)
        for attr in ("entry_ev", "equity_entry", "total_debt_entry", "entry_leverage",
                     "exit_ev", "equity_exit", "net_debt_exit", "irr", "cash_on_cash",
                     "holding_years", "debt_schedule", "warnings"):
            assert hasattr(result, attr)


# ─────────────────────────────────────────────────────────────────────────────
# Gap G — ev_ufcf_ltm in compute_peer_multiples
# ─────────────────────────────────────────────────────────────────────────────

from auto_valuation.data.comps import compute_peer_multiples, compute_peer_set_stats


class TestEVUFCFMultiple:
    BASE_PARAMS = dict(
        peer_ticker="TEST",
        market_cap_mm=1000.0,
        net_debt_mm=200.0,
        revenue_ltm=500.0,
        ebitda_ltm=120.0,
        ebit_ltm=90.0,
        fcf_ltm=70.0,
        net_income_ltm=60.0,
    )

    def test_ev_ufcf_present_when_provided(self):
        result = compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=80.0)
        assert "ev_ufcf_ltm" in result

    def test_ev_ufcf_correct_value(self):
        ev = 1000.0 + 200.0
        result = compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=80.0)
        assert result["ev_ufcf_ltm"] == pytest.approx(ev / 80.0)

    def test_ev_ufcf_absent_when_not_provided(self):
        result = compute_peer_multiples(**self.BASE_PARAMS)
        assert "ev_ufcf_ltm" not in result

    def test_ev_ufcf_absent_when_zero(self):
        result = compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=0.0)
        assert "ev_ufcf_ltm" not in result

    def test_ev_ufcf_absent_when_negative(self):
        result = compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=-10.0)
        assert "ev_ufcf_ltm" not in result

    def test_existing_multiples_unaffected(self):
        """Adding ufcf_ltm should not change any existing multiple."""
        without = compute_peer_multiples(**self.BASE_PARAMS)
        with_ufcf = compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=80.0)
        for key in ("ev_revenue_ltm", "ev_ebitda_ltm", "ev_ebit_ltm", "p_fcf_ltm", "p_e_ltm"):
            assert with_ufcf[key] == pytest.approx(without[key])

    def test_backward_compat_no_ufcf_param(self):
        """Call without ufcf_ltm (old callers) must not raise."""
        result = compute_peer_multiples(**self.BASE_PARAMS)
        assert result["ev_ebitda_ltm"] == pytest.approx(1200.0 / 120.0)

    def test_peer_set_stats_handles_ev_ufcf_absent(self):
        """compute_peer_set_stats must handle missing ev_ufcf_ltm gracefully."""
        peers = [
            compute_peer_multiples(**self.BASE_PARAMS),  # no ufcf
            compute_peer_multiples(**self.BASE_PARAMS),  # no ufcf
        ]
        stats = compute_peer_set_stats(peers)
        assert stats["ev_ufcf_ltm"]["n"] == 0

    def test_peer_set_stats_with_ev_ufcf(self):
        """Stats should include ev_ufcf_ltm when provided."""
        peers = [
            compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=80.0),
            compute_peer_multiples(**self.BASE_PARAMS, ufcf_ltm=100.0),
        ]
        stats = compute_peer_set_stats(peers)
        assert stats["ev_ufcf_ltm"]["n"] == 2
        ev = 1200.0
        m1 = ev / 80.0
        m2 = ev / 100.0
        expected_median = (m1 + m2) / 2.0
        assert stats["ev_ufcf_ltm"]["median"] == pytest.approx(expected_median, rel=0.01)

    def test_ev_ufcf_with_negative_net_debt(self):
        """Net-cash company: EV < market_cap, multiple still computed correctly."""
        params = dict(self.BASE_PARAMS)
        params["net_debt_mm"] = -100.0  # net cash
        result = compute_peer_multiples(**params, ufcf_ltm=60.0)
        ev = 1000.0 - 100.0
        assert result["ev_ufcf_ltm"] == pytest.approx(ev / 60.0)
