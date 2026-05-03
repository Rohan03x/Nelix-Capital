"""
tests/test_session10_gaps.py

Unit tests for all Session 10 gap implementations:
  - rollforward_apic / rollforward_aoci
  - compute_predicted_beta_blume
  - compute_wacc_with_preferred
  - validate_wacc_currency_consistency / compute_cross_currency_wacc
  - apply_wacc_step_down
  - compute_reinvestment_rate
  - compute_apv
  - exclude_nm_multiples / compute_peer_ev
  - export_xlsx_to_pdf (smoke — no LibreOffice required)
  - webhook_notify (smoke — no real URL required)
"""

from __future__ import annotations

import math
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# APIC / AOCI rollforward  (Part 14)
# ─────────────────────────────────────────────────────────────────────────────

class TestRollforwardApic:
    from auto_valuation.model.balance_sheet import rollforward_apic

    def test_nike_style_no_treasury(self):
        # NIKE: buybacks REDUCE APIC
        from auto_valuation.model.balance_sheet import rollforward_apic
        result = rollforward_apic(
            opening_apic=5_000.0,
            sbc_expense=300.0,
            equity_issuances=667.0,
            buybacks=2_000.0,
            uses_treasury_stock=False,  # NIKE style
        )
        assert result == pytest.approx(5_000.0 + 300.0 + 667.0 - 2_000.0)

    def test_treasury_stock_style_buybacks_not_reduce_apic(self):
        from auto_valuation.model.balance_sheet import rollforward_apic
        result = rollforward_apic(
            opening_apic=5_000.0,
            sbc_expense=300.0,
            equity_issuances=667.0,
            buybacks=2_000.0,
            uses_treasury_stock=True,  # treasury stock method
        )
        # buybacks do NOT reduce APIC
        assert result == pytest.approx(5_000.0 + 300.0 + 667.0)

    def test_no_activity(self):
        from auto_valuation.model.balance_sheet import rollforward_apic
        assert rollforward_apic(1_000.0) == pytest.approx(1_000.0)

    def test_negative_opening_apic(self):
        from auto_valuation.model.balance_sheet import rollforward_apic
        result = rollforward_apic(-200.0, sbc_expense=50.0)
        assert result == pytest.approx(-150.0)


class TestRollforwardAoci:
    def test_basic_rollforward(self):
        from auto_valuation.model.balance_sheet import rollforward_aoci
        r = rollforward_aoci(
            opening_aoci=-100.0,
            fx_translation_gain_loss=20.0,
            pension_oci_adjustment=-10.0,
        )
        assert r["total_oci"] == pytest.approx(10.0)
        assert r["closing_aoci"] == pytest.approx(-90.0)

    def test_all_components(self):
        from auto_valuation.model.balance_sheet import rollforward_aoci
        r = rollforward_aoci(
            opening_aoci=0.0,
            fx_translation_gain_loss=5.0,
            unrealized_securities_gain_loss=3.0,
            pension_oci_adjustment=-8.0,
            cash_flow_hedge_gain_loss=2.0,
            other_oci=1.0,
        )
        assert r["total_oci"] == pytest.approx(3.0)
        assert r["closing_aoci"] == pytest.approx(3.0)

    def test_keys_present(self):
        from auto_valuation.model.balance_sheet import rollforward_aoci
        r = rollforward_aoci(0.0)
        assert "opening_aoci" in r
        assert "total_oci" in r
        assert "closing_aoci" in r

    def test_zero_oci(self):
        from auto_valuation.model.balance_sheet import rollforward_aoci
        r = rollforward_aoci(opening_aoci=-500.0)
        assert r["closing_aoci"] == pytest.approx(-500.0)


# ─────────────────────────────────────────────────────────────────────────────
# Blume beta  (Part 7 / Macabacus)
# ─────────────────────────────────────────────────────────────────────────────

class TestBlumeAdjustment:
    def test_market_beta_unchanged(self):
        from auto_valuation.assumptions.wacc import compute_predicted_beta_blume
        # β_raw = 1.0 → β_adj = 1.0 (market beta is already the mean)
        assert compute_predicted_beta_blume(1.0) == pytest.approx(1.0)

    def test_high_beta_pulled_down(self):
        from auto_valuation.assumptions.wacc import compute_predicted_beta_blume
        result = compute_predicted_beta_blume(2.0)
        # 0.67 × 2.0 + 0.33 × 1.0 = 1.34 + 0.33 = 1.67
        assert result == pytest.approx(1.67, rel=1e-6)

    def test_low_beta_pulled_up(self):
        from auto_valuation.assumptions.wacc import compute_predicted_beta_blume
        result = compute_predicted_beta_blume(0.5)
        # 0.67 × 0.5 + 0.33 × 1.0 = 0.335 + 0.33 = 0.665
        assert result == pytest.approx(0.665, rel=1e-6)

    def test_zero_beta(self):
        from auto_valuation.assumptions.wacc import compute_predicted_beta_blume
        result = compute_predicted_beta_blume(0.0)
        assert result == pytest.approx(0.33, rel=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# 4-component WACC with preferred  (Part 75)
# ─────────────────────────────────────────────────────────────────────────────

class TestWaccWithPreferred:
    def test_basic_4component(self):
        from auto_valuation.assumptions.wacc import compute_wacc_with_preferred
        # Simple equal-weight scenario for sanity check
        wacc, weights = compute_wacc_with_preferred(
            ke=0.10,
            kd_after_tax=0.04,
            k_preferred=0.06,
            k_lease_at=0.03,
            equity_mv_m=400.0,
            debt_mv_m=200.0,
            preferred_mv_m=200.0,
            lease_liability_m=200.0,
        )
        # V = 1000, weights = 0.4 / 0.2 / 0.2 / 0.2
        expected = 0.10 * 0.4 + 0.04 * 0.2 + 0.06 * 0.2 + 0.03 * 0.2
        assert wacc == pytest.approx(expected)

    def test_weights_sum_to_one(self):
        from auto_valuation.assumptions.wacc import compute_wacc_with_preferred
        _, w = compute_wacc_with_preferred(
            ke=0.09, kd_after_tax=0.04, k_preferred=0.06, k_lease_at=0.03,
            equity_mv_m=500.0, debt_mv_m=300.0, preferred_mv_m=100.0,
            lease_liability_m=100.0,
        )
        total_pct = w["E_pct"] + w["D_pct"] + w["P_pct"] + w["L_pct"]
        assert total_pct == pytest.approx(1.0)

    def test_zero_capital_raises(self):
        from auto_valuation.assumptions.wacc import compute_wacc_with_preferred
        with pytest.raises(ValueError):
            compute_wacc_with_preferred(0.1, 0.04, 0.06, 0.03, 0.0, 0.0, 0.0, 0.0)

    def test_no_preferred_matches_3component(self):
        from auto_valuation.assumptions.wacc import compute_wacc_with_preferred, compute_wacc_with_leases
        # With preferred = 0, 4-component should match 3-component
        wacc4, _ = compute_wacc_with_preferred(
            ke=0.09, kd_after_tax=0.04, k_preferred=0.06, k_lease_at=0.03,
            equity_mv_m=600.0, debt_mv_m=300.0, preferred_mv_m=0.0, lease_liability_m=100.0,
        )
        wacc3, _ = compute_wacc_with_leases(
            ke=0.09, kd_after_tax=0.04, k_lease=0.03,
            equity_mv_m=600.0, debt_mv_m=300.0, lease_liability_m=100.0,
            tax_rate=0.21, lease_tax_deductible=False,
        )
        assert wacc4 == pytest.approx(wacc3, rel=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-currency WACC  (Part 46.3)
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossCurrencyWacc:
    def test_matching_currencies_ok(self):
        from auto_valuation.assumptions.wacc import validate_wacc_currency_consistency
        # Should NOT raise
        validate_wacc_currency_consistency("USD", "USD", "USD")

    def test_currency_mismatch_raises(self):
        from auto_valuation.assumptions.wacc import validate_wacc_currency_consistency
        with pytest.raises(ValueError, match="currency mismatch"):
            validate_wacc_currency_consistency("USD", "USD", "EUR")

    def test_erp_mismatch_raises(self):
        from auto_valuation.assumptions.wacc import validate_wacc_currency_consistency
        with pytest.raises(ValueError):
            validate_wacc_currency_consistency("EUR", "USD", "EUR")

    def test_compute_cross_currency_wacc_values(self):
        from auto_valuation.assumptions.wacc import compute_cross_currency_wacc
        wacc = compute_cross_currency_wacc(
            ke=0.08,
            kd_after_tax=0.03,
            equity_weight=0.7,
            debt_weight=0.3,
            rf_currency="EUR",
            erp_currency="EUR",
            company_currency="EUR",
        )
        assert wacc == pytest.approx(0.08 * 0.7 + 0.03 * 0.3)

    def test_cross_currency_raises_on_mismatch(self):
        from auto_valuation.assumptions.wacc import compute_cross_currency_wacc
        with pytest.raises(ValueError):
            compute_cross_currency_wacc(
                ke=0.08, kd_after_tax=0.03, equity_weight=0.7, debt_weight=0.3,
                rf_currency="USD", erp_currency="USD", company_currency="EUR",
            )


# ─────────────────────────────────────────────────────────────────────────────
# WACC step-down / mean reversion  (Part 48.1)
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyWaccStepDown:
    def test_returns_correct_length(self):
        from auto_valuation.assumptions.wacc import apply_wacc_step_down
        result = apply_wacc_step_down(0.15, 0.10, 7, 3)
        assert len(result) == 7

    def test_final_year_at_target(self):
        from auto_valuation.assumptions.wacc import apply_wacc_step_down
        result = apply_wacc_step_down(0.15, 0.10, 5, 3)
        assert result[-1] == pytest.approx(0.10)

    def test_monotonic_decline(self):
        from auto_valuation.assumptions.wacc import apply_wacc_step_down
        result = apply_wacc_step_down(0.15, 0.10, 5, 3)
        for i in range(len(result) - 1):
            assert result[i] >= result[i + 1]

    def test_constant_when_base_equals_target(self):
        from auto_valuation.assumptions.wacc import apply_wacc_step_down
        result = apply_wacc_step_down(0.10, 0.10, 5, 3)
        assert all(w == pytest.approx(0.10) for w in result)

    def test_zero_transition_snaps_to_target(self):
        from auto_valuation.assumptions.wacc import apply_wacc_step_down
        result = apply_wacc_step_down(0.15, 0.10, 5, 0)
        # With transition_years=0 all divide by zero? Let's check the logic handles it
        # Year 1: progress=1/0 → ZeroDivisionError or yr>0 so all = target
        # The implementation: if yr <= 0: never triggers → all target
        assert all(w == pytest.approx(0.10) for w in result)


# ─────────────────────────────────────────────────────────────────────────────
# Reinvestment rate  (Part 52.1)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeReinvestmentRate:
    def test_basic_positive(self):
        from auto_valuation.forecast.terminal_value import compute_reinvestment_rate
        # capex=100, da=80 → net_capex=20; delta_nowc=10; reinvestment=30; nopat=200
        rr = compute_reinvestment_rate(nopat=200.0, capex=100.0, da=80.0, delta_nowc=10.0)
        assert rr == pytest.approx(0.15)

    def test_net_capex_floor_at_zero(self):
        from auto_valuation.forecast.terminal_value import compute_reinvestment_rate
        # capex < da → net_capex is floored at 0
        rr = compute_reinvestment_rate(nopat=100.0, capex=50.0, da=80.0, delta_nowc=5.0)
        # net_capex = max(50-80, 0) = 0; reinvestment = 5; RR = 5/100 = 0.05
        assert rr == pytest.approx(0.05)

    def test_zero_nopat_returns_zero(self):
        from auto_valuation.forecast.terminal_value import compute_reinvestment_rate
        rr = compute_reinvestment_rate(nopat=0.0, capex=100.0, da=80.0, delta_nowc=10.0)
        assert rr == 0.0

    def test_negative_nopat_returns_zero(self):
        from auto_valuation.forecast.terminal_value import compute_reinvestment_rate
        rr = compute_reinvestment_rate(nopat=-50.0, capex=100.0, da=80.0, delta_nowc=10.0)
        assert rr == 0.0

    def test_roic_growth_consistency(self):
        from auto_valuation.forecast.terminal_value import compute_reinvestment_rate
        # ROIC=15%, target g=2.5% → expected RR = 2.5/15 = 0.1667
        rr = compute_reinvestment_rate(nopat=150.0, capex=50.0, da=25.0, delta_nowc=0.0)
        # net_capex = 25; reinvestment = 25; RR = 25/150 = 0.1667
        assert rr == pytest.approx(25.0 / 150.0)


# ─────────────────────────────────────────────────────────────────────────────
# APV  (Part 17.2)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeApv:
    def test_zero_debt_apv_equals_unlevered(self):
        from auto_valuation.model.itax_shield import compute_apv
        ufcfs = [100.0, 100.0, 100.0]
        ibd   = [0.0, 0.0, 0.0, 0.0]  # no debt
        result = compute_apv(ufcfs, ku=0.10, ibd_schedule=ibd,
                             kd_pretax=0.05, tax_rate=0.21,
                             terminal_growth=0.025)
        # With zero debt, PV(ITS) = 0
        assert result["pv_its"] == pytest.approx(0.0)
        assert result["apv"] == result["ev_unlevered"]

    def test_positive_debt_increases_apv(self):
        from auto_valuation.model.itax_shield import compute_apv
        ufcfs = [100.0, 100.0, 100.0]
        ibd_no_debt = [0.0, 0.0, 0.0, 0.0]
        ibd_with_debt = [500.0, 500.0, 500.0, 500.0]
        r_no = compute_apv(ufcfs, ku=0.10, ibd_schedule=ibd_no_debt,
                           kd_pretax=0.05, tax_rate=0.21, terminal_growth=0.025)
        r_with = compute_apv(ufcfs, ku=0.10, ibd_schedule=ibd_with_debt,
                             kd_pretax=0.05, tax_rate=0.21, terminal_growth=0.025)
        # APV with debt > APV without debt (ITS is valuable)
        assert r_with["apv"] > r_no["apv"]

    def test_result_keys_present(self):
        from auto_valuation.model.itax_shield import compute_apv
        result = compute_apv(
            [50.0, 60.0], ku=0.09, ibd_schedule=[200.0, 180.0, 160.0],
            kd_pretax=0.05, tax_rate=0.21, terminal_growth=0.025,
        )
        assert "ev_unlevered" in result
        assert "pv_its" in result
        assert "apv" in result
        assert "its_schedule" in result

    def test_apv_equals_ev_unlevered_plus_pv_its(self):
        from auto_valuation.model.itax_shield import compute_apv
        result = compute_apv(
            [80.0, 90.0, 100.0], ku=0.10, ibd_schedule=[400.0, 350.0, 300.0, 250.0],
            kd_pretax=0.05, tax_rate=0.25, terminal_growth=0.025,
        )
        assert result["apv"] == pytest.approx(
            result["ev_unlevered"] + result["pv_its"], rel=1e-6
        )

    def test_empty_ufcfs(self):
        from auto_valuation.model.itax_shield import compute_apv
        result = compute_apv([], ku=0.10, ibd_schedule=[100.0],
                             kd_pretax=0.05, tax_rate=0.21, terminal_growth=0.025)
        assert result["ev_unlevered"] == pytest.approx(0.0)
        assert result["apv"] == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# exclude_nm_multiples  (Part 21.2)
# ─────────────────────────────────────────────────────────────────────────────

class TestExcludeNmMultiples:
    def test_removes_none(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        result = exclude_nm_multiples([10.0, None, 12.0, None, 15.0])
        assert None not in result

    def test_removes_negative(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        result = exclude_nm_multiples([10.0, -5.0, 12.0, 0.0, 15.0])
        assert all(v > 0 for v in result)

    def test_removes_extreme_outliers(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        # 10, 11, 12, 13, 14 are normal; 200 is extreme outlier
        result = exclude_nm_multiples([10.0, 11.0, 12.0, 13.0, 14.0, 200.0])
        assert 200.0 not in result

    def test_normal_values_preserved(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        vals = [8.0, 10.0, 12.0, 14.0, 16.0]
        result = exclude_nm_multiples(vals)
        assert len(result) == 5

    def test_all_none_returns_empty(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        result = exclude_nm_multiples([None, None, None])
        assert result == []

    def test_single_value(self):
        from auto_valuation.data.comps import exclude_nm_multiples
        result = exclude_nm_multiples([10.0])
        assert result == [10.0]


# ─────────────────────────────────────────────────────────────────────────────
# compute_peer_ev  (Part 5.2)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputePeerEv:
    def test_basic_ev(self):
        from auto_valuation.data.comps import compute_peer_ev
        # Simple: mktcap=1000, debt=200, cash=50
        ev = compute_peer_ev(
            market_cap_mm=1_000.0,
            ibd_mm=200.0,
            cash_mm=50.0,
        )
        assert ev == pytest.approx(1_150.0)

    def test_with_nci_and_preferred(self):
        from auto_valuation.data.comps import compute_peer_ev
        ev = compute_peer_ev(
            market_cap_mm=1_000.0,
            ibd_mm=200.0,
            cash_mm=100.0,
            st_investments_mm=50.0,
            nci_mm=30.0,
            preferred_mm=20.0,
        )
        # 1000 + 200 - 100 - 50 + 30 + 20 = 1100
        assert ev == pytest.approx(1_100.0)

    def test_net_cash_company(self):
        from auto_valuation.data.comps import compute_peer_ev
        # No debt, lots of cash → EV < market cap
        ev = compute_peer_ev(
            market_cap_mm=500.0,
            ibd_mm=0.0,
            cash_mm=100.0,
        )
        assert ev == pytest.approx(400.0)

    def test_zero_inputs(self):
        from auto_valuation.data.comps import compute_peer_ev
        ev = compute_peer_ev(0.0, 0.0, 0.0)
        assert ev == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# export_xlsx_to_pdf (smoke test — no LibreOffice required)
# ─────────────────────────────────────────────────────────────────────────────

class TestExportXlsxToPdf:
    def test_nonexistent_file_returns_none(self, tmp_path):
        from auto_valuation.output.deliver import export_xlsx_to_pdf
        result = export_xlsx_to_pdf(tmp_path / "nonexistent.xlsx")
        assert result is None

    def test_returns_none_gracefully_without_libreoffice(self, tmp_path):
        from auto_valuation.output.deliver import export_xlsx_to_pdf
        # Create a minimal file (even non-xlsx) to trigger the "not found" path
        xlsx = tmp_path / "test.xlsx"
        xlsx.write_bytes(b"PK")  # minimal zip-like stub
        # Without LibreOffice installed, should return None gracefully
        result = export_xlsx_to_pdf(xlsx)
        # Either None (no LO) or a Path (if LO installed in CI) — both valid
        assert result is None or result.exists()


# ─────────────────────────────────────────────────────────────────────────────
# webhook_notify (smoke test — no real URL required)
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookNotify:
    def test_bad_url_returns_false(self):
        from auto_valuation.output.deliver import webhook_notify
        result = webhook_notify(
            url="http://127.0.0.1:1/nonexistent",
            ticker="TEST",
            status="SUCCESS",
            timeout=1,
        )
        assert result is False

    def test_invalid_url_returns_false(self):
        from auto_valuation.output.deliver import webhook_notify
        result = webhook_notify(
            url="not_a_url",
            ticker="TEST",
            status="ERROR",
            timeout=1,
        )
        assert result is False
