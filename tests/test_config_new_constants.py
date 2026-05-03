"""Tests for the new config.py constants added this session."""
import pytest
from auto_valuation import config


class TestNewConfigConstants:
    def test_comps_min_peers(self):
        assert config.COMPS_MIN_PEERS == 3

    def test_comps_max_peers(self):
        assert config.COMPS_MAX_PEERS == 15

    def test_comps_mcap_fractions(self):
        assert config.COMPS_MCAP_MIN_FRACTION == pytest.approx(0.20)
        assert config.COMPS_MCAP_MAX_FRACTION == pytest.approx(5.0)

    def test_comps_proforma_flags(self):
        assert isinstance(config.COMPS_EXCLUDE_PROFORMA_FLAGGED, bool)
        assert config.COMPS_PROFORMA_LOOKBACK_DAYS == 365

    def test_circular_ref_params(self):
        assert config.CIRCULAR_REF_MAX_ITER == 50
        assert config.CIRCULAR_REF_TOL == pytest.approx(0.001)

    def test_intangibles_amort_years(self):
        assert config.INTANGIBLES_AMORT_YEARS_DEFAULT == 10

    def test_fred_rf_series_dict(self):
        assert isinstance(config.FRED_RF_SERIES, dict)
        assert "USD" in config.FRED_RF_SERIES
        assert "EUR" in config.FRED_RF_SERIES
        assert "GBP" in config.FRED_RF_SERIES
        assert "default" in config.FRED_RF_SERIES
        assert config.FRED_RF_SERIES["USD"] == "GS10"

    def test_net_debt_defaults(self):
        assert isinstance(config.NET_DEBT_DEFAULTS, dict)
        assert "add_pension" in config.NET_DEBT_DEFAULTS
        assert "add_finance_leases" in config.NET_DEBT_DEFAULTS
        assert config.NET_DEBT_DEFAULTS["add_pension"] is True

    def test_sector_gating(self):
        assert "Financials" in config.FINANCIAL_SECTORS
        assert "Energy" in config.MINING_SECTORS
        assert "Materials" in config.MINING_SECTORS
        assert "Real Estate" in config.REAL_ESTATE_SECTORS

    def test_existing_constants_unchanged(self):
        """Ensure existing constants were not accidentally overwritten."""
        assert config.WACC_HARD_MIN == pytest.approx(0.03)
        assert config.WACC_HARD_MAX == pytest.approx(0.30)
        assert config.TERMINAL_GROWTH_DEFAULT == pytest.approx(0.025)
        assert config.TERMINAL_GROWTH_GDP_CAP == pytest.approx(0.040)
        assert config.TAX_RATE_DEFAULT == pytest.approx(0.21)
        assert config.FORECAST_YEARS == 7
