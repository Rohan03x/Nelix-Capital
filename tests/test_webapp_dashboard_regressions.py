from __future__ import annotations

import copy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from webapp.data.ai_commentary import generate_commentary
from webapp.data import fmp_client
from webapp.data.eodhd_client import _derive_ebit_margin_target
from webapp.data.excel_export import build_excel_bytes
from webapp.data.samples import REGISTRY, _apply_overrides


def _midyear_annuity(rate_pct: float, years: int) -> float:
    rate = rate_pct / 100
    return sum(1 / (1 + rate) ** (period - 0.5) for period in range(1, years + 1))


def test_apply_overrides_matches_corrected_dcf_math() -> None:
    base = copy.deepcopy(REGISTRY["NKE"])
    overridden = copy.deepcopy(REGISTRY["NKE"])

    _apply_overrides(overridden, {"wacc": 9.4, "g": 2.5})

    forecast_years = max(len(base.get("forecast") or []), 7)
    expected_pv_tv = (
        base["terminal_ufcf"]
        * (1 + 2.5 / 100)
        / ((9.4 - 2.5) / 100)
        / (1 + 9.4 / 100) ** forecast_years
    )
    expected_pv_uf = base["pv_ufcfs"] * (
        _midyear_annuity(9.4, forecast_years) / _midyear_annuity(base["wacc"], forecast_years)
    )
    expected_iv = (expected_pv_tv + expected_pv_uf - base["net_debt"]) / base["diluted_shares"]

    assert overridden["intrinsic_value"] == pytest.approx(expected_iv, abs=0.01)

    sensitivity = overridden["sensitivity"]
    # Correct orientation: rows = g values, columns = wacc values
    # iv_grid[g_idx][wacc_idx]
    center = sensitivity["iv_grid"][sensitivity["base_g_idx"]][sensitivity["base_wacc_idx"]]
    assert center == pytest.approx(overridden["intrinsic_value"], abs=0.1)

    # Along a fixed g row: higher wacc (rightmost) → lower IV
    mid_row = sensitivity["base_g_idx"]
    assert sensitivity["iv_grid"][mid_row][0] > sensitivity["iv_grid"][mid_row][-1]

    # Along a fixed wacc column: higher g (bottom row) → higher IV
    mid_col = sensitivity["base_wacc_idx"]
    assert sensitivity["iv_grid"][0][mid_col] < sensitivity["iv_grid"][-1][mid_col]


def test_excel_export_forecast_rows_follow_updated_forecast_inputs() -> None:
    data = copy.deepcopy(REGISTRY["NKE"])
    _apply_overrides(data, {"wacc": 9.4, "g": 2.5})

    workbook = load_workbook(BytesIO(build_excel_bytes(data)), data_only=False)
    sheet = workbook["valuation"]
    forecast_cols = list(range(12, 19))

    interest_expense = [sheet.cell(348, col).value for col in forecast_cols]
    diluted_shares = [sheet.cell(371, col).value for col in forecast_cols]
    supplemental_shares = [sheet.cell(543, col).value for col in forecast_cols]
    residual_nwc = [sheet.cell(444, col).value for col in forecast_cols]
    change_in_nwc = [sheet.cell(475, col).value for col in forecast_cols]

    assert all(value < 0 for value in interest_expense)
    assert diluted_shares[0] > diluted_shares[-1]
    assert supplemental_shares == diluted_shares
    assert any(abs(value) > 1e-6 for value in residual_nwc)

    component_sums = [
        sum(sheet.cell(row, col).value for row in (441, 442, 443, 444))
        for col in forecast_cols
    ]
    for total, nowc in zip(component_sums, change_in_nwc):
        assert total == pytest.approx(-nowc, abs=1e-6)

    first_forecast_col = forecast_cols[0]
    assert sheet.cell(321, first_forecast_col).value == pytest.approx(
        sheet.cell(339, first_forecast_col).value / sheet.cell(337, first_forecast_col).value,
        abs=1e-9,
    )
    assert "Levered Hist." in sheet.cell(474, 1).value


def test_excel_export_uses_display_currency_and_profile_metadata() -> None:
    data = copy.deepcopy(REGISTRY["NKE"])
    data.update(
        {
            "display_currency": "EUR",
            "display_currency_symbol": "€",
            "currency": "EUR",
            "model_currency": "USD",
            "quote_currency": "USD",
            "exchange": "EPA",
            "sector": "Industrials",
            "industry": "Electrical Equipment",
            "price_date": "2025-05-01",
            "confidence_breakdown": {
                "suitability_note": "Use DCF with caution for capital-intensive cyclicals.",
            },
        }
    )

    workbook = load_workbook(BytesIO(build_excel_bytes(data)), data_only=False)
    sheet = workbook["valuation"]

    assert "EUR M" in sheet["B5"].value
    assert "model currency USD" in sheet["B5"].value
    assert sheet["B6"].value == "EPA | Industrials | Electrical Equipment"
    assert "capital-intensive cyclicals" in sheet["B7"].value
    assert sheet["A11"].value == "Current Market Price (EUR/share)"
    assert sheet["A35"].value == "DCF Intrinsic Value (EUR/share)"


def test_fmp_cached_reads_disk_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fmp_client, "_CACHE_DIR", tmp_path)
    fmp_client._CACHE.clear()

    first = fmp_client._cached("fred:dgs10", lambda: {"rate": 4.2})
    assert first == {"rate": 4.2}
    assert (tmp_path / "fmp_fred_dgs10.json").exists()

    fmp_client._CACHE.clear()
    second = fmp_client._cached("fred:dgs10", lambda: {"rate": 9.9})
    assert second == {"rate": 4.2}


def test_ai_commentary_uses_correct_premium_discount_wording() -> None:
    undervalued = generate_commentary(
        {
            "company_name": "Shell plc",
            "price": 45.30,
            "intrinsic_value": 129.79,
            "upside_pct": 186.5,
            "wacc": 6.5,
            "terminal_growth": 2.5,
            "tv_pct": 78.0,
            "confidence_score": 62,
        }
    )["valuation_summary"]
    overvalued = generate_commentary(
        {
            "company_name": "Tesla Inc",
            "price": 381.63,
            "intrinsic_value": 20.65,
            "upside_pct": -94.6,
            "wacc": 12.8,
            "terminal_growth": 2.5,
            "tv_pct": 57.8,
            "confidence_score": 61,
        }
    )["valuation_summary"]

    assert "premium to the current market price" in undervalued
    assert "discount to the current market price" in overvalued


def test_ebit_margin_target_respects_recent_profitable_regime_shift() -> None:
    target, source = _derive_ebit_margin_target(
        5.9,
        [-7.8, -14.8, -1.6, 0.1, 6.0, 12.4, 17.5, 10.5, 9.6, 5.9],
        24.6,
        "Auto Manufacturers",
    )

    assert target == pytest.approx(12.2, abs=0.1)
    assert source == "Recent profitable regime + historical anchor"