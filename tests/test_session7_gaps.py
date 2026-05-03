"""
Session 7 gap-implementation tests.

Covers:
  1. compute_ufcf in income_statement.py now includes SBC parameter (v4.0 A.1)
  2. compute_historical_ufcf returns 'sbc' key and includes SBC in UFCF
  3. run_dcf in forecast/dcf.py includes SBC, defaults to 7 years, NIKE TV convention
  4. compute_revenue_growth_profile supports '1stage', '2stage', 'hmodel'
  5. check_nowc_sign validates negative NOWC correctly
  6. check_da_capex_ratio flags high D&A/CapEx ratios
  7. config.FORECAST_YEARS == 7
"""
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2 — compute_ufcf with SBC  (v4.0 A.1)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeUfcfSbc:
    def test_sbc_zero_backward_compat(self):
        """sbc=0 should behave identically to the old signature."""
        from auto_valuation.model.income_statement import compute_ufcf
        result_no_sbc = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=20)
        result_sbc0   = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=20, sbc=0)
        assert result_no_sbc == result_sbc0

    def test_sbc_increases_ufcf(self):
        """SBC add-back should increase UFCF vs not including it."""
        from auto_valuation.model.income_statement import compute_ufcf
        ufcf_without = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=20, sbc=0)
        ufcf_with    = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=20, sbc=50)
        assert ufcf_with == ufcf_without + 50

    def test_ufcf_formula(self):
        """UFCF = NOPAT + D&A + SBC − CapEx − ΔNOWC."""
        from auto_valuation.model.income_statement import compute_ufcf, compute_nopat
        ebit, tax, da, capex, delta_nowc, sbc = 1000, 0.21, 100, 80, 20, 50
        nopat = compute_nopat(ebit, tax)
        expected = nopat + da + sbc - capex - delta_nowc
        assert compute_ufcf(ebit, tax, da, capex, delta_nowc, sbc) == pytest.approx(expected)

    def test_negative_delta_nowc_adds_to_ufcf(self):
        """Negative ΔNOWC (cash released by WC contraction) increases UFCF."""
        from auto_valuation.model.income_statement import compute_ufcf
        ufcf_pos = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=50)
        ufcf_neg = compute_ufcf(ebit=1000, tax_rate=0.21, da=100, capex=80, delta_nowc=-50)
        assert ufcf_neg > ufcf_pos


class TestComputeHistoricalUfcfSbc:
    def _make_stmts(self):
        return [
            {"calendarYear": "2023", "revenue": 10000, "ebit": 1500,
             "depreciationAndAmortization": 400},
            {"calendarYear": "2022", "revenue": 9000, "ebit": 1200,
             "depreciationAndAmortization": 350},
        ]

    def _make_cfs(self):
        return [
            {"calendarYear": "2023", "capitalExpenditure": -300,
             "stockBasedCompensation": 200},
            {"calendarYear": "2022", "capitalExpenditure": -250,
             "stockBasedCompensation": 150},
        ]

    def _make_bs(self):
        return [
            {"calendarYear": "2023", "netReceivables": 1200, "inventory": 500,
             "accountPayables": 800},
            {"calendarYear": "2022", "netReceivables": 1100, "inventory": 450,
             "accountPayables": 720},
        ]

    def test_sbc_key_present(self):
        from auto_valuation.model.income_statement import compute_historical_ufcf
        results = compute_historical_ufcf(self._make_stmts(), self._make_cfs(), self._make_bs())
        assert len(results) >= 1
        assert "sbc" in results[0], "Expected 'sbc' key in historical UFCF result"

    def test_sbc_included_in_ufcf(self):
        """UFCF with SBC=200 should be 200 higher than the no-SBC version."""
        from auto_valuation.model.income_statement import compute_historical_ufcf
        results_with = compute_historical_ufcf(self._make_stmts(), self._make_cfs(), self._make_bs())
        cfs_no_sbc = [
            {"calendarYear": "2023", "capitalExpenditure": -300, "stockBasedCompensation": 0},
            {"calendarYear": "2022", "capitalExpenditure": -250, "stockBasedCompensation": 0},
        ]
        results_without = compute_historical_ufcf(self._make_stmts(), cfs_no_sbc, self._make_bs())
        # Year 2023: SBC=200, so UFCF should differ by 200
        sbc = results_with[0]["sbc"]
        diff = results_with[0]["ufcf"] - results_without[0]["ufcf"]
        assert diff == pytest.approx(sbc, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 3 — run_dcf: 7-year default, SBC, NIKE TV convention
# ─────────────────────────────────────────────────────────────────────────────

def _make_stmts():
    return [
        {"calendarYear": str(y), "revenue": 10000 * (1.05 ** (2024 - y)),
         "ebit": 1500 * (1.05 ** (2024 - y)),
         "netIncome": 1100 * (1.05 ** (2024 - y)),
         "depreciationAndAmortization": 400, "incomeTaxExpense": 300}
        for y in [2024, 2023, 2022, 2021, 2020]
    ]

def _make_cfs():
    return [
        {"calendarYear": str(y), "capitalExpenditure": -300,
         "stockBasedCompensation": 150, "depreciationAndAmortization": 400}
        for y in [2024, 2023, 2022, 2021, 2020]
    ]

def _make_bs():
    return [
        {"calendarYear": str(y), "netReceivables": 1200, "inventory": 500,
         "accountPayables": 800, "totalAssets": 8000, "totalEquity": 4000,
         "longTermDebt": 1500, "shortTermDebt": 200}
        for y in [2024, 2023, 2022, 2021, 2020]
    ]


class TestRunDcf:
    def test_default_forecast_years_is_7(self):
        from auto_valuation.forecast.dcf import run_dcf
        import inspect
        sig = inspect.signature(run_dcf)
        assert sig.parameters["forecast_years"].default == 7

    def test_run_dcf_returns_7_years(self):
        from auto_valuation.forecast.dcf import run_dcf
        result = run_dcf(
            ticker="TEST", scenario="base",
            income_stmts=_make_stmts(), cash_flows=_make_cfs(),
            balance_sheets=_make_bs(),
            wacc=0.10, terminal_growth=0.025,
            near_term_growth=0.05, target_ebit_margin=0.15,
        )
        assert len(result.forecast_years_data) == 7

    def test_run_dcf_ev_positive(self):
        from auto_valuation.forecast.dcf import run_dcf
        result = run_dcf(
            ticker="TEST", scenario="base",
            income_stmts=_make_stmts(), cash_flows=_make_cfs(),
            balance_sheets=_make_bs(),
            wacc=0.10, terminal_growth=0.025,
            near_term_growth=0.05, target_ebit_margin=0.15,
        )
        assert result.enterprise_value > 0

    def test_sbc_pct_override_zero_lower_ufcf(self):
        """Setting sbc_pct=0 should give lower or equal UFCF than the default."""
        from auto_valuation.forecast.dcf import run_dcf
        result_with_sbc = run_dcf(
            ticker="TEST", scenario="base",
            income_stmts=_make_stmts(), cash_flows=_make_cfs(),
            balance_sheets=_make_bs(),
            wacc=0.10, terminal_growth=0.025,
            near_term_growth=0.05, target_ebit_margin=0.15,
        )
        result_no_sbc = run_dcf(
            ticker="TEST", scenario="base",
            income_stmts=_make_stmts(), cash_flows=_make_cfs(),
            balance_sheets=_make_bs(),
            wacc=0.10, terminal_growth=0.025,
            near_term_growth=0.05, target_ebit_margin=0.15,
            sbc_pct_override=0.0,
        )
        # With SBC add-back, EV should be >= without SBC
        assert result_with_sbc.enterprise_value >= result_no_sbc.enterprise_value

    def test_nike_tv_no_extra_growth(self):
        """
        NIKE convention: terminal_ufcf = last_year_ufcf (no ×(1+g)).
        Verify terminal_ufcf == last forecast year UFCF.
        """
        from auto_valuation.forecast.dcf import run_dcf
        result = run_dcf(
            ticker="TEST", scenario="base",
            income_stmts=_make_stmts(), cash_flows=_make_cfs(),
            balance_sheets=_make_bs(),
            wacc=0.10, terminal_growth=0.025,
            near_term_growth=0.05, target_ebit_margin=0.15,
            sbc_pct_override=0.0,
        )
        last_ufcf = result.forecast_years_data[-1].ufcf
        assert result.terminal_ufcf == pytest.approx(last_ufcf)


# ─────────────────────────────────────────────────────────────────────────────
# 4 — compute_revenue_growth_profile growth_profile options
# ─────────────────────────────────────────────────────────────────────────────

class TestRevenueGrowthProfile:
    def _make_stmts(self):
        revs = [10000 * (1.08 ** (4 - i)) for i in range(5)]
        return [{"calendarYear": str(2024 - i), "revenue": r}
                for i, r in enumerate(revs)]

    def test_1stage_constant_rate(self):
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        near_term, schedule = compute_revenue_growth_profile(
            self._make_stmts(), forecast_years=7, terminal_g=0.025,
            growth_profile="1stage"
        )
        # All rates should be the same (constant near-term)
        assert all(g == pytest.approx(near_term) for g in schedule)

    def test_1stage_length(self):
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        _, schedule = compute_revenue_growth_profile(
            self._make_stmts(), forecast_years=7, growth_profile="1stage"
        )
        assert len(schedule) == 7

    def test_2stage_length_and_fades(self):
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        terminal_g = 0.025
        near_term, schedule = compute_revenue_growth_profile(
            self._make_stmts(), forecast_years=7, terminal_g=terminal_g,
            growth_profile="2stage"
        )
        assert len(schedule) == 7
        # First year = near_term, last year = terminal_g
        assert schedule[0] == pytest.approx(near_term, abs=1e-9)
        assert schedule[-1] == pytest.approx(terminal_g, abs=1e-9)

    def test_hmodel_length(self):
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        _, schedule = compute_revenue_growth_profile(
            self._make_stmts(), forecast_years=7, growth_profile="hmodel"
        )
        assert len(schedule) == 7

    def test_invalid_profile_raises(self):
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        with pytest.raises(ValueError, match="growth_profile"):
            compute_revenue_growth_profile(
                self._make_stmts(), growth_profile="invalid"
            )

    def test_default_profile_is_1stage(self):
        """Default growth_profile should be '1stage' per arch plan A.4."""
        import inspect
        from auto_valuation.assumptions.engine import compute_revenue_growth_profile
        sig = inspect.signature(compute_revenue_growth_profile)
        assert sig.parameters["growth_profile"].default == "1stage"


# ─────────────────────────────────────────────────────────────────────────────
# 5 — check_nowc_sign
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckNowcSign:
    def test_normal_positive_nowc_passes(self):
        from auto_valuation.validation.checks import check_nowc_sign
        # NOWC = 5% of revenue — normal range
        result = check_nowc_sign([500], [10000])
        assert result.status == "PASS"

    def test_moderate_negative_nowc_passes(self):
        """Negative NOWC between -30% and 0% is valid (Amazon pattern)."""
        from auto_valuation.validation.checks import check_nowc_sign
        result = check_nowc_sign([-1500], [10000])  # -15% — valid
        assert result.status == "PASS"

    def test_extreme_negative_nowc_warns(self):
        """NOWC below -30% of revenue should warn."""
        from auto_valuation.validation.checks import check_nowc_sign
        result = check_nowc_sign([-4000], [10000])  # -40% — extreme
        assert result.status == "WARN"
        assert result.value < -0.30

    def test_high_positive_nowc_warns(self):
        """NOWC above 20% of revenue should warn."""
        from auto_valuation.validation.checks import check_nowc_sign
        result = check_nowc_sign([2500], [10000])  # 25% — high
        assert result.status == "WARN"
        assert result.value > 0.20

    def test_empty_input_passes(self):
        from auto_valuation.validation.checks import check_nowc_sign
        result = check_nowc_sign([], [])
        assert result.status == "PASS"

    def test_zero_revenue_passes(self):
        from auto_valuation.validation.checks import check_nowc_sign
        result = check_nowc_sign([1000], [0])
        assert result.status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# 6 — check_da_capex_ratio
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckDaCapexRatio:
    def test_normal_ratio_passes(self):
        from auto_valuation.validation.checks import check_da_capex_ratio
        result = check_da_capex_ratio(da=100, capex=90)
        assert result.status == "PASS"

    def test_high_ratio_warns(self):
        from auto_valuation.validation.checks import check_da_capex_ratio
        result = check_da_capex_ratio(da=500, capex=100)  # 5× — above 3× threshold
        assert result.status == "WARN"

    def test_zero_capex_skipped(self):
        from auto_valuation.validation.checks import check_da_capex_ratio
        result = check_da_capex_ratio(da=100, capex=0)
        assert result.status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# 7 — config.FORECAST_YEARS == 7
# ─────────────────────────────────────────────────────────────────────────────

def test_forecast_years_constant():
    from auto_valuation import config
    assert config.FORECAST_YEARS == 7
