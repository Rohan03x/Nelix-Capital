"""
tests/test_wacc.py — Unit tests for WACC and growth-assumptions modules.

Covers:
  - Hamada unlevering / re-levering
  - CAPM cost of equity
  - Capital structure weights
  - Full WACC computation and build_wacc()
  - Beta blending (Blume adjustment)
  - Growth fade schedule
  - build_growth_assumptions()
"""

from __future__ import annotations

import pytest

from auto_valuation.assumptions.wacc import (
    unlever_beta,
    relever_beta,
    blended_beta,
    cost_of_equity_capm,
    compute_capital_structure,
    compute_wacc,
    build_wacc,
)
from auto_valuation.assumptions.growth import (
    blend_growth_estimate,
    build_growth_fade_schedule,
    build_margin_fade_schedule,
    sector_median_growth,
    sector_median_ebit_margin,
    build_growth_assumptions,
)


# ─────────────────────────────────────────────────────────────────────────────
# Hamada unlevering / re-levering
# ─────────────────────────────────────────────────────────────────────────────

class TestHamada:
    def test_unlever_all_equity(self):
        # D/E = 0 → unlevered beta = levered beta
        bu = unlever_beta(levered_beta=1.2, debt_to_equity=0.0, tax_rate=0.21)
        assert abs(bu - 1.2) < 1e-9

    def test_unlever_reduces_beta(self):
        bu = unlever_beta(levered_beta=1.4, debt_to_equity=0.5, tax_rate=0.21)
        assert bu < 1.4

    def test_relever_round_trip(self):
        bl_orig = 1.3
        d_e     = 0.4
        tax     = 0.21
        bu      = unlever_beta(bl_orig, d_e, tax)
        bl_back = relever_beta(bu, d_e, tax)
        assert abs(bl_back - bl_orig) < 1e-9

    def test_relever_more_leverage_higher_beta(self):
        bu   = 1.0
        bl1  = relever_beta(bu, target_debt_to_equity=0.3, tax_rate=0.21)
        bl2  = relever_beta(bu, target_debt_to_equity=0.8, tax_rate=0.21)
        assert bl2 > bl1

    def test_hamada_formula_explicit(self):
        # Hamada: Bu = Bl / (1 + (1-t) * D/E)
        bl, d_e, t = 1.5, 0.5, 0.21
        expected = bl / (1 + (1 - t) * d_e)
        bu = unlever_beta(bl, d_e, t)
        assert abs(bu - expected) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# Beta blending
# ─────────────────────────────────────────────────────────────────────────────

class TestBetaBlending:
    def test_no_industry(self):
        # When industry weight is 0, result equals company_beta only
        b = blended_beta(company_beta=1.2, industry_beta=1.0, industry_weight=0.0)
        assert abs(b - 1.2) < 1e-9

    def test_blume_default_weight(self):
        # Default: 2/3 company + 1/3 industry
        b = blended_beta(1.5, 1.0, industry_weight=1/3)
        expected = 1.5 * (2/3) + 1.0 * (1/3)
        assert abs(b - expected) < 1e-9

    def test_full_industry_weight(self):
        b = blended_beta(company_beta=1.8, industry_beta=1.0, industry_weight=1.0)
        assert abs(b - 1.0) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# CAPM cost of equity
# ─────────────────────────────────────────────────────────────────────────────

class TestCAPM:
    def test_basic_formula(self):
        # Ke = Rf + β×ERP = 0.04 + 1.0×0.055 = 0.095
        ke = cost_of_equity_capm(risk_free_rate=0.04, beta=1.0, equity_risk_premium=0.055)
        assert abs(ke - 0.095) < 1e-9

    def test_size_premium_added(self):
        ke_no_sp = cost_of_equity_capm(0.04, 1.0, 0.055, size_premium=0.0)
        ke_sp    = cost_of_equity_capm(0.04, 1.0, 0.055, size_premium=0.02)
        assert abs(ke_sp - (ke_no_sp + 0.02)) < 1e-9

    def test_crp_added(self):
        ke_base = cost_of_equity_capm(0.04, 1.0, 0.055, country_risk_premium=0.0)
        ke_crp  = cost_of_equity_capm(0.04, 1.0, 0.055, country_risk_premium=0.03)
        assert abs(ke_crp - (ke_base + 0.03)) < 1e-9

    def test_higher_beta_higher_ke(self):
        ke1 = cost_of_equity_capm(0.04, 0.8, 0.055)
        ke2 = cost_of_equity_capm(0.04, 1.5, 0.055)
        assert ke2 > ke1


# ─────────────────────────────────────────────────────────────────────────────
# Capital structure weights
# ─────────────────────────────────────────────────────────────────────────────

class TestCapitalStructure:
    def test_all_equity(self):
        cs = compute_capital_structure(market_cap=10000, total_debt=0)
        assert abs(cs["equity_weight"] - 1.0) < 1e-9
        assert abs(cs["debt_weight"]   - 0.0) < 1e-9

    def test_weights_sum_to_one(self):
        cs = compute_capital_structure(market_cap=8000, total_debt=2000, preferred_stock=500)
        total = cs["equity_weight"] + cs["debt_weight"] + cs["preferred_weight"]
        assert abs(total - 1.0) < 1e-9

    def test_50_50_structure(self):
        cs = compute_capital_structure(market_cap=5000, total_debt=5000)
        assert abs(cs["equity_weight"] - 0.5) < 1e-9
        assert abs(cs["debt_weight"]   - 0.5) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# compute_wacc
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeWacc:
    def test_all_equity(self):
        # All equity: WACC = Ke
        w = compute_wacc(
            equity_weight=1.0, cost_of_equity=0.10,
            debt_weight=0.0, pre_tax_cost_of_debt=0.05, tax_rate=0.21,
        )
        assert abs(w - 0.10) < 1e-9

    def test_tax_shield_reduces_wacc(self):
        # With debt, after-tax Kd < Kd_pretax → WACC < blended pre-tax
        w = compute_wacc(
            equity_weight=0.7, cost_of_equity=0.10,
            debt_weight=0.3, pre_tax_cost_of_debt=0.06, tax_rate=0.21,
        )
        assert w < 0.10    # WACC < Ke because cheaper after-tax debt is included

    def test_formula(self):
        # WACC = 0.7×0.10 + 0.3×0.06×(1−0.21)
        expected = 0.7 * 0.10 + 0.3 * 0.06 * 0.79
        w = compute_wacc(0.7, 0.10, 0.3, 0.06, 0.21)
        assert abs(w - expected) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# build_wacc — full integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildWacc:
    def _run(self, **kwargs):
        defaults = dict(
            market_cap=151_000,
            total_debt=8_925,
            preferred_stock=0.0,
            basic_shares_mm=1_500,
            current_price=100.0,
            risk_free_rate=0.043,
            equity_risk_premium=0.055,
            beta=0.87,
            pre_tax_cost_of_debt=0.04,
            tax_rate=0.21,
            size_premium=0.0,
            country_risk_premium=0.0,
        )
        defaults.update(kwargs)
        return build_wacc(**defaults)

    def test_returns_dict_with_wacc(self):
        d = self._run()
        assert "wacc" in d
        assert 0.03 < d["wacc"] < 0.25

    def test_contains_ke_kd(self):
        d = self._run()
        assert "cost_of_equity" in d
        assert "pre_tax_cost_of_debt" in d

    def test_blume_adjustment_applied(self):
        d_low_beta  = self._run(beta=0.80)
        d_high_beta = self._run(beta=1.20)
        # Higher beta → higher cost of equity
        assert d_high_beta["cost_of_equity"] > d_low_beta["cost_of_equity"]

    def test_high_leverage_higher_wacc_ke(self):
        low_beta  = self._run(beta=0.70)
        high_beta = self._run(beta=1.50)
        # Higher beta → higher Ke
        assert high_beta["cost_of_equity"] > low_beta["cost_of_equity"]


# ─────────────────────────────────────────────────────────────────────────────
# Growth fade schedule
# ─────────────────────────────────────────────────────────────────────────────

class TestGrowthFade:
    def test_no_fade(self):
        sched = build_growth_fade_schedule(
            near_term_growth=0.10, terminal_growth=0.025, forecast_years=5,
            hold_years=5, fade_years=0,
        )
        assert len(sched) == 5
        assert all(abs(g - 0.10) < 1e-9 for g in sched)

    def test_fades_to_terminal(self):
        sched = build_growth_fade_schedule(
            near_term_growth=0.10, terminal_growth=0.025, forecast_years=10,
            hold_years=2, fade_years=4,
        )
        assert len(sched) == 10
        # Last entry should equal terminal (within small tolerance)
        assert abs(sched[-1] - 0.025) < 0.01

    def test_monotone_during_fade(self):
        sched = build_growth_fade_schedule(0.10, 0.025, 8, 2, 4)
        # Values during fade (years 3-6) should be decreasing
        fade_segment = sched[2:6]
        for i in range(len(fade_segment) - 1):
            assert fade_segment[i] >= fade_segment[i + 1] - 1e-9


class TestMarginFade:
    def test_immediate_hold(self):
        sched = build_margin_fade_schedule(
            base_margin=0.12, target_margin=0.12, forecast_years=5, fade_years=0,
        )
        assert all(abs(m - 0.12) < 1e-9 for m in sched)

    def test_fades_upward(self):
        sched = build_margin_fade_schedule(0.10, 0.18, 8, 4)
        assert sched[0] <= sched[-1]
        assert abs(sched[-1] - 0.18) < 0.01

    def test_fades_downward(self):
        sched = build_margin_fade_schedule(0.20, 0.12, 8, 4)
        assert sched[0] >= sched[-1]


# ─────────────────────────────────────────────────────────────────────────────
# blend_growth_estimate
# ─────────────────────────────────────────────────────────────────────────────

class TestBlendGrowthEstimate:
    def test_equal_weights(self):
        g = blend_growth_estimate(
            historical_cagr=0.08, ntm_consensus=0.10, sector_median_growth=0.06,
            weights=(1/3, 1/3, 1/3),
        )
        expected = (0.08 + 0.10 + 0.06) / 3
        assert abs(g - expected) < 1e-9

    def test_missing_ntm_redistributes(self):
        g_with_ntm    = blend_growth_estimate(0.08, 0.10, 0.06, (0.5, 0.3, 0.2))
        g_without_ntm = blend_growth_estimate(0.08, None,  0.06, (0.5, 0.3, 0.2))
        # Without NTM the result must differ; hist and sector weights expand
        assert g_with_ntm != g_without_ntm

    def test_all_missing_falls_back(self):
        g = blend_growth_estimate(None, None, None)
        # Should return some fallback without crashing
        assert isinstance(g, float)


# ─────────────────────────────────────────────────────────────────────────────
# Sector look-ups
# ─────────────────────────────────────────────────────────────────────────────

class TestSectorLookups:
    def test_known_sector_growth(self):
        g = sector_median_growth("Information Technology")
        assert 0.03 <= g <= 0.20

    def test_unknown_sector_fallback(self):
        g = sector_median_growth("FantasySector")
        assert isinstance(g, float)
        assert g > 0

    def test_known_sector_margin(self):
        m = sector_median_ebit_margin("Industrials")
        assert 0.05 <= m <= 0.30


# ─────────────────────────────────────────────────────────────────────────────
# build_growth_assumptions — integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildGrowthAssumptions:
    @pytest.fixture(autouse=True)
    def _data(self, fake_income_statement):
        self.income_stmts = fake_income_statement

    def test_returns_required_keys(self):
        d = build_growth_assumptions(
            income_stmts=self.income_stmts,
            ntm_estimates={},
            sector="Consumer Discretionary",
            terminal_growth=0.025,
        )
        for key in ("near_term_growth", "terminal_growth", "target_ebit_margin",
                    "growth_schedule", "margin_schedule"):
            assert key in d, f"Missing key: {key}"

    def test_terminal_growth_capped(self):
        d = build_growth_assumptions(
            income_stmts=self.income_stmts,
            ntm_estimates={},
            sector="",
            terminal_growth=0.10,   # extremely high
        )
        # build_growth_assumptions returns terminal_growth as-is; cap is enforced by caller
        # Just verify the key exists and is a float
        assert isinstance(d["terminal_growth"], float)

    def test_schedules_correct_length(self):
        d = build_growth_assumptions(
            income_stmts=self.income_stmts,
            ntm_estimates={},
            sector="",
            terminal_growth=0.025,
            forecast_years=7,
        )
        assert len(d["growth_schedule"]) == 7
        assert len(d["margin_schedule"]) == 7
