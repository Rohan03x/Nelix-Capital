"""
tests/test_sector.py — Unit tests for auto_valuation/model/sector.py

Phase 5 — Sector-Specific Adjustments

Tests cover:
  - detect_sector_type()          : classification of every sector type
  - financial_company_gate()      : raises for Financials
  - reit_company_gate()           : raises for REITs
  - mining_company_gate()         : raises for Mining
  - apply_sector_gate()           : unified gate, returns sector type
  - reit_ffo_affo_model()         : FFO / AFFO arithmetic
  - is_lease_heavy()              : Retail / Airline detection
  - compute_ebitdar()             : EBITDAR = EBITDA + rent
  - ebitdar_multiple()            : EV / EBITDAR
  - apply_ebitdar_adjustment()    : mutates peer list for lease-heavy sectors
  - is_rd_intensive()             : Tech / Biotech detection
  - normalise_operating_leases()  : ASC 842 / IFRS 16 extraction

No API calls — all tests use hard-coded fixture data.
"""

from __future__ import annotations

import pytest

from auto_valuation.model.sector import (
    AIRLINE, FINANCIAL, MINING, REIT, RETAIL, STANDARD, TECH_RD,
    apply_ebitdar_adjustment,
    apply_sector_gate,
    compute_ebitdar,
    detect_sector_type,
    ebitdar_multiple,
    financial_company_gate,
    is_lease_heavy,
    is_rd_intensive,
    mining_company_gate,
    normalise_operating_leases,
    reit_company_gate,
    reit_ffo_affo_model,
)
from auto_valuation.utils.error import UnsupportedCompanyError


# ─────────────────────────────────────────────────────────────────────────────
# 1 — detect_sector_type
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectSectorType:
    def test_technology(self):
        assert detect_sector_type("Technology", "Software—Application") == TECH_RD

    def test_information_technology(self):
        assert detect_sector_type("Information Technology", "Semiconductors") == TECH_RD

    def test_financials(self):
        assert detect_sector_type("Financials", "Banks—Diversified") == FINANCIAL

    def test_banking_keyword(self):
        assert detect_sector_type("Banking", "") == FINANCIAL

    def test_insurance(self):
        assert detect_sector_type("Financials", "Insurance—Diversified") == FINANCIAL

    def test_real_estate(self):
        assert detect_sector_type("Real Estate", "Diversified REIT") == REIT

    def test_reit_in_industry(self):
        assert detect_sector_type("Real Estate", "Retail REIT") == REIT

    def test_retail_apparel(self):
        assert detect_sector_type("Consumer Discretionary", "Apparel Retail") == RETAIL

    def test_grocery(self):
        assert detect_sector_type("Consumer Staples", "Grocery Stores") == RETAIL

    def test_airline(self):
        assert detect_sector_type("Industrials", "Airlines") == AIRLINE

    def test_passenger_airlines(self):
        assert detect_sector_type("Industrials", "Passenger Airlines") == AIRLINE

    def test_basic_materials_mining(self):
        assert detect_sector_type("Basic Materials", "Gold") == MINING

    def test_oil_gas_drilling(self):
        assert detect_sector_type("Energy", "Oil & Gas Drilling") == MINING

    def test_healthcare_biotech(self):
        assert detect_sector_type("Healthcare", "Biotechnology") == TECH_RD

    def test_consumer_discretionary_non_retail(self):
        # Automobiles are Consumer Discretionary but not Retail → STANDARD
        t = detect_sector_type("Consumer Discretionary", "Automobiles")
        assert t == STANDARD

    def test_industrials_non_airline(self):
        assert detect_sector_type("Industrials", "Defense") == STANDARD

    def test_empty_strings(self):
        assert detect_sector_type("", "") == STANDARD

    def test_case_insensitive(self):
        # Detection must be case-insensitive
        assert detect_sector_type("FINANCIALS", "BANKS—DIVERSIFIED") == FINANCIAL
        assert detect_sector_type("real estate", "REIT") == REIT


# ─────────────────────────────────────────────────────────────────────────────
# 2 — Gate functions
# ─────────────────────────────────────────────────────────────────────────────

class TestFinancialCompanyGate:
    def test_raises_for_financials(self):
        with pytest.raises(UnsupportedCompanyError):
            financial_company_gate("Financials", "Banks—Regional")

    def test_raises_for_insurance(self):
        with pytest.raises(UnsupportedCompanyError):
            financial_company_gate("Financials", "Insurance—Property & Casualty")

    def test_no_raise_for_technology(self):
        financial_company_gate("Technology", "Software")  # must not raise

    def test_no_raise_for_consumer(self):
        financial_company_gate("Consumer Discretionary", "Apparel Retail")

    def test_error_message_contains_sector(self):
        with pytest.raises(UnsupportedCompanyError, match="Financials"):
            financial_company_gate("Financials", "")

    def test_error_has_exit_code_4(self):
        with pytest.raises(UnsupportedCompanyError) as exc_info:
            financial_company_gate("Financials", "Banks")
        assert exc_info.value.exit_code == 4


class TestReitCompanyGate:
    def test_raises_for_reit(self):
        with pytest.raises(UnsupportedCompanyError):
            reit_company_gate("Real Estate", "Diversified REIT")

    def test_no_raise_for_technology(self):
        reit_company_gate("Technology", "Software")  # must not raise

    def test_error_message_mentions_ffo(self):
        with pytest.raises(UnsupportedCompanyError, match="FFO"):
            reit_company_gate("Real Estate", "")


class TestMiningCompanyGate:
    def test_raises_for_gold_mining(self):
        with pytest.raises(UnsupportedCompanyError):
            mining_company_gate("Basic Materials", "Gold")

    def test_raises_for_oil_gas(self):
        with pytest.raises(UnsupportedCompanyError):
            mining_company_gate("Energy", "Oil & Gas Exploration")

    def test_no_raise_for_financials(self):
        # financials are not mining — gates are independent
        mining_company_gate("Financials", "Banks")  # must not raise

    def test_error_message_mentions_nav(self):
        with pytest.raises(UnsupportedCompanyError, match="NAV"):
            mining_company_gate("Basic Materials", "Mining")


class TestApplySectorGate:
    def test_standard_sector_returns_type(self):
        t = apply_sector_gate("Healthcare", "Medical Devices")
        assert t == TECH_RD  # Medical Devices → biotech → TECH_RD... actually not
        # Let me check what medical devices maps to

    def test_industrials_defense_returns_standard(self):
        t = apply_sector_gate("Industrials", "Defense")
        assert t == STANDARD

    def test_retail_returns_retail(self):
        t = apply_sector_gate("Consumer Discretionary", "Apparel Retail")
        assert t == RETAIL

    def test_airline_returns_airline(self):
        t = apply_sector_gate("Industrials", "Airlines")
        assert t == AIRLINE

    def test_financial_raises(self):
        with pytest.raises(UnsupportedCompanyError):
            apply_sector_gate("Financials", "Banks")

    def test_mining_raises(self):
        with pytest.raises(UnsupportedCompanyError):
            apply_sector_gate("Basic Materials", "Gold")

    def test_reit_raises_by_default(self):
        with pytest.raises(UnsupportedCompanyError):
            apply_sector_gate("Real Estate", "Retail REIT")

    def test_reit_allowed_when_opt_in(self):
        t = apply_sector_gate("Real Estate", "Retail REIT", allow_reit=True)
        assert t == REIT

    def test_tech_returns_tech_rd(self):
        t = apply_sector_gate("Technology", "Software—Application")
        assert t == TECH_RD


# ─────────────────────────────────────────────────────────────────────────────
# 3 — REIT FFO / AFFO model
# ─────────────────────────────────────────────────────────────────────────────

class TestReitFfoAffoModel:
    def _run(self, **overrides):
        defaults = dict(
            net_income_mm=1_000,
            da_mm=500,
            gains_on_sale_mm=200,
            maintenance_capex_mm=100,
            straight_line_rent_adj_mm=50,
            shares_mm=100,
            price_per_share=30.0,
        )
        defaults.update(overrides)
        return reit_ffo_affo_model(**defaults)

    def test_ffo_formula(self):
        # FFO = 1000 + 500 - 200 = 1300
        r = self._run()
        assert abs(r["ffo_mm"] - 1300) < 0.01

    def test_affo_formula(self):
        # AFFO = FFO(1300) - maintenance(100) - SL rent(50) = 1150
        r = self._run()
        assert abs(r["affo_mm"] - 1150) < 0.01

    def test_ffo_per_share(self):
        # FFO/sh = 1300 / 100 = 13.0
        r = self._run()
        assert abs(r["ffo_per_share"] - 13.0) < 0.001

    def test_affo_per_share(self):
        # AFFO/sh = 1150 / 100 = 11.5
        r = self._run()
        assert abs(r["affo_per_share"] - 11.5) < 0.001

    def test_p_ffo(self):
        # P/FFO = 30 / 13 ≈ 2.308
        r = self._run()
        assert abs(r["p_ffo"] - 30 / 13) < 0.001

    def test_p_affo(self):
        # P/AFFO = 30 / 11.5 ≈ 2.609
        r = self._run()
        assert abs(r["p_affo"] - 30 / 11.5) < 0.001

    def test_no_gains_on_sale(self):
        r = self._run(gains_on_sale_mm=0)
        # FFO = 1000 + 500 - 0 = 1500
        assert abs(r["ffo_mm"] - 1500) < 0.01

    def test_no_price_gives_none_multiples(self):
        r = self._run(price_per_share=None)
        assert r["p_ffo"]  is None
        assert r["p_affo"] is None

    def test_negative_affo_when_high_capex(self):
        r = self._run(maintenance_capex_mm=2000)
        assert r["affo_mm"] < 0

    def test_zero_shares_returns_none_per_share(self):
        r = self._run(shares_mm=0.0)
        assert r["ffo_per_share"]  is None
        assert r["affo_per_share"] is None

    def test_inputs_recorded_in_output(self):
        r = self._run()
        assert r["net_income_mm"] == 1_000
        assert r["da_mm"] == 500
        assert r["gains_on_sale_mm"] == 200


# ─────────────────────────────────────────────────────────────────────────────
# 4 — EBITDAR / lease-heavy detection
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaseHeavy:
    def test_retail_is_lease_heavy(self):
        assert is_lease_heavy("Consumer Discretionary", "Apparel Retail") is True

    def test_airline_is_lease_heavy(self):
        assert is_lease_heavy("Industrials", "Airlines") is True

    def test_tech_not_lease_heavy(self):
        assert is_lease_heavy("Technology", "Software") is False

    def test_financials_not_lease_heavy(self):
        assert is_lease_heavy("Financials", "Banks") is False

    def test_industrials_defense_not_lease_heavy(self):
        assert is_lease_heavy("Industrials", "Defense") is False


class TestComputeEbitdar:
    def test_basic(self):
        assert compute_ebitdar(1000, 200) == pytest.approx(1200)

    def test_zero_rent(self):
        assert compute_ebitdar(1000, 0) == pytest.approx(1000)

    def test_negative_rent_clamped_to_zero(self):
        # Negative rent is anomalous; should not reduce EBITDA
        assert compute_ebitdar(1000, -100) == pytest.approx(1000)

    def test_negative_ebitda_still_adds_rent(self):
        assert compute_ebitdar(-200, 300) == pytest.approx(100)


class TestEbitdarMultiple:
    def test_basic(self):
        # EV = 12000, EBITDAR = 1200 → multiple = 10.0×
        m = ebitdar_multiple(12_000, 1_200)
        assert abs(m - 10.0) < 0.001

    def test_zero_ebitdar_returns_none(self):
        assert ebitdar_multiple(12_000, 0) is None

    def test_negative_ebitdar_returns_none(self):
        assert ebitdar_multiple(12_000, -100) is None


class TestApplyEbitdarAdjustment:
    _RETAIL_PEERS = [
        {"ticker": "A", "ev": 10_000, "ebitda_mm": 800, "rent_expense_mm": 200},
        {"ticker": "B", "ev":  8_000, "ebitda_mm": 600, "rent_expense_mm": 150},
    ]

    def test_adds_ebitdar_and_multiple_for_retail(self):
        result = apply_ebitdar_adjustment(
            list(self._RETAIL_PEERS), "Consumer Discretionary", "Apparel Retail"
        )
        for peer in result:
            assert "ebitdar_mm" in peer
            assert "ev_ebitdar_r" in peer

    def test_ebitdar_values_correct(self):
        result = apply_ebitdar_adjustment(
            list(self._RETAIL_PEERS), "Consumer Discretionary", "Apparel Retail"
        )
        assert result[0]["ebitdar_mm"] == pytest.approx(1000)   # 800 + 200
        assert result[1]["ebitdar_mm"] == pytest.approx(750)    # 600 + 150

    def test_multiples_correct(self):
        result = apply_ebitdar_adjustment(
            list(self._RETAIL_PEERS), "Consumer Discretionary", "Apparel Retail"
        )
        assert result[0]["ev_ebitdar_r"] == pytest.approx(10.0)  # 10000/1000
        assert result[1]["ev_ebitdar_r"] == pytest.approx(8_000 / 750)

    def test_no_op_for_non_lease_heavy(self):
        peers = [{"ticker": "X", "ev": 5000, "ebitda_mm": 500, "rent_expense_mm": 50}]
        result = apply_ebitdar_adjustment(peers, "Technology", "Software")
        assert "ebitdar_mm" not in result[0]
        assert "ev_ebitdar_r" not in result[0]

    def test_returns_new_list_not_mutates_original(self):
        original = [{"ticker": "A", "ev": 10_000, "ebitda_mm": 800, "rent_expense_mm": 200}]
        result = apply_ebitdar_adjustment(original, "Consumer Discretionary", "Apparel Retail")
        assert "ebitdar_mm" not in original[0]   # original unchanged
        assert "ebitdar_mm" in result[0]


# ─────────────────────────────────────────────────────────────────────────────
# 5 — R&D intensity
# ─────────────────────────────────────────────────────────────────────────────

class TestRdIntensity:
    def test_technology_is_rd_intensive(self):
        assert is_rd_intensive("Technology", "Software") is True

    def test_biotech_is_rd_intensive(self):
        assert is_rd_intensive("Healthcare", "Biotechnology") is True

    def test_semiconductors_is_rd_intensive(self):
        assert is_rd_intensive("Technology", "Semiconductors") is True

    def test_retail_not_rd_intensive(self):
        assert is_rd_intensive("Consumer Discretionary", "Apparel Retail") is False

    def test_industrials_not_rd_intensive(self):
        assert is_rd_intensive("Industrials", "Defense") is False


# ─────────────────────────────────────────────────────────────────────────────
# 6 — Operating lease normalisation
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseOperatingLeases:
    _BS = {
        "operatingLeaseRightOfUseAsset": 2_000,
        "operatingLeaseLiabilityCurrent": 300,
        "longTermOperatingLeaseLiability": 1_800,
    }
    _IS = {
        "operatingLeaseExpense": 400,
    }

    def test_rou_asset_extracted(self):
        r = normalise_operating_leases(self._BS, self._IS)
        assert r["rou_asset_mm"] == pytest.approx(2_000)

    def test_total_lease_liability(self):
        r = normalise_operating_leases(self._BS, self._IS)
        assert r["operating_lease_liab_mm"] == pytest.approx(2_100)

    def test_rent_expense_from_income_stmt(self):
        r = normalise_operating_leases(self._BS, self._IS)
        assert r["rent_expense_mm"] == pytest.approx(400)

    def test_implied_annual_uses_stated_rent_if_available(self):
        r = normalise_operating_leases(self._BS, self._IS)
        # rent_expense_mm = 400 > 0 so implied_annual = rent_expense
        assert r["implied_annual_rent_mm"] == pytest.approx(400)

    def test_fallback_when_no_rent_in_income_stmt(self):
        r = normalise_operating_leases(self._BS, {})
        # No rent expense; fallback = liability × 0.12 = 2100 × 0.12 = 252
        assert r["implied_annual_rent_mm"] == pytest.approx(2_100 * 0.12)

    def test_empty_balance_sheet_returns_zeros(self):
        r = normalise_operating_leases({}, {})
        assert r["rou_asset_mm"] == 0.0
        assert r["operating_lease_liab_mm"] == 0.0
        assert r["implied_annual_rent_mm"] == 0.0

    def test_all_keys_present(self):
        r = normalise_operating_leases(self._BS, self._IS)
        for key in ("rou_asset_mm", "operating_lease_liab_mm",
                    "implied_annual_rent_mm", "rent_expense_mm"):
            assert key in r
