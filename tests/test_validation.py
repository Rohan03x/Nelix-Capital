"""
tests/test_validation.py — Unit tests for auto_valuation/validation/checks.py

Phase 7 — Validation & Quality Control

Tests cover every public function in checks.py:
  validate_fmp_data()              : field coverage, FAIL on missing critical field
  check_revenue_sanity()           : negative revenue halt, YoY growth spike
  check_wacc_range()               : hard bounds, soft bounds, nominal pass
  check_terminal_growth_ceiling()  : ceiling gate, low-growth warn
  check_tv_pct_of_ev()             : high TV%, zero EV guard
  check_terminal_roic_vs_wacc()    : ROIC < WACC warn, ROIC ≥ WACC pass
  check_balance_sheet_closes()     : in-balance, out-of-balance
  check_negative_ev()              : negative EV warning
  check_capex_vs_da()              : under-investment, over-investment, normal
  check_nowc_sign()                : negative NWC is valid
  check_net_debt_sign()            : large net-cash warn, moderate net-cash pass
  check_sbc_terminal_dilution()    : SBC > threshold warn
  check_revenue_growth_vs_margins(): revenue-management signal detection
  check_nci_materiality()          : material NCI warn
  check_pension_materiality()      : material pension warn
  check_restatement_detection()    : step-change detection
  check_price_freshness()          : stale price warn, fresh pass
  run_all_data_checks()            : combined run, DataQualityError on FAIL

No live API calls — all tests use synthetic data.
"""

from __future__ import annotations

import datetime
import pytest

from auto_valuation.validation.checks import (
    ValidationResult,
    check_balance_sheet_closes,
    check_capex_vs_da,
    check_negative_ev,
    check_net_debt_sign,
    check_nci_materiality,
    check_nowc_sign,
    check_pension_materiality,
    check_price_freshness,
    check_restatement_detection,
    check_revenue_growth_vs_margins,
    check_revenue_sanity,
    check_sbc_terminal_dilution,
    check_terminal_growth_ceiling,
    check_terminal_roic_vs_wacc,
    check_tv_pct_of_ev,
    check_wacc_range,
    run_all_data_checks,
    validate_fmp_data,
)
from auto_valuation.utils.error import DataQualityError


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is(revenue=50_000, ebit=7_000, net_income=5_000, da=2_500, year="2023"):
    return {"calendarYear": year, "revenue": revenue, "ebit": ebit,
            "net_income": net_income, "da": da, "cfo": 6_000, "capex": -2_800}

def _bs(total_assets=40_000, total_equity=14_000, total_liabilities=26_000):
    return {"calendarYear": "2023",
            "total_assets": total_assets,
            "total_equity": total_equity,
            "total_liabilities": total_liabilities}

def _cf(capex=-2_800, cfo=6_000):
    return {"calendarYear": "2023", "capex": capex, "cfo": cfo}

_GOOD_IS = [
    _is(year="2023"), _is(revenue=46_000, year="2022"), _is(revenue=42_000, year="2021"),
]
_GOOD_BS = [_bs()]
_GOOD_CF = [_cf()]


# ─────────────────────────────────────────────────────────────────────────────
# 1 — validate_fmp_data
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateFmpData:
    def test_all_good_passes(self):
        results = validate_fmp_data(_GOOD_IS, _GOOD_BS, _GOOD_CF)
        fails = [r for r in results if r.status == "FAIL"]
        assert len(fails) == 0

    def test_missing_revenue_fails(self):
        bad_is = [{"calendarYear": str(y), "ebit": 100, "net_income": 50, "da": 20}
                  for y in range(2021, 2024)]
        results = validate_fmp_data(bad_is, _GOOD_BS, _GOOD_CF)
        names = [r.name for r in results if r.status == "FAIL"]
        assert any("revenue" in n.lower() for n in names)

    def test_only_one_year_warns(self):
        one_year_is = [{"calendarYear": "2023", "revenue": 50_000,
                        "ebit": 7_000, "net_income": 5_000, "da": 2_500}]
        results = validate_fmp_data(one_year_is, _GOOD_BS, _GOOD_CF)
        warns = [r for r in results if r.status == "WARN"]
        # At least revenue warn; income stmt fields have < 3 years
        assert len(warns) >= 1

    def test_returns_list_of_validation_results(self):
        results = validate_fmp_data(_GOOD_IS, _GOOD_BS, _GOOD_CF)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, ValidationResult)

    def test_each_result_has_required_fields(self):
        results = validate_fmp_data(_GOOD_IS, _GOOD_BS, _GOOD_CF)
        for r in results:
            assert hasattr(r, "name")
            assert hasattr(r, "status")
            assert r.status in ("PASS", "WARN", "FAIL")

    def test_missing_bs_field_fails(self):
        bad_bs = [{"calendarYear": "2023", "total_assets": 40_000}]  # missing equity / liabilities
        results = validate_fmp_data(_GOOD_IS, bad_bs, _GOOD_CF)
        fails = [r for r in results if r.status == "FAIL"]
        assert len(fails) > 0


# ─────────────────────────────────────────────────────────────────────────────
# 2 — check_revenue_sanity
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRevenueSanity:
    def test_normal_revenue_passes(self):
        r = check_revenue_sanity(_GOOD_IS)
        statuses = {res.status for res in r}
        assert "FAIL" not in statuses

    def test_negative_revenue_fails(self):
        bad = [_is(revenue=-1_000)]
        r = check_revenue_sanity(bad)
        assert any(res.status == "FAIL" for res in r)

    def test_zero_revenue_passes(self):
        # Zero revenue is not negative — treated as PASS by the sanity check
        bad = [_is(revenue=0)]
        r = check_revenue_sanity(bad)
        assert all(res.status != "FAIL" for res in r)

    def test_empty_list_fails(self):
        r = check_revenue_sanity([])
        assert any(res.status == "FAIL" for res in r)

    def test_large_growth_warns(self):
        # 210% growth (> 200% threshold) triggers WARN
        stmts = [
            _is(revenue=93_000, year="2023"),
            _is(revenue=30_000, year="2022"),  # 210% growth
        ]
        r = check_revenue_sanity(stmts)
        assert any(res.status == "WARN" for res in r)

    def test_moderate_growth_passes(self):
        stmts = [
            _is(revenue=53_000, year="2023"),
            _is(revenue=50_000, year="2022"),
        ]
        r = check_revenue_sanity(stmts)
        warns = [res for res in r if res.status == "WARN"]
        assert all("GROWTH" not in w.name for w in warns)


# ─────────────────────────────────────────────────────────────────────────────
# 3 — check_wacc_range
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckWaccRange:
    def test_normal_wacc_passes(self):
        assert check_wacc_range(0.10).status == "PASS"

    def test_below_hard_min_fails(self):
        assert check_wacc_range(0.01).status == "FAIL"

    def test_above_hard_max_fails(self):
        assert check_wacc_range(0.35).status == "FAIL"

    def test_below_soft_min_warns(self):
        assert check_wacc_range(0.05).status == "WARN"

    def test_above_soft_max_warns(self):
        assert check_wacc_range(0.18).status == "WARN"

    def test_boundary_hard_min_passes(self):
        # exactly at hard_min should not FAIL
        assert check_wacc_range(0.03).status in ("PASS", "WARN")

    def test_boundary_hard_max_passes(self):
        assert check_wacc_range(0.30).status in ("PASS", "WARN")


# ─────────────────────────────────────────────────────────────────────────────
# 4 — check_terminal_growth_ceiling
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckTerminalGrowthCeiling:
    def test_normal_growth_passes(self):
        assert check_terminal_growth_ceiling(0.025).status == "PASS"

    def test_at_ceiling_fails(self):
        assert check_terminal_growth_ceiling(0.04).status == "FAIL"

    def test_above_ceiling_fails(self):
        assert check_terminal_growth_ceiling(0.05).status == "FAIL"

    def test_very_low_warns(self):
        assert check_terminal_growth_ceiling(0.003).status == "WARN"

    def test_custom_ceiling(self):
        r = check_terminal_growth_ceiling(0.03, gdp_growth_ceiling=0.04)
        assert r.status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# 5 — check_tv_pct_of_ev
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckTvPctOfEv:
    def test_low_tv_pct_passes(self):
        assert check_tv_pct_of_ev(pv_tv=500, total_ev=1_000).status == "PASS"

    def test_high_tv_pct_warns(self):
        assert check_tv_pct_of_ev(pv_tv=900, total_ev=1_000).status == "WARN"

    def test_zero_ev_fails(self):
        assert check_tv_pct_of_ev(pv_tv=500, total_ev=0).status == "FAIL"

    def test_negative_ev_fails(self):
        assert check_tv_pct_of_ev(pv_tv=500, total_ev=-100).status == "FAIL"

    def test_custom_threshold(self):
        r = check_tv_pct_of_ev(pv_tv=750, total_ev=1_000, warn_threshold=0.90)
        assert r.status == "PASS"

    def test_value_represents_ratio(self):
        r = check_tv_pct_of_ev(pv_tv=700, total_ev=1_000)
        assert r.value == pytest.approx(0.70)


# ─────────────────────────────────────────────────────────────────────────────
# 6 — check_terminal_roic_vs_wacc
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckTerminalRoicVsWacc:
    def test_roic_above_wacc_passes(self):
        assert check_terminal_roic_vs_wacc(0.15, 0.10).status == "PASS"

    def test_roic_equal_wacc_passes(self):
        assert check_terminal_roic_vs_wacc(0.10, 0.10).status == "PASS"

    def test_roic_below_wacc_warns(self):
        assert check_terminal_roic_vs_wacc(0.08, 0.10).status == "WARN"

    def test_value_is_roic(self):
        r = check_terminal_roic_vs_wacc(0.12, 0.10)
        assert r.value == pytest.approx(0.12)


# ─────────────────────────────────────────────────────────────────────────────
# 7 — check_balance_sheet_closes
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckBalanceSheetCloses:
    def test_balanced_passes(self):
        r = check_balance_sheet_closes(40_000, 26_000, 14_000, year="2023")
        assert r.status == "PASS"

    def test_out_of_balance_fails(self):
        r = check_balance_sheet_closes(40_000, 26_000, 15_000, year="2023")
        assert r.status == "FAIL"

    def test_small_rounding_error_passes(self):
        # diff of 0.5 should be within default 1.0 tolerance
        r = check_balance_sheet_closes(40_000, 26_000.5, 14_000, year="2023")
        assert r.status == "PASS"

    def test_custom_tolerance(self):
        r = check_balance_sheet_closes(40_000, 26_000, 14_010, tolerance_mm=20.0)
        assert r.status == "PASS"

    def test_year_in_name(self):
        r = check_balance_sheet_closes(40_000, 26_000, 14_000, year="2022")
        assert "2022" in r.name


# ─────────────────────────────────────────────────────────────────────────────
# 8 — check_negative_ev
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckNegativeEv:
    def test_positive_ev_passes(self):
        assert check_negative_ev(10_000).status == "PASS"

    def test_zero_ev_passes(self):
        # zero is not negative
        assert check_negative_ev(0).status == "PASS"

    def test_negative_ev_warns(self):
        assert check_negative_ev(-500).status == "WARN"


# ─────────────────────────────────────────────────────────────────────────────
# 9 — check_capex_vs_da
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckCapexVsDa:
    def test_normal_ratio_passes(self):
        r = check_capex_vs_da(2_000, 2_500, year="2023")
        assert r.status == "PASS"

    def test_under_investment_warns(self):
        r = check_capex_vs_da(500, 2_500, year="2023")
        assert r.status == "WARN"
        assert r.value < 0.50

    def test_over_investment_warns(self):
        r = check_capex_vs_da(15_000, 2_500, year="2023")
        assert r.status == "WARN"
        assert r.value > 5.0

    def test_zero_da_passes(self):
        # Can't compute ratio — should not crash
        r = check_capex_vs_da(1_000, 0, year="2023")
        assert r.status == "PASS"

    def test_year_in_name(self):
        r = check_capex_vs_da(2_000, 2_500, year="2021")
        assert "2021" in r.name


# ─────────────────────────────────────────────────────────────────────────────
# 10 — check_nowc_sign
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckNowcSign:
    def test_positive_nowc_passes(self):
        assert check_nowc_sign([5_000], [50_000]).status == "PASS"

    def test_negative_nowc_is_valid_pass(self):
        # Amazon / Costco pattern — negative NWC (-10%) is valid, not an error
        r = check_nowc_sign([-2_000], [50_000])
        assert r.status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# 11 — check_net_debt_sign  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckNetDebtSign:
    def test_positive_net_debt_passes(self):
        r = check_net_debt_sign(8_000)
        assert r.status == "PASS"

    def test_moderate_net_cash_passes(self):
        r = check_net_debt_sign(-1_000)
        assert r.status == "PASS"

    def test_large_net_cash_warns(self):
        r = check_net_debt_sign(-20_000)
        assert r.status == "WARN"

    def test_threshold_boundary(self):
        # exactly at threshold = -5_000 → warn
        r = check_net_debt_sign(-5_001)
        assert r.status == "WARN"

    def test_value_recorded(self):
        r = check_net_debt_sign(10_000)
        assert r.value == 10_000


# ─────────────────────────────────────────────────────────────────────────────
# 12 — check_sbc_terminal_dilution  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckSbcTerminalDilution:
    def test_low_sbc_passes(self):
        r = check_sbc_terminal_dilution(sbc_mm=500, revenue_mm=50_000)
        assert r.status == "PASS"
        assert r.value == pytest.approx(0.01)

    def test_high_sbc_warns(self):
        r = check_sbc_terminal_dilution(sbc_mm=3_000, revenue_mm=50_000)
        assert r.status == "WARN"

    def test_exactly_at_threshold_warns(self):
        r = check_sbc_terminal_dilution(sbc_mm=2_500, revenue_mm=50_000)
        assert r.status == "WARN"

    def test_zero_revenue_passes(self):
        r = check_sbc_terminal_dilution(sbc_mm=500, revenue_mm=0)
        assert r.status == "PASS"

    def test_custom_threshold(self):
        r = check_sbc_terminal_dilution(sbc_mm=3_000, revenue_mm=50_000,
                                         warn_threshold=0.10)
        assert r.status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# 13 — check_revenue_growth_vs_margins  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRevenueGrowthVsMargins:
    def _make_stmts(self, revs, ebits):
        """Build income_stmts list (most-recent first) from revenue/ebit lists."""
        years = list(range(2023, 2023 - len(revs), -1))
        return [{"calendarYear": str(y), "revenue": r, "ebit": e}
                for y, r, e in zip(years, revs, ebits)]

    def test_normal_growth_no_signal(self):
        # Revenue growing, margins stable
        stmts = self._make_stmts(
            revs=[55_000, 50_000, 46_000],
            ebits=[7_700, 7_000, 6_440],
        )
        results = check_revenue_growth_vs_margins(stmts)
        assert all(r.status != "WARN" for r in results)

    def test_single_year_no_signal(self):
        results = check_revenue_growth_vs_margins([_is()])
        assert results == []

    def test_two_flags_triggers_warn(self):
        # Revenue falling two years running, margin improving each time
        stmts = self._make_stmts(
            revs=[40_000, 45_000, 50_000],   # declining (most-recent first)
            ebits=[6_800, 7_200, 7_500],     # margin: 17% > 16% > 15% — improving
        )
        results = check_revenue_growth_vs_margins(stmts)
        assert any(r.status == "WARN" for r in results)

    def test_one_flag_no_warn(self):
        # Only 1 year of the pattern — should not trigger
        # 2023 vs 2022: revenue falls, margin rises → flag
        # 2022 vs 2021: revenue falls, margin falls  → no flag
        stmts = self._make_stmts(
            revs=[40_000, 45_000, 50_000],   # declining (most-recent first)
            ebits=[6_800, 6_000, 7_000],     # margins: 17%, 13.3%, 14% — only yr1 expands
        )
        results = check_revenue_growth_vs_margins(stmts)
        assert all(r.status != "WARN" for r in results)

    def test_insufficient_data_returns_empty(self):
        results = check_revenue_growth_vs_margins([])
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# 14 — check_nci_materiality  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckNciMateriality:
    def test_immaterial_nci_passes(self):
        r = check_nci_materiality(minority_interest_mm=100, total_equity_mm=10_000)
        assert r.status == "PASS"

    def test_material_nci_warns(self):
        r = check_nci_materiality(minority_interest_mm=600, total_equity_mm=10_000)
        assert r.status == "WARN"

    def test_exactly_at_threshold_warns(self):
        r = check_nci_materiality(minority_interest_mm=500, total_equity_mm=10_000)
        assert r.status == "WARN"

    def test_zero_equity_passes(self):
        r = check_nci_materiality(minority_interest_mm=100, total_equity_mm=0)
        assert r.status == "PASS"

    def test_custom_threshold(self):
        r = check_nci_materiality(minority_interest_mm=600, total_equity_mm=10_000,
                                   warn_threshold=0.10)
        assert r.status == "PASS"

    def test_value_is_ratio(self):
        r = check_nci_materiality(minority_interest_mm=1_000, total_equity_mm=10_000)
        assert r.value == pytest.approx(0.10)


# ─────────────────────────────────────────────────────────────────────────────
# 15 — check_pension_materiality  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckPensionMateriality:
    def test_immaterial_pension_passes(self):
        r = check_pension_materiality(pension_obligation_mm=500, total_assets_mm=40_000)
        assert r.status == "PASS"

    def test_material_pension_warns(self):
        r = check_pension_materiality(pension_obligation_mm=2_500, total_assets_mm=40_000)
        assert r.status == "WARN"

    def test_zero_assets_passes(self):
        r = check_pension_materiality(pension_obligation_mm=500, total_assets_mm=0)
        assert r.status == "PASS"

    def test_custom_threshold(self):
        r = check_pension_materiality(pension_obligation_mm=2_500, total_assets_mm=40_000,
                                       warn_threshold=0.10)
        assert r.status == "PASS"

    def test_value_is_ratio(self):
        r = check_pension_materiality(pension_obligation_mm=2_000, total_assets_mm=40_000)
        assert r.value == pytest.approx(0.05)


# ─────────────────────────────────────────────────────────────────────────────
# 16 — check_restatement_detection  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRestatementDetection:
    def test_smooth_growth_no_signal(self):
        stmts = [
            _is(revenue=55_000, year="2023"),
            _is(revenue=50_000, year="2022"),
            _is(revenue=46_000, year="2021"),
        ]
        results = check_restatement_detection(stmts)
        assert all(r.status != "WARN" for r in results)

    def test_large_jump_warns(self):
        stmts = [
            _is(revenue=90_000, year="2023"),
            _is(revenue=30_000, year="2022"),   # 200% jump
        ]
        results = check_restatement_detection(stmts)
        assert any(r.status == "WARN" for r in results)

    def test_large_drop_warns(self):
        stmts = [
            _is(revenue=15_000, year="2023"),
            _is(revenue=50_000, year="2022"),   # 70% drop
        ]
        results = check_restatement_detection(stmts)
        assert any(r.status == "WARN" for r in results)

    def test_custom_threshold(self):
        # Use a value strictly above the custom threshold to trigger a warn
        stmts = [
            _is(revenue=68_000, year="2023"),
            _is(revenue=50_000, year="2022"),   # 36% jump > 30% threshold
        ]
        results = check_restatement_detection(stmts, revenue_jump_threshold=0.30)
        assert any(r.status == "WARN" for r in results)

    def test_empty_returns_empty(self):
        assert check_restatement_detection([]) == []

    def test_single_year_returns_empty(self):
        assert check_restatement_detection([_is()]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 17 — check_price_freshness  (NEW — Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckPriceFreshness:
    def _today(self):
        return datetime.date.today().isoformat()

    def _days_ago(self, n):
        return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

    def test_fresh_price_passes(self):
        r = check_price_freshness(self._days_ago(1), stale_days=5)
        assert r.status == "PASS"

    def test_stale_price_warns(self):
        r = check_price_freshness(self._days_ago(10), stale_days=5)
        assert r.status == "WARN"

    def test_exactly_stale_warns(self):
        r = check_price_freshness(self._days_ago(6), stale_days=5)
        assert r.status == "WARN"

    def test_today_price_passes(self):
        r = check_price_freshness(self._today(), stale_days=5)
        assert r.status == "PASS"

    def test_future_date_warns(self):
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        r = check_price_freshness(future)
        assert r.status == "WARN"

    def test_invalid_date_warns(self):
        r = check_price_freshness("not-a-date")
        assert r.status == "WARN"

    def test_delta_value_recorded(self):
        r = check_price_freshness(self._days_ago(2), stale_days=5)
        assert r.status == "PASS"
        assert r.value == 2

    def test_custom_today_override(self):
        # Use a fixed "today" so tests are deterministic regardless of run date
        r = check_price_freshness("2025-01-01", today="2025-01-04", stale_days=5)
        assert r.status == "PASS"
        assert r.value == 3

    def test_stale_with_custom_today(self):
        r = check_price_freshness("2025-01-01", today="2025-01-15", stale_days=5)
        assert r.status == "WARN"


# ─────────────────────────────────────────────────────────────────────────────
# 18 — run_all_data_checks
# ─────────────────────────────────────────────────────────────────────────────

class TestRunAllDataChecks:
    def test_good_data_passes(self):
        results = run_all_data_checks(_GOOD_IS, _GOOD_BS, _GOOD_CF)
        fails = [r for r in results if r.status == "FAIL"]
        assert len(fails) == 0

    def test_returns_list_of_validation_results(self):
        results = run_all_data_checks(_GOOD_IS, _GOOD_BS, _GOOD_CF)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, ValidationResult)

    def test_raises_data_quality_error_on_missing_revenue(self):
        bad_is = [{"calendarYear": str(y), "ebit": 100, "net_income": 50, "da": 20}
                  for y in range(2021, 2024)]
        with pytest.raises(DataQualityError):
            run_all_data_checks(bad_is, _GOOD_BS, _GOOD_CF)

    def test_raises_data_quality_error_on_negative_revenue(self):
        bad_is = [_is(revenue=-1_000)]
        with pytest.raises(DataQualityError):
            run_all_data_checks(bad_is, _GOOD_BS, _GOOD_CF)

    def test_raises_on_empty_income_stmts(self):
        with pytest.raises(DataQualityError):
            run_all_data_checks([], _GOOD_BS, _GOOD_CF)

    def test_warns_do_not_raise(self):
        # 200% revenue jump produces WARN, not FAIL — should not raise
        stmts = [
            _is(revenue=90_000, year="2023"),
            _is(revenue=30_000, year="2022"),
            _is(revenue=28_000, year="2021"),
        ]
        results = run_all_data_checks(stmts, _GOOD_BS, _GOOD_CF)
        assert any(r.status == "WARN" for r in results)


# ─────────────────────────────────────────────────────────────────────────────
# 19 — ValidationResult helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestValidationResult:
    def test_pass_is_ok(self):
        assert ValidationResult("X", "PASS").is_ok() is True

    def test_warn_is_ok(self):
        assert ValidationResult("X", "WARN").is_ok() is True

    def test_fail_is_not_ok(self):
        assert ValidationResult("X", "FAIL").is_ok() is False
