"""
data/validator.py — Strict FMP data quality gate with halt logic.

This module is DISTINCT from validation/checks.py:
  - validation/checks.py:  advisory checks that return ValidationResult objects
  - data/validator.py:     strict gate that RAISES DataQualityError on critical failures

Use this early in the pipeline (before any modelling) to reject data that
cannot support a valid DCF.

Reference: Architecture Plan Part 61.1, v9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Error class
# ─────────────────────────────────────────────────────────────────────────────

class DataQualityError(ValueError):
    """
    Raised when FMP data fails a critical quality gate.
    Pipeline should halt and return exit code 3.
    """
    def __init__(self, message: str, checks: list["DataCheck"] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or []


# ─────────────────────────────────────────────────────────────────────────────
# DataCheck result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DataCheck:
    """Individual data quality check outcome."""
    name:     str
    passed:   bool
    severity: str   # "HALT" | "WARN" | "INFO"
    message:  str   = ""
    value:    Any   = None


# ─────────────────────────────────────────────────────────────────────────────
# Critical field lists
# ─────────────────────────────────────────────────────────────────────────────

_HALT_IS_FIELDS: list[str] = [
    "revenue",
    "ebit",
    "netIncome",
]

_HALT_BS_FIELDS: list[str] = [
    "totalAssets",
    "totalEquity",
]

_HALT_CF_FIELDS: list[str] = [
    "operatingCashFlow",
    "capitalExpenditure",
]

# Fields that are WARN only if missing (not HALT)
_WARN_IS_FIELDS: list[str] = [
    "grossProfit",
    "depreciationAndAmortization",
    "stockBasedCompensation",
    "incomeTaxExpense",
]

_WARN_BS_FIELDS: list[str] = [
    "shortTermDebt",
    "longTermDebt",
    "cashAndCashEquivalents",
    "totalCurrentAssets",
    "totalCurrentLiabilities",
]


# ─────────────────────────────────────────────────────────────────────────────
# Individual checkers
# ─────────────────────────────────────────────────────────────────────────────

def _count_populated(
    stmts: list[dict],
    field: str,
    years: int = 5,
) -> int:
    """Count how many of the last `years` records have a non-None value for `field`."""
    return sum(
        1 for s in stmts[:years]
        if s.get(field) is not None
    )


def _check_critical_fields(
    stmts: list[dict],
    fields: list[str],
    stmt_label: str,
    min_populated: int = 3,
) -> list[DataCheck]:
    checks: list[DataCheck] = []
    for fld in fields:
        populated = _count_populated(stmts, fld)
        if populated == 0:
            checks.append(DataCheck(
                name=f"HALT_{stmt_label}_{fld}",
                passed=False,
                severity="HALT",
                message=(
                    f"Critical field '{fld}' is absent in all {stmt_label} records. "
                    "Cannot build DCF model."
                ),
                value=0,
            ))
        elif populated < min_populated:
            checks.append(DataCheck(
                name=f"WARN_{stmt_label}_{fld}",
                passed=True,        # not a halt — model can still proceed
                severity="WARN",
                message=(
                    f"Field '{fld}' available in only {populated} of 5 {stmt_label} years. "
                    "Model accuracy may be reduced."
                ),
                value=populated,
            ))
        else:
            checks.append(DataCheck(
                name=f"OK_{stmt_label}_{fld}",
                passed=True,
                severity="INFO",
                message="",
                value=populated,
            ))
    return checks


def _check_revenue_not_negative(income_stmts: list[dict]) -> DataCheck:
    """Revenue must be positive in the most recent year — otherwise halt."""
    rev = (income_stmts[0].get("revenue") or 0) if income_stmts else 0
    if rev < 0:
        return DataCheck(
            name="HALT_REVENUE_NEGATIVE",
            passed=False,
            severity="HALT",
            message=f"Most recent revenue is negative (${rev:,.0f}M). Cannot model.",
            value=rev,
        )
    if rev == 0:
        return DataCheck(
            name="HALT_REVENUE_ZERO",
            passed=False,
            severity="HALT",
            message="Most recent revenue is zero. No data to model.",
            value=0,
        )
    return DataCheck(
        name="OK_REVENUE_POSITIVE", passed=True, severity="INFO", value=rev,
    )


def _check_stmt_count(
    stmts: list[dict],
    label: str,
    min_years: int = 3,
) -> DataCheck:
    """Need at least min_years of annual data to compute averages."""
    n = len(stmts)
    if n < min_years:
        return DataCheck(
            name=f"HALT_{label}_INSUFFICIENT_YEARS",
            passed=False,
            severity="HALT",
            message=(
                f"Only {n} year(s) of {label} data available "
                f"(minimum {min_years} required)."
            ),
            value=n,
        )
    return DataCheck(
        name=f"OK_{label}_YEARS", passed=True, severity="INFO", value=n,
    )


def _check_balance_sheet_identity(
    balance_sheets: list[dict],
    tolerance_mm: float = 100.0,
) -> list[DataCheck]:
    """
    Assets ≈ Liabilities + Equity in reported data.
    Large discrepancies suggest FMP unit errors or missing fields.
    """
    checks: list[DataCheck] = []
    for i, bs in enumerate(balance_sheets[:3]):
        total_assets     = bs.get("totalAssets") or 0
        total_equity     = bs.get("totalEquity") or 0
        total_liab_equity = bs.get("totalLiabilitiesAndStockholdersEquity") or 0

        if total_liab_equity > 0:
            diff = abs(total_assets - total_liab_equity)
        elif total_equity > 0 and total_assets > 0:
            # Approximate: assets - equity = implicit liabilities
            diff = 0.0   # can't verify without explicit liabilities figure
        else:
            diff = 0.0

        year = bs.get("calendarYear", str(i))
        if diff > tolerance_mm:
            checks.append(DataCheck(
                name=f"WARN_BS_IDENTITY_{year}",
                passed=True,   # WARN not HALT — data may still be usable
                severity="WARN",
                message=(
                    f"Balance sheet identity off by ${diff:,.0f}M in {year}. "
                    "Possible unit error or missing fields."
                ),
                value=diff,
            ))
        else:
            checks.append(DataCheck(
                name=f"OK_BS_IDENTITY_{year}", passed=True, severity="INFO", value=diff,
            ))
    return checks


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def validate_fmp_data_strict(
    income_stmts:  list[dict],
    balance_sheets: list[dict],
    cash_flows:    list[dict],
    min_is_years:  int = 3,
    min_bs_years:  int = 2,
    min_cf_years:  int = 2,
) -> list[DataCheck]:
    """
    Run all strict data quality checks.

    HALT checks: immediately raise DataQualityError if any fail.
    WARN checks: added to the returned list but do not raise.

    Returns: list of DataCheck results (all passed — fails are raised as exception).
    Raises:  DataQualityError if any HALT check fails.

    Reference: Architecture Plan Part 61.1 (v9).
    """
    all_checks: list[DataCheck] = []

    # ── 1. Sufficient history ────────────────────────────────────────────────
    all_checks.append(_check_stmt_count(income_stmts,  "IS", min_is_years))
    all_checks.append(_check_stmt_count(balance_sheets, "BS", min_bs_years))
    all_checks.append(_check_stmt_count(cash_flows,    "CF", min_cf_years))

    # ── 2. Revenue sanity ────────────────────────────────────────────────────
    all_checks.append(_check_revenue_not_negative(income_stmts))

    # ── 3. Critical IS / BS / CF fields ─────────────────────────────────────
    all_checks.extend(_check_critical_fields(income_stmts,  _HALT_IS_FIELDS, "IS"))
    all_checks.extend(_check_critical_fields(balance_sheets, _HALT_BS_FIELDS, "BS"))
    all_checks.extend(_check_critical_fields(cash_flows,    _HALT_CF_FIELDS, "CF"))

    # ── 4. Warning-level fields ──────────────────────────────────────────────
    all_checks.extend(_check_critical_fields(income_stmts, _WARN_IS_FIELDS, "IS_WARN", min_populated=1))
    all_checks.extend(_check_critical_fields(balance_sheets, _WARN_BS_FIELDS, "BS_WARN", min_populated=1))

    # ── 5. Balance sheet identity ────────────────────────────────────────────
    all_checks.extend(_check_balance_sheet_identity(balance_sheets))

    # ── Collect HALT failures ────────────────────────────────────────────────
    halt_checks = [c for c in all_checks if not c.passed and c.severity == "HALT"]
    if halt_checks:
        msg = "; ".join(c.message for c in halt_checks)
        raise DataQualityError(
            f"FMP data failed critical quality gate: {msg}",
            checks=halt_checks,
        )

    return all_checks


def get_data_quality_warnings(checks: list[DataCheck]) -> list[str]:
    """Extract human-readable warning strings from a DataCheck list."""
    return [c.message for c in checks if c.severity == "WARN" and c.message]
