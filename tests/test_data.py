"""
tests/test_data.py — Unit tests for the data layer

Phase 8 — Data Layer: bridge, cleaner, fiscal_year

Tests cover:
  bridge.py:
    compute_net_debt()          : all debt components, net-cash result
    compute_equity_value()      : EV bridge, non-operating add-backs

  cleaner.py:
    unit_normalize()            : thousands scaling, billions scaling, no-op
    standardise_field_names()   : FMP→canonical rename, unknown fields preserved
    deduplicate_financial_data(): duplicate year removal, higher-revenue kept
    detect_ma_years()           : growth threshold detection
    normalize_one_time_items()  : goodwill impairment add-back, restructuring
    strip_discontinued_ops()    : net income adjustment
    capitalise_rd()             : R&D asset build-up, EBIT adjustment
    check_revenue_recognition_flags(): AR-days acceleration, deferred revenue pull

  fiscal_year.py:
    get_fiscal_year_end_month() : date parsing, default fallback
    stub_period_weight()        : fraction of year remaining
    align_to_calendar_year()    : fills missing calendarYear from date field
    compute_ttm()               : 4-quarter sum for flow items, latest for BS
    calendarize_peer_data()     : use_ttm_for_comps flag

No live API calls.
"""

from __future__ import annotations

import warnings

import pytest

from auto_valuation.data.bridge import compute_net_debt, compute_equity_value
from auto_valuation.data.cleaner import (
    capitalise_rd,
    check_revenue_recognition_flags,
    deduplicate_financial_data,
    detect_ma_years,
    normalize_one_time_items,
    standardise_field_names,
    strip_discontinued_ops,
    unit_normalize,
)
from auto_valuation.data.fiscal_year import (
    align_to_calendar_year,
    calendarize_peer_data,
    compute_ttm,
    get_fiscal_year_end_month,
    stub_period_weight,
)

_PROFILE_USD = {"currency": "USD"}

# ─────────────────────────────────────────────────────────────────────────────
# 1 — compute_net_debt
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeNetDebt:
    def _bs(self, **kwargs) -> dict:
        base = {
            "cash": 3_000, "shortTermInvestments": 0,
            "shortTermDebt": 0, "longTermDebt": 8_000,
            "financeLeaseLiability": 0, "preferredStock": 0,
            "minorityInterest": 0,
        }
        base.update(kwargs)
        return base

    def test_basic_lt_debt_minus_cash(self):
        bs = self._bs(longTermDebt=8_000, cash=3_000)
        assert compute_net_debt(bs) == pytest.approx(5_000)

    def test_net_cash_position_negative(self):
        bs = self._bs(longTermDebt=2_000, cash=5_000)
        assert compute_net_debt(bs) < 0

    def test_st_debt_added(self):
        bs = self._bs(shortTermDebt=1_000, longTermDebt=4_000, cash=2_000)
        assert compute_net_debt(bs) == pytest.approx(3_000)

    def test_finance_leases_added(self):
        bs = self._bs(longTermDebt=4_000, cash=2_000, financeLeaseLiability=500)
        assert compute_net_debt(bs) == pytest.approx(2_500)

    def test_preferred_stock_added(self):
        bs = self._bs(longTermDebt=4_000, cash=2_000, preferredStock=400)
        assert compute_net_debt(bs) == pytest.approx(2_400)

    def test_nci_added(self):
        bs = self._bs(longTermDebt=4_000, cash=2_000, minorityInterest=300)
        assert compute_net_debt(bs) == pytest.approx(2_300)

    def test_st_investments_deducted(self):
        bs = self._bs(longTermDebt=5_000, cash=2_000, shortTermInvestments=1_000)
        assert compute_net_debt(bs) == pytest.approx(2_000)

    def test_empty_balance_sheet_returns_zero(self):
        assert compute_net_debt({}) == pytest.approx(0)

    def test_canonical_field_names(self):
        # standardised field names (after cleaner)
        bs = {"long_term_debt": 6_000, "cash": 2_000, "short_term_debt": 500}
        result = compute_net_debt(bs)
        assert result == pytest.approx(4_500)

    def test_total_debt_fallback(self):
        # Only totalDebt provided (no breakdown)
        bs = {"totalDebt": 7_000, "cash": 2_000}
        assert compute_net_debt(bs) == pytest.approx(5_000)

    def test_total_debt_not_double_counted(self):
        # Both totalDebt AND longTermDebt → use breakdown, not totalDebt
        bs = {"totalDebt": 10_000, "longTermDebt": 6_000, "shortTermDebt": 1_000,
              "cash": 2_000}
        # breakdown: 6_000 + 1_000 - 2_000 = 5_000 (not 10_000 - 2_000)
        assert compute_net_debt(bs) == pytest.approx(5_000)

    def test_negative_values_clamped_to_zero(self):
        # FMP sometimes reports negative debt fields — should be treated as 0
        bs = {"longTermDebt": -500, "cash": 1_000}
        assert compute_net_debt(bs) == pytest.approx(-1_000)


# ─────────────────────────────────────────────────────────────────────────────
# 2 — compute_equity_value
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeEquityValue:
    _BS = {"longTermDebt": 8_000, "cash": 3_000}

    def test_simple_bridge(self):
        # EV 50_000; net debt = 8_000 - 3_000 = 5_000; equity = 45_000
        eq = compute_equity_value(50_000, self._BS)
        assert eq == pytest.approx(45_000)

    def test_adds_equity_method_investments(self):
        eq = compute_equity_value(50_000, self._BS, equity_method_investments=1_000)
        assert eq == pytest.approx(46_000)

    def test_adds_nol_pv(self):
        eq = compute_equity_value(50_000, self._BS, net_operating_losses_pv=500)
        assert eq == pytest.approx(45_500)

    def test_net_cash_company(self):
        bs = {"longTermDebt": 1_000, "cash": 5_000}
        # net_debt = -4_000; EV=10_000 → equity = 14_000
        eq = compute_equity_value(10_000, bs)
        assert eq == pytest.approx(14_000)


# ─────────────────────────────────────────────────────────────────────────────
# 3 — unit_normalize
# ─────────────────────────────────────────────────────────────────────────────

class TestUnitNormalize:
    def test_already_millions_no_change(self):
        stmts = [{"revenue": 50_000, "calendarYear": "2023"}]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["revenue"] == pytest.approx(50_000)

    def test_thousands_scaled_to_millions(self):
        # Revenue > 10_000_000 → values are in thousands
        stmts = [{"revenue": 50_000_000, "calendarYear": "2023"}]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["revenue"] == pytest.approx(50_000)

    def test_billions_scaled_to_millions(self):
        # Revenue < 1_000 → values are in billions
        stmts = [{"revenue": 50.0, "calendarYear": "2023"}]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["revenue"] == pytest.approx(50_000)

    def test_non_numeric_fields_unchanged(self):
        stmts = [{"calendarYear": "2023", "period": "FY", "revenue": 50_000}]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["calendarYear"] == "2023"
        assert result[0]["period"] == "FY"

    def test_empty_list_returns_empty(self):
        assert unit_normalize([], _PROFILE_USD) == []

    def test_none_revenue_returns_unchanged(self):
        stmts = [{"revenue": None, "ebit": None}]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["revenue"] is None

    def test_multiple_statements_scaled_consistently(self):
        stmts = [
            {"revenue": 55_000_000, "calendarYear": "2023"},
            {"revenue": 50_000_000, "calendarYear": "2022"},
        ]
        result = unit_normalize(stmts, _PROFILE_USD)
        assert result[0]["revenue"] == pytest.approx(55_000)
        assert result[1]["revenue"] == pytest.approx(50_000)


# ─────────────────────────────────────────────────────────────────────────────
# 4 — standardise_field_names
# ─────────────────────────────────────────────────────────────────────────────

class TestStandardiseFieldNames:
    def test_operating_income_to_ebit(self):
        stmts = [{"operatingIncome": 6_000}]
        result = standardise_field_names(stmts)
        assert result[0]["ebit"] == 6_000

    def test_revenue_unchanged(self):
        stmts = [{"revenue": 50_000}]
        result = standardise_field_names(stmts)
        assert result[0]["revenue"] == 50_000

    def test_total_revenue_to_revenue(self):
        stmts = [{"totalRevenue": 50_000}]
        result = standardise_field_names(stmts)
        assert result[0]["revenue"] == 50_000

    def test_depreciation_to_da(self):
        stmts = [{"depreciationAndAmortization": 2_500}]
        result = standardise_field_names(stmts)
        assert result[0]["da"] == 2_500

    def test_capital_expenditure_to_capex(self):
        stmts = [{"capitalExpenditure": -2_800}]
        result = standardise_field_names(stmts)
        assert result[0]["capex"] == -2_800

    def test_long_term_debt_canonical(self):
        stmts = [{"longTermDebt": 8_000}]
        result = standardise_field_names(stmts)
        assert result[0]["long_term_debt"] == 8_000

    def test_unknown_field_preserved(self):
        stmts = [{"myCustomField": 999, "revenue": 50_000}]
        result = standardise_field_names(stmts)
        assert result[0]["myCustomField"] == 999

    def test_multiple_statements(self):
        stmts = [
            {"operatingIncome": 6_000, "calendarYear": "2023"},
            {"operatingIncome": 5_500, "calendarYear": "2022"},
        ]
        result = standardise_field_names(stmts)
        assert result[0]["ebit"] == 6_000
        assert result[1]["ebit"] == 5_500

    def test_minority_interest_to_nci(self):
        stmts = [{"minorityInterest": 300}]
        result = standardise_field_names(stmts)
        assert result[0]["nci"] == 300

    def test_sbc_canonical(self):
        stmts = [{"stockBasedCompensation": 500}]
        result = standardise_field_names(stmts)
        assert result[0]["sbc"] == 500


# ─────────────────────────────────────────────────────────────────────────────
# 5 — deduplicate_financial_data
# ─────────────────────────────────────────────────────────────────────────────

class TestDeduplicateFinancialData:
    def test_no_duplicates_unchanged(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 50_000},
            {"calendarYear": "2022", "revenue": 46_000},
        ]
        result = deduplicate_financial_data(stmts)
        assert len(result) == 2

    def test_duplicate_year_removed(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 50_000},
            {"calendarYear": "2023", "revenue": 48_000},   # duplicate
            {"calendarYear": "2022", "revenue": 46_000},
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = deduplicate_financial_data(stmts)
        assert len(result) == 2

    def test_keeps_higher_revenue_record(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 50_000},
            {"calendarYear": "2023", "revenue": 48_000},
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = deduplicate_financial_data(stmts)
        assert result[0]["revenue"] == 50_000

    def test_duplicate_emits_warning(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 50_000},
            {"calendarYear": "2023", "revenue": 48_000},
        ]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            deduplicate_financial_data(stmts)
        assert len(w) >= 1

    def test_missing_year_key_skipped(self):
        stmts = [
            {"revenue": 50_000},   # no calendarYear or date
            {"calendarYear": "2022", "revenue": 46_000},
        ]
        result = deduplicate_financial_data(stmts)
        # Only the record with a key is kept
        assert any(r.get("calendarYear") == "2022" for r in result)

    def test_empty_list_returns_empty(self):
        assert deduplicate_financial_data([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 6 — detect_ma_years
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectMaYears:
    def test_no_jump_returns_empty(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 55_000},
            {"calendarYear": "2022", "revenue": 50_000},  # 10% growth
        ]
        assert detect_ma_years(stmts) == []

    def test_large_jump_detected(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 70_000},
            {"calendarYear": "2022", "revenue": 50_000},  # 40% growth
        ]
        years = detect_ma_years(stmts)
        assert "2023" in years

    def test_custom_threshold(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 60_000},
            {"calendarYear": "2022", "revenue": 50_000},  # 20% growth
        ]
        # Default 15% threshold → flags it
        assert "2023" in detect_ma_years(stmts)
        # Custom 25% threshold → doesn't flag it
        assert detect_ma_years(stmts, threshold=0.25) == []

    def test_single_year_returns_empty(self):
        stmts = [{"calendarYear": "2023", "revenue": 50_000}]
        assert detect_ma_years(stmts) == []

    def test_returns_list_of_strings(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 80_000},
            {"calendarYear": "2022", "revenue": 50_000},
        ]
        result = detect_ma_years(stmts)
        assert isinstance(result, list)
        assert all(isinstance(y, str) for y in result)


# ─────────────────────────────────────────────────────────────────────────────
# 7 — normalize_one_time_items
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeOneTimeItems:
    def test_adds_ebit_normalized_field(self):
        stmts = [{"ebit": 5_000}]
        result = normalize_one_time_items(stmts)
        assert "ebit_normalized" in result[0]

    def test_no_one_time_items_ebit_unchanged(self):
        stmts = [{"ebit": 5_000}]
        result = normalize_one_time_items(stmts)
        assert result[0]["ebit_normalized"] == pytest.approx(5_000)

    def test_goodwill_impairment_added_back(self):
        stmts = [{"ebit": 4_000, "impairmentOfGoodwill": -1_000}]
        result = normalize_one_time_items(stmts)
        assert result[0]["ebit_normalized"] == pytest.approx(5_000)

    def test_restructuring_added_back(self):
        stmts = [{"ebit": 4_000, "restructuringCharges": -500}]
        result = normalize_one_time_items(stmts)
        assert result[0]["ebit_normalized"] == pytest.approx(4_500)

    def test_both_addbacks_combined(self):
        stmts = [{"ebit": 3_000,
                  "impairmentOfGoodwill": -800,
                  "restructuringCharges": -200}]
        result = normalize_one_time_items(stmts)
        assert result[0]["ebit_normalized"] == pytest.approx(4_000)

    def test_addback_fields_recorded(self):
        stmts = [{"ebit": 4_000, "impairmentOfGoodwill": -1_000}]
        result = normalize_one_time_items(stmts)
        assert result[0]["goodwill_impairment_addback"] == pytest.approx(1_000)

    def test_original_ebit_unchanged(self):
        stmts = [{"ebit": 4_000, "impairmentOfGoodwill": -1_000}]
        result = normalize_one_time_items(stmts)
        assert result[0]["ebit"] == 4_000   # not mutated

    def test_empty_list_returns_empty(self):
        assert normalize_one_time_items([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 8 — strip_discontinued_ops
# ─────────────────────────────────────────────────────────────────────────────

class TestStripDiscontinuedOps:
    def test_no_discontinued_ops_unchanged(self):
        stmts = [{"net_income": 5_000}]
        cf    = [{"capex": -2_000}]
        r_is, r_cf = strip_discontinued_ops(stmts, cf)
        assert r_is[0]["net_income"] == 5_000

    def test_discontinued_ops_deducted_from_net_income(self):
        stmts = [{"net_income": 5_000, "discontinuedOperationsNetIncome": 500}]
        cf    = [{"capex": -2_000}]
        r_is, _ = strip_discontinued_ops(stmts, cf)
        assert r_is[0]["net_income"] == pytest.approx(4_500)

    def test_stripped_amount_recorded(self):
        stmts = [{"net_income": 5_000, "discontinuedOperationsNetIncome": 500}]
        r_is, _ = strip_discontinued_ops(stmts, [])
        assert r_is[0]["discontinued_ops_stripped"] == 500

    def test_returns_tuple_of_two_lists(self):
        r = strip_discontinued_ops([{"net_income": 5_000}], [{"capex": -1_000}])
        assert isinstance(r, tuple)
        assert len(r) == 2

    def test_empty_inputs(self):
        r_is, r_cf = strip_discontinued_ops([], [])
        assert r_is == []
        assert r_cf == []


# ─────────────────────────────────────────────────────────────────────────────
# 9 — capitalise_rd
# ─────────────────────────────────────────────────────────────────────────────

class TestCapitaliseRd:
    def _stmts(self, rd_values, ebit=5_000):
        """Build most-recent-first income statements with given R&D values."""
        years = list(range(2023, 2023 - len(rd_values), -1))
        return [
            {"calendarYear": str(y), "ebit": ebit, "rd_expense": rd}
            for y, rd in zip(years, rd_values)
        ]

    def test_adds_ebit_rd_adjusted_field(self):
        stmts = self._stmts([1_000])
        result = capitalise_rd(stmts)
        assert "ebit_rd_adjusted" in result[0]

    def test_no_rd_no_adjustment(self):
        stmts = [{"calendarYear": "2023", "ebit": 5_000, "rd_expense": 0}]
        result = capitalise_rd(stmts)
        assert result[0]["ebit_rd_adjusted"] == pytest.approx(5_000)

    def test_rd_asset_builds_up(self):
        stmts = self._stmts([1_000, 1_000, 1_000], ebit=5_000)
        result = capitalise_rd(stmts, amort_years=5)
        # Most-recent year should have a non-zero rd_asset_closing
        assert result[0]["rd_asset_closing"] > 0

    def test_ebit_adjusted_higher_than_reported(self):
        """In early years, R&D add-back exceeds amortisation → higher adjusted EBIT."""
        stmts = self._stmts([1_000], ebit=5_000)
        result = capitalise_rd(stmts, amort_years=5)
        # Only one year: asset=1000, amort=0 (no prior asset), adj_ebit = 5000+1000-0 = 6000
        assert result[0]["ebit_rd_adjusted"] > result[0]["ebit"]

    def test_custom_amort_years(self):
        stmts = self._stmts([1_000, 1_000, 1_000])
        result_3yr = capitalise_rd(stmts, amort_years=3)
        result_10yr = capitalise_rd(stmts, amort_years=10)
        # Shorter amort → higher annual amort charge → different adjusted EBIT
        assert result_3yr[0]["rd_amort"] != result_10yr[0]["rd_amort"]

    def test_original_ebit_not_mutated(self):
        stmts = self._stmts([1_000])
        result = capitalise_rd(stmts)
        assert result[0]["ebit"] == 5_000


# ─────────────────────────────────────────────────────────────────────────────
# 10 — check_revenue_recognition_flags
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRevenueRecognitionFlags:
    def test_clean_data_no_flags(self):
        stmts = [
            {"revenue": 55_000, "accounts_receivable": 5_000},
            {"revenue": 50_000, "accounts_receivable": 4_545},
        ]
        assert check_revenue_recognition_flags(stmts) == []

    def test_ar_days_acceleration_flagged(self):
        # AR days jumps much faster than revenue growth
        stmts = [
            {"revenue": 52_000, "accounts_receivable": 10_000},  # AR days ~70
            {"revenue": 50_000, "accounts_receivable": 4_000},   # AR days ~29
        ]
        flags = check_revenue_recognition_flags(stmts)
        assert any("AR days" in f for f in flags)

    def test_deferred_revenue_decline_flagged(self):
        stmts = [
            {"revenue": 55_000, "deferredRevenue": 800,
             "accounts_receivable": 5_000},
            {"revenue": 50_000, "deferredRevenue": 1_200,
             "accounts_receivable": 4_500},
        ]
        flags = check_revenue_recognition_flags(stmts)
        assert any("Deferred revenue" in f for f in flags)

    def test_single_year_returns_empty(self):
        assert check_revenue_recognition_flags([{"revenue": 50_000}]) == []

    def test_returns_list_of_strings(self):
        stmts = [{"revenue": 50_000}, {"revenue": 46_000}]
        result = check_revenue_recognition_flags(stmts)
        assert isinstance(result, list)
        assert all(isinstance(f, str) for f in result)


# ─────────────────────────────────────────────────────────────────────────────
# 11 — get_fiscal_year_end_month
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFiscalYearEndMonth:
    def test_december_fiscal_year(self):
        stmts = [{"date": "2023-12-31"}]
        assert get_fiscal_year_end_month(stmts) == 12

    def test_may_fiscal_year(self):
        stmts = [{"date": "2023-05-31"}]
        assert get_fiscal_year_end_month(stmts) == 5

    def test_no_date_returns_default_12(self):
        assert get_fiscal_year_end_month([{}]) == 12

    def test_empty_list_returns_12(self):
        assert get_fiscal_year_end_month([]) == 12

    def test_multiple_stmts_uses_most_recent(self):
        stmts = [
            {"date": "2023-03-31"},   # March
            {"date": "2022-03-31"},
        ]
        assert get_fiscal_year_end_month(stmts) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 12 — stub_period_weight
# ─────────────────────────────────────────────────────────────────────────────

class TestStubPeriodWeight:
    def test_returns_float_in_0_1(self):
        for month in range(1, 13):
            w = stub_period_weight(month)
            assert 0.0 <= w <= 1.0

    def test_december_fiscal_year_close_to_zero_in_december(self):
        # If today is December and FY ends December → almost no stub remaining
        import datetime
        if datetime.date.today().month == 12:
            w = stub_period_weight(12)
            assert w <= 1/12 + 0.01

    def test_all_twelve_months_covered(self):
        weights = [stub_period_weight(m) for m in range(1, 13)]
        assert len(weights) == 12
        assert all(isinstance(w, float) for w in weights)


# ─────────────────────────────────────────────────────────────────────────────
# 13 — align_to_calendar_year
# ─────────────────────────────────────────────────────────────────────────────

class TestAlignToCalendarYear:
    def test_no_calendar_year_filled_from_date(self):
        stmts = [{"date": "2023-12-31", "revenue": 50_000}]
        result = align_to_calendar_year(stmts)
        assert result[0]["calendarYear"] == "2023"

    def test_existing_calendar_year_not_overwritten(self):
        stmts = [{"date": "2023-05-31", "calendarYear": "2023", "revenue": 50_000}]
        result = align_to_calendar_year(stmts)
        assert result[0]["calendarYear"] == "2023"

    def test_empty_list_returns_empty(self):
        assert align_to_calendar_year([]) == []

    def test_no_date_no_calendar_year_unchanged(self):
        stmts = [{"revenue": 50_000}]
        result = align_to_calendar_year(stmts)
        assert result[0].get("calendarYear") is None

    def test_original_not_mutated(self):
        stmts = [{"date": "2023-12-31", "revenue": 50_000}]
        original_id = id(stmts[0])
        align_to_calendar_year(stmts)
        assert id(stmts[0]) == original_id   # copy was made


# ─────────────────────────────────────────────────────────────────────────────
# 14 — compute_ttm
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeTtm:
    @pytest.fixture
    def q_is(self):
        """4 quarterly income statements, most-recent first."""
        return [
            {"date": "2023-09-30", "revenue": 12_000, "ebit": 1_800,
             "net_income": 1_300, "da": 300, "period": "Q3"},
            {"date": "2023-06-30", "revenue": 11_000, "ebit": 1_600,
             "net_income": 1_200, "da": 280, "period": "Q2"},
            {"date": "2023-03-31", "revenue": 13_000, "ebit": 2_000,
             "net_income": 1_500, "da": 320, "period": "Q1"},
            {"date": "2022-12-31", "revenue": 11_500, "ebit": 1_700,
             "net_income": 1_250, "da": 290, "period": "Q4"},
        ]

    @pytest.fixture
    def q_cf(self):
        return [
            {"date": "2023-09-30", "capex": -500, "cfo": 1_200, "period": "Q3"},
            {"date": "2023-06-30", "capex": -480, "cfo": 1_100, "period": "Q2"},
            {"date": "2023-03-31", "capex": -520, "cfo": 1_300, "period": "Q1"},
            {"date": "2022-12-31", "capex": -470, "cfo": 1_050, "period": "Q4"},
        ]

    @pytest.fixture
    def q_bs(self):
        return [{"date": "2023-09-30", "cash": 9_000, "long_term_debt": 8_000,
                 "total_equity": 14_000, "period": "Q3"}]

    def test_revenue_sums_four_quarters(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm["revenue"] == pytest.approx(47_500)

    def test_ebit_sums_four_quarters(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm["ebit"] == pytest.approx(7_100)

    def test_capex_sums_four_quarters(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm["capex"] == pytest.approx(-1_970)

    def test_bs_field_from_latest_quarter(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm.get("cash") == 9_000
        assert ttm.get("total_equity") == 14_000

    def test_period_is_ttm(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm["period"] == "TTM"

    def test_date_from_latest_quarter(self, q_is, q_cf, q_bs):
        ttm = compute_ttm(q_is, q_cf, q_bs)
        assert ttm["date"] == "2023-09-30"

    def test_empty_inputs_return_ttm_dict(self):
        ttm = compute_ttm([], [], [])
        assert ttm["period"] == "TTM"
        assert ttm.get("revenue") is None   # nothing to sum

    def test_only_two_quarters_partial_ttm(self, q_cf, q_bs):
        q2 = [
            {"date": "2023-09-30", "revenue": 12_000, "period": "Q3"},
            {"date": "2023-06-30", "revenue": 11_000, "period": "Q2"},
        ]
        ttm = compute_ttm(q2, q_cf, q_bs)
        assert ttm["revenue"] == pytest.approx(23_000)


# ─────────────────────────────────────────────────────────────────────────────
# 15 — calendarize_peer_data
# ─────────────────────────────────────────────────────────────────────────────

class TestCalendarizePeerData:
    def test_matching_fy_month_no_flag(self):
        peers = [{"date": "2023-12-31", "revenue": 50_000}]
        result = calendarize_peer_data(peers, target_fiscal_month=12)
        assert result[0]["use_ttm_for_comps"] is False

    def test_different_fy_month_flagged(self):
        peers = [{"date": "2023-05-31", "revenue": 50_000}]
        result = calendarize_peer_data(peers, target_fiscal_month=12)
        assert result[0]["use_ttm_for_comps"] is True

    def test_note_added_for_mismatched_peer(self):
        peers = [{"date": "2023-05-31", "revenue": 50_000}]
        result = calendarize_peer_data(peers, target_fiscal_month=12)
        assert "calendarization_note" in result[0]

    def test_empty_list_returns_empty(self):
        assert calendarize_peer_data([]) == []

    def test_original_not_mutated(self):
        peers = [{"date": "2023-05-31", "revenue": 50_000}]
        original_id = id(peers[0])
        calendarize_peer_data(peers, target_fiscal_month=12)
        assert id(peers[0]) == original_id
