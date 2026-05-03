"""
tests/conftest.py — Shared pytest fixtures for all test modules.

Reference: Architecture Plan Part 49.2.
"""

from __future__ import annotations

import pytest
from pathlib import Path

# ── Root path fixture ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


# ── Minimal fake FMP financial data ───────────────────────────────────────────
# Mirrors real FMP response shapes. Used by unit tests so no live API calls needed.

@pytest.fixture
def fake_income_statement() -> list[dict]:
    """5 years of synthetic income statement data (most-recent-first, like FMP)."""
    return [
        {
            "date": "2023-05-31", "symbol": "NKE", "calendarYear": "2023",
            "period": "FY", "revenue": 51217, "costOfRevenue": 28925,
            "grossProfit": 22292, "researchAndDevelopmentExpenses": 0,
            "sellingGeneralAndAdministrativeExpenses": 13667,
            "operatingIncome": 6201, "interestExpense": 143,
            "incomeTaxExpense": 1131, "netIncome": 5070,
            "depreciationAndAmortization": 521, "ebitda": 6722,
        },
        {
            "date": "2022-05-31", "symbol": "NKE", "calendarYear": "2022",
            "period": "FY", "revenue": 46710, "costOfRevenue": 25231,
            "grossProfit": 21479, "researchAndDevelopmentExpenses": 0,
            "sellingGeneralAndAdministrativeExpenses": 12475,
            "operatingIncome": 5798, "interestExpense": 129,
            "incomeTaxExpense": 605, "netIncome": 5147,
            "depreciationAndAmortization": 498, "ebitda": 6296,
        },
        {
            "date": "2021-05-31", "symbol": "NKE", "calendarYear": "2021",
            "period": "FY", "revenue": 44538, "costOfRevenue": 24576,
            "grossProfit": 19962, "researchAndDevelopmentExpenses": 0,
            "sellingGeneralAndAdministrativeExpenses": 10954,
            "operatingIncome": 4979, "interestExpense": 175,
            "incomeTaxExpense": 934, "netIncome": 5745,
            "depreciationAndAmortization": 477, "ebitda": 5456,
        },
    ]


@pytest.fixture
def fake_balance_sheet() -> list[dict]:
    return [
        {
            "date": "2023-05-31", "symbol": "NKE",
            "cashAndCashEquivalents": 9403, "shortTermInvestments": 0,
            "netReceivables": 4131, "inventory": 8508,
            "totalCurrentAssets": 22186,
            "propertyPlantEquipmentNet": 2254, "goodwill": 273,
            "intangibleAssets": 274, "totalAssets": 37646,
            "totalCurrentLiabilities": 9754,
            "longTermDebt": 8925, "shortTermDebt": 0,
            "totalLiabilities": 27831,
            "commonStock": 1, "retainedEarnings": 9596,
            "totalStockholdersEquity": 9815, "totalEquity": 9815,
            "minorityInterest": 0,
        },
    ]


@pytest.fixture
def fake_cash_flow() -> list[dict]:
    return [
        {
            "date": "2023-05-31", "symbol": "NKE",
            "netIncome": 5070, "depreciationAndAmortization": 521,
            "changeInWorkingCapital": -1200,
            "capitalExpenditure": -879,
            "stockBasedCompensation": 517,
            "operatingCashFlow": 4770, "freeCashFlow": 3891,
        },
    ]


@pytest.fixture
def fake_profile() -> dict:
    return {
        "symbol": "NKE", "companyName": "NIKE Inc.",
        "exchange": "NYSE", "currency": "USD",
        "sector": "Consumer Discretionary",
        "industry": "Footwear & Accessories",
        "mktCap": 151_000,          # USD millions
        "price": 96.42,
        "beta": 0.87,
        "ipoDate": "1980-12-02",
    }
