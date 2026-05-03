"""
tests/test_new_modules.py — Tests for all newly created modules.

Covers:
  - model/discounting.py    (XNPV, XIRR, build_dcf_dates)
  - model/ratios.py         (DuPont, EVA, ROIC, coverage, FCFE, CFADS)
  - model/scenarios.py      (apply_scenario, run_all_scenarios)
  - model/reit.py           (FFO, AFFO, NAV)
  - model/monte_carlo.py    (run_monte_carlo_dcf)
  - model/forecast.py       (build_three_statement_forecast)
  - sensitivity/irr.py      (compute_implied_wacc, compute_fcfe_series)
  - data/estimates.py       (NTMEstimates, compute_ntm_multiples)
  - data/peers.py           (validate_peer_list, get_peers_from_overrides)
  - output/metrics.py       (compute_bvps_and_pb, compute_peg)
  - output/excel_writer.py  (FMT_* constants, build_output_path)
  - output/football_field.py (FootballFieldBand, build_football_field_bands)
  - output/tornado_chart.py  (compute_tornado_data)
  - assumptions/overrides.py (load_overrides, validate_overrides, deep_merge)
  - assumptions/defaults.py  (sector defaults, get helpers)
  - data/macro.py            (compute_size_premium, compute_crp)
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# model/discounting.py
# ═══════════════════════════════════════════════════════════════════════════

class TestXNPV:
    def test_single_cash_flow_no_discount(self):
        from auto_valuation.model.discounting import compute_xnpv
        d = date(2024, 1, 1)
        pv = compute_xnpv(0.0, [100.0], [d])
        assert pv == pytest.approx(100.0)

    def test_two_period_exact(self):
        from auto_valuation.model.discounting import compute_xnpv
        d0 = date(2024, 1, 1)
        d1 = date(2025, 1, 1)
        # date 0 → no discount; date 1 → exact days / 365.25 later
        pv2 = compute_xnpv(0.10, [0.0, 100.0], [d0, d1])
        days = (d1 - d0).days
        expected = 100.0 / (1.10 ** (days / 365.25))
        assert pv2 == pytest.approx(expected, rel=1e-6)

    def test_length_mismatch_raises(self):
        from auto_valuation.model.discounting import compute_xnpv
        with pytest.raises(ValueError):
            compute_xnpv(0.10, [100.0], [date(2024, 1, 1), date(2025, 1, 1)])

    def test_rate_below_negative_one_raises(self):
        from auto_valuation.model.discounting import compute_xnpv
        with pytest.raises(ValueError):
            compute_xnpv(-1.5, [100.0], [date(2024, 1, 1)])


class TestXIRR:
    def test_simple_irr(self):
        from auto_valuation.model.discounting import compute_xirr
        # Invest -1000 today, receive +1100 in 1 year → IRR ≈ 10%
        dates = [date(2024, 1, 1), date(2025, 1, 1)]
        cfs   = [-1000.0, 1100.0]
        irr = compute_xirr(cfs, dates)
        assert irr == pytest.approx(0.10, abs=0.002)

    def test_no_sign_change_raises(self):
        from auto_valuation.model.discounting import compute_xirr
        with pytest.raises(ValueError):
            compute_xirr([100.0, 200.0], [date(2024, 1, 1), date(2025, 1, 1)])


class TestBuildDCFDates:
    def test_length(self):
        from auto_valuation.model.discounting import build_dcf_dates
        dates = build_dcf_dates(date(2024, 1, 1), 7)
        assert len(dates) == 7

    def test_mid_year_first_date(self):
        from auto_valuation.model.discounting import build_dcf_dates
        d0    = date(2024, 1, 1)
        dates = build_dcf_dates(d0, 7, mid_year_convention=True)
        days_0 = (dates[0] - d0).days
        # Should be ≈ 0.5 × 365.25 ≈ 183 days
        assert 180 <= days_0 <= 186


# ═══════════════════════════════════════════════════════════════════════════
# model/ratios.py
# ═══════════════════════════════════════════════════════════════════════════

class TestDuPont:
    def test_roe_3factor(self):
        from auto_valuation.model.ratios import compute_dupont_3factor
        res = compute_dupont_3factor(
            net_income=200, revenue=1000, total_assets=2000, total_equity=1000
        )
        assert res["roe"] == pytest.approx(0.20, rel=1e-4)
        assert res["net_margin"] == pytest.approx(0.20)
        assert res["asset_turnover"] == pytest.approx(0.50)
        assert res["equity_multiplier"] == pytest.approx(2.0)

    def test_roe_5factor(self):
        from auto_valuation.model.ratios import compute_dupont_5factor
        res = compute_dupont_5factor(
            net_income=140, pretax_income=200, ebit=250,
            revenue=1000, total_assets=2000, total_equity=1000
        )
        assert "roe" in res
        assert res["roe"] == pytest.approx(0.14, rel=1e-4)


class TestROIC:
    def test_basic(self):
        from auto_valuation.model.ratios import compute_roic
        roic = compute_roic(100, 1000, 1200)
        assert roic == pytest.approx(100 / 1100, rel=1e-4)

    def test_no_closing(self):
        from auto_valuation.model.ratios import compute_roic
        roic = compute_roic(100, 1000)
        assert roic == pytest.approx(0.10)


class TestIncrementalROIC:
    def test_basic(self):
        from auto_valuation.model.ratios import compute_incremental_roic
        nopat = [100, 120, 145]
        ic    = [1000, 1100, 1250]
        result = compute_incremental_roic(nopat, ic)
        assert len(result) == 2
        assert result[0] == pytest.approx(20 / 100, rel=1e-4)

    def test_zero_delta_ic(self):
        from auto_valuation.model.ratios import compute_incremental_roic
        result = compute_incremental_roic([100, 110], [1000, 1000])
        assert result[0] is None


class TestEVA:
    def test_positive_eva(self):
        from auto_valuation.model.ratios import compute_eva
        eva, cc, roic = compute_eva(100, 0.09, 800)
        assert cc == pytest.approx(72.0)
        assert eva == pytest.approx(28.0)
        assert roic == pytest.approx(100 / 800)

    def test_eva_series(self):
        from auto_valuation.model.ratios import compute_eva_series
        results = compute_eva_series([100, 110], 0.09, [800, 900])
        assert len(results) == 2
        assert results[0][0] == pytest.approx(100 - 0.09 * 800)


class TestCoverageRatios:
    def test_icr(self):
        from auto_valuation.model.ratios import compute_coverage_ratios
        r = compute_coverage_ratios(
            ebit=200, ebitda=250, interest_expense=50,
            debt_service=80, capex=40
        )
        assert r["icr"] == pytest.approx(4.0)
        assert r["ebitda_icr"] == pytest.approx(5.0)


class TestBVPS:
    def test_bvps(self):
        from auto_valuation.model.ratios import compute_bvps
        bvps = compute_bvps(total_equity_mm=1000, basic_shares_mm=100)
        assert bvps == pytest.approx(10.0)

    def test_peg(self):
        from auto_valuation.model.ratios import compute_peg_ratio
        peg = compute_peg_ratio(20.0, 15.0)
        assert peg == pytest.approx(20.0 / 15.0)

    def test_peg_negative_growth(self):
        from auto_valuation.model.ratios import compute_peg_ratio
        assert compute_peg_ratio(20.0, -5.0) is None


# ═══════════════════════════════════════════════════════════════════════════
# model/scenarios.py
# ═══════════════════════════════════════════════════════════════════════════

class TestScenarios:
    def test_base_case_unchanged(self):
        from auto_valuation.model.scenarios import apply_scenario
        base = {"near_term_growth": 0.05, "wacc": 0.09, "terminal_g": 0.025}
        res  = apply_scenario(base, "base")
        assert res["near_term_growth"] == pytest.approx(0.05)
        assert res["wacc"]             == pytest.approx(0.09)

    def test_bull_increases_growth(self):
        from auto_valuation.model.scenarios import apply_scenario, SCENARIO_DELTAS
        base = {"near_term_growth": 0.05, "wacc": 0.09, "terminal_g": 0.025,
                "ebit_margin_current": 0.15, "ebit_margin_terminal": 0.18}
        res  = apply_scenario(base, "bull")
        delta = SCENARIO_DELTAS["bull"]["near_term_growth"]
        assert res["near_term_growth"] == pytest.approx(0.05 + delta)

    def test_bear_decreases_growth(self):
        from auto_valuation.model.scenarios import apply_scenario, SCENARIO_DELTAS
        base = {"near_term_growth": 0.05, "wacc": 0.09, "terminal_g": 0.025,
                "ebit_margin_current": 0.15, "ebit_margin_terminal": 0.18}
        res  = apply_scenario(base, "bear")
        delta = SCENARIO_DELTAS["bear"]["near_term_growth"]
        assert res["near_term_growth"] == pytest.approx(0.05 + delta)

    def test_unknown_scenario_raises(self):
        from auto_valuation.model.scenarios import apply_scenario
        with pytest.raises(ValueError):
            apply_scenario({}, "extreme_bull")

    def test_run_all_scenarios(self):
        from auto_valuation.model.scenarios import run_all_scenarios
        base = {"near_term_growth": 0.05, "wacc": 0.09, "terminal_g": 0.025,
                "ebit_margin_current": 0.15, "ebit_margin_terminal": 0.18}
        def fake_dcf(params):
            return {"equity_value_mm": 1000 * (1 + params.get("near_term_growth", 0))}
        results = run_all_scenarios(base, fake_dcf)
        assert "bull" in results
        assert "base" in results
        assert "bear" in results
        # Bull > Base > Bear
        assert results["bull"]["equity_value_mm"] > results["base"]["equity_value_mm"]
        assert results["base"]["equity_value_mm"] > results["bear"]["equity_value_mm"]

    def test_wacc_floor_enforcement(self):
        from auto_valuation.model.scenarios import apply_scenario
        # Extreme values should be clipped
        base = {"wacc": 0.02, "terminal_g": 0.025,
                "near_term_growth": 0.0, "ebit_margin_current": 0.0, "ebit_margin_terminal": 0.0}
        res = apply_scenario(base, "bear")
        # terminal_g must stay below WACC
        assert res["terminal_g"] < res["wacc"]


# ═══════════════════════════════════════════════════════════════════════════
# model/reit.py
# ═══════════════════════════════════════════════════════════════════════════

class TestREIT:
    def test_ffo(self):
        from auto_valuation.model.reit import compute_ffo
        ffo = compute_ffo(net_income=100, depreciation_amortization=50, gains_on_sale_of_property=20)
        assert ffo == pytest.approx(130.0)

    def test_affo(self):
        from auto_valuation.model.reit import compute_affo
        affo = compute_affo(ffo=130, maintenance_capex=15, straight_line_rent_adj=5)
        assert affo == pytest.approx(110.0)

    def test_nav(self):
        from auto_valuation.model.reit import compute_reit_nav
        nav = compute_reit_nav(
            noi_stabilised=50, cap_rate=0.05,
            cash_and_equivalents=100, other_assets=50,
            total_debt=400, preferred_equity=50
        )
        assert nav["gross_asset_value_mm"] == pytest.approx(1000.0)
        assert nav["nav_equity_mm"] == pytest.approx(700.0)

    def test_nav_zero_cap_rate_raises(self):
        from auto_valuation.model.reit import compute_reit_nav
        with pytest.raises(ValueError):
            compute_reit_nav(50, 0.0, 100, 50, 400)

    def test_multiples(self):
        from auto_valuation.model.reit import compute_reit_multiples
        r = compute_reit_multiples(share_price=20, ffo_per_share=2, affo_per_share=1.8)
        assert r["p_ffo"]  == pytest.approx(10.0)
        assert r["p_affo"] == pytest.approx(20.0 / 1.8, rel=1e-4)


# ═══════════════════════════════════════════════════════════════════════════
# model/monte_carlo.py
# ═══════════════════════════════════════════════════════════════════════════

class TestMonteCarlo:
    def _simple_dcf(self, params):
        """Simple mock DCF: equity value = 1000 × (1 + growth) / wacc."""
        g    = params.get("revenue_cagr", 0.05)
        wacc = params.get("wacc", 0.09)
        if wacc <= 0:
            return {"equity_value_mm": 0}
        return {"equity_value_mm": 1000 * (1 + g) / wacc}

    def test_returns_correct_shape(self):
        from auto_valuation.model.monte_carlo import run_monte_carlo_dcf
        arr, stats = run_monte_carlo_dcf(
            self._simple_dcf,
            {"revenue_cagr": 0.05, "ebit_margin_y5": 0.15, "wacc": 0.09, "terminal_g": 0.025},
            n_trials=500, seed=42
        )
        assert len(arr) == stats["n_valid"]
        assert stats["n_valid"] > 400   # should rarely fail
        assert stats["p50"] > 0

    def test_reproducible_with_seed(self):
        from auto_valuation.model.monte_carlo import run_monte_carlo_dcf
        base = {"revenue_cagr": 0.05, "ebit_margin_y5": 0.15, "wacc": 0.09, "terminal_g": 0.025}
        _, s1 = run_monte_carlo_dcf(self._simple_dcf, base, n_trials=200, seed=99)
        _, s2 = run_monte_carlo_dcf(self._simple_dcf, base, n_trials=200, seed=99)
        assert s1["p50"] == pytest.approx(s2["p50"])

    def test_wacc_always_gt_terminal_g(self):
        """All trials must maintain WACC > terminal_g."""
        from auto_valuation.model.monte_carlo import run_monte_carlo_dcf
        import numpy as np

        captured = []
        def capture_dcf(params):
            captured.append((params["wacc"], params["terminal_g"]))
            return {"equity_value_mm": 1000}

        run_monte_carlo_dcf(
            capture_dcf,
            {"revenue_cagr": 0.05, "ebit_margin_y5": 0.15, "wacc": 0.09, "terminal_g": 0.025},
            n_trials=200, seed=7
        )
        for wacc, g in captured:
            assert wacc > g, f"WACC={wacc} must be > terminal_g={g}"

    def test_all_trials_fail_raises(self):
        from auto_valuation.model.monte_carlo import run_monte_carlo_dcf
        def broken(params): raise RuntimeError("fail")
        with pytest.raises(RuntimeError, match="all trials failed"):
            run_monte_carlo_dcf(broken, {"revenue_cagr": 0.05, "ebit_margin_y5": 0.15,
                                          "wacc": 0.09, "terminal_g": 0.025},
                                n_trials=10, seed=1)


# ═══════════════════════════════════════════════════════════════════════════
# model/forecast.py
# ═══════════════════════════════════════════════════════════════════════════

class TestThreeStatementForecast:
    def _base_kwargs(self):
        return dict(
            base_revenue=1000.0,
            base_ebit_margin=0.15,
            base_da=50.0,
            base_sbc=20.0,
            base_cash=100.0,
            base_ibd=200.0,
            base_ppe_net=400.0,
            base_goodwill=100.0,
            base_other_lta=50.0,
            base_nowc=80.0,
            base_retained_earnings=300.0,
            base_other_liabilities=150.0,
            base_other_equity=200.0,
            revenue_growth_rates=[0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.025],
            ebit_margin_schedule=[0.15]*7,
            capex_pct_schedule=[0.05]*7,
            da_pct_revenue=0.05,
            sbc_pct_revenue=0.02,
            effective_tax_rate=0.25,
            dso_days=45.0,
            dio_days=30.0,
            dpo_days=35.0,
            cost_of_debt=0.05,
            debt_to_total_assets=0.30,
            forecast_years=7,
        )

    def test_returns_correct_number_of_years(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        years = build_three_statement_forecast(**self._base_kwargs())
        assert len(years) == 7

    def test_revenue_grows(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        years = build_three_statement_forecast(**self._base_kwargs())
        assert years[0].revenue > 1000.0
        assert years[1].revenue > years[0].revenue

    def test_cash_floor_maintained(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        years = build_three_statement_forecast(**self._base_kwargs())
        for fy in years:
            min_cash = 0.02 * fy.revenue
            assert fy.cash >= min_cash - 0.01, f"Cash floor violated in year {fy.year}"

    def test_ibd_convergence(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        years = build_three_statement_forecast(**self._base_kwargs())
        for fy in years:
            # IBD ≈ D/TA × total_assets
            expected_ibd = 0.30 * fy.total_assets
            assert fy.ibd == pytest.approx(expected_ibd, rel=1e-3), \
                f"IBD convergence failed in year {fy.year}"

    def test_ocf_formula(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        kwargs = self._base_kwargs()
        years  = build_three_statement_forecast(**kwargs)
        for fy in years:
            # OCF = NI + D&A + SBC - ΔNOWC
            expected_ocf = fy.net_income + fy.da + fy.sbc - fy.delta_nowc
            assert fy.ocf == pytest.approx(expected_ocf, rel=1e-4)

    def test_pension_expense(self):
        from auto_valuation.model.forecast import build_three_statement_forecast
        kwargs = self._base_kwargs()
        kwargs["pension_service_pct"] = 0.01
        kwargs["pension_interest_flat"] = 5.0
        years = build_three_statement_forecast(**kwargs)
        # Year 1 pension should be ≈ year1_revenue × 0.01 + 5
        fy1 = years[0]
        expected = fy1.revenue * 0.01 + 5.0
        assert fy1.pension_expense == pytest.approx(expected, rel=1e-4)


class TestBalanceSheetClose:
    def test_no_raise_on_closed_bs(self):
        from auto_valuation.model.forecast import ForecastYear, check_balance_sheet_closes
        fy = ForecastYear(year=1)
        fy.total_assets      = 1000.0
        fy.total_liabilities = 600.0
        fy.total_equity      = 400.0
        assert check_balance_sheet_closes(fy) is True

    def test_raises_on_open_bs(self):
        from auto_valuation.model.forecast import ForecastYear, check_balance_sheet_closes
        fy = ForecastYear(year=1)
        fy.total_assets      = 1000.0
        fy.total_liabilities = 600.0
        fy.total_equity      = 300.0   # doesn't close
        with pytest.raises(RuntimeError):
            check_balance_sheet_closes(fy)


# ═══════════════════════════════════════════════════════════════════════════
# sensitivity/irr.py
# ═══════════════════════════════════════════════════════════════════════════

class TestImpliedWACC:
    def test_known_wacc(self):
        from auto_valuation.sensitivity.irr import compute_implied_wacc
        # 5 equal FCFs of 100, TV=500, PV at 10% mid-year
        ufcfs = [100.0, 100.0, 100.0, 100.0, 100.0]
        tv    = 500.0
        # Compute PV at 10%
        pv = sum(100 / 1.10 ** (i + 0.5) for i in range(5)) + 500 / 1.10 ** 5
        implied = compute_implied_wacc(ufcfs, tv, pv, mid_year=True)
        assert implied == pytest.approx(0.10, abs=0.001)


class TestFCFESeries:
    def test_basic(self):
        from auto_valuation.sensitivity.irr import compute_fcfe_series
        ufcfs    = [100.0, 110.0]
        int_exp  = [-20.0, -22.0]
        net_borr = [10.0, 5.0]
        fcfe = compute_fcfe_series(ufcfs, int_exp, 0.25, net_borr)
        # FCFE = UFCF + ITS + net_borrowings
        # ITS = -int_exp × tax = 20×0.25 = 5
        assert fcfe[0] == pytest.approx(100 + 5 + 10)


# ═══════════════════════════════════════════════════════════════════════════
# data/estimates.py
# ═══════════════════════════════════════════════════════════════════════════

class TestNTMEstimates:
    def test_to_dict(self):
        from auto_valuation.data.estimates import NTMEstimates
        est = NTMEstimates(revenue_mm=1000, ebitda_mm=200, source="fmp")
        d   = est.to_dict()
        assert d["ntm_revenue_mm"] == 1000
        assert d["source"] == "fmp"

    def test_compute_ntm_multiples(self):
        from auto_valuation.data.estimates import NTMEstimates, compute_ntm_multiples
        est = NTMEstimates(revenue_mm=1000, ebitda_mm=200)
        m   = compute_ntm_multiples(5000, 3000, est, diluted_shares_mm=100)
        assert m["ntm_ev_revenue"] == pytest.approx(5.0)
        assert m["ntm_ev_ebitda"]  == pytest.approx(25.0)

    def test_apply_overrides(self):
        from auto_valuation.data.estimates import NTMEstimates, apply_ntm_overrides
        est = NTMEstimates(revenue_mm=1000)
        ov  = {"ntm_revenue_mm": 1200, "ntm_ebitda_mm": 250}
        est = apply_ntm_overrides(est, ov)
        assert est.revenue_mm == pytest.approx(1200)
        assert est.ebitda_mm  == pytest.approx(250)


# ═══════════════════════════════════════════════════════════════════════════
# data/peers.py
# ═══════════════════════════════════════════════════════════════════════════

class TestPeers:
    def test_validate_deduplication(self):
        from auto_valuation.data.peers import validate_peer_list
        peers = validate_peer_list(["AAPL", "MSFT", "AAPL", "GOOG"], min_peers=2)
        assert len(peers) == 3
        assert "AAPL" in peers

    def test_validate_truncates(self):
        from auto_valuation.data.peers import validate_peer_list
        peers = validate_peer_list(["A", "B", "C", "D", "E"], min_peers=2, max_peers=3)
        assert len(peers) == 3

    def test_get_peers_from_overrides(self):
        from auto_valuation.data.peers import get_peers_from_overrides
        ov    = {"peer_tickers": ["AAPL", "MSFT", "GOOG"], "exclude_peers": ["GOOG"]}
        peers = get_peers_from_overrides(ov)
        assert "GOOG" not in peers
        assert "AAPL" in peers

    def test_no_override_returns_none(self):
        from auto_valuation.data.peers import get_peers_from_overrides
        assert get_peers_from_overrides({}) is None


# ═══════════════════════════════════════════════════════════════════════════
# output/metrics.py
# ═══════════════════════════════════════════════════════════════════════════

class TestOutputMetrics:
    def test_bvps_and_pb(self):
        from auto_valuation.output.metrics import compute_bvps_and_pb
        r = compute_bvps_and_pb(total_equity_mm=1000, basic_shares_mm=100, current_price=15)
        assert r["bvps"]     == pytest.approx(10.0)
        assert r["pb_ratio"] == pytest.approx(1.5)

    def test_pb_none_when_no_price(self):
        from auto_valuation.output.metrics import compute_bvps_and_pb
        r = compute_bvps_and_pb(1000, 100)
        assert r["pb_ratio"] is None

    def test_peg(self):
        from auto_valuation.output.metrics import compute_peg
        assert compute_peg(20.0, 15.0) == pytest.approx(20.0 / 15.0)

    def test_peg_none_for_zero_growth(self):
        from auto_valuation.output.metrics import compute_peg
        assert compute_peg(20.0, 0.0) is None


# ═══════════════════════════════════════════════════════════════════════════
# output/excel_writer.py
# ═══════════════════════════════════════════════════════════════════════════

class TestExcelWriter:
    def test_fmt_constants_defined(self):
        from auto_valuation.output.excel_writer import (
            FMT_CURRENCY_M, FMT_PCT_1DP, FMT_MULTIPLE, FMT_PRICE,
            FMT_NEG_RED, FMT_DATE
        )
        assert FMT_CURRENCY_M == "#,##0"
        assert "%" in FMT_PCT_1DP
        assert "x" in FMT_MULTIPLE.lower() or '"x"' in FMT_MULTIPLE

    def test_build_output_path(self):
        from auto_valuation.output.excel_writer import build_output_path
        with tempfile.TemporaryDirectory() as tmp:
            p = build_output_path("AAPL", output_dir=tmp)
            assert "AAPL" in str(p)
            assert str(p).endswith(".xlsx")
            assert Path(tmp).exists()

    def test_row_format_map_coverage(self):
        from auto_valuation.output.excel_writer import ROW_FORMAT_MAP
        assert "revenue" in ROW_FORMAT_MAP
        assert "ufcf"    in ROW_FORMAT_MAP
        assert "wacc"    in ROW_FORMAT_MAP


# ═══════════════════════════════════════════════════════════════════════════
# output/football_field.py
# ═══════════════════════════════════════════════════════════════════════════

class TestFootballField:
    def test_band_width(self):
        from auto_valuation.output.football_field import FootballFieldBand
        b = FootballFieldBand("DCF", low=80.0, high=120.0)
        assert b.width()    == pytest.approx(40.0)
        assert b.midpoint   == pytest.approx(100.0)

    def test_build_bands_returns_4(self):
        from auto_valuation.output.football_field import build_football_field_bands
        bands = build_football_field_bands(80, 120, 75, 110, 70, 100, 85, 105)
        assert len(bands) == 4
        assert bands[0].label == "DCF"
        assert bands[3].label == "52-Week Range"

    def test_none_bands(self):
        from auto_valuation.output.football_field import FootballFieldBand
        b = FootballFieldBand("TX", low=None, high=None)
        assert b.width() is None


# ═══════════════════════════════════════════════════════════════════════════
# output/tornado_chart.py
# ═══════════════════════════════════════════════════════════════════════════

class TestTornadoChart:
    def _mock_dcf(self, params):
        return 100.0 + params.get("wacc", 0.09) * -500.0 + params.get("terminal_g", 0.025) * 200.0

    def test_compute_tornado_data(self):
        from auto_valuation.output.tornado_chart import compute_tornado_data
        base   = {"wacc": 0.09, "terminal_g": 0.025}
        drivers = {"wacc": (0.08, 0.10), "terminal_g": (0.02, 0.03)}
        rows = compute_tornado_data(self._mock_dcf, base, drivers)
        assert len(rows) == 2
        # Should be sorted by magnitude descending
        assert rows[0]["magnitude"] >= rows[1]["magnitude"]

    def test_each_row_has_required_keys(self):
        from auto_valuation.output.tornado_chart import compute_tornado_data
        base   = {"wacc": 0.09}
        rows = compute_tornado_data(lambda p: 100.0, base, {"wacc": (0.08, 0.10)})
        for key in ("driver", "impact_low", "impact_high", "magnitude"):
            assert key in rows[0]


# ═══════════════════════════════════════════════════════════════════════════
# assumptions/overrides.py
# ═══════════════════════════════════════════════════════════════════════════

class TestOverrides:
    def test_validate_valid_keys(self):
        from auto_valuation.assumptions.overrides import validate_overrides
        raw = {"wacc_override": 0.09, "terminal_g": 0.025}
        cleaned = validate_overrides(raw)
        assert cleaned["wacc_override"] == pytest.approx(0.09)

    def test_strip_comment_keys(self):
        from auto_valuation.assumptions.overrides import validate_overrides
        raw = {"_version": "v4", "_comment": "test", "terminal_g": 0.025}
        cleaned = validate_overrides(raw)
        assert "_version" not in cleaned
        assert "terminal_g" in cleaned

    def test_range_violation_raises(self):
        from auto_valuation.assumptions.overrides import validate_overrides
        from auto_valuation.utils.error import ConfigError
        with pytest.raises(ConfigError):
            validate_overrides({"wacc_override": 2.0})  # > 0.80

    def test_deep_merge(self):
        from auto_valuation.assumptions.overrides import deep_merge
        base     = {"a": 1, "nested": {"x": 10, "y": 20}}
        override = {"a": 2, "nested": {"y": 99}, "b": 3}
        result   = deep_merge(base, override)
        assert result["a"]             == 2
        assert result["nested"]["x"]   == 10   # preserved
        assert result["nested"]["y"]   == 99   # overridden
        assert result["b"]             == 3

    def test_load_overrides_missing_file(self):
        from auto_valuation.assumptions.overrides import load_overrides
        result = load_overrides("NONEXISTENT_TICKER_XYZ", overrides_dir="overrides")
        assert result == {}

    def test_load_overrides_from_file(self):
        from auto_valuation.assumptions.overrides import load_overrides
        with tempfile.TemporaryDirectory() as tmp:
            data = {"terminal_g": 0.03, "_comment": "test"}
            path = Path(tmp) / "TEST.json"
            path.write_text(json.dumps(data))
            result = load_overrides("TEST", overrides_dir=tmp)
            assert result["terminal_g"] == pytest.approx(0.03)
            assert "_comment" not in result

    def test_int_to_float_coercion(self):
        from auto_valuation.assumptions.overrides import validate_overrides
        raw = {"terminal_g": 0}   # int 0 should coerce to float 0.0
        cleaned = validate_overrides(raw)
        assert isinstance(cleaned["terminal_g"], float)
        assert cleaned["terminal_g"] == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════
# assumptions/defaults.py
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaults:
    def test_get_sector_ebit_margin(self):
        from auto_valuation.assumptions.defaults import get_sector_ebit_margin
        margin = get_sector_ebit_margin("Technology")
        assert 0 < margin < 1

    def test_get_sector_ebit_margin_fuzzy(self):
        from auto_valuation.assumptions.defaults import get_sector_ebit_margin
        # "technology" (lowercase) should match "Technology"
        margin = get_sector_ebit_margin("technology")
        assert 0 < margin < 1

    def test_get_sector_ebit_margin_unknown(self):
        from auto_valuation.assumptions.defaults import get_sector_ebit_margin
        margin = get_sector_ebit_margin("SomeUnknownSector_XYZ")
        # Should return the Default value without error
        assert 0 < margin < 1

    def test_get_sector_capex_pct(self):
        from auto_valuation.assumptions.defaults import get_sector_capex_pct
        pct = get_sector_capex_pct("Energy")
        assert 0 < pct < 1

    def test_get_sector_wc_days(self):
        from auto_valuation.assumptions.defaults import get_sector_wc_days
        wc = get_sector_wc_days("Consumer Staples")
        assert "dso" in wc
        assert "dio" in wc
        assert "dpo" in wc


# ═══════════════════════════════════════════════════════════════════════════
# data/macro.py
# ═══════════════════════════════════════════════════════════════════════════

class TestMacro:
    def test_compute_size_premium_large_cap(self):
        from auto_valuation.data.macro import compute_size_premium
        # Very large cap (e.g. $500B) → size premium ≈ 0
        sp = compute_size_premium(500_000)
        assert sp >= 0
        assert sp < 0.02

    def test_compute_size_premium_micro_cap(self):
        from auto_valuation.data.macro import compute_size_premium
        # Micro cap ($50M) → higher premium
        sp = compute_size_premium(50)
        assert sp > 0.02

    def test_compute_crp_us_zero(self):
        from auto_valuation.data.macro import compute_crp
        crp = compute_crp("US")
        assert crp == pytest.approx(0.0)

    def test_compute_crp_known_country(self):
        from auto_valuation.data.macro import compute_crp
        # Brazil has non-zero CRP
        crp = compute_crp("BR")
        assert crp > 0

    def test_compute_crp_unknown_returns_zero(self):
        from auto_valuation.data.macro import compute_crp
        crp = compute_crp("XX")
        assert crp == pytest.approx(0.0)

    def test_compute_total_beta(self):
        from auto_valuation.data.macro import compute_total_beta
        # If correlation = 1, total beta == market beta
        total = compute_total_beta(market_beta=1.2, correlation_with_market=1.0)
        assert total == pytest.approx(1.2)

    def test_compute_total_beta_low_correlation(self):
        from auto_valuation.data.macro import compute_total_beta
        # Lower correlation → higher total beta
        total = compute_total_beta(market_beta=1.0, correlation_with_market=0.5)
        assert total == pytest.approx(2.0)

    def test_compute_unlevered_beta_cash_adjusted(self):
        from auto_valuation.data.macro import compute_unlevered_beta_cash_adjusted
        # With no cash, beta_cash_adj should equal input
        result = compute_unlevered_beta_cash_adjusted(1.0, 0.0, 1000.0)
        # May return float or tuple(beta, cash_pct) — extract scalar
        beta = result[0] if isinstance(result, (tuple, list)) else result
        assert beta == pytest.approx(1.0)

    def test_rf_fallbacks_present(self):
        from auto_valuation.data.macro import RF_FALLBACKS
        assert "USD" in RF_FALLBACKS
        assert "EUR" in RF_FALLBACKS
        assert RF_FALLBACKS["USD"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# output/scenarios_sheet.py  (import test only — openpyxl required)
# ═══════════════════════════════════════════════════════════════════════════

class TestScenariosSheet:
    def test_import(self):
        """Module must be importable."""
        import auto_valuation.output.scenarios_sheet  # noqa: F401

    def test_write_scenarios_sheet(self):
        """Write scenarios sheet without crashing when openpyxl available."""
        try:
            import openpyxl
        except ImportError:
            pytest.skip("openpyxl not available")

        from auto_valuation.output.scenarios_sheet import write_scenarios_sheet
        wb = openpyxl.Workbook()
        results = {
            "bull": {"equity_value_mm": 1200, "implied_share_price": 60.0,
                     "enterprise_value_mm": 1500, "pv_cashflows_mm": 500,
                     "pv_terminal_value_mm": 1000, "tv_pct_of_ev": 0.667,
                     "wacc": 0.085, "terminal_g": 0.030,
                     "revenue_cagr": 0.07, "ebit_margin_y5": 0.17},
            "base": {"equity_value_mm": 1000, "implied_share_price": 50.0,
                     "enterprise_value_mm": 1300, "pv_cashflows_mm": 430,
                     "pv_terminal_value_mm": 870, "tv_pct_of_ev": 0.67,
                     "wacc": 0.09, "terminal_g": 0.025,
                     "revenue_cagr": 0.05, "ebit_margin_y5": 0.15},
            "bear": {"equity_value_mm": 800,  "implied_share_price": 40.0,
                     "enterprise_value_mm": 1100, "pv_cashflows_mm": 380,
                     "pv_terminal_value_mm": 720, "tv_pct_of_ev": 0.654,
                     "wacc": 0.095, "terminal_g": 0.020,
                     "revenue_cagr": 0.03, "ebit_margin_y5": 0.13},
        }
        write_scenarios_sheet(wb, results, current_price=48.0)
        assert "Scenarios" in [ws.title for ws in wb.worksheets]
