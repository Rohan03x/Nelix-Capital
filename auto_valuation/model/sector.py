"""
model/sector.py — Sector-specific model adjustments and gating logic.

Reference: Architecture Plan Parts 26, 35, 59.

Supported sector types
──────────────────────
  STANDARD  : All other GICS sectors; UFCF DCF applies without modification.
  FINANCIAL : GICS 40 Financials — DCF not supported; raises UnsupportedCompanyError.
  REIT      : GICS 60 Real Estate — standard DCF not appropriate; FFO/AFFO model used.
  RETAIL    : Consumer Discretionary/Staples with high operating lease costs —
              EBITDAR adjustment normalises for lease costs.
  AIRLINE   : Airlines — EBITDAR adjustment applied; very high lease intensity.
  MINING    : Energy/Materials mining — NAV model required; raises UnsupportedCompanyError.
  TECH_RD   : Technology/Biotech with material R&D; R&D capitalisation available
              (optional, controlled by config.rd_capitalise flag).

All monetary values in USD millions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from auto_valuation.utils.error import UnsupportedCompanyError, safe_divide


# ─────────────────────────────────────────────────────────────────────────────
# Sector type constants
# ─────────────────────────────────────────────────────────────────────────────

STANDARD  = "standard"
FINANCIAL = "financial"
REIT      = "reit"
RETAIL    = "retail"
AIRLINE   = "airline"
MINING    = "mining"
TECH_RD   = "tech_rd"


# FMP sector / industry keyword maps ─────────────────────────────────────────

_FINANCIAL_SECTORS = frozenset({"financials", "banking", "insurance"})

_REIT_SECTORS      = frozenset({"real estate"})
_REIT_INDUSTRIES   = frozenset({
    "reit", "real estate investment trust",
    "diversified reit", "industrial reit", "retail reit",
    "office reit", "residential reit", "mortgage reit",
    "healthcare reit", "specialty reit",
})

_RETAIL_INDUSTRIES = frozenset({
    "apparel retail", "broadline retail", "specialty retail",
    "internet retail", "home improvement retail",
    "grocery stores", "food retail", "department stores",
    "drug retail", "auto parts",
})

_AIRLINE_INDUSTRIES = frozenset({
    "airlines", "passenger airlines", "air freight & logistics",
    "airport services",
})

_MINING_SECTORS    = frozenset({"basic materials", "materials"})
_MINING_INDUSTRIES = frozenset({
    "gold", "silver", "copper", "mining", "steel",
    "aluminum", "diversified metals", "coal", "oil & gas",
    "oil & gas exploration", "oil & gas drilling", "uranium",
})

_TECH_RD_SECTORS   = frozenset({"technology", "information technology"})
_RD_INTENSIVE_INDUSTRIES = frozenset({
    "software", "semiconductors", "biotechnology", "pharmaceuticals",
    "drug manufacturers", "life sciences tools",
    "medical devices", "internet software & services",
})


# ─────────────────────────────────────────────────────────────────────────────
# Sector detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_sector_type(sector: str, industry: str = "") -> str:
    """
    Classify a company into one of the supported sector types based on
    FMP profile strings.

    Parameters
    ----------
    sector   : profile["sector"]   (e.g. "Technology", "Financials")
    industry : profile["industry"] (e.g. "Software—Application")

    Returns
    -------
    One of: STANDARD | FINANCIAL | REIT | RETAIL | AIRLINE | MINING | TECH_RD

    Reference: Architecture Plan Parts 35, 26, 59.
    """
    s = sector.lower().strip()
    i = industry.lower().strip()

    # Financials (GICS 40) — highest-priority gate
    if s in _FINANCIAL_SECTORS or any(kw in s for kw in ("bank", "insur", "financial")):
        return FINANCIAL

    # Real Estate / REITs (GICS 60)
    if s in _REIT_SECTORS or any(kw in i for kw in _REIT_INDUSTRIES):
        return REIT

    # Airline (before generic industrials check)
    if any(kw in i for kw in _AIRLINE_INDUSTRIES):
        return AIRLINE

    # Retail (Consumer Discretionary / Staples with lease-heavy business)
    if any(kw in i for kw in _RETAIL_INDUSTRIES):
        return RETAIL

    # Mining / Resources
    if s in _MINING_SECTORS or any(kw in i for kw in _MINING_INDUSTRIES):
        return MINING

    # Technology / Biotech with R&D intensity
    if s in _TECH_RD_SECTORS or any(kw in i for kw in _RD_INTENSIVE_INDUSTRIES):
        return TECH_RD

    return STANDARD


# ─────────────────────────────────────────────────────────────────────────────
# Gate functions — raise if sector not supported by UFCF DCF
# ─────────────────────────────────────────────────────────────────────────────

def financial_company_gate(sector: str, industry: str = "") -> None:
    """
    Raise UnsupportedCompanyError (exit 4) if the company is a Financial.

    Financial companies (banks, insurance, diversified financials) are not
    appropriate for UFCF-based DCF because:
      • Interest income/expense is an operating item, not a financing item.
      • Working capital concepts do not apply.
      • Regulatory capital requirements dominate capital structure.

    Reference: Architecture Plan Part 35.

    Raises
    ------
    UnsupportedCompanyError if sector == "Financials" (GICS 40).
    """
    if detect_sector_type(sector, industry) == FINANCIAL:
        raise UnsupportedCompanyError(
            f"Company in sector '{sector}' (industry: '{industry}') is classified as "
            "a Financial. UFCF-based DCF is not appropriate for banks, insurance "
            "companies, or diversified financials. Use a dividend-discount model or "
            "excess-return model instead.",
        )


def reit_company_gate(sector: str, industry: str = "") -> None:
    """
    Raise UnsupportedCompanyError if the company is a REIT.

    REITs use FFO/AFFO as the primary valuation metric, not UFCF.
    Use reit_ffo_affo_model() to compute FFO/AFFO metrics instead.

    Reference: Architecture Plan Part 59.1.

    Raises
    ------
    UnsupportedCompanyError if sector == "Real Estate" (GICS 60).
    """
    if detect_sector_type(sector, industry) == REIT:
        raise UnsupportedCompanyError(
            f"Company in sector '{sector}' (industry: '{industry}') is classified as "
            "a REIT. Standard UFCF DCF does not apply. Use FFO/AFFO model instead. "
            "Call reit_ffo_affo_model() to compute Funds from Operations.",
        )


def mining_company_gate(sector: str, industry: str = "") -> None:
    """
    Raise UnsupportedCompanyError if the company is a Mining/Resources company.

    Mining companies require a Net Asset Value (NAV) model based on
    reserve life and commodity price curves, not a UFCF DCF.

    Reference: Architecture Plan Part 59.2.

    Raises
    ------
    UnsupportedCompanyError if sector/industry matches mining keywords.
    """
    if detect_sector_type(sector, industry) == MINING:
        raise UnsupportedCompanyError(
            f"Company in sector '{sector}' (industry: '{industry}') is classified as "
            "a Mining/Resources company. UFCF DCF is not appropriate — use a "
            "Net Asset Value (NAV) model based on reserve life and commodity prices.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# REIT  —  FFO / AFFO model  (Part 59.1)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class REITMetrics:
    net_income_mm:        float
    da_mm:                float
    gains_on_sale_mm:     float
    maintenance_capex_mm: float
    straight_line_rent_mm: float

    ffo_mm:  float = 0.0    # Funds from Operations
    affo_mm: float = 0.0    # Adjusted FFO
    p_ffo:   float | None = None   # Price / FFO
    p_affo:  float | None = None   # Price / AFFO


def reit_ffo_affo_model(
    net_income_mm:        float,
    da_mm:                float,
    gains_on_sale_mm:     float = 0.0,
    maintenance_capex_mm: float = 0.0,
    straight_line_rent_adj_mm: float = 0.0,
    shares_mm:            float = 1.0,
    price_per_share:      float | None = None,
) -> dict[str, Any]:
    """
    Compute FFO and AFFO for a REIT.

    Definitions (NAREIT standard):
      FFO  = Net Income + D&A − Gains on Sale of Real Estate
      AFFO = FFO − Maintenance CapEx − Straight-Line Rent Adjustment

    Parameters
    ----------
    net_income_mm        : Reported net income (USD mm)
    da_mm                : Depreciation & amortization (USD mm)
    gains_on_sale_mm     : Gains on disposal of real estate assets (USD mm, ≥ 0)
    maintenance_capex_mm : Recurring maintenance capital expenditure (USD mm, ≥ 0)
    straight_line_rent_adj_mm : Non-cash straight-line rent adjustment (USD mm)
    shares_mm            : Diluted shares outstanding (millions)
    price_per_share      : Current market price for P/FFO and P/AFFO computation

    Returns
    -------
    Dict with ffo_mm, affo_mm, ffo_per_share, affo_per_share, p_ffo, p_affo.

    Reference: Architecture Plan Part 59.1.
    """
    ffo  = net_income_mm + da_mm - gains_on_sale_mm
    affo = ffo - maintenance_capex_mm - straight_line_rent_adj_mm

    ffo_per_share  = safe_divide(ffo,  shares_mm, None)
    affo_per_share = safe_divide(affo, shares_mm, None)

    p_ffo:  float | None = None
    p_affo: float | None = None
    if price_per_share and ffo_per_share and ffo_per_share > 0:
        p_ffo = price_per_share / ffo_per_share
    if price_per_share and affo_per_share and affo_per_share > 0:
        p_affo = price_per_share / affo_per_share

    return {
        "ffo_mm":          ffo,
        "affo_mm":         affo,
        "ffo_per_share":   ffo_per_share,
        "affo_per_share":  affo_per_share,
        "p_ffo":           p_ffo,
        "p_affo":          p_affo,
        # Inputs (for audit trail)
        "net_income_mm":        net_income_mm,
        "da_mm":                da_mm,
        "gains_on_sale_mm":     gains_on_sale_mm,
        "maintenance_capex_mm": maintenance_capex_mm,
        "straight_line_rent_adj_mm": straight_line_rent_adj_mm,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EBITDAR  —  Retail & Airlines  (Part 26)
# ─────────────────────────────────────────────────────────────────────────────

def is_lease_heavy(sector: str, industry: str = "") -> bool:
    """
    Return True if the company is in a lease-heavy industry (Retail, Airlines)
    where EBITDAR is the preferred normalised earnings metric.

    Reference: Architecture Plan Part 26.
    """
    t = detect_sector_type(sector, industry)
    return t in (RETAIL, AIRLINE)


def compute_ebitdar(
    ebitda_mm: float,
    rent_expense_mm: float,
) -> float:
    """
    Compute EBITDAR = EBITDA + rent expense (operating lease payments).

    Under old US GAAP (pre-ASC 842) or for normalisation purposes, rent
    expense is added back to EBITDA to produce a metric that is independent
    of lease-vs-buy financing decisions.

    Under ASC 842 / IFRS 16 (post-2019), operating leases appear as:
      • Depreciation of right-of-use (ROU) asset — already in D&A → EBITDA
      • Interest on lease liability — below EBIT
    So for post-2019 statements, EBITDA may already be close to EBITDAR.
    Use rent_expense_mm = 0 if the statements are already post-ASC 842.

    Parameters
    ----------
    ebitda_mm       : LTM EBITDA (USD mm)
    rent_expense_mm : Gross operating lease / rent expense (USD mm, ≥ 0)

    Returns
    -------
    EBITDAR in USD mm.

    Reference: Architecture Plan Part 26.
    """
    return ebitda_mm + max(0.0, rent_expense_mm)


def ebitdar_multiple(ev_mm: float, ebitdar_mm: float) -> float | None:
    """
    Compute EV / EBITDAR multiple.  Returns None if EBITDAR ≤ 0.

    Reference: Architecture Plan Part 26.
    """
    return safe_divide(ev_mm, ebitdar_mm, None) if ebitdar_mm > 0 else None


def apply_ebitdar_adjustment(
    peer_data: list[dict],
    sector: str,
    industry: str = "",
) -> list[dict]:
    """
    For Retail or Airline peers, compute EBITDAR and the EV/EBITDAR multiple
    and add them to each peer dict.

    Expects each peer dict to contain:
      rent_expense_mm : gross rent / operating lease expense
      ebitda_mm       : LTM EBITDA
      ev              : enterprise value

    Returns the mutated list with 'ebitdar_mm' and 'ev_ebitdar_r' keys added.

    Reference: Architecture Plan Part 26.
    """
    if not is_lease_heavy(sector, industry):
        return peer_data

    result = []
    for peer in peer_data:
        peer = dict(peer)
        rent = peer.get("rent_expense_mm") or 0.0
        ebitda = peer.get("ebitda_mm") or 0.0
        ev = peer.get("ev") or 0.0
        ebitdar = compute_ebitdar(ebitda, rent)
        peer["ebitdar_mm"]  = ebitdar
        peer["ev_ebitdar_r"] = ebitdar_multiple(ev, ebitdar)
        result.append(peer)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# R&D intensity flag  (Part 55.2)
# ─────────────────────────────────────────────────────────────────────────────

def is_rd_intensive(sector: str, industry: str = "") -> bool:
    """
    Return True if the company is in a sector where R&D capitalisation
    is commonly applied (Technology, Healthcare Biotech/Pharma).

    R&D capitalisation (capitalise_rd in cleaner.py) is always opt-in via
    config.rd_capitalise.  This helper indicates when it is *worth considering*.

    Reference: Architecture Plan Part 55.2.
    """
    return detect_sector_type(sector, industry) == TECH_RD


# ─────────────────────────────────────────────────────────────────────────────
# Operating lease normalisation  (ASC 842 / IFRS 16, Part 75)
# ─────────────────────────────────────────────────────────────────────────────

def normalise_operating_leases(
    balance_sheet: dict,
    income_stmt: dict,
) -> dict[str, Any]:
    """
    Extract operating lease data from a post-ASC 842 / IFRS 16 balance sheet
    and compute the implied annual lease cost for EBITDAR normalisation.

    Under ASC 842/IFRS 16, operating leases are capitalised as:
      • Right-of-use (ROU) asset  — asset side of balance sheet
      • Operating lease liability — liability side

    The EBITDA under post-lease-standard financials already excludes cash rent;
    instead, D&A includes ROU depreciation.  To compute 'EBITDA pre-lease',
    add back the operating lease depreciation (= ROU / remaining lease term).

    Parameters
    ----------
    balance_sheet : latest annual or TTM balance sheet dict
    income_stmt   : matching income statement dict

    Returns
    -------
    Dict with:
      rou_asset_mm            : right-of-use asset (mm)
      operating_lease_liab_mm : operating lease liability (current + non-current, mm)
      implied_annual_rent_mm  : estimated annual operating lease cost
      rent_expense_mm         : reported rent / lease expense (if available)
    """
    # ROU asset (FMP canonical / standard field names)
    rou = (
        balance_sheet.get("operatingLeaseRightOfUseAsset")
        or balance_sheet.get("right_of_use_asset")
        or 0.0
    )

    # Operating lease liabilities
    op_lease_curr = (
        balance_sheet.get("operatingLeaseLiability")
        or balance_sheet.get("operatingLeaseLiabilityCurrent")
        or 0.0
    )
    op_lease_nc = (
        balance_sheet.get("operatingLeaseLiabilityNonCurrent")
        or balance_sheet.get("longTermOperatingLeaseLiability")
        or 0.0
    )
    total_op_lease_liab = (op_lease_curr or 0.0) + (op_lease_nc or 0.0)

    # Rent / lease expense from income statement (pre-ASC842 or supplemental)
    rent_exp = (
        income_stmt.get("rent_expense")
        or income_stmt.get("rentExpenses")
        or income_stmt.get("operatingLeaseExpense")
        or 0.0
    )

    # Estimate annual rent from liability if not directly available
    # Using simplistic assumption: remaining lease term ≈ liability / current payment
    # Better estimate: use reported rent if available; fallback = liability × 0.12
    implied_annual = rent_exp if rent_exp > 0 else total_op_lease_liab * 0.12

    return {
        "rou_asset_mm":             float(rou or 0.0),
        "operating_lease_liab_mm":  float(total_op_lease_liab),
        "implied_annual_rent_mm":   float(implied_annual),
        "rent_expense_mm":          float(rent_exp),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience — unified sector gate (call once from main.py)
# ─────────────────────────────────────────────────────────────────────────────

def apply_sector_gate(
    sector: str,
    industry: str = "",
    allow_reit: bool = False,
) -> str:
    """
    Run all sector gates in one call and return the detected sector type.

    Raises UnsupportedCompanyError for Financials and Mining.
    If allow_reit=False (default), also raises for REITs.
    Returns the sector type string for downstream branching.

    Usage in main.py::run_valuation()
    ----------------------------------
    sector_type = apply_sector_gate(sector, industry)
    if sector_type in (RETAIL, AIRLINE):
        # apply EBITDAR normalisation
    if sector_type == TECH_RD and cfg.rd_capitalise:
        # R&D capitalisation already handled in cleaner

    Reference: Architecture Plan Parts 35, 59.
    """
    t = detect_sector_type(sector, industry)

    if t == FINANCIAL:
        financial_company_gate(sector, industry)  # always raises

    if t == MINING:
        mining_company_gate(sector, industry)      # always raises

    if t == REIT and not allow_reit:
        reit_company_gate(sector, industry)        # raises unless caller opts-in

    return t


# ─────────────────────────────────────────────────────────────────────────────
# Checklist-canonical aliases & helpers (Part 80)
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical checklist name → apply_ebitdar_adjustment
ebitdar_adjustment = apply_ebitdar_adjustment

#: Canonical checklist name → mining_company_gate
mining_nav_unsupported = mining_company_gate
