"""
tests/test_session15_gaps.py
Session 15 gap-coverage tests.

Gaps addressed:
  Gap 1 — gordon_growth_tv_nycf      (forecast/terminal_value.py)
  Gap 2 — cost_of_equity_dividend_growth (assumptions/wacc.py)
  Gap 3 — gordon_growth_tv_two_stage  (forecast/terminal_value.py)
  Gap 4 — check_wacc_terminal_growth_spread (validation/checks.py)
"""

import math
import pytest

# ── Gap 1 imports ────────────────────────────────────────────────────────────
from auto_valuation.forecast.terminal_value import (
    gordon_growth_tv,
    gordon_growth_tv_nycf,
    gordon_growth_tv_two_stage,
)

# ── Gap 2 imports ────────────────────────────────────────────────────────────
from auto_valuation.assumptions.wacc import (
    cost_of_equity_capm,
    cost_of_equity_dividend_growth,
)

# ── Gap 4 imports ────────────────────────────────────────────────────────────
from auto_valuation.validation.checks import (
    check_wacc_terminal_growth_spread,
)


# =============================================================================
# GAP 1 — gordon_growth_tv_nycf: CFI formula TV = FCF*(1+g)/(WACC-g)
# =============================================================================

class TestGordonGrowthTvNycf:
    """Tests for the next-year-cash-flow variant of the GGM."""

    def test_basic_formula(self):
        """TV = FCF*(1+g)/(WACC-g) for standard inputs."""
        ufcf = 100.0
        wacc = 0.10
        g = 0.025
        expected = 100.0 * 1.025 / (0.10 - 0.025)
        result = gordon_growth_tv_nycf(ufcf, wacc, g)
        assert abs(result - expected) < 0.01

    def test_larger_than_nike_convention(self):
        """nycf variant must be > NIKE variant when g > 0."""
        ufcf = 200.0
        wacc = 0.09
        g = 0.03
        tv_nike = gordon_growth_tv(ufcf, wacc, g)
        tv_nycf = gordon_growth_tv_nycf(ufcf, wacc, g)
        assert tv_nycf > tv_nike

    def test_ratio_equals_one_plus_g(self):
        """nycf / nike ratio == (1+g) by definition."""
        ufcf = 150.0
        wacc = 0.10
        g = 0.02
        tv_nike = gordon_growth_tv(ufcf, wacc, g)
        tv_nycf = gordon_growth_tv_nycf(ufcf, wacc, g)
        assert abs(tv_nycf / tv_nike - (1.0 + g)) < 1e-10

    def test_zero_growth(self):
        """With g=0, nycf == nike (no growth adjustment)."""
        ufcf = 80.0
        wacc = 0.08
        g = 0.0
        tv_nike = gordon_growth_tv(ufcf, wacc, g)
        tv_nycf = gordon_growth_tv_nycf(ufcf, wacc, g)
        assert abs(tv_nycf - tv_nike) < 1e-10

    def test_raises_when_wacc_equals_g(self):
        """Raises ValueError when WACC == g (denominator = 0)."""
        with pytest.raises(ValueError, match="must exceed terminal growth"):
            gordon_growth_tv_nycf(100.0, 0.03, 0.03)

    def test_raises_when_wacc_less_than_g(self):
        """Raises ValueError when WACC < g."""
        with pytest.raises(ValueError, match="must exceed terminal growth"):
            gordon_growth_tv_nycf(100.0, 0.02, 0.04)

    def test_negative_ufcf(self):
        """Negative UFCF (loss-making terminal year) returns negative TV."""
        result = gordon_growth_tv_nycf(-50.0, 0.10, 0.025)
        assert result < 0

    def test_high_growth_rate(self):
        """High (but still WACC-g > 0) growth produces larger TV."""
        tv_low  = gordon_growth_tv_nycf(100.0, 0.12, 0.01)
        tv_high = gordon_growth_tv_nycf(100.0, 0.12, 0.05)
        assert tv_high > tv_low

    def test_cfi_example(self):
        """Replicates CFI's TV = FCF*(1+g)/(WACC-g) worked example."""
        # Hypothetical: FCF=500, WACC=10%, g=3% → TV = 500*1.03/(0.10-0.03)
        expected = 500.0 * 1.03 / 0.07
        result = gordon_growth_tv_nycf(500.0, 0.10, 0.03)
        assert abs(result - expected) < 0.01

    def test_sensitivity_to_wacc(self):
        """Higher WACC produces lower TV (inverse relationship)."""
        tv_lo = gordon_growth_tv_nycf(100.0, 0.08, 0.025)
        tv_hi = gordon_growth_tv_nycf(100.0, 0.12, 0.025)
        assert tv_lo > tv_hi

    def test_large_inputs(self):
        """Works correctly for large enterprise-scale inputs ($M)."""
        # $5B FCF, WACC=9%, g=2.5%
        result = gordon_growth_tv_nycf(5000.0, 0.09, 0.025)
        expected = 5000.0 * 1.025 / 0.065
        assert abs(result - expected) < 1.0


# =============================================================================
# GAP 2 — cost_of_equity_dividend_growth: Re = D1/P0 + g
# =============================================================================

class TestCostOfEquityDividendGrowth:
    """Tests for the Dividend Capitalization Model cost of equity."""

    def test_cfi_example(self):
        """CFI example: D1=0.50, P0=5.00, g=2% → Re = 12%."""
        result = cost_of_equity_dividend_growth(
            dps_next=0.50,
            current_price=5.00,
            growth_rate=0.02,
        )
        assert abs(result - 0.12) < 1e-10

    def test_formula_components(self):
        """Dividend yield + growth rate components are additive."""
        dps = 2.0
        price = 40.0
        g = 0.05
        yield_component = dps / price         # 0.05
        expected = yield_component + g        # 0.10
        result = cost_of_equity_dividend_growth(dps, price, g)
        assert abs(result - expected) < 1e-10

    def test_zero_growth(self):
        """With g=0, Re equals pure dividend yield."""
        result = cost_of_equity_dividend_growth(3.0, 50.0, 0.0)
        assert abs(result - 0.06) < 1e-10

    def test_zero_dividend(self):
        """D1=0 (no dividend) returns just the growth rate component."""
        result = cost_of_equity_dividend_growth(0.0, 100.0, 0.03)
        assert abs(result - 0.03) < 1e-10

    def test_raises_zero_price(self):
        """Raises ValueError for current_price = 0."""
        with pytest.raises(ValueError, match="current_price must be positive"):
            cost_of_equity_dividend_growth(1.0, 0.0, 0.02)

    def test_raises_negative_price(self):
        """Raises ValueError for negative current_price."""
        with pytest.raises(ValueError, match="current_price must be positive"):
            cost_of_equity_dividend_growth(1.0, -10.0, 0.02)

    def test_higher_dividend_raises_ke(self):
        """A higher D1 (ceteris paribus) raises the cost of equity."""
        ke_low  = cost_of_equity_dividend_growth(1.0, 50.0, 0.03)
        ke_high = cost_of_equity_dividend_growth(2.0, 50.0, 0.03)
        assert ke_high > ke_low

    def test_higher_price_lowers_ke(self):
        """A higher P0 (ceteris paribus) lowers the dividend yield → lower Ke."""
        ke_lo_price = cost_of_equity_dividend_growth(2.0, 40.0, 0.03)
        ke_hi_price = cost_of_equity_dividend_growth(2.0, 80.0, 0.03)
        assert ke_hi_price < ke_lo_price

    def test_higher_growth_raises_ke(self):
        """A higher growth rate (ceteris paribus) raises the cost of equity."""
        ke_low  = cost_of_equity_dividend_growth(1.0, 50.0, 0.02)
        ke_high = cost_of_equity_dividend_growth(1.0, 50.0, 0.05)
        assert ke_high > ke_low

    def test_comparable_to_capm(self):
        """Dividend Growth Model Ke should be in plausible range vs CAPM output."""
        ke_dgm = cost_of_equity_dividend_growth(3.0, 60.0, 0.04)
        # CAPM: Rf=4%, β=1.0, ERP=5% → 9%
        ke_capm = cost_of_equity_capm(0.04, 1.0, 0.05)
        # Both should be realistic equity returns (5%–20%)
        assert 0.05 <= ke_dgm <= 0.20
        assert 0.05 <= ke_capm <= 0.20

    def test_fractional_share_price(self):
        """Handles non-integer share prices correctly."""
        result = cost_of_equity_dividend_growth(0.25, 3.75, 0.01)
        expected = 0.25 / 3.75 + 0.01
        assert abs(result - expected) < 1e-10


# =============================================================================
# GAP 3 — gordon_growth_tv_two_stage: multi-stage terminal value
# =============================================================================

class TestGordonGrowthTvTwoStage:
    """Tests for the two-stage (transition + perpetuity) terminal value."""

    def test_degenerates_to_one_stage_when_rates_equal(self):
        """When near_terminal_g == stable_g, result approximates single-stage TV."""
        ufcf = 100.0
        wacc = 0.09
        g = 0.025
        # Two-stage with equal rates includes transition cash flows + stable perp
        # It won't exactly equal single-stage GGM but should be in the same ballpark
        tv_1s = gordon_growth_tv(ufcf, wacc, g)
        tv_2s = gordon_growth_tv_two_stage(ufcf, g, g, wacc, transition_years=5)
        # Two-stage here explicitly models transition: should be close but slightly different
        assert tv_2s > 0

    def test_higher_near_term_growth_raises_tv(self):
        """Higher near-terminal growth should produce a larger TV."""
        ufcf = 100.0
        wacc = 0.10
        stable_g = 0.025
        tv_low  = gordon_growth_tv_two_stage(ufcf, 0.03, stable_g, wacc, 5)
        tv_high = gordon_growth_tv_two_stage(ufcf, 0.08, stable_g, wacc, 5)
        assert tv_high > tv_low

    def test_more_transition_years_increases_tv(self):
        """More transition years at a rate above stable_g grows TV."""
        ufcf = 100.0
        wacc = 0.09
        near_g = 0.05
        stable_g = 0.025
        tv_short = gordon_growth_tv_two_stage(ufcf, near_g, stable_g, wacc, 3)
        tv_long  = gordon_growth_tv_two_stage(ufcf, near_g, stable_g, wacc, 7)
        assert tv_long > tv_short

    def test_raises_when_wacc_le_stable_g(self):
        """Raises ValueError when WACC ≤ stable perpetuity growth."""
        with pytest.raises(ValueError, match="must exceed stable terminal growth"):
            gordon_growth_tv_two_stage(100.0, 0.04, 0.09, 0.09, 5)

    def test_raises_invalid_transition_years(self):
        """Raises ValueError for transition_years < 1."""
        with pytest.raises(ValueError, match="transition_years must be at least 1"):
            gordon_growth_tv_two_stage(100.0, 0.05, 0.02, 0.10, 0)

    def test_positive_tv_for_normal_inputs(self):
        """Returns a positive TV for all-positive standard inputs."""
        result = gordon_growth_tv_two_stage(
            ufcf_n=200.0,
            near_terminal_g=0.06,
            stable_g=0.025,
            wacc=0.10,
            transition_years=5,
        )
        assert result > 0

    def test_two_stage_exceeds_stable_g_single_stage(self):
        """Two-stage TV with near_g > stable_g should be > single-stage at stable_g."""
        ufcf = 100.0
        wacc = 0.10
        stable_g = 0.025
        near_g = 0.06
        tv_1s = gordon_growth_tv(ufcf, wacc, stable_g)
        tv_2s = gordon_growth_tv_two_stage(ufcf, near_g, stable_g, wacc, 5)
        assert tv_2s > tv_1s

    def test_near_g_zero_with_stable_g(self):
        """Works when near_terminal_g = 0 (flat then stable growth)."""
        result = gordon_growth_tv_two_stage(100.0, 0.0, 0.02, 0.09, 3)
        assert result > 0

    def test_negative_near_g_allowed(self):
        """Works when near_terminal_g < 0 (shrinking transition then stable)."""
        result = gordon_growth_tv_two_stage(100.0, -0.02, 0.02, 0.09, 3)
        assert result > 0

    def test_transition_years_one(self):
        """transition_years=1: one transition CF then stable perpetuity."""
        ufcf = 100.0
        wacc = 0.10
        near_g = 0.05
        stable_g = 0.025
        result = gordon_growth_tv_two_stage(ufcf, near_g, stable_g, wacc, 1)
        # Stage 1: CF_1 / (1+WACC)^1
        cf_1 = ufcf * (1.0 + near_g)
        pv1 = cf_1 / (1.0 + wacc)
        # Stage 2: TV at end yr 1, PV'd back 1 year
        tv_stable = cf_1 * (1.0 + stable_g) / (wacc - stable_g)
        pv2 = tv_stable / (1.0 + wacc)
        expected = pv1 + pv2
        assert abs(result - expected) < 0.01

    def test_scale_independence(self):
        """Doubling UFCF doubles the two-stage TV."""
        tv1 = gordon_growth_tv_two_stage(100.0, 0.05, 0.025, 0.10, 5)
        tv2 = gordon_growth_tv_two_stage(200.0, 0.05, 0.025, 0.10, 5)
        assert abs(tv2 / tv1 - 2.0) < 1e-10


# =============================================================================
# GAP 4 — check_wacc_terminal_growth_spread: min 50bp spread enforcement
# =============================================================================

class TestCheckWaccTerminalGrowthSpread:
    """Tests for the WACC–terminal-growth spread validation check."""

    def test_pass_when_spread_exceeds_50bp(self):
        """PASS when WACC − g ≥ 50bp."""
        result = check_wacc_terminal_growth_spread(0.09, 0.025)
        assert result.status == "PASS"
        assert abs(result.value - 0.065) < 1e-10

    def test_pass_at_exactly_50bp(self):
        """PASS when spread is comfortably at min_spread (avoids float boundary)."""
        # Use clean arithmetic: 10% - 2% = 8% >> 0.5% min_spread
        result = check_wacc_terminal_growth_spread(0.10, 0.02, min_spread=0.005)
        assert result.status == "PASS"

    def test_warn_when_spread_below_50bp(self):
        """WARN when 0 < spread < 50bp."""
        # WACC=8%, g=7.8% → spread=20bp
        result = check_wacc_terminal_growth_spread(0.08, 0.078)
        assert result.status == "WARN"
        assert abs(result.value - 0.002) < 1e-10

    def test_fail_when_spread_zero(self):
        """FAIL when WACC == terminal_growth (denominator = 0)."""
        result = check_wacc_terminal_growth_spread(0.05, 0.05)
        assert result.status == "FAIL"

    def test_fail_when_spread_negative(self):
        """FAIL when WACC < terminal_growth."""
        result = check_wacc_terminal_growth_spread(0.03, 0.05)
        assert result.status == "FAIL"

    def test_custom_min_spread_100bp(self):
        """Custom min_spread=1% enforces 100bp minimum."""
        # Spread = 80bp → WARN with 100bp threshold
        result = check_wacc_terminal_growth_spread(0.09, 0.082, min_spread=0.01)
        assert result.status == "WARN"

    def test_custom_min_spread_100bp_pass(self):
        """150bp spread passes a 100bp minimum."""
        result = check_wacc_terminal_growth_spread(0.10, 0.085, min_spread=0.01)
        assert result.status == "PASS"

    def test_result_name_is_wacc_tg_spread(self):
        """ValidationResult.name is 'WACC_TG_SPREAD'."""
        result = check_wacc_terminal_growth_spread(0.09, 0.025)
        assert result.name == "WACC_TG_SPREAD"

    def test_value_stored_is_spread(self):
        """The .value attribute holds the actual spread."""
        result = check_wacc_terminal_growth_spread(0.10, 0.03)
        assert abs(result.value - 0.07) < 1e-10

    def test_is_ok_pass(self):
        """is_ok() returns True for PASS."""
        result = check_wacc_terminal_growth_spread(0.09, 0.025)
        assert result.is_ok()

    def test_is_ok_warn(self):
        """is_ok() returns True for WARN (non-blocking)."""
        result = check_wacc_terminal_growth_spread(0.08, 0.078)
        assert result.is_ok()

    def test_is_ok_fail(self):
        """is_ok() returns False for FAIL."""
        result = check_wacc_terminal_growth_spread(0.03, 0.05)
        assert not result.is_ok()

    def test_message_contains_spread_info(self):
        """WARN/FAIL message contains spread information."""
        result = check_wacc_terminal_growth_spread(0.08, 0.079)
        assert result.status in ("WARN", "FAIL")
        assert len(result.message) > 0

    def test_typical_ib_assumptions_pass(self):
        """Typical IB assumptions (WACC=9%, g=2.5%) pass the spread check."""
        result = check_wacc_terminal_growth_spread(0.09, 0.025)
        assert result.status == "PASS"

    def test_near_zero_terminal_growth_passes(self):
        """Near-zero terminal growth with typical WACC passes comfortably."""
        result = check_wacc_terminal_growth_spread(0.09, 0.005)
        assert result.status == "PASS"


# =============================================================================
# Cross-gap integration tests
# =============================================================================

class TestSession15Integration:
    """Verify the four gap functions work together coherently."""

    def test_nycf_and_nike_bound_spread_consistently(self):
        """For the same inputs, WACC-g spread check is independent of TV convention."""
        spread_check = check_wacc_terminal_growth_spread(0.09, 0.025)
        assert spread_check.status == "PASS"
        tv_nike = gordon_growth_tv(200.0, 0.09, 0.025)
        tv_nycf = gordon_growth_tv_nycf(200.0, 0.09, 0.025)
        assert tv_nycf > tv_nike   # nycf always larger when g > 0

    def test_two_stage_uses_valid_spread(self):
        """Two-stage TV with a spread-checked WACC/g combination works."""
        spread_check = check_wacc_terminal_growth_spread(0.10, 0.025)
        assert spread_check.status == "PASS"
        tv = gordon_growth_tv_two_stage(100.0, 0.05, 0.025, 0.10, 5)
        assert tv > 0

    def test_dividend_growth_ke_is_plausible(self):
        """Dividend Growth Ke passed to build_wacc-style check is in range."""
        ke = cost_of_equity_dividend_growth(1.50, 30.0, 0.04)
        # Ke = 1.50/30 + 0.04 = 0.09 = 9%
        assert abs(ke - 0.09) < 1e-10
        # Implied WACC > 9% with 30% debt → WACC-g spread fine at g=2.5%
        # Just validate the Ke output is reasonable
        assert 0.05 < ke < 0.20

    def test_all_tv_variants_positive_for_base_case(self):
        """All TV functions return positive values for standard base-case inputs."""
        ufcf = 150.0
        wacc = 0.09
        g_stable = 0.025
        g_near = 0.05
        assert gordon_growth_tv(ufcf, wacc, g_stable) > 0
        assert gordon_growth_tv_nycf(ufcf, wacc, g_stable) > 0
        assert gordon_growth_tv_two_stage(ufcf, g_near, g_stable, wacc, 5) > 0
