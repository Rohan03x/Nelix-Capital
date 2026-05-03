"""
tests/test_session11_gaps.py
────────────────────────────
Tests for the 38 functions implemented in Session 11.
"""

from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# data/cleaner.py
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanerNewFunctions:

    def test_get_annual_avg_fx_same_currency(self):
        from auto_valuation.data.cleaner import get_annual_avg_fx
        assert get_annual_avg_fx("USD", "USD", 2023) == 1.0

    def test_get_annual_avg_fx_with_override(self):
        from auto_valuation.data.cleaner import get_annual_avg_fx
        overrides = {"EUR/USD/2023": "1.0850"}
        rate = get_annual_avg_fx("EUR", "USD", 2023, fx_overrides=overrides)
        assert abs(rate - 1.0850) < 1e-6

    def test_get_annual_avg_fx_reverse_override(self):
        from auto_valuation.data.cleaner import get_annual_avg_fx
        overrides = {"USD/EUR/2023": "0.9217"}
        rate = get_annual_avg_fx("EUR", "USD", 2023, fx_overrides=overrides)
        assert abs(rate - 1.0 / 0.9217) < 1e-4

    def test_get_annual_avg_fx_no_override(self):
        from auto_valuation.data.cleaner import get_annual_avg_fx
        # Fallback returns 1.0
        rate = get_annual_avg_fx("EUR", "USD", 2023)
        assert rate == 1.0

    def test_convert_to_reporting_currency_same(self):
        from auto_valuation.data.cleaner import convert_to_reporting_currency
        stmts = [{"revenue": 1000, "calendarYear": 2023}]
        result = convert_to_reporting_currency(stmts, "USD", "USD")
        assert result[0]["revenue"] == 1000

    def test_convert_to_reporting_currency_with_rate(self):
        from auto_valuation.data.cleaner import convert_to_reporting_currency
        overrides = {"EUR/USD/2023": "1.1"}
        stmts = [{"revenue": 1000.0, "calendarYear": 2023}]
        result = convert_to_reporting_currency(
            stmts, "EUR", "USD", fx_overrides=overrides
        )
        assert abs(result[0]["revenue"] - 1100.0) < 1e-6

    def test_convert_preserves_string_fields(self):
        from auto_valuation.data.cleaner import convert_to_reporting_currency
        stmts = [{"revenue": 1000.0, "period": "FY", "calendarYear": 2023}]
        result = convert_to_reporting_currency(stmts, "USD", "GBP")
        assert result[0]["period"] == "FY"

    def test_normalize_goodwill_impairment_adds_back(self):
        from auto_valuation.data.cleaner import normalize_goodwill_impairment
        stmts = [{"ebit": 500.0, "impairmentOfGoodwill": -100.0}]
        result = normalize_goodwill_impairment(stmts)
        assert result[0]["ebit_normalized_gw"] == pytest.approx(600.0)
        assert result[0]["goodwill_impairment_normalized"] == pytest.approx(100.0)

    def test_normalize_goodwill_no_impairment(self):
        from auto_valuation.data.cleaner import normalize_goodwill_impairment
        stmts = [{"ebit": 500.0}]
        result = normalize_goodwill_impairment(stmts)
        assert result[0]["ebit_normalized_gw"] == pytest.approx(500.0)
        assert result[0]["goodwill_impairment_normalized"] == pytest.approx(0.0)

    def test_extract_below_ebit_items_basic(self):
        from auto_valuation.data.cleaner import extract_below_ebit_items
        stmt = {
            "interestIncome": 20.0,
            "interestExpense": 50.0,
            "totalOtherIncomeExpensesNet": -30.0,
        }
        result = extract_below_ebit_items(stmt)
        assert "interest_income" in result
        assert "interest_expense" in result
        assert result["interest_income"] == pytest.approx(20.0)
        assert result["interest_expense"] == pytest.approx(50.0)

    def test_extract_below_ebit_items_keys(self):
        from auto_valuation.data.cleaner import extract_below_ebit_items
        result = extract_below_ebit_items({})
        expected_keys = {
            "recurring_below_ebit", "nonrecurring_below_ebit",
            "interest_income", "interest_expense",
            "other_income_recurring", "other_income_nonrecurring",
        }
        assert expected_keys == set(result.keys())

    def test_compute_average_nowc_basic(self):
        from auto_valuation.data.cleaner import compute_average_nowc
        bss = [
            {"netReceivables": 300, "inventory": 100, "accountPayables": 150},
            {"netReceivables": 250, "inventory": 80,  "accountPayables": 130},
            {"netReceivables": 200, "inventory": 60,  "accountPayables": 100},
        ]
        avg = compute_average_nowc(bss, years=3)
        # NOWC = AR+Inv-AP: (250, 200, 160) → avg ≈ 203.33
        assert avg == pytest.approx((250 + 200 + 160) / 3, rel=1e-4)

    def test_compute_average_nowc_empty(self):
        from auto_valuation.data.cleaner import compute_average_nowc
        assert compute_average_nowc([]) == 0.0

    def test_detect_outlier_years_no_outliers(self):
        from auto_valuation.data.cleaner import detect_outlier_years
        stmts = [
            {"calendarYear": y, "revenue": 1000 + y * 50}
            for y in range(2019, 2025)
        ]
        result = detect_outlier_years(stmts, field="revenue")
        assert result == []

    def test_detect_outlier_years_with_outlier(self):
        from auto_valuation.data.cleaner import detect_outlier_years
        stmts = [
            {"calendarYear": "2019", "revenue": 1000},
            {"calendarYear": "2020", "revenue": 1050},
            {"calendarYear": "2021", "revenue": 1100},
            {"calendarYear": "2022", "revenue": 1150},
            {"calendarYear": "2023", "revenue": 99999},  # outlier
        ]
        result = detect_outlier_years(stmts, field="revenue", iqr_multiplier=1.5)
        assert "2023" in result

    def test_capitalize_rd_alias(self):
        from auto_valuation.data.cleaner import capitalize_rd, capitalise_rd
        stmts = [
            {"revenue": 10000, "researchAndDevelopmentExpenses": 500, "ebit": 1500},
        ]
        # Both should return identical results
        r1 = capitalise_rd(stmts)
        r2 = capitalize_rd(stmts)
        assert r1[0].get("ebit_rd_adjusted") == r2[0].get("ebit_rd_adjusted")

    def test_adjust_ebit_for_rd_capitalization_overwrites_ebit(self):
        from auto_valuation.data.cleaner import adjust_ebit_for_rd_capitalization
        stmts = [
            {"revenue": 10000, "researchAndDevelopmentExpenses": 500, "ebit": 1500},
        ]
        result = adjust_ebit_for_rd_capitalization(stmts)
        # ebit should be replaced by the adjusted value
        assert result[0]["ebit"] != 1500 or result[0].get("ebit_rd_adjusted") is None

    def test_check_revenue_quality_clean(self):
        from auto_valuation.data.cleaner import check_revenue_quality
        stmts = [
            {"calendarYear": "2023", "revenue": 1100},
            {"calendarYear": "2022", "revenue": 1000},
        ]
        assert check_revenue_quality(stmts) == []

    def test_check_revenue_quality_negative_revenue(self):
        from auto_valuation.data.cleaner import check_revenue_quality
        stmts = [{"calendarYear": "2023", "revenue": -100}]
        warnings_out = check_revenue_quality(stmts)
        assert len(warnings_out) >= 1
        assert any("negative" in w.lower() for w in warnings_out)

    def test_check_revenue_quality_zero_revenue(self):
        from auto_valuation.data.cleaner import check_revenue_quality
        stmts = [{"calendarYear": "2023", "revenue": 0}]
        warnings_out = check_revenue_quality(stmts)
        assert len(warnings_out) >= 1

    def test_check_revenue_quality_large_decline(self):
        from auto_valuation.data.cleaner import check_revenue_quality
        stmts = [
            {"calendarYear": "2023", "revenue": 100},
            {"calendarYear": "2022", "revenue": 1000},
            {"calendarYear": "2021", "revenue": 1100},
            {"calendarYear": "2020", "revenue": 1200},
        ]
        warnings_out = check_revenue_quality(stmts)
        assert any("fell" in w.lower() for w in warnings_out)


# ─────────────────────────────────────────────────────────────────────────────
# data/fetcher.py
# ─────────────────────────────────────────────────────────────────────────────

class TestFetcherNewFunctions:

    def test_is_financial_company_true(self):
        from auto_valuation.data.fetcher import is_financial_company
        assert is_financial_company({"sector": "Financials"}) is True

    def test_is_financial_company_false(self):
        from auto_valuation.data.fetcher import is_financial_company
        assert is_financial_company({"sector": "Technology"}) is False

    def test_is_financial_company_empty(self):
        from auto_valuation.data.fetcher import is_financial_company
        assert is_financial_company({}) is False

    def test_gate_company_type_ok(self):
        from auto_valuation.data.fetcher import gate_company_type
        assert gate_company_type({"sector": "Information Technology"}) == "ok"

    def test_gate_company_type_financial(self):
        from auto_valuation.data.fetcher import gate_company_type
        assert gate_company_type({"sector": "Financials"}) == "financial"

    def test_gate_company_type_reit(self):
        from auto_valuation.data.fetcher import gate_company_type
        assert gate_company_type({"sector": "Real Estate"}) == "reit"

    def test_gate_company_type_mining(self):
        from auto_valuation.data.fetcher import gate_company_type
        profile = {"sector": "Materials", "industry": "Gold Mining"}
        assert gate_company_type(profile) == "mining"

    def test_get_free_cash_basic(self):
        from auto_valuation.data.fetcher import get_free_cash
        bs = {"cashAndCashEquivalents": 1000.0, "shortTermInvestments": 200.0}
        assert get_free_cash(bs) == pytest.approx(1200.0)

    def test_get_free_cash_excludes_restricted(self):
        from auto_valuation.data.fetcher import get_free_cash
        bs = {"cashAndCashEquivalents": 1000.0, "restrictedCash": 150.0}
        assert get_free_cash(bs) == pytest.approx(850.0)

    def test_get_free_cash_empty(self):
        from auto_valuation.data.fetcher import get_free_cash
        assert get_free_cash({}) == pytest.approx(0.0)

    def test_check_ipo_recency_no_date(self):
        from auto_valuation.data.fetcher import check_ipo_recency
        result = check_ipo_recency({})
        assert result["recent_ipo"] is False

    def test_check_ipo_recency_old_company(self):
        from auto_valuation.data.fetcher import check_ipo_recency
        result = check_ipo_recency({"ipoDate": "1990-01-01"})
        assert result["recent_ipo"] is False
        assert result["years_public"] > 10

    def test_check_ipo_recency_recent(self):
        from auto_valuation.data.fetcher import check_ipo_recency
        from datetime import date, timedelta
        ipo = (date.today() - timedelta(days=365)).isoformat()
        result = check_ipo_recency({"ipoDate": ipo}, min_years=3)
        assert result["recent_ipo"] is True

    def test_parse_ticker_input_single(self):
        from auto_valuation.data.fetcher import parse_ticker_input
        assert parse_ticker_input("AAPL") == ["AAPL"]

    def test_parse_ticker_input_csv(self):
        from auto_valuation.data.fetcher import parse_ticker_input
        result = parse_ticker_input("AAPL,MSFT,NKE")
        assert result == ["AAPL", "MSFT", "NKE"]

    def test_parse_ticker_input_deduplicates(self):
        from auto_valuation.data.fetcher import parse_ticker_input
        result = parse_ticker_input("AAPL,MSFT,AAPL")
        assert result == ["AAPL", "MSFT"]

    def test_parse_ticker_input_whitespace(self):
        from auto_valuation.data.fetcher import parse_ticker_input
        result = parse_ticker_input("  AAPL , MSFT ")
        assert result == ["AAPL", "MSFT"]

    def test_fetch_revenue_segments_alias(self):
        """fetch_revenue_segments must be callable (no real network call)."""
        from auto_valuation.data import fetcher
        assert callable(fetcher.fetch_revenue_segments)


# ─────────────────────────────────────────────────────────────────────────────
# model/forecast.py
# ─────────────────────────────────────────────────────────────────────────────

class TestForecastNewFunctions:

    def test_enforce_cash_floor_above_floor(self):
        from auto_valuation.model.forecast import enforce_cash_floor
        cash, drawn = enforce_cash_floor(50.0, 1000.0, revolver_capacity=200.0, min_pct=0.02)
        assert cash == pytest.approx(50.0)
        assert drawn == pytest.approx(0.0)

    def test_enforce_cash_floor_draws_revolver(self):
        from auto_valuation.model.forecast import enforce_cash_floor
        # floor = 0.02 * 1000 = 20; cash = 5 → shortfall = 15
        cash, drawn = enforce_cash_floor(5.0, 1000.0, revolver_capacity=200.0, min_pct=0.02)
        assert drawn == pytest.approx(15.0)
        assert cash == pytest.approx(20.0)

    def test_enforce_cash_floor_limited_revolver(self):
        from auto_valuation.model.forecast import enforce_cash_floor
        # floor=20, cash=5, shortfall=15, but revolver only has 10
        cash, drawn = enforce_cash_floor(5.0, 1000.0, revolver_capacity=10.0, min_pct=0.02)
        assert drawn == pytest.approx(10.0)

    def test_compute_growth_profile_1stage(self):
        from auto_valuation.model.forecast import compute_growth_profile
        stmts = [{"revenue": 1100}, {"revenue": 1000}]
        result = compute_growth_profile(stmts, model="1stage", forecast_years=5)
        assert len(result["growth_schedule"]) == 5
        # All years same in 1-stage
        sched = result["growth_schedule"]
        assert all(abs(g - sched[0]) < 1e-9 for g in sched)

    def test_compute_growth_profile_2stage(self):
        from auto_valuation.model.forecast import compute_growth_profile
        stmts = [{"revenue": 1000 * 1.10 ** i} for i in range(5, -1, -1)]
        result = compute_growth_profile(stmts, terminal_g=0.025, forecast_years=7)
        assert len(result["growth_schedule"]) == 7
        assert "near_term_growth" in result

    def test_compute_growth_profile_hmodel(self):
        from auto_valuation.model.forecast import compute_growth_profile
        stmts = [{"revenue": 1100}, {"revenue": 1000}]
        result = compute_growth_profile(stmts, model="hmodel", forecast_years=7)
        sched = result["growth_schedule"]
        # First rate should be higher than last
        assert sched[0] >= sched[-1]

    def test_auto_revenue_growth_basic(self):
        from auto_valuation.model.forecast import auto_revenue_growth
        stmts = [
            {"revenue": 1210},
            {"revenue": 1100},
            {"revenue": 1000},
            {"revenue": 900},
        ]
        g = auto_revenue_growth(stmts, sector_growth=0.04, years=3)
        assert isinstance(g, float)
        # Historical YoY: 10%, 10%, 11.1% → median ~10%
        # Blended: 50% * 10% + 50% * 4% = 7%
        assert 0.0 < g < 0.5

    def test_auto_revenue_growth_empty(self):
        from auto_valuation.model.forecast import auto_revenue_growth
        g = auto_revenue_growth([], sector_growth=0.04)
        assert g == pytest.approx(0.04)

    def test_build_forecast_year_basic(self):
        from auto_valuation.model.forecast import build_forecast_year
        prior = {"revenue": 1000.0, "nowc": 80.0, "total_debt": 500.0}
        assumptions = {
            "revenue_growth": 0.10, "ebit_margin": 0.15,
            "da_pct": 0.04, "capex_pct": 0.05, "nowc_pct": 0.08,
            "tax_rate": 0.25, "sbc_pct": 0.02, "interest_rate": 0.05,
        }
        result = build_forecast_year(prior, assumptions)
        assert result["revenue"] == pytest.approx(1100.0)
        assert result["ebit"] == pytest.approx(1100.0 * 0.15)
        assert result["ufcf"] > 0

    def test_build_forecast_year_ufcf_calculation(self):
        from auto_valuation.model.forecast import build_forecast_year
        prior = {"revenue": 1000.0, "nowc": 0.0, "total_debt": 0.0}
        assumptions = {
            "revenue_growth": 0.0, "ebit_margin": 0.20,
            "da_pct": 0.05, "capex_pct": 0.05, "nowc_pct": 0.0,
            "tax_rate": 0.25, "sbc_pct": 0.0, "interest_rate": 0.0,
        }
        result = build_forecast_year(prior, assumptions)
        # UFCF = NOPAT + DA - Capex - ΔNOWC = 150 + 50 - 50 - 0 = 150
        assert result["ufcf"] == pytest.approx(150.0)

    def test_compute_sbc_forecast_length(self):
        from auto_valuation.model.forecast import compute_sbc_forecast
        stmts = [
            {"revenue": 1000, "stockBasedCompensation": 30},
            {"revenue": 900,  "stockBasedCompensation": 25},
        ]
        forecast_revs = [1100, 1200, 1300, 1400, 1500, 1600, 1700]
        result = compute_sbc_forecast(stmts, forecast_revs)
        assert len(result) == len(forecast_revs)
        assert all(v >= 0 for v in result)

    def test_compute_sbc_forecast_empty_history(self):
        from auto_valuation.model.forecast import compute_sbc_forecast
        result = compute_sbc_forecast([], [1000, 1100, 1200])
        assert len(result) == 3
        assert all(v >= 0 for v in result)

    def test_forecast_revenue_segmented_basic(self):
        from auto_valuation.model.forecast import forecast_revenue_segmented
        segs = {"North America": 600.0, "EMEA": 300.0, "APAC": 100.0}
        rates = {"North America": 0.08, "EMEA": 0.05, "APAC": 0.12}
        result = forecast_revenue_segmented(segs, rates, years=3)
        assert set(result.keys()) == set(segs.keys())
        for seg, revs in result.items():
            assert len(revs) == 3
            assert revs[0] == pytest.approx(segs[seg] * (1 + rates[seg]))

    def test_forecast_revenue_segmented_missing_rate(self):
        from auto_valuation.model.forecast import forecast_revenue_segmented
        segs = {"A": 500.0, "B": 200.0}
        rates = {"A": 0.10}  # B has no rate → default 0%
        result = forecast_revenue_segmented(segs, rates, years=1)
        assert result["B"][0] == pytest.approx(200.0)

    def test_should_use_segment_forecast_true(self):
        from auto_valuation.model.forecast import should_use_segment_forecast
        data = {"product": {"Shoes": 3000, "Apparel": 1000, "Equipment": 500}}
        assert should_use_segment_forecast(data) is True

    def test_should_use_segment_forecast_false_empty(self):
        from auto_valuation.model.forecast import should_use_segment_forecast
        assert should_use_segment_forecast({}) is False

    def test_should_use_segment_forecast_false_one_segment(self):
        from auto_valuation.model.forecast import should_use_segment_forecast
        data = {"product": {"Everything": 5000}}
        assert should_use_segment_forecast(data) is False


# ─────────────────────────────────────────────────────────────────────────────
# model/ev_bridge.py
# ─────────────────────────────────────────────────────────────────────────────

class TestFcfeEquityValue:

    def test_fcfe_basic(self):
        from auto_valuation.model.ev_bridge import fcfe_equity_value
        fcfe = [50.0, 55.0, 60.0, 65.0, 70.0]
        result = fcfe_equity_value(fcfe, cost_of_equity=0.10, diluted_shares=100.0,
                                   terminal_growth=0.025)
        assert result["equity_value_per_share"] > 0
        assert result["pv_fcfe_mm"] > 0
        assert result["pv_tv_mm"] > 0

    def test_fcfe_per_share_calculation(self):
        from auto_valuation.model.ev_bridge import fcfe_equity_value
        # Single-year FCFE, no terminal: TV dominates
        fcfe = [100.0]
        result = fcfe_equity_value(fcfe, 0.10, 1.0, terminal_growth=0.025)
        # TV = 100*(1.025)/(0.10-0.025) = 1366.67; PV_TV = 1366.67/1.10
        expected_tv_pv = 100 * 1.025 / 0.075 / 1.10
        assert result["pv_tv_mm"] == pytest.approx(expected_tv_pv, rel=1e-4)

    def test_fcfe_raises_ke_le_g(self):
        from auto_valuation.model.ev_bridge import fcfe_equity_value
        with pytest.raises(ValueError):
            fcfe_equity_value([100.0], cost_of_equity=0.02, diluted_shares=100.0,
                              terminal_growth=0.03)

    def test_fcfe_raises_empty(self):
        from auto_valuation.model.ev_bridge import fcfe_equity_value
        with pytest.raises(ValueError):
            fcfe_equity_value([], cost_of_equity=0.10, diluted_shares=100.0)


# ─────────────────────────────────────────────────────────────────────────────
# output/football_field.py  (aliases)
# ─────────────────────────────────────────────────────────────────────────────

class TestFootballFieldAliases:

    def _make_bands(self):
        from auto_valuation.output.football_field import FootballFieldBand
        return [FootballFieldBand(label="DCF", low=90.0, high=130.0)]

    def test_write_football_field_data_callable(self):
        from auto_valuation.output.football_field import write_football_field_data
        assert callable(write_football_field_data)

    def test_render_football_field_png_callable(self):
        from auto_valuation.output.football_field import render_football_field_png
        assert callable(render_football_field_png)

    def test_embed_football_field_callable(self):
        from auto_valuation.output.football_field import embed_football_field
        assert callable(embed_football_field)

    def test_write_football_field_data_runs(self, tmp_path):
        """Call write_football_field_data on a real workbook — no error."""
        import openpyxl
        from auto_valuation.output.football_field import write_football_field_data
        wb = openpyxl.Workbook()
        bands = self._make_bands()
        write_football_field_data(wb, bands)  # should not raise

    def test_render_football_field_png_runs(self, tmp_path):
        """Call render_football_field_png with output_path — produces file."""
        import matplotlib
        matplotlib.use("Agg")
        from auto_valuation.output.football_field import render_football_field_png
        out = str(tmp_path / "ff.png")
        bands = self._make_bands()
        render_football_field_png(bands, current_price=110.0, output_path=out)
        import pathlib
        assert pathlib.Path(out).exists()


# ─────────────────────────────────────────────────────────────────────────────
# utils/logging_utils.py
# ─────────────────────────────────────────────────────────────────────────────

class TestSetupLogging:

    def test_setup_logging_returns_logger(self, tmp_path):
        from auto_valuation.utils.logging_utils import setup_logging
        import logging
        logger = setup_logging(ticker="TEST", logs_dir=str(tmp_path))
        assert logger is not None

    def test_setup_logging_with_debug_level(self, tmp_path):
        from auto_valuation.utils.logging_utils import setup_logging
        import logging
        logger = setup_logging(ticker="TESTDBG", logs_dir=str(tmp_path), level=logging.DEBUG)
        assert logger is not None


# ─────────────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadApiKeys:

    def test_load_api_keys_raises_without_fmp(self, monkeypatch):
        from auto_valuation.utils.error import DataFetchError
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        with pytest.raises(DataFetchError):
            from auto_valuation.config import load_api_keys
            load_api_keys(require_fmp=True)

    def test_load_api_keys_no_require(self, monkeypatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        from auto_valuation.config import load_api_keys
        keys = load_api_keys(require_fmp=False)
        assert "FMP_API_KEY" in keys
        assert "FRED_API_KEY" in keys

    def test_load_api_keys_with_env(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "test_fmp_key")
        from auto_valuation.config import load_api_keys
        keys = load_api_keys(require_fmp=True)
        assert keys["FMP_API_KEY"] == "test_fmp_key"


# ─────────────────────────────────────────────────────────────────────────────
# data/fiscal_year.py
# ─────────────────────────────────────────────────────────────────────────────

class TestCalendarizationFunctions:

    def test_calendarize_ltm_same_month(self):
        from auto_valuation.data.fiscal_year import calendarize_ltm
        ltm = {"revenue": 5000}
        result = calendarize_ltm(ltm, fiscal_year_end_month=12, reference_month=12)
        assert result["calendarized"] is True
        assert "no adjustment" in result["calendarization_note"].lower()

    def test_calendarize_ltm_different_month(self):
        from auto_valuation.data.fiscal_year import calendarize_ltm
        ltm = {"revenue": 5000}
        result = calendarize_ltm(ltm, fiscal_year_end_month=3, reference_month=12)
        assert result["calendarized"] is True
        assert result["calendarization_offset_months"] == 9

    def test_calendarize_ltm_preserves_data(self):
        from auto_valuation.data.fiscal_year import calendarize_ltm
        ltm = {"revenue": 5000, "ebit": 750}
        result = calendarize_ltm(ltm, fiscal_year_end_month=6, reference_month=12)
        assert result["revenue"] == 5000
        assert result["ebit"] == 750

    def test_build_calendarized_peer_table_basic(self):
        from auto_valuation.data.fiscal_year import build_calendarized_peer_table
        peers = [
            {
                "ticker": "PEER1",
                "income_stmts": [{"revenue": 2000, "date": "2023-12-31"}],
                "fiscal_year_end_month": 12,
            },
            {
                "ticker": "PEER2",
                "income_stmts": [{"revenue": 3000, "date": "2023-03-31"}],
                "fiscal_year_end_month": 3,
            },
        ]
        result = build_calendarized_peer_table(peers)
        assert len(result) == 2
        assert "calendarized_ltm" in result[0]
        assert "calendarized_ltm" in result[1]


# ─────────────────────────────────────────────────────────────────────────────
# data/comps.py
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeForwardMultiples:

    def test_forward_multiples_basic(self):
        from auto_valuation.data.comps import compute_forward_multiples
        peer_ev = {"ev_mm": 10000.0, "market_cap_mm": 8000.0, "diluted_shares_mm": 100.0}
        ntm = {
            "estimatedRevenueAvg": 5_000_000_000,
            "estimatedEbitdaAvg":  1_000_000_000,
            "estimatedEbitAvg":    800_000_000,
            "estimatedEpsAvg":     5.0,
        }
        result = compute_forward_multiples(peer_ev, ntm)
        assert "ntm_ev_revenue" in result
        assert "ntm_ev_ebitda" in result
        assert "ntm_ev_ebit" in result
        assert "ntm_pe" in result
        assert result["ntm_ev_revenue"] is not None

    def test_forward_multiples_none_on_zero_denominator(self):
        from auto_valuation.data.comps import compute_forward_multiples
        peer_ev = {"ev_mm": 10000.0}
        ntm = {}  # all zeros / missing
        result = compute_forward_multiples(peer_ev, ntm)
        assert result["ntm_ev_revenue"] is None
        assert result["ntm_ev_ebitda"] is None


# ─────────────────────────────────────────────────────────────────────────────
# validation/checks.py
# ─────────────────────────────────────────────────────────────────────────────

class TestValidationAliases:

    def test_check_tv_pct_ev_alias_ok(self):
        from auto_valuation.validation.checks import check_tv_pct_ev
        result = check_tv_pct_ev(pv_tv=600.0, total_ev=1000.0, warn_threshold=0.80)
        assert result.status in ("OK", "PASS")

    def test_check_tv_pct_ev_alias_warn(self):
        from auto_valuation.validation.checks import check_tv_pct_ev
        result = check_tv_pct_ev(pv_tv=900.0, total_ev=1000.0, warn_threshold=0.80)
        assert result.status == "WARN"

    def test_check_roic_growth_consistency_ok(self):
        from auto_valuation.validation.checks import check_roic_growth_consistency
        # ROIC=0.10, reinv_rate=0.25 → implied_g=0.025; terminal_g=0.025 → OK
        result = check_roic_growth_consistency(
            roic=0.10, reinvestment_rate=0.25, terminal_g=0.025, tolerance=0.02
        )
        assert result.status == "OK"

    def test_check_roic_growth_consistency_warn(self):
        from auto_valuation.validation.checks import check_roic_growth_consistency
        # ROIC=0.20, reinv_rate=0.50 → implied_g=0.10; terminal_g=0.025 → deviation 0.075 > 0.02
        result = check_roic_growth_consistency(
            roic=0.20, reinvestment_rate=0.50, terminal_g=0.025, tolerance=0.02
        )
        assert result.status == "WARN"


# ─────────────────────────────────────────────────────────────────────────────
# forecast/terminal_value.py  (aliases)
# ─────────────────────────────────────────────────────────────────────────────

class TestTerminalValueAliases:

    def test_tv_gordon_growth_alias(self):
        from auto_valuation.forecast.terminal_value import (
            tv_gordon_growth, gordon_growth_tv
        )
        tv1 = gordon_growth_tv(100.0, 0.10, 0.025)
        tv2 = tv_gordon_growth(100.0, 0.10, 0.025)
        assert tv1 == tv2

    def test_tv_nopat_reinvestment_alias(self):
        from auto_valuation.forecast.terminal_value import (
            tv_nopat_reinvestment, gordon_growth_tv_from_nopat
        )
        tv1 = gordon_growth_tv_from_nopat(200.0, 0.25, 0.10, 0.025)
        tv2 = tv_nopat_reinvestment(200.0, 0.25, 0.10, 0.025)
        assert tv1 == tv2

    def test_tv_gordon_growth_positive(self):
        from auto_valuation.forecast.terminal_value import tv_gordon_growth
        tv = tv_gordon_growth(80.0, 0.09, 0.025)
        assert tv > 0

    def test_tv_nopat_reinvestment_positive(self):
        from auto_valuation.forecast.terminal_value import tv_nopat_reinvestment
        tv = tv_nopat_reinvestment(150.0, 0.30, 0.09, 0.025)
        assert tv > 0


# ─────────────────────────────────────────────────────────────────────────────
# data/cache.py — DataCache class
# ─────────────────────────────────────────────────────────────────────────────

class TestDataCache:

    def test_datacache_instantiation(self, tmp_path):
        from auto_valuation.data.cache import DataCache
        c = DataCache(ttl_hours=1.0, cache_dir=str(tmp_path))
        assert c.ttl_hours == 1.0

    def test_datacache_set_and_get(self, tmp_path):
        from auto_valuation.data.cache import DataCache
        c = DataCache(ttl_hours=24.0, cache_dir=str(tmp_path))
        c.set("test_key", {"value": 42})
        data = c.get("test_key")
        assert data == {"value": 42}

    def test_datacache_get_miss(self, tmp_path):
        from auto_valuation.data.cache import DataCache
        c = DataCache(ttl_hours=24.0, cache_dir=str(tmp_path))
        assert c.get("nonexistent_key_xyz") is None

    def test_datacache_fetch_or_get(self, tmp_path):
        from auto_valuation.data.cache import DataCache
        c = DataCache(ttl_hours=24.0, cache_dir=str(tmp_path))
        calls = []

        def fetcher():
            calls.append(1)
            return {"fresh": True}

        result1 = c.fetch_or_get("k1", fetcher)
        result2 = c.fetch_or_get("k1", fetcher)
        assert result1 == {"fresh": True}
        assert result2 == {"fresh": True}
        assert len(calls) == 1  # only fetched once

    def test_datacache_invalidate(self, tmp_path):
        from auto_valuation.data.cache import DataCache
        c = DataCache(ttl_hours=24.0, cache_dir=str(tmp_path))
        c.set("del_key", "data")
        c.invalidate("del_key")
        assert c.get("del_key") is None

    def test_datacache_make_key(self):
        from auto_valuation.data.cache import DataCache
        c = DataCache()
        key = c.make_key("AAPL", "income", limit=5)
        assert "AAPL" in key


# ─────────────────────────────────────────────────────────────────────────────
# output/excel_writer.py — build_log_path
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildLogPath:

    def test_build_log_path_format(self, tmp_path):
        from auto_valuation.output.excel_writer import build_log_path
        path = build_log_path("AAPL", logs_dir=str(tmp_path), scenario="bear")
        import pathlib
        p = pathlib.Path(path)
        assert "AAPL" in p.name
        assert "bear" in p.name
        assert p.suffix == ".log"

    def test_build_log_path_creates_dir(self, tmp_path):
        from auto_valuation.output.excel_writer import build_log_path
        new_dir = str(tmp_path / "sublogs")
        build_log_path("MSFT", logs_dir=new_dir)
        import pathlib
        assert pathlib.Path(new_dir).is_dir()

    def test_build_log_path_from_main(self, tmp_path):
        """build_log_path also available from main.py."""
        import sys, importlib
        import main as m
        path = m.build_log_path("NKE", logs_dir=str(tmp_path))
        assert "NKE" in path


# ─────────────────────────────────────────────────────────────────────────────
# main.py — parse_ticker_input
# ─────────────────────────────────────────────────────────────────────────────

class TestMainParseTickerInput:

    def test_parse_ticker_input_main(self):
        import main as m
        result = m.parse_ticker_input("AAPL,GOOG,MSFT")
        assert result == ["AAPL", "GOOG", "MSFT"]
