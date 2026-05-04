"""
tests/test_model.py — Unit tests for the model layer

Phase 9 — Model Layer:
  model/income_statement.py  : revenue forecast, EBIT margin, D&A%, tax, NOPAT, UFCF
  model/working_capital.py   : DSO/DIO/DPO, NOWC from BS, NOWC from days
  model/dilution.py          : TSM, RSUs, convertibles, compute_fully_diluted_shares
  model/debt.py              : cost of debt, debt schedule, leverage metrics
  model/balance_sheet.py     : capex forecast, PP&E rollforward, invested capital, ROIC
  forecast/terminal_value.py : gordon growth TV, exit multiple TV, PV TV, sensitivity table

No live API calls.
"""

from __future__ import annotations

import math

import pytest

# ── income_statement ──────────────────────────────────────────────────────────
from auto_valuation.model.income_statement import (
    build_ebit_margin_forecast,
    build_revenue_forecast,
    compute_nopat,
    compute_ufcf,
    historical_da_pct,
    historical_ebit_margin,
    historical_revenue_cagr,
    infer_revenue_lifecycle_stage,
    normalise_tax_rate,
    revenue_growth_fade_schedule,
)

# ── working_capital ───────────────────────────────────────────────────────────
from auto_valuation.model.working_capital import (
    compute_cwc_days,
    compute_dio,
    compute_dpo,
    compute_dso,
    compute_nowc_from_bs,
    compute_nowc_from_days,
)

# ── dilution ──────────────────────────────────────────────────────────────────
from auto_valuation.model.dilution import (
    add_rsu_dilution,
    compute_fully_diluted_shares,
    compute_price_per_share,
    convertible_dilution,
    treasury_stock_method,
)

# ── debt ──────────────────────────────────────────────────────────────────────
from auto_valuation.model.debt import (
    build_debt_schedule,
    compute_debt_to_equity,
    compute_interest_coverage,
    compute_net_debt_to_ebitda,
    historical_cost_of_debt,
)

# ── balance_sheet ─────────────────────────────────────────────────────────────
from auto_valuation.model.balance_sheet import (
    build_capex_forecast,
    build_ppe_rollforward,
    compute_invested_capital,
    compute_roic,
)

# ── terminal_value ────────────────────────────────────────────────────────────
from auto_valuation.forecast.terminal_value import (
    exit_multiple_tv,
    gordon_growth_tv,
    implied_terminal_growth,
    pv_terminal_value,
    tv_sensitivity_table,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1 — historical_revenue_cagr
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoricalRevenueCagr:
    def _stmts(self):
        return [
            {"calendarYear": "2023", "revenue": 60_000},
            {"calendarYear": "2022", "revenue": 55_000},
            {"calendarYear": "2021", "revenue": 50_000},
            {"calendarYear": "2020", "revenue": 46_000},
            {"calendarYear": "2019", "revenue": 40_000},
            {"calendarYear": "2018", "revenue": 36_000},
        ]

    def test_5year_cagr(self):
        # 36_000 → 60_000 over 5 years: (60/36)^(1/5) - 1 ≈ 10.76%
        cagr = historical_revenue_cagr(self._stmts(), years=5)
        assert cagr == pytest.approx((60_000 / 36_000) ** 0.2 - 1, rel=1e-4)

    def test_insufficient_data_returns_zero(self):
        stmts = [{"calendarYear": "2023", "revenue": 50_000}]
        assert historical_revenue_cagr(stmts, years=5) == pytest.approx(0.0)

    def test_zero_base_revenue_returns_zero(self):
        stmts = [
            {"calendarYear": "2023", "revenue": 50_000},
            {"calendarYear": "2022", "revenue": 0},
        ]
        assert historical_revenue_cagr(stmts, years=1) == pytest.approx(0.0)

    def test_unsorted_input_handled(self):
        stmts = [
            {"calendarYear": "2021", "revenue": 50_000},
            {"calendarYear": "2023", "revenue": 60_000},
            {"calendarYear": "2022", "revenue": 55_000},
        ]
        cagr = historical_revenue_cagr(stmts, years=2)
        assert cagr == pytest.approx((60_000 / 50_000) ** 0.5 - 1, rel=1e-4)


# ─────────────────────────────────────────────────────────────────────────────
# 2 — build_revenue_forecast
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildRevenueForecast:
    def test_returns_correct_length(self):
        result = build_revenue_forecast(100_000, 0.10, 0.03, forecast_years=10)
        assert len(result) == 10

    def test_near_term_years_use_near_term_growth(self):
        revenues = build_revenue_forecast(100_000, 0.10, 0.03, forecast_years=5, fade_start_year=3)
        # Year 1: 100_000 * 1.10 = 110_000
        assert revenues[0] == pytest.approx(110_000)
        # Year 2: 110_000 * 1.10 = 121_000
        assert revenues[1] == pytest.approx(121_000)
        # Year 3: 121_000 * 1.10 = 133_100
        assert revenues[2] == pytest.approx(133_100)

    def test_revenues_are_strictly_positive(self):
        result = build_revenue_forecast(50_000, 0.08, 0.02, forecast_years=10)
        assert all(r > 0 for r in result)

    def test_revenue_monotonically_positive(self):
        result = build_revenue_forecast(50_000, 0.08, 0.02, forecast_years=10)
        assert all(result[i] < result[i + 1] for i in range(len(result) - 1))

    def test_zero_growth_flat_revenue(self):
        result = build_revenue_forecast(50_000, 0.0, 0.0, forecast_years=5)
        assert all(r == pytest.approx(50_000) for r in result)

    def test_terminal_growth_applied_at_end(self):
        revenues = build_revenue_forecast(100_000, 0.10, 0.03, forecast_years=10, fade_start_year=3)
        # Last year applies terminal growth 0.03 (linear fade reaches 0.03 at year 10)
        growth_yr10 = revenues[9] / revenues[8] - 1
        assert growth_yr10 == pytest.approx(0.03, abs=1e-4)

    def test_lifecycle_stage_inference(self):
        assert infer_revenue_lifecycle_stage(500, 0.22, 0.03) == "hypergrowth"
        assert infer_revenue_lifecycle_stage(90_000, 0.04, 0.025) == "mature"

    def test_mature_lifecycle_fades_earlier_than_growth(self):
        mature = revenue_growth_fade_schedule(0.08, 0.03, forecast_years=7, fade_start_year=3, lifecycle_stage="mature")
        growth = revenue_growth_fade_schedule(0.08, 0.03, forecast_years=7, fade_start_year=3, lifecycle_stage="growth")
        assert mature[1] < growth[1]
        assert mature[-1] == pytest.approx(0.03)


# ─────────────────────────────────────────────────────────────────────────────
# 3 — build_ebit_margin_forecast
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildEbitMarginForecast:
    def test_returns_correct_length(self):
        result = build_ebit_margin_forecast(0.12, 0.18, forecast_years=10)
        assert len(result) == 10

    def test_first_year_not_base_margin(self):
        # Year 1 is base + 1/fade_years * (target - base)
        result = build_ebit_margin_forecast(0.10, 0.20, forecast_years=10, fade_years=10)
        assert result[0] == pytest.approx(0.10 + (0.20 - 0.10) * (1 / 10), rel=1e-4)

    def test_last_year_equals_target_margin(self):
        result = build_ebit_margin_forecast(0.10, 0.20, forecast_years=10, fade_years=7)
        assert result[9] == pytest.approx(0.20)

    def test_holds_target_after_fade(self):
        result = build_ebit_margin_forecast(0.10, 0.20, forecast_years=10, fade_years=5)
        # Years 6-10 should all equal target
        for m in result[5:]:
            assert m == pytest.approx(0.20)

    def test_margin_monotonically_increases(self):
        result = build_ebit_margin_forecast(0.10, 0.20, forecast_years=10, fade_years=7)
        for i in range(len(result) - 1):
            assert result[i] <= result[i + 1] + 1e-10


# ─────────────────────────────────────────────────────────────────────────────
# 4 — historical_ebit_margin
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoricalEbitMargin:
    def test_returns_median(self):
        stmts = [
            {"revenue": 100, "ebit": 12},   # 12%
            {"revenue": 100, "ebit": 10},   # 10%
            {"revenue": 100, "ebit": 8},    # 8%
        ]
        # Median of [12%, 10%, 8%] = 10%
        assert historical_ebit_margin(stmts) == pytest.approx(0.10)

    def test_uses_ebit_normalized_when_available(self):
        stmts = [{"revenue": 100, "ebit": 8, "ebit_normalized": 12}]
        assert historical_ebit_margin(stmts) == pytest.approx(0.12)

    def test_skips_zero_revenue(self):
        stmts = [
            {"revenue": 0, "ebit": 10},
            {"revenue": 100, "ebit": 10},
        ]
        assert historical_ebit_margin(stmts) == pytest.approx(0.10)

    def test_empty_returns_zero(self):
        assert historical_ebit_margin([]) == pytest.approx(0.0)

    def test_use_normalized_false_uses_raw_ebit(self):
        stmts = [{"revenue": 100, "ebit": 8, "ebit_normalized": 12}]
        assert historical_ebit_margin(stmts, use_normalized=False) == pytest.approx(0.08)


# ─────────────────────────────────────────────────────────────────────────────
# 5 — historical_da_pct
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoricalDaPct:
    def test_basic_median(self):
        stmts = [
            {"revenue": 100, "da": 4},   # 4%
            {"revenue": 100, "da": 3},   # 3%
            {"revenue": 100, "da": 5},   # 5%
        ]
        assert historical_da_pct(stmts) == pytest.approx(0.04)

    def test_empty_returns_fallback(self):
        assert historical_da_pct([]) == pytest.approx(0.03)

    def test_zero_revenue_skipped(self):
        stmts = [
            {"revenue": 0, "da": 5},
            {"revenue": 100, "da": 4},
        ]
        assert historical_da_pct(stmts) == pytest.approx(0.04)


# ─────────────────────────────────────────────────────────────────────────────
# 6 — normalise_tax_rate
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseTaxRate:
    def test_averages_effective_rates(self):
        stmts = [
            {"ebit": 10_000, "pretaxIncome": 9_500, "incomeTaxExpense": 2_280},  # 24%
            {"ebit": 10_000, "pretaxIncome": 9_500, "incomeTaxExpense": 1_900},  # 20%
        ]
        rate = normalise_tax_rate(stmts)
        assert rate == pytest.approx(0.22, rel=0.01)

    def test_insufficient_data_returns_statutory(self):
        stmts = [{"ebit": 10_000, "pretaxIncome": 9_000, "incomeTaxExpense": 2_000}]  # only 1
        assert normalise_tax_rate(stmts) == pytest.approx(0.21)

    def test_negative_ebt_year_excluded(self):
        stmts = [
            {"pretaxIncome": -500, "incomeTaxExpense": 100},  # negative EBT — excluded
            {"pretaxIncome": 9_000, "incomeTaxExpense": 2_250},  # 25%
            {"pretaxIncome": 9_500, "incomeTaxExpense": 1_900},  # 20%
        ]
        rate = normalise_tax_rate(stmts)
        assert rate == pytest.approx(0.225)

    def test_outlier_rate_above_max_excluded(self):
        stmts = [
            {"pretaxIncome": 1_000, "incomeTaxExpense": 500},   # 50% → excluded (>40%)
            {"pretaxIncome": 9_000, "incomeTaxExpense": 2_160},  # 24%
            {"pretaxIncome": 9_500, "incomeTaxExpense": 2_000},  # 21.05%
        ]
        rate = normalise_tax_rate(stmts)
        assert 0.21 < rate < 0.25

    def test_result_clamped_within_bounds(self):
        stmts = [
            {"pretaxIncome": 9_000, "incomeTaxExpense": 2_250},
            {"pretaxIncome": 9_500, "incomeTaxExpense": 1_995},
        ]
        rate = normalise_tax_rate(stmts)
        assert 0.05 <= rate <= 0.40


# ─────────────────────────────────────────────────────────────────────────────
# 7 — compute_nopat / compute_ufcf
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeNopat:
    def test_basic_nopat(self):
        assert compute_nopat(10_000, 0.25) == pytest.approx(7_500)

    def test_zero_tax_rate(self):
        assert compute_nopat(10_000, 0.0) == pytest.approx(10_000)

    def test_100pct_tax(self):
        assert compute_nopat(10_000, 1.0) == pytest.approx(0.0)


class TestComputeUfcf:
    def test_basic_ufcf(self):
        # NOPAT=7_500, da=2_000, capex=2_500, dnowc=300
        # UFCF = 7_500 + 2_000 - 2_500 - 300 = 6_700
        result = compute_ufcf(ebit=10_000, tax_rate=0.25, da=2_000, capex=2_500, delta_nowc=300)
        assert result == pytest.approx(6_700)

    def test_negative_dnowc_increases_ufcf(self):
        # Releasing working capital is a cash inflow
        result = compute_ufcf(ebit=10_000, tax_rate=0.25, da=2_000, capex=2_500, delta_nowc=-500)
        assert result == pytest.approx(7_500)

    def test_zero_da_zero_capex(self):
        result = compute_ufcf(ebit=10_000, tax_rate=0.21, da=0, capex=0, delta_nowc=0)
        assert result == pytest.approx(7_900)


# ─────────────────────────────────────────────────────────────────────────────
# 8 — compute_dso / compute_dio / compute_dpo / compute_cwc_days
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkingCapitalDays:
    def test_dso_basic(self):
        # AR=5_000, revenue=50_000 → DSO = 5_000*365/50_000 = 36.5
        assert compute_dso(5_000, 50_000) == pytest.approx(36.5)

    def test_dso_zero_revenue_returns_zero(self):
        assert compute_dso(5_000, 0) == pytest.approx(0.0)

    def test_dio_basic(self):
        # Inventory=2_000, COGS=30_000 → DIO = 2_000*365/30_000 ≈ 24.33
        assert compute_dio(2_000, 30_000) == pytest.approx(2_000 * 365 / 30_000)

    def test_dpo_basic(self):
        # AP=3_000, COGS=30_000 → DPO = 3_000*365/30_000 ≈ 36.5
        assert compute_dpo(3_000, 30_000) == pytest.approx(36.5)

    def test_cwc_days_formula(self):
        dso, dio, dpo = 36.5, 24.33, 36.5
        assert compute_cwc_days(dso, dio, dpo) == pytest.approx(dso + dio - dpo)


# ─────────────────────────────────────────────────────────────────────────────
# 9 — compute_nowc_from_bs / compute_nowc_from_days
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeNowc:
    def test_nowc_from_bs_basic(self):
        bs = {"accounts_receivable": 5_000, "inventory": 2_000, "accounts_payable": 3_000}
        assert compute_nowc_from_bs(bs) == pytest.approx(4_000)

    def test_nowc_can_be_negative(self):
        # Amazon-style: large AP, small AR+Inv
        bs = {"accounts_receivable": 1_000, "inventory": 500, "accounts_payable": 8_000}
        assert compute_nowc_from_bs(bs) < 0

    def test_nowc_from_bs_fmp_field_names(self):
        bs = {"netReceivables": 5_000, "inventory": 2_000, "accountPayables": 3_000}
        assert compute_nowc_from_bs(bs) == pytest.approx(4_000)

    def test_nowc_from_days_basic(self):
        # DSO=36.5, revenue=50_000 → AR = 5_000
        # DIO=24.33, COGS=30_000 → Inv ≈ 2_000
        # DPO=36.5, COGS=30_000 → AP = 3_000
        # NOWC = 5_000 + 2_000 - 3_000 = 4_000
        nowc = compute_nowc_from_days(
            revenue=50_000, cogs=30_000,
            dso=36.5,
            dio=2_000 * 365 / 30_000,
            dpo=36.5,
        )
        assert nowc == pytest.approx(4_000, rel=0.01)

    def test_nowc_from_days_zero_cogs(self):
        # Service company: DIO=0, DPO=0 → NOWC = AR only
        nowc = compute_nowc_from_days(revenue=50_000, cogs=0, dso=36.5, dio=0, dpo=0)
        assert nowc == pytest.approx(5_000, rel=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 10 — treasury_stock_method
# ─────────────────────────────────────────────────────────────────────────────

class TestTreasuryStockMethod:
    def test_in_the_money_dilution(self):
        # 10MM options, strike=50, price=100 → net new = 10 * (1-50/100) = 5MM
        result = treasury_stock_method(500.0, 10.0, 50.0, 100.0)
        assert result == pytest.approx(505.0)

    def test_out_of_money_no_dilution(self):
        result = treasury_stock_method(500.0, 10.0, 120.0, 100.0)
        assert result == pytest.approx(500.0)

    def test_at_the_money_no_dilution(self):
        result = treasury_stock_method(500.0, 10.0, 100.0, 100.0)
        assert result == pytest.approx(500.0)

    def test_zero_options_no_dilution(self):
        result = treasury_stock_method(500.0, 0.0, 50.0, 100.0)
        assert result == pytest.approx(500.0)

    def test_zero_price_no_dilution(self):
        result = treasury_stock_method(500.0, 10.0, 50.0, 0.0)
        assert result == pytest.approx(500.0)


# ─────────────────────────────────────────────────────────────────────────────
# 11 — add_rsu_dilution
# ─────────────────────────────────────────────────────────────────────────────

class TestAddRsuDilution:
    def test_basic_rsu_dilution(self):
        # 10MM RSUs, 40% tax withhold → 6MM net shares
        result = add_rsu_dilution(500.0, 10.0, 0.40)
        assert result == pytest.approx(506.0)

    def test_zero_rsus_no_change(self):
        result = add_rsu_dilution(500.0, 0.0)
        assert result == pytest.approx(500.0)

    def test_custom_withhold_pct(self):
        result = add_rsu_dilution(500.0, 10.0, 0.50)
        assert result == pytest.approx(505.0)


# ─────────────────────────────────────────────────────────────────────────────
# 12 — convertible_dilution
# ─────────────────────────────────────────────────────────────────────────────

class TestConvertibleDilution:
    def test_if_converted_adds_all_shares(self):
        # Face=500MM, conv_price=50, price=100 → in-the-money → 10MM new shares
        result = convertible_dilution(500.0, 500.0, 50.0, 100.0, method="if_converted")
        assert result == pytest.approx(510.0)

    def test_out_of_money_no_dilution(self):
        result = convertible_dilution(500.0, 500.0, 120.0, 100.0, method="if_converted")
        assert result == pytest.approx(500.0)

    def test_tsm_method_net_shares(self):
        # Face=500MM, conv=50, price=100 → 10MM potential, net=10*(1-50/100)=5MM
        result = convertible_dilution(500.0, 500.0, 50.0, 100.0, method="tsm")
        assert result == pytest.approx(505.0)

    def test_zero_face_no_dilution(self):
        result = convertible_dilution(500.0, 0.0, 50.0, 100.0)
        assert result == pytest.approx(500.0)


# ─────────────────────────────────────────────────────────────────────────────
# 13 — compute_fully_diluted_shares
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeFullyDilutedShares:
    def test_basic_shares_only(self):
        r = compute_fully_diluted_shares(500.0, 100.0)
        assert r["fully_diluted_mm"] == pytest.approx(500.0)
        assert r["total_dilution_mm"] == pytest.approx(0.0)

    def test_options_dilution_included(self):
        r = compute_fully_diluted_shares(
            500.0, 100.0,
            options_outstanding_mm=10.0, options_avg_strike=50.0,
        )
        # 10 * (1 - 50/100) = 5 net new
        assert r["after_options"] == pytest.approx(505.0)
        assert r["fully_diluted_mm"] == pytest.approx(505.0)

    def test_rsus_dilution_included(self):
        r = compute_fully_diluted_shares(
            500.0, 100.0,
            unvested_rsus_mm=10.0, rsu_tax_withhold_pct=0.40,
        )
        assert r["after_rsus"] == pytest.approx(506.0)

    def test_all_securities_combined(self):
        r = compute_fully_diluted_shares(
            500.0, 100.0,
            options_outstanding_mm=10.0, options_avg_strike=50.0,  # +5MM
            unvested_rsus_mm=10.0, rsu_tax_withhold_pct=0.40,      # +6MM
            convertible_face_mm=500.0, convertible_price=50.0,      # +10MM if-converted
        )
        # 500 + 5 + 6 + 10 = 521MM
        assert r["fully_diluted_mm"] == pytest.approx(521.0)
        assert r["total_dilution_mm"] == pytest.approx(21.0)

    def test_result_contains_all_keys(self):
        r = compute_fully_diluted_shares(500.0, 100.0)
        for key in ("basic_shares", "after_options", "after_warrants",
                    "after_rsus", "after_convertibles", "fully_diluted_mm",
                    "total_dilution_mm"):
            assert key in r


# ─────────────────────────────────────────────────────────────────────────────
# 14 — compute_price_per_share
# ─────────────────────────────────────────────────────────────────────────────

class TestComputePricePerShare:
    def test_basic(self):
        # 50_000MM equity / 500MM shares = $100/share
        assert compute_price_per_share(50_000.0, 500.0) == pytest.approx(100.0)

    def test_zero_shares_returns_zero(self):
        assert compute_price_per_share(50_000.0, 0.0) == pytest.approx(0.0)

    def test_small_company(self):
        assert compute_price_per_share(1_000.0, 100.0) == pytest.approx(10.0)


# ─────────────────────────────────────────────────────────────────────────────
# 15 — historical_cost_of_debt
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoricalCostOfDebt:
    def _data(self):
        is_ = [
            {"calendarYear": "2023", "interestExpense": 400},
            {"calendarYear": "2022", "interestExpense": 380},
        ]
        bs = [
            {"calendarYear": "2023", "longTermDebt": 8_000, "shortTermDebt": 0},
            {"calendarYear": "2022", "longTermDebt": 7_600, "shortTermDebt": 0},
        ]
        return is_, bs

    def test_computes_from_interest_and_debt(self):
        is_, bs = self._data()
        rate = historical_cost_of_debt(is_, bs)
        # year1: 400/8_000=5%; year2: 380/7_600=5% → avg=5%
        assert rate == pytest.approx(0.05)

    def test_no_data_returns_fallback(self):
        rate = historical_cost_of_debt([], [], fallback=0.055)
        assert rate == pytest.approx(0.055)

    def test_clamped_to_max_15pct(self):
        # Abnormally high interest expense → clamped at 15%
        is_ = [{"calendarYear": "2023", "interestExpense": 5_000}]
        bs  = [{"calendarYear": "2023", "longTermDebt": 10_000, "shortTermDebt": 0}]
        rate = historical_cost_of_debt(is_, bs)
        assert rate <= 0.15


# ─────────────────────────────────────────────────────────────────────────────
# 16 — build_debt_schedule
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildDebtSchedule:
    def test_returns_correct_length(self):
        records = build_debt_schedule(10_000, 0.05, forecast_years=5)
        assert len(records) == 5

    def test_bullet_structure_debt_unchanged(self):
        records = build_debt_schedule(10_000, 0.05, repayment_schedule=None, forecast_years=5)
        for r in records:
            assert r["closing_debt"] == pytest.approx(10_000)

    def test_repayment_schedule_reduces_debt(self):
        records = build_debt_schedule(10_000, 0.05, repayment_schedule=[1_000] * 5)
        assert records[-1]["closing_debt"] == pytest.approx(5_000)

    def test_interest_expense_on_opening_debt(self):
        records = build_debt_schedule(10_000, 0.05, forecast_years=3)
        # Year 1 interest = 10_000 * 0.05 = 500
        assert records[0]["interest_expense"] == pytest.approx(500.0)

    def test_debt_cannot_go_negative(self):
        records = build_debt_schedule(1_000, 0.05, repayment_schedule=[5_000] * 5)
        for r in records:
            assert r["closing_debt"] >= 0.0

    def test_year_index_starts_at_1(self):
        records = build_debt_schedule(10_000, 0.05, forecast_years=3)
        assert records[0]["year_index"] == 1
        assert records[2]["year_index"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 17 — leverage metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestLeverageMetrics:
    def test_debt_to_equity_basic(self):
        assert compute_debt_to_equity(4_000, 8_000) == pytest.approx(0.5)

    def test_debt_to_equity_zero_equity(self):
        assert compute_debt_to_equity(4_000, 0) == pytest.approx(0.0)

    def test_net_debt_to_ebitda_basic(self):
        assert compute_net_debt_to_ebitda(5_000, 10_000) == pytest.approx(0.5)

    def test_net_debt_to_ebitda_zero_ebitda(self):
        assert compute_net_debt_to_ebitda(5_000, 0) == pytest.approx(0.0)

    def test_interest_coverage_basic(self):
        assert compute_interest_coverage(10_000, 500) == pytest.approx(20.0)

    def test_interest_coverage_zero_interest(self):
        assert compute_interest_coverage(10_000, 0) == math.inf


# ─────────────────────────────────────────────────────────────────────────────
# 18 — build_capex_forecast
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildCapexForecast:
    def test_basic(self):
        revenues = [100_000, 110_000, 121_000]
        result = build_capex_forecast(revenues, 0.04)
        assert result == pytest.approx([4_000, 4_400, 4_840])

    def test_zero_pct(self):
        result = build_capex_forecast([100_000, 110_000], 0.0)
        assert result == [0.0, 0.0]

    def test_length_matches_revenues(self):
        revs = list(range(10_000, 20_000, 1_000))
        result = build_capex_forecast(revs, 0.05)
        assert len(result) == len(revs)


# ─────────────────────────────────────────────────────────────────────────────
# 19 — build_ppe_rollforward
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPpeRollforward:
    def test_basic_rollforward(self):
        records = build_ppe_rollforward(20_000, [4_000, 4_400], [2_500, 2_600])
        # Year 1: 20_000 + 4_000 - 2_500 = 21_500
        assert records[0]["closing_ppe"] == pytest.approx(21_500)
        # Year 2: 21_500 + 4_400 - 2_600 = 23_300
        assert records[1]["closing_ppe"] == pytest.approx(23_300)

    def test_year_index_starts_at_1(self):
        records = build_ppe_rollforward(20_000, [4_000], [2_500])
        assert records[0]["year_index"] == 1

    def test_closing_feeds_next_opening(self):
        records = build_ppe_rollforward(20_000, [4_000, 4_400], [2_500, 2_600])
        assert records[1]["opening_ppe"] == pytest.approx(records[0]["closing_ppe"])

    def test_length_matches_schedule(self):
        records = build_ppe_rollforward(20_000, [4_000] * 5, [2_500] * 5)
        assert len(records) == 5


# ─────────────────────────────────────────────────────────────────────────────
# 20 — compute_invested_capital / compute_roic
# ─────────────────────────────────────────────────────────────────────────────

class TestInvestedCapital:
    def _bs(self):
        return {
            "shareholders_equity": 15_000,
            "long_term_debt": 8_000,
            "short_term_debt": 500,
            "cash": 3_000,
            "st_investments": 500,
            "nci": 300,
        }

    def test_basic_invested_capital(self):
        # equity=15_000, net_debt=8_000+500-3_000-500=5_000, nci=300 → IC=20_300
        ic = compute_invested_capital(self._bs())
        assert ic == pytest.approx(20_300)

    def test_zero_debt_equals_equity_plus_nci(self):
        bs = {"shareholders_equity": 20_000, "cash": 0, "nci": 200}
        ic = compute_invested_capital(bs)
        assert ic == pytest.approx(20_200)

    def test_compute_roic_basic(self):
        # NOPAT=3_000, IC=20_000 → ROIC=15%
        assert compute_roic(3_000, 20_000) == pytest.approx(0.15)

    def test_compute_roic_zero_ic(self):
        assert compute_roic(3_000, 0) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 21 — gordon_growth_tv
# ─────────────────────────────────────────────────────────────────────────────

class TestGordonGrowthTv:
    def test_basic_gordon_growth(self):
        # TV = 8_000 / (0.10 - 0.03) = 114_285.71
        tv = gordon_growth_tv(8_000, 0.10, 0.03)
        assert tv == pytest.approx(114_285.71, rel=1e-4)

    def test_raises_when_wacc_leq_g(self):
        with pytest.raises(ValueError):
            gordon_growth_tv(8_000, 0.03, 0.03)

    def test_raises_when_wacc_less_than_g(self):
        with pytest.raises(ValueError):
            gordon_growth_tv(8_000, 0.02, 0.04)

    def test_higher_wacc_lower_tv(self):
        tv_low_wacc  = gordon_growth_tv(8_000, 0.09, 0.03)
        tv_high_wacc = gordon_growth_tv(8_000, 0.12, 0.03)
        assert tv_low_wacc > tv_high_wacc

    def test_higher_growth_higher_tv(self):
        tv_low_g  = gordon_growth_tv(8_000, 0.10, 0.02)
        tv_high_g = gordon_growth_tv(8_000, 0.10, 0.04)
        assert tv_high_g > tv_low_g


# ─────────────────────────────────────────────────────────────────────────────
# 22 — exit_multiple_tv
# ─────────────────────────────────────────────────────────────────────────────

class TestExitMultipleTv:
    def test_basic_exit_multiple(self):
        assert exit_multiple_tv(10_000, 12.0) == pytest.approx(120_000)

    def test_zero_ebitda_zero_tv(self):
        assert exit_multiple_tv(0, 12.0) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 23 — pv_terminal_value
# ─────────────────────────────────────────────────────────────────────────────

class TestPvTerminalValue:
    def test_mid_year_convention(self):
        tv = 100_000
        pv = pv_terminal_value(tv, 0.10, 10, mid_year_convention=True)
        assert pv == pytest.approx(tv / (1.10 ** 10.5), rel=1e-6)

    def test_end_of_year_convention(self):
        tv = 100_000
        pv = pv_terminal_value(tv, 0.10, 10, mid_year_convention=False)
        assert pv == pytest.approx(tv / (1.10 ** 10), rel=1e-6)

    def test_higher_wacc_lower_pv(self):
        pv_low  = pv_terminal_value(100_000, 0.08, 10)
        pv_high = pv_terminal_value(100_000, 0.12, 10)
        assert pv_low > pv_high

    def test_longer_forecast_lower_pv(self):
        pv_10yr = pv_terminal_value(100_000, 0.10, 10)
        pv_15yr = pv_terminal_value(100_000, 0.10, 15)
        assert pv_10yr > pv_15yr


# ─────────────────────────────────────────────────────────────────────────────
# 24 — implied_terminal_growth / tv_sensitivity_table
# ─────────────────────────────────────────────────────────────────────────────

class TestImpliedTerminalGrowth:
    def test_basic_back_solve(self):
        # TV = UFCF / (wacc - g) → g = wacc - UFCF/TV
        tv = gordon_growth_tv(8_000, 0.10, 0.03)
        g = implied_terminal_growth(tv, 8_000, 0.10)
        assert g == pytest.approx(0.03, abs=1e-6)

    def test_zero_tv_returns_zero(self):
        assert implied_terminal_growth(0.0, 8_000, 0.10) == pytest.approx(0.0)


class TestTvSensitivityTable:
    def test_returns_dict_of_correct_size(self):
        wacc_range   = [0.09, 0.10, 0.11]
        growth_range = [0.02, 0.03, 0.04]
        result = tv_sensitivity_table(8_000, wacc_range, growth_range, 10)
        # Excludes wacc==g combos; 3×3=9 minus 0 invalid (none equal here)
        assert len(result) == 9

    def test_invalid_wacc_leq_g_excluded(self):
        wacc_range   = [0.03, 0.10]
        growth_range = [0.03, 0.04]
        result = tv_sensitivity_table(8_000, wacc_range, growth_range, 10)
        # (0.03, 0.03) invalid; (0.03, 0.04) invalid → 2 valid entries (both for 0.10)
        assert all(w > g for (w, g) in result)

    def test_pv_values_are_positive(self):
        result = tv_sensitivity_table(8_000, [0.09, 0.10], [0.02, 0.03], 10)
        assert all(v > 0 for v in result.values())

    def test_keys_are_wacc_g_tuples(self):
        result = tv_sensitivity_table(8_000, [0.10], [0.03], 10)
        key = next(iter(result))
        assert isinstance(key, tuple) and len(key) == 2
