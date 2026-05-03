"""
tests/test_session13_gaps.py — Session 13 gap implementations from external source analysis.

Sources audited:
  - Macabacus: UFCF, WACC, Terminal Value, Comparable Companies, SOTP, APV
  - Corporate Finance Institute (CFI): DCF Model Training Guide
  - Investopedia: DCF fundamentals

New implementations validated:
  1. compute_implied_ebitda_multiple()  — TV cross-check (Macabacus)
  2. compute_tv_crosscheck()            — Full TV cross-check helper
  3. compute_sotp_valuation()           — SOTP with SegmentValuation
  4. SegmentValuation.segment_ev        — Property calculation
  5. allocate_overhead_by_revenue()     — Overhead allocation
  6. compute_ddm_gordon()               — GGM DDM single-stage
  7. compute_ddm_two_stage()            — Two-stage DDM
  8. compute_ddm_h_model()              — H-model DDM
  9. implied_ke_from_price()            — Reverse-solve cost of equity
  10. compute_payout_ratio()            — DPS / EPS
  11. compute_sustainable_growth_rate() — ROE × retention
  12. compute_nol_carryforward()        — NOL schedule
  13. apply_nol_to_tax()                — Single-year NOL application
  14. pv_nol_carryforward()             — PV of TLC for APV
  15. check_nol_utilisation()           — NOL flag
  16. compute_ebita()                   — EBIT + non-deductible goodwill amort
  17. compute_ufcf_from_ebita()         — Macabacus Exhibit A UFCF method
  18. sotp_valuation alias              — identity alias

Tests: 54 new tests (all must pass without scipy).
"""

from __future__ import annotations

import math
import pytest

# ── TV cross-check ─────────────────────────────────────────────────────────
from auto_valuation.forecast.terminal_value import (
    compute_implied_ebitda_multiple,
    compute_tv_crosscheck,
    implied_terminal_growth,
)

# ── SOTP ──────────────────────────────────────────────────────────────────
from auto_valuation.model.sotp import (
    SegmentValuation,
    SOTPResult,
    compute_sotp_valuation,
    allocate_overhead_by_revenue,
    sotp_valuation,
)

# ── DDM ────────────────────────────────────────────────────────────────────
from auto_valuation.model.ddm import (
    compute_ddm_gordon,
    compute_ddm_two_stage,
    compute_ddm_h_model,
    implied_ke_from_price,
    compute_payout_ratio,
    compute_sustainable_growth_rate,
)

# ── NOL ────────────────────────────────────────────────────────────────────
from auto_valuation.model.nol import (
    compute_nol_carryforward,
    apply_nol_to_tax,
    pv_nol_carryforward,
    check_nol_utilisation,
)

# ── Income statement ────────────────────────────────────────────────────────
from auto_valuation.model.income_statement import (
    compute_ebita,
    compute_ufcf_from_ebita,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. TV Cross-check
# ═══════════════════════════════════════════════════════════════════════════

class TestImpliedEbitdaMultiple:
    def test_basic(self):
        # TV = 1000, EBITDA = 100 → 10x
        result = compute_implied_ebitda_multiple(1000.0, 100.0)
        assert result == pytest.approx(10.0)

    def test_zero_ebitda(self):
        # Should return 0.0, not raise
        assert compute_implied_ebitda_multiple(500.0, 0.0) == 0.0

    def test_negative_ebitda(self):
        # Should return 0.0 for negative EBITDA
        assert compute_implied_ebitda_multiple(500.0, -50.0) == 0.0

    def test_proportional(self):
        # double TV → double multiple
        m1 = compute_implied_ebitda_multiple(800.0, 100.0)
        m2 = compute_implied_ebitda_multiple(1600.0, 100.0)
        assert m2 == pytest.approx(m1 * 2)

    def test_large_multiple(self):
        # High-growth company
        m = compute_implied_ebitda_multiple(5000.0, 200.0)
        assert m == pytest.approx(25.0)


class TestComputeTvCrosscheck:
    def setup_method(self):
        # TV from GGM: last_ufcf / (wacc - g)
        # Using NIKE convention: TV = UFCF / (WACC - g)
        self.wacc = 0.10
        self.g = 0.03
        self.ufcf = 100.0
        self.tv = self.ufcf / (self.wacc - self.g)   # ≈ 1428.57
        self.ebitda_n = 150.0

    def test_returns_dict(self):
        result = compute_tv_crosscheck(self.tv, self.ufcf, self.wacc, self.ebitda_n)
        assert "tv" in result
        assert "implied_g" in result
        assert "implied_multiple" in result
        assert "warnings" in result

    def test_implied_g_matches_input(self):
        result = compute_tv_crosscheck(self.tv, self.ufcf, self.wacc, self.ebitda_n)
        # For NIKE TV = UFCF / (WACC - g): implied_g = WACC - UFCF/TV
        assert result["implied_g"] == pytest.approx(self.g, abs=1e-6)

    def test_implied_multiple_correct(self):
        result = compute_tv_crosscheck(self.tv, self.ufcf, self.wacc, self.ebitda_n)
        expected_mult = self.tv / self.ebitda_n
        assert result["implied_multiple"] == pytest.approx(expected_mult)

    def test_delta_multiple_computed(self):
        result = compute_tv_crosscheck(self.tv, self.ufcf, self.wacc, self.ebitda_n,
                                       ev_ebitda_multiple_comps=8.5)
        assert result["delta_multiple"] is not None
        expected = (self.tv / self.ebitda_n) - 8.5
        assert result["delta_multiple"] == pytest.approx(expected, abs=0.01)

    def test_no_comps_delta_is_none(self):
        result = compute_tv_crosscheck(self.tv, self.ufcf, self.wacc, self.ebitda_n)
        assert result["delta_multiple"] is None

    def test_high_implied_g_warning(self):
        # TV based on 6% growth (above 5% threshold)
        tv_high = 100.0 / (0.10 - 0.06)  # = 2500
        result = compute_tv_crosscheck(tv_high, 100.0, 0.10, 200.0)
        assert any("5%" in w or "economy" in w for w in result["warnings"])

    def test_negative_implied_g_warning(self):
        # TV so low that implied g is negative
        tv_low = 50.0  # WACC - UFCF/TV = 0.10 - 100/50 = -1.9 (very negative)
        result = compute_tv_crosscheck(tv_low, 100.0, 0.10, 50.0)
        assert any("negative" in w.lower() for w in result["warnings"])


# ═══════════════════════════════════════════════════════════════════════════
# 2. SOTP
# ═══════════════════════════════════════════════════════════════════════════

class TestSegmentValuation:
    def test_ebitda_based(self):
        seg = SegmentValuation(name="Cloud", metric_value=100.0, multiple=12.0, metric_type="ebitda")
        assert seg.segment_ev == pytest.approx(1200.0)

    def test_revenue_based(self):
        seg = SegmentValuation(name="SaaS", metric_value=500.0, multiple=5.0, metric_type="revenue")
        assert seg.segment_ev == pytest.approx(2500.0)

    def test_dcf_based(self):
        seg = SegmentValuation(name="Legacy", metric_value=0.0, multiple=0.0, metric_type="dcf", ev_dcf=800.0)
        assert seg.segment_ev == pytest.approx(800.0)

    def test_minority_deduction(self):
        # 20% minority → EV × 0.80
        seg = SegmentValuation(name="JV", metric_value=200.0, multiple=8.0, minority_pct=0.20)
        assert seg.segment_ev == pytest.approx(200.0 * 8.0 * 0.80)

    def test_zero_metric(self):
        seg = SegmentValuation(name="Zero", metric_value=0.0, multiple=10.0)
        assert seg.segment_ev == 0.0

    def test_to_dict_has_segment_ev(self):
        seg = SegmentValuation(name="A", metric_value=100.0, multiple=10.0)
        d = seg.to_dict()
        assert d["segment_ev"] == pytest.approx(1000.0)


class TestComputeSotpValuation:
    def setup_method(self):
        self.segs = [
            SegmentValuation("Cloud", metric_value=100.0, multiple=12.0),
            SegmentValuation("Legacy", metric_value=200.0, multiple=6.0),
        ]
        # Total EV = 1200 + 1200 = 2400
        self.net_debt = 400.0
        self.shares = 100.0

    def test_total_ev(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares)
        assert r.total_segment_ev_mm == pytest.approx(2400.0)

    def test_equity_value(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares)
        assert r.equity_value_mm == pytest.approx(2400.0 - 400.0)

    def test_equity_per_share(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares)
        assert r.equity_per_share == pytest.approx((2400.0 - 400.0) / 100.0)

    def test_overhead_deducted(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares,
                                   corporate_overhead_mm=50.0)
        assert r.total_ev_mm == pytest.approx(2400.0 - 50.0)

    def test_non_op_assets_added(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares,
                                   non_operating_assets_mm=100.0)
        assert r.total_ev_mm == pytest.approx(2400.0 + 100.0)

    def test_premium_discount(self):
        # equity_per_share ≈ 20; current_price=16 → +25% premium
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares,
                                   current_price=16.0)
        assert r.premium_discount_pct == pytest.approx(0.25, abs=0.01)

    def test_empty_segments_returns_zero(self):
        r = compute_sotp_valuation([], net_debt_mm=0.0, diluted_shares_mm=100.0)
        assert r.equity_per_share == 0.0

    def test_negative_equity_warning(self):
        # net_debt > total_ev
        r = compute_sotp_valuation(self.segs, net_debt_mm=9999.0, diluted_shares_mm=100.0)
        assert r.equity_value_mm < 0
        assert any("negative" in w.lower() or "distressed" in w.lower() for w in r.warnings)

    def test_alias_identity(self):
        assert sotp_valuation is compute_sotp_valuation

    def test_returns_sotp_result(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares)
        assert isinstance(r, SOTPResult)

    def test_to_dict(self):
        r = compute_sotp_valuation(self.segs, self.net_debt, self.shares)
        d = r.to_dict()
        assert "equity_per_share" in d
        assert "segment_evs" in d


class TestAllocateOverhead:
    def test_proportional_allocation(self):
        segs = [SegmentValuation("A", 100.0, 10.0), SegmentValuation("B", 200.0, 10.0)]
        revs = [300.0, 700.0]  # 30% / 70%
        allocs = allocate_overhead_by_revenue(segs, 100.0, revs)
        assert allocs[0] == pytest.approx(30.0)
        assert allocs[1] == pytest.approx(70.0)

    def test_equal_revenue(self):
        segs = [SegmentValuation("A", 100.0, 10.0), SegmentValuation("B", 100.0, 10.0)]
        allocs = allocate_overhead_by_revenue(segs, 60.0, [100.0, 100.0])
        assert allocs[0] == pytest.approx(30.0)
        assert allocs[1] == pytest.approx(30.0)

    def test_zero_revenues_even_split(self):
        segs = [SegmentValuation("A", 100.0, 10.0), SegmentValuation("B", 100.0, 10.0)]
        allocs = allocate_overhead_by_revenue(segs, 60.0, [0.0, 0.0])
        assert sum(allocs) == pytest.approx(60.0)


# ═══════════════════════════════════════════════════════════════════════════
# 3. DDM
# ═══════════════════════════════════════════════════════════════════════════

class TestDdmGordon:
    def test_basic(self):
        # P = DPS_1 / (ke - g) = 3.0 / (0.10 - 0.03) ≈ 42.86
        p = compute_ddm_gordon(3.0, 0.10, 0.03)
        assert p == pytest.approx(3.0 / 0.07, rel=1e-6)

    def test_ke_equals_g_raises(self):
        with pytest.raises(ValueError):
            compute_ddm_gordon(3.0, 0.05, 0.05)

    def test_ke_less_than_g_raises(self):
        with pytest.raises(ValueError):
            compute_ddm_gordon(3.0, 0.04, 0.05)

    def test_zero_dps_returns_zero(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p = compute_ddm_gordon(0.0, 0.10, 0.03)
        assert p == 0.0

    def test_higher_ke_lower_price(self):
        p1 = compute_ddm_gordon(3.0, 0.10, 0.03)
        p2 = compute_ddm_gordon(3.0, 0.12, 0.03)
        assert p1 > p2


class TestDdmTwoStage:
    def test_basic(self):
        # Simple sanity: price must be positive
        p = compute_ddm_two_stage(2.0, 0.15, 0.03, 0.12, 0.10, near_years=5)
        assert p > 0

    def test_stable_growth_equals_ke_raises(self):
        with pytest.raises(ValueError):
            compute_ddm_two_stage(2.0, 0.15, 0.10, 0.12, 0.10, near_years=5)

    def test_higher_near_growth_higher_price(self):
        p_high = compute_ddm_two_stage(2.0, 0.20, 0.03, 0.12, 0.10, near_years=5)
        p_low  = compute_ddm_two_stage(2.0, 0.05, 0.03, 0.12, 0.10, near_years=5)
        assert p_high > p_low

    def test_convergence_to_gordon_single_period(self):
        # With 1 near_year and near_growth = stable_growth, should approximate GGM
        # Not exact because two different ke values; just check result is finite and positive
        p = compute_ddm_two_stage(2.0, 0.05, 0.05, 0.10, 0.10, near_years=1)
        assert p > 0 and math.isfinite(p)

    def test_invalid_near_years(self):
        with pytest.raises(ValueError):
            compute_ddm_two_stage(2.0, 0.10, 0.03, 0.12, 0.10, near_years=0)


class TestDdmHModel:
    def test_basic(self):
        # P = DPS_0 × (1 + g_stable)/(ke - g_stable)  + DPS_0 × H × (g_h - g_s)/(ke - g_s)
        p = compute_ddm_h_model(2.0, 0.20, 0.04, 0.10, half_life=5.0)
        spread = 0.10 - 0.04
        expected = 2.0 * (1 + 0.04) / spread + 2.0 * 5.0 * (0.20 - 0.04) / spread
        assert p == pytest.approx(expected, rel=1e-6)

    def test_zero_premium_equals_gordon(self):
        # If g_high = g_stable → H-model = Gordon Growth
        dps0 = 3.0; g = 0.03; ke = 0.10
        p_h = compute_ddm_h_model(dps0, g, g, ke, half_life=5.0)
        p_g = compute_ddm_gordon(dps0 * (1 + g), ke, g)
        assert p_h == pytest.approx(p_g, rel=1e-6)

    def test_ke_le_g_raises(self):
        with pytest.raises(ValueError):
            compute_ddm_h_model(2.0, 0.20, 0.10, 0.08, half_life=5.0)


class TestImpliedKeFromPrice:
    def test_basic(self):
        # ke = DPS_1 / P + g = 3.0 / 42.857 + 0.03 ≈ 0.10
        ke = implied_ke_from_price(current_price=42.857, dps_next=3.0, terminal_growth=0.03)
        assert ke == pytest.approx(0.10, abs=0.001)

    def test_zero_price_raises(self):
        with pytest.raises(ValueError):
            implied_ke_from_price(0.0, 3.0, 0.03)

    def test_higher_price_lower_ke(self):
        ke1 = implied_ke_from_price(40.0, 3.0, 0.03)
        ke2 = implied_ke_from_price(60.0, 3.0, 0.03)
        assert ke1 > ke2


class TestPayoutAndSustainableGrowth:
    def test_payout_ratio(self):
        assert compute_payout_ratio(2.0, 4.0) == pytest.approx(0.50)

    def test_payout_zero_eps(self):
        assert compute_payout_ratio(1.0, 0.0) == 0.0

    def test_sustainable_growth(self):
        # g = ROE × retention = 0.15 × (1 - 0.40) = 0.09
        assert compute_sustainable_growth_rate(0.15, 0.40) == pytest.approx(0.09)

    def test_full_payout_zero_growth(self):
        assert compute_sustainable_growth_rate(0.15, 1.0) == 0.0

    def test_zero_roe(self):
        assert compute_sustainable_growth_rate(0.0, 0.40) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 4. NOL / Tax Loss Carryforward
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeNolCarryforward:
    def test_basic_utilisation(self):
        # 100m NOL, two profit years: 60 then 80
        sched = compute_nol_carryforward(100.0, [60.0, 80.0], utilisation_cap_pct=1.0)
        assert sched[0]["nol_used"] == pytest.approx(60.0)
        assert sched[0]["nol_closing"] == pytest.approx(40.0)
        assert sched[1]["nol_used"] == pytest.approx(40.0)  # only 40 left
        assert sched[1]["nol_closing"] == pytest.approx(0.0)

    def test_80_pct_cap_default(self):
        # With TCJA 80% cap: only 80% of taxable income can be shielded
        sched = compute_nol_carryforward(200.0, [100.0], utilisation_cap_pct=0.80)
        assert sched[0]["nol_used"] == pytest.approx(80.0)  # min(200, 100×0.8)
        assert sched[0]["effective_taxable_income"] == pytest.approx(20.0)

    def test_loss_year_increases_nol(self):
        sched = compute_nol_carryforward(50.0, [-30.0, 100.0], utilisation_cap_pct=1.0)
        # Year 1 loss: NOL grows from 50 to 80
        assert sched[0]["nol_closing"] == pytest.approx(80.0)
        assert sched[0]["nol_used"] == 0.0

    def test_section_382_cap(self):
        # Annual cap = 20m; income = 100m; NOL = 200m → only 20m used
        sched = compute_nol_carryforward(200.0, [100.0], utilisation_cap_pct=1.0,
                                         section_382_annual_cap_mm=20.0)
        assert sched[0]["nol_used"] == pytest.approx(20.0)

    def test_zero_nol(self):
        sched = compute_nol_carryforward(0.0, [100.0])
        assert sched[0]["nol_used"] == 0.0

    def test_returns_correct_keys(self):
        sched = compute_nol_carryforward(100.0, [50.0])
        expected_keys = {"year", "taxable_income", "nol_opening", "nol_used",
                         "effective_taxable_income", "nol_closing"}
        assert expected_keys.issubset(sched[0].keys())


class TestApplyNolToTax:
    def test_basic(self):
        taxes, nol_used, nol_rem = apply_nol_to_tax(100.0, 80.0, 0.21, 1.0)
        # max usable = 100 × 1.0 = 100; min(80, 100) = 80
        # effective_ti = 20; taxes = 20 × 0.21 = 4.2
        assert nol_used == pytest.approx(80.0)
        assert nol_rem == pytest.approx(0.0)
        assert taxes == pytest.approx(20.0 * 0.21)

    def test_tcja_cap(self):
        taxes, nol_used, nol_rem = apply_nol_to_tax(100.0, 200.0, 0.21, 0.80)
        # usable = min(200, 80) = 80
        assert nol_used == pytest.approx(80.0)
        assert taxes == pytest.approx(20.0 * 0.21)

    def test_zero_income_no_utilisation(self):
        taxes, nol_used, nol_rem = apply_nol_to_tax(0.0, 200.0, 0.21)
        assert nol_used == 0.0
        assert taxes == 0.0

    def test_no_nol(self):
        taxes, nol_used, nol_rem = apply_nol_to_tax(100.0, 0.0, 0.21)
        assert nol_used == 0.0
        assert taxes == pytest.approx(21.0)


class TestPvNolCarryforward:
    def test_positive_pv(self):
        sched = compute_nol_carryforward(100.0, [50.0, 80.0], utilisation_cap_pct=1.0)
        pv = pv_nol_carryforward(sched, tax_rate=0.21, ku=0.10)
        assert pv > 0

    def test_zero_nol_zero_pv(self):
        sched = compute_nol_carryforward(0.0, [100.0])
        pv = pv_nol_carryforward(sched, 0.21, 0.10)
        assert pv == 0.0

    def test_higher_discount_lower_pv(self):
        sched = compute_nol_carryforward(200.0, [100.0, 100.0], utilisation_cap_pct=1.0)
        pv_low = pv_nol_carryforward(sched, 0.21, 0.05)
        pv_high = pv_nol_carryforward(sched, 0.21, 0.15)
        assert pv_low > pv_high


class TestCheckNolUtilisation:
    def test_fully_utilised(self):
        sched = compute_nol_carryforward(100.0, [100.0], utilisation_cap_pct=1.0)
        result = check_nol_utilisation(sched)
        assert result["fully_utilised"] is True
        assert result["status"] == "PASS"

    def test_not_utilised_triggers_warn(self):
        # Only 10m income with 200m NOL
        sched = compute_nol_carryforward(200.0, [10.0], utilisation_cap_pct=1.0)
        result = check_nol_utilisation(sched)
        assert result["status"] == "WARN"
        assert result["remaining_nol"] > 0

    def test_empty_schedule(self):
        result = check_nol_utilisation([])
        assert result["status"] == "PASS"  # no schedule = no problem

    def test_pct_utilised_accuracy(self):
        sched = compute_nol_carryforward(100.0, [50.0], utilisation_cap_pct=1.0)
        result = check_nol_utilisation(sched)
        assert result["pct_utilised"] == pytest.approx(0.50)


# ═══════════════════════════════════════════════════════════════════════════
# 5. EBITA / Macabacus Exhibit A UFCF
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeEbita:
    def test_zero_goodwill(self):
        # No goodwill amort → EBITA = EBIT
        assert compute_ebita(500.0, 0.0) == pytest.approx(500.0)

    def test_positive_goodwill(self):
        assert compute_ebita(500.0, 50.0) == pytest.approx(550.0)

    def test_negative_goodwill_clamped_to_zero(self):
        # Negative goodwill doesn't reduce EBITA
        assert compute_ebita(500.0, -20.0) == pytest.approx(500.0)

    def test_default_arg(self):
        assert compute_ebita(300.0) == pytest.approx(300.0)


class TestComputeUfcfFromEbita:
    def test_no_goodwill_matches_standard(self):
        """
        With goodwill_amort=0, compute_ufcf_from_ebita should give same result
        as standard compute_ufcf (EBIT×(1-t) + DA + SBC - capex - ΔNOWC).
        """
        from auto_valuation.model.income_statement import compute_ufcf
        ebit = 200.0; tr = 0.21; da = 30.0; capex = 40.0; dnowc = 10.0; sbc = 5.0
        standard = compute_ufcf(ebit, tr, da, capex, dnowc, sbc)
        ebita_m  = compute_ufcf_from_ebita(ebit, tr, da, capex, dnowc, 0.0, sbc)
        assert ebita_m == pytest.approx(standard)

    def test_goodwill_correct_tax_base(self):
        """
        Non-deductible goodwill amort is added back to EBIT to compute EBITA,
        but taxes are levied on EBITA (higher tax base).

        The naive approach treats goodwill amort like deductible DA:
            naive = EBIT × (1-t) + (DA + goodwill) - capex - ΔNOWC   [WRONG]
        The EBITA method is correct:
            ebita = EBITA × (1-t) + DA - capex - ΔNOWC               [RIGHT]

        Since non-deductible goodwill generates tax liability not reflected in
        the naive method, UFCF_from_ebita < naive_ufcf.
        """
        from auto_valuation.model.income_statement import compute_ufcf
        ebit = 200.0; tr = 0.21; da = 30.0; capex = 40.0; dnowc = 10.0; goodwill = 50.0
        # Naive (wrong): passes da+goodwill as if both are deductible
        naive = compute_ufcf(ebit, tr, da + goodwill, capex, dnowc)
        # Correct EBITA method: goodwill increases tax base
        correct = compute_ufcf_from_ebita(ebit, tr, da, capex, dnowc, goodwill)
        # Correct taxes (on EBITA) are higher → UFCF is lower
        assert correct < naive

    def test_formula_manual(self):
        ebit = 100.0; tr = 0.25; da = 20.0; capex = 15.0; dnowc = 5.0; gw = 10.0
        # EBITA = 110; Unlevered NI = 110 × 0.75 = 82.5; UFCF = 82.5 + 20 - 15 - 5 = 82.5
        expected = 110.0 * (1 - 0.25) + 20.0 - 15.0 - 5.0
        result = compute_ufcf_from_ebita(ebit, tr, da, capex, dnowc, gw)
        assert result == pytest.approx(expected)

    def test_positive_ufcf(self):
        result = compute_ufcf_from_ebita(500.0, 0.21, 80.0, 60.0, 20.0, 0.0, 10.0)
        assert result > 0
