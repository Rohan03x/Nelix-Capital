"""
tests/test_session8_gaps.py — Tests for all 12 functions added in Session 8.

Covers:
  - compute_revenue_bridge             (model/forecast.py)
  - decompose_historical_revenue_growth (assumptions/revenue.py)
  - rollforward_debt_schedule          (model/debt.py)
  - validate_debt_schedule             (model/debt.py)
  - validate_net_debt                  (data/bridge.py)
  - GLOBAL_DEFAULTS + _deep_merge      (config.py)
  - compute_wacc_with_leases           (assumptions/wacc.py)
  - PrecedentDeal dataclass            (data/transactions.py)
  - TransactionCompsResult dataclass   (data/transactions.py)
  - filter_peers_with_events           (data/comps.py)
  - build_ff_data_with_52wk            (output/football_field.py)
"""

from __future__ import annotations

import pytest

# ─── Imports ──────────────────────────────────────────────────────────────────

from auto_valuation.model.forecast import compute_revenue_bridge
from auto_valuation.assumptions.revenue import decompose_historical_revenue_growth
from auto_valuation.model.debt import rollforward_debt_schedule, validate_debt_schedule
from auto_valuation.data.bridge import validate_net_debt
from auto_valuation.config import GLOBAL_DEFAULTS, _deep_merge, FORECAST_YEARS, TERMINAL_GROWTH_DEFAULT
from auto_valuation.assumptions.wacc import compute_wacc_with_leases
from auto_valuation.data.transactions import PrecedentDeal, TransactionCompsResult
from auto_valuation.data.comps import filter_peers_with_events
from auto_valuation.output.football_field import build_ff_data_with_52wk


# ─────────────────────────────────────────────────────────────────────────────
# 1. compute_revenue_bridge
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeRevenueBridge:
    def test_basic_two_year(self):
        revenues, breakdown = compute_revenue_bridge(
            base_revenue=1000.0,
            price_growth_rates=[0.03, 0.03],
            volume_growth_rates=[0.02, 0.02],
        )
        assert len(revenues) == 2
        assert len(breakdown) == 2
        # Year 1: 1000 × 1.03 × 1.02 × 1.0 = 1050.6
        assert abs(revenues[0] - 1000.0 * 1.03 * 1.02) < 0.01

    def test_revenue_compounds_correctly(self):
        revenues, _ = compute_revenue_bridge(
            base_revenue=500.0,
            price_growth_rates=[0.05, 0.05, 0.05],
            volume_growth_rates=[0.02, 0.02, 0.02],
        )
        expected_yr1 = 500.0 * 1.05 * 1.02
        assert abs(revenues[0] - expected_yr1) < 0.01
        expected_yr2 = expected_yr1 * 1.05 * 1.02
        assert abs(revenues[1] - expected_yr2) < 0.01

    def test_zero_price_growth_pure_volume(self):
        revenues, breakdown = compute_revenue_bridge(
            base_revenue=1000.0,
            price_growth_rates=[0.0, 0.0],
            volume_growth_rates=[0.10, 0.10],
        )
        assert abs(revenues[0] - 1100.0) < 0.01
        assert abs(breakdown[0]["price_effect_m"]) < 1e-9

    def test_mix_shift_applied(self):
        revenues, breakdown = compute_revenue_bridge(
            base_revenue=1000.0,
            price_growth_rates=[0.0],
            volume_growth_rates=[0.0],
            mix_shift=0.05,
        )
        assert abs(revenues[0] - 1050.0) < 0.01
        assert abs(breakdown[0]["mix_effect_m"] - 50.0) < 0.01

    def test_returns_tuple(self):
        result = compute_revenue_bridge(1000.0, [0.02], [0.01])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_breakdown_keys(self):
        _, breakdown = compute_revenue_bridge(1000.0, [0.03], [0.02])
        row = breakdown[0]
        for key in ("year", "revenue", "price_effect_m", "volume_effect_m", "mix_effect_m", "blended_growth"):
            assert key in row

    def test_volume_shorter_than_price_defaults_to_zero(self):
        revenues, _ = compute_revenue_bridge(
            base_revenue=1000.0,
            price_growth_rates=[0.05, 0.05, 0.05],
            volume_growth_rates=[0.02],  # only 1 year provided
        )
        # Year 3: volume defaults to 0
        expected_yr2 = 1000.0 * 1.05 * 1.02 * 1.05  # vol=0.02 yr1 only
        # Year 3 volume = 0
        assert abs(revenues[2] - revenues[1] * 1.05) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# 2. decompose_historical_revenue_growth
# ─────────────────────────────────────────────────────────────────────────────

class TestDecomposeHistoricalRevenueGrowth:
    def test_blended_growth_only(self):
        revenues = [1000.0, 1050.0, 1102.5]
        blended, prices, volumes = decompose_historical_revenue_growth(revenues)
        assert len(blended) == 2
        assert abs(blended[0] - 0.05) < 1e-9
        assert prices is None
        assert volumes is None

    def test_with_price_decomposition(self):
        revenues = [1000.0, 1050.0]
        prices   = [100.0, 105.0]   # 5% price growth; volume flat = 10
        blended, pg, vg = decompose_historical_revenue_growth(revenues, prices)
        assert len(blended) == 1
        assert len(pg) == 1
        assert len(vg) == 1
        assert abs(pg[0] - 0.05) < 1e-9    # 5% price growth
        assert abs(vg[0]) < 1e-9           # 0% volume growth (10 → 10)

    def test_price_list_wrong_length_returns_none(self):
        revenues = [1000.0, 1050.0, 1100.0]
        prices   = [100.0, 105.0]  # too short
        blended, pg, vg = decompose_historical_revenue_growth(revenues, prices)
        assert pg is None
        assert vg is None

    def test_single_element_returns_empty(self):
        blended, _, _ = decompose_historical_revenue_growth([1000.0])
        assert blended == []

    def test_zero_prior_year_returns_none(self):
        revenues = [0.0, 1000.0]
        blended, _, _ = decompose_historical_revenue_growth(revenues)
        assert blended[0] is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. rollforward_debt_schedule
# ─────────────────────────────────────────────────────────────────────────────

class TestRollforwardDebtSchedule:
    def test_basic_schedule(self):
        schedule = rollforward_debt_schedule(
            ibd_opening=1000.0,
            scheduled_repayments=[100.0, 100.0, 100.0],
            kd_pretax=0.05,
        )
        assert len(schedule) == 3
        assert schedule[0]["ibd_opening"] == 1000.0
        assert schedule[0]["ibd_closing"] == 900.0
        assert abs(schedule[0]["interest_expense"] - (1000 + 900) / 2 * 0.05) < 0.01

    def test_ibd_floors_at_zero(self):
        schedule = rollforward_debt_schedule(
            ibd_opening=50.0,
            scheduled_repayments=[100.0, 100.0],
            kd_pretax=0.05,
        )
        assert schedule[0]["ibd_closing"] == 0.0
        assert schedule[1]["ibd_opening"] == 0.0

    def test_new_debt_issuance_adds_to_balance(self):
        schedule = rollforward_debt_schedule(
            ibd_opening=1000.0,
            scheduled_repayments=[100.0],
            new_debt_issuance=[200.0],
            kd_pretax=0.05,
        )
        assert schedule[0]["ibd_closing"] == 1100.0
        assert schedule[0]["new_issuance"] == 200.0

    def test_per_year_kd_list(self):
        schedule = rollforward_debt_schedule(
            ibd_opening=1000.0,
            scheduled_repayments=[100.0, 100.0],
            kd_pretax=[0.04, 0.06],
        )
        avg_yr1 = (1000 + 900) / 2
        assert abs(schedule[0]["interest_expense"] - avg_yr1 * 0.04) < 0.01
        avg_yr2 = (900 + 800) / 2
        assert abs(schedule[1]["interest_expense"] - avg_yr2 * 0.06) < 0.01

    def test_schedule_keys(self):
        schedule = rollforward_debt_schedule(1000.0, [100.0])
        for key in ("year", "ibd_opening", "repayment", "new_issuance", "ibd_closing", "interest_expense"):
            assert key in schedule[0]

    def test_empty_repayments_returns_empty(self):
        schedule = rollforward_debt_schedule(1000.0, [])
        assert schedule == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. validate_debt_schedule
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateDebtSchedule:
    def test_valid_schedule_does_not_raise(self):
        validate_debt_schedule(ibd_total=1000.0, scheduled_repayments=[100.0, 100.0, 100.0])

    def test_exact_total_does_not_raise(self):
        validate_debt_schedule(ibd_total=300.0, scheduled_repayments=[100.0, 100.0, 100.0])

    def test_within_tolerance_does_not_raise(self):
        # 5% tolerance: 1000 × 1.05 = 1050; 1040 should pass
        validate_debt_schedule(ibd_total=1000.0, scheduled_repayments=[1040.0])

    def test_over_tolerance_raises(self):
        with pytest.raises(ValueError, match="exceed"):
            validate_debt_schedule(ibd_total=100.0, scheduled_repayments=[200.0])


# ─────────────────────────────────────────────────────────────────────────────
# 5. validate_net_debt
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateNetDebt:
    def test_consistent_values_returns_true(self):
        result = validate_net_debt(net_debt_m=2000.0, ev_m=12000.0, equity_value_m=10000.0)
        assert result is True

    def test_within_tolerance_returns_true(self):
        # diff = 0.5m < 1.0m tolerance
        result = validate_net_debt(net_debt_m=2000.5, ev_m=12000.0, equity_value_m=10000.0)
        assert result is True

    def test_discrepancy_raises(self):
        with pytest.raises(ValueError, match="discrepancy"):
            validate_net_debt(net_debt_m=2000.0, ev_m=12000.0, equity_value_m=9000.0)

    def test_custom_tolerance(self):
        # Tolerance set to 5m; diff = 3m → should pass
        result = validate_net_debt(
            net_debt_m=2003.0, ev_m=12000.0, equity_value_m=10000.0, tolerance_m=5.0
        )
        assert result is True

    def test_zero_net_debt(self):
        # EV = equity_value when net_debt = 0
        result = validate_net_debt(net_debt_m=0.0, ev_m=5000.0, equity_value_m=5000.0)
        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. GLOBAL_DEFAULTS and _deep_merge
# ─────────────────────────────────────────────────────────────────────────────

class TestGlobalDefaults:
    def test_global_defaults_is_dict(self):
        assert isinstance(GLOBAL_DEFAULTS, dict)

    def test_forecast_years_matches_constant(self):
        assert GLOBAL_DEFAULTS["forecast_years"] == FORECAST_YEARS

    def test_terminal_g_matches_constant(self):
        assert GLOBAL_DEFAULTS["terminal_g"] == TERMINAL_GROWTH_DEFAULT

    def test_required_keys_present(self):
        for key in ("forecast_years", "terminal_g", "mid_year_convention",
                    "capex_override", "net_debt_flags"):
            assert key in GLOBAL_DEFAULTS

    def test_net_debt_flags_dict(self):
        flags = GLOBAL_DEFAULTS["net_debt_flags"]
        assert isinstance(flags, dict)
        for key in ("add_pension", "add_leases", "add_preferred", "add_nci"):
            assert key in flags


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99, "c": 3}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_nested_merge(self):
        base = {"flags": {"x": True, "y": False}}
        override = {"flags": {"y": True}}
        result = _deep_merge(base, override)
        assert result["flags"]["x"] is True   # base preserved
        assert result["flags"]["y"] is True   # overridden

    def test_base_unchanged(self):
        base = {"a": 1}
        override = {"a": 2}
        _deep_merge(base, override)
        assert base["a"] == 1   # original not mutated

    def test_empty_override(self):
        base = {"a": 1, "b": 2}
        result = _deep_merge(base, {})
        assert result == base

    def test_nested_dict_not_replaced_wholesale(self):
        base = {"d": {"x": 1, "y": 2}}
        override = {"d": {"z": 3}}
        result = _deep_merge(base, override)
        assert result["d"]["x"] == 1   # preserved from base
        assert result["d"]["z"] == 3   # added from override


# ─────────────────────────────────────────────────────────────────────────────
# 7. compute_wacc_with_leases
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeWaccWithLeases:
    def test_basic_three_component(self):
        wacc, weights = compute_wacc_with_leases(
            ke=0.10,
            kd_after_tax=0.04,
            k_lease=0.05,
            equity_mv_m=6000.0,
            debt_mv_m=3000.0,
            lease_liability_m=1000.0,
            tax_rate=0.21,
        )
        V = 10000.0
        k_lease_at = 0.05 * (1 - 0.21)
        expected = 0.10 * (6000 / V) + 0.04 * (3000 / V) + k_lease_at * (1000 / V)
        assert abs(wacc - expected) < 1e-9

    def test_non_deductible_lease(self):
        wacc_ded, _ = compute_wacc_with_leases(
            ke=0.10, kd_after_tax=0.04, k_lease=0.05,
            equity_mv_m=6000, debt_mv_m=3000, lease_liability_m=1000,
            tax_rate=0.21, lease_tax_deductible=True,
        )
        wacc_nded, _ = compute_wacc_with_leases(
            ke=0.10, kd_after_tax=0.04, k_lease=0.05,
            equity_mv_m=6000, debt_mv_m=3000, lease_liability_m=1000,
            tax_rate=0.21, lease_tax_deductible=False,
        )
        # Non-deductible lease has higher k_lease_at → higher WACC (all else equal)
        assert wacc_nded > wacc_ded

    def test_weights_sum_to_one(self):
        _, weights = compute_wacc_with_leases(
            ke=0.10, kd_after_tax=0.04, k_lease=0.05,
            equity_mv_m=5000, debt_mv_m=3000, lease_liability_m=2000,
            tax_rate=0.21,
        )
        assert abs(weights["E_pct"] + weights["D_pct"] + weights["L_pct"] - 1.0) < 1e-9

    def test_zero_lease_matches_two_component(self):
        """When lease = 0, reduces to standard 2-component WACC."""
        wacc, weights = compute_wacc_with_leases(
            ke=0.10, kd_after_tax=0.04, k_lease=0.05,
            equity_mv_m=6000, debt_mv_m=4000, lease_liability_m=0.0,
            tax_rate=0.21,
        )
        V = 10000.0
        expected = 0.10 * (6000 / V) + 0.04 * (4000 / V)
        assert abs(wacc - expected) < 1e-9

    def test_zero_capital_raises(self):
        with pytest.raises(ValueError):
            compute_wacc_with_leases(0.10, 0.04, 0.05, 0.0, 0.0, 0.0, 0.21)

    def test_returns_tuple(self):
        result = compute_wacc_with_leases(0.10, 0.04, 0.05, 6000, 3000, 1000, 0.21)
        assert isinstance(result, tuple)
        assert len(result) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 8. PrecedentDeal dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestPrecedentDeal:
    def test_instantiation_required_fields(self):
        deal = PrecedentDeal(
            target_name="TargetCo",
            acquirer_name="AcquirerCo",
            announcement_date="2023-01-15",
            enterprise_value=5000.0,
            equity_value=4500.0,
        )
        assert deal.target_name == "TargetCo"
        assert deal.enterprise_value == 5000.0
        assert deal.status == "closed"   # default

    def test_optional_fields_default_none(self):
        deal = PrecedentDeal("T", "A", "2023-01-01", 1000.0, 900.0)
        assert deal.ltm_revenue is None
        assert deal.ltm_ebitda is None
        assert deal.sector is None

    def test_all_fields(self):
        deal = PrecedentDeal(
            target_name="T",
            acquirer_name="A",
            announcement_date="2020-06-01",
            enterprise_value=10000.0,
            equity_value=8000.0,
            ltm_revenue=3000.0,
            ltm_ebitda=500.0,
            ltm_ebit=400.0,
            ltm_net_income=300.0,
            sector="Technology",
            status="closed",
            notes="Friendly acquisition",
        )
        assert deal.ltm_ebitda == 500.0
        assert deal.notes == "Friendly acquisition"


# ─────────────────────────────────────────────────────────────────────────────
# 9. TransactionCompsResult dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionCompsResult:
    def test_default_instantiation(self):
        result = TransactionCompsResult()
        assert result.deals == []
        assert result.ev_ebitda_median is None
        assert result.is_estimated is False

    def test_with_data(self):
        result = TransactionCompsResult(
            deals=[],
            ev_ebitda_25th=8.0,
            ev_ebitda_median=10.0,
            ev_ebitda_75th=12.0,
            implied_ev_low=8000.0,
            implied_ev_high=12000.0,
            source="user_json",
        )
        assert result.ev_ebitda_median == 10.0
        assert result.source == "user_json"

    def test_separate_instances_have_separate_deals(self):
        r1 = TransactionCompsResult()
        r2 = TransactionCompsResult()
        r1.deals.append("deal1")
        # Due to field(default_factory=list), r2.deals should be separate
        assert r2.deals == []


# ─────────────────────────────────────────────────────────────────────────────
# 10. filter_peers_with_events
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterPeersWithEvents:
    def test_exclude_false_returns_all(self):
        tickers = ["AAPL", "MSFT", "GOOG"]
        warnings = {"MSFT": ["M&A announced"]}
        result = filter_peers_with_events(tickers, warnings, exclude_flagged=False)
        assert result == tickers

    def test_exclude_true_removes_flagged(self):
        tickers = ["AAPL", "MSFT", "GOOG"]
        warnings = {"MSFT": ["M&A announced"]}
        result = filter_peers_with_events(tickers, warnings, exclude_flagged=True)
        assert "MSFT" not in result
        assert "AAPL" in result
        assert "GOOG" in result

    def test_empty_warnings_returns_all(self):
        tickers = ["A", "B", "C"]
        result = filter_peers_with_events(tickers, {}, exclude_flagged=True)
        assert result == tickers

    def test_all_flagged_returns_empty(self):
        tickers = ["A", "B"]
        warnings = {"A": ["issue1"], "B": ["issue2"]}
        result = filter_peers_with_events(tickers, warnings, exclude_flagged=True)
        assert result == []

    def test_returns_new_list_not_original(self):
        tickers = ["A", "B", "C"]
        result = filter_peers_with_events(tickers, {}, exclude_flagged=False)
        result.append("D")
        assert "D" not in tickers


# ─────────────────────────────────────────────────────────────────────────────
# 11. build_ff_data_with_52wk
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildFfDataWith52wk:
    def test_all_four_bands(self):
        data = build_ff_data_with_52wk(
            dcf_ev_low=9000.0, dcf_ev_high=11000.0,
            comps_ev_low=8500.0, comps_ev_high=10500.0,
            tx_ev_low=9500.0, tx_ev_high=12000.0,
            net_debt=2000.0,
            diluted_shares=100.0,
            price_52wk_low=65.0, price_52wk_high=90.0,
        )
        assert len(data) == 4
        labels = [d["label"] for d in data]
        assert "DCF (WACC / TV Sensitivity)" in labels
        assert "52-Week Trading Range" in labels

    def test_dcf_price_conversion(self):
        data = build_ff_data_with_52wk(
            dcf_ev_low=12000.0, dcf_ev_high=14000.0,
            comps_ev_low=None, comps_ev_high=None,
            tx_ev_low=None, tx_ev_high=None,
            net_debt=2000.0,
            diluted_shares=100.0,
            price_52wk_low=None, price_52wk_high=None,
        )
        assert len(data) == 1
        # price_low = (12000 - 2000) / 100 = 100
        assert abs(data[0]["low"] - 100.0) < 0.01
        # price_high = (14000 - 2000) / 100 = 120
        assert abs(data[0]["high"] - 120.0) < 0.01

    def test_52wk_band_passthrough(self):
        data = build_ff_data_with_52wk(
            dcf_ev_low=None, dcf_ev_high=None,
            comps_ev_low=None, comps_ev_high=None,
            tx_ev_low=None, tx_ev_high=None,
            net_debt=0.0, diluted_shares=100.0,
            price_52wk_low=50.0, price_52wk_high=80.0,
        )
        assert len(data) == 1
        assert data[0]["low"] == 50.0
        assert data[0]["high"] == 80.0

    def test_none_ev_excludes_band(self):
        data = build_ff_data_with_52wk(
            dcf_ev_low=None, dcf_ev_high=None,   # excluded
            comps_ev_low=8000.0, comps_ev_high=10000.0,
            tx_ev_low=None, tx_ev_high=None,      # excluded
            net_debt=1000.0, diluted_shares=100.0,
            price_52wk_low=None, price_52wk_high=None,  # excluded
        )
        assert len(data) == 1
        assert "Trading Comps" in data[0]["label"]

    def test_negative_equity_floors_at_zero(self):
        """EV < net_debt → implied equity negative → price floors at 0."""
        data = build_ff_data_with_52wk(
            dcf_ev_low=500.0, dcf_ev_high=600.0,
            comps_ev_low=None, comps_ev_high=None,
            tx_ev_low=None, tx_ev_high=None,
            net_debt=2000.0,  # EV < net_debt
            diluted_shares=100.0,
            price_52wk_low=None, price_52wk_high=None,
        )
        assert data[0]["low"] == 0.0
        assert data[0]["high"] == 0.0

    def test_empty_when_all_none(self):
        data = build_ff_data_with_52wk(
            None, None, None, None, None, None,
            net_debt=0.0, diluted_shares=100.0,
            price_52wk_low=None, price_52wk_high=None,
        )
        assert data == []

    def test_band_dict_keys(self):
        data = build_ff_data_with_52wk(
            dcf_ev_low=10000.0, dcf_ev_high=12000.0,
            comps_ev_low=None, comps_ev_high=None,
            tx_ev_low=None, tx_ev_high=None,
            net_debt=2000.0, diluted_shares=100.0,
            price_52wk_low=None, price_52wk_high=None,
        )
        for key in ("label", "low", "high"):
            assert key in data[0]
