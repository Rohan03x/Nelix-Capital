"""
model/sotp.py — Sum-of-the-Parts (SOTP) valuation.

Reference: Macabacus "Sum-of-the-Parts Analysis", Architecture Plan Part 45.2.

Methodology:
  1. Value each business segment independently using appropriate multiples or DCF.
  2. Sum segment EVs → aggregate EV.
  3. Deduct net debt (and other non-operating adjustments).
  4. Divide by diluted shares → equity per share.

Walk:
  Σ Segment EVs (EBITDA-based, EBIT-based, revenue-based, or DCF)
  + Non-operating assets (equity investments, held-for-sale, etc.)
  − Corporate overhead (valued at blended multiple or standalone)
  − Net debt (IBD + preferred + NCI − cash − ST investments)
  = Equity value
  ÷ Diluted shares
  = Equity per share

All monetary values in USD millions.  Shares in millions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from auto_valuation.utils.error import safe_divide

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SegmentValuation:
    """
    Single business segment with its financial metrics and chosen multiple.

    metric_type controls which statistic the multiple is applied to:
      'ebitda'  — EV/EBITDA multiple (most common)
      'ebit'    — EV/EBIT multiple
      'revenue' — EV/Revenue multiple (for pre-profit or SaaS segments)
      'dcf'     — Provide ev_dcf directly; multiple and metric_value are ignored
    """
    name:          str
    metric_value:  float                         # EBITDA / EBIT / Revenue ($M)
    multiple:      float                         # e.g. 10.0 for 10×
    metric_type:   Literal["ebitda", "ebit", "revenue", "dcf"] = "ebitda"
    ev_dcf:        float = 0.0                  # Used only when metric_type="dcf"
    minority_pct:  float = 0.0                  # % owned by minorities; deducted from EV

    @property
    def segment_ev(self) -> float:
        """Enterprise value of this segment."""
        if self.metric_type == "dcf":
            raw_ev = self.ev_dcf
        elif self.metric_value > 0:
            raw_ev = self.metric_value * self.multiple
        else:
            raw_ev = 0.0
        # Deduct minority interest portion
        if self.minority_pct > 0:
            raw_ev = raw_ev * (1.0 - self.minority_pct)
        return max(raw_ev, 0.0)

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "metric_type":  self.metric_type,
            "metric_value": self.metric_value,
            "multiple":     self.multiple,
            "ev_dcf":       self.ev_dcf,
            "minority_pct": self.minority_pct,
            "segment_ev":   self.segment_ev,
        }


@dataclass
class SOTPResult:
    """Output of SOTP analysis."""
    segment_evs:              list[dict]    = field(default_factory=list)
    total_segment_ev_mm:      float = 0.0
    non_operating_assets_mm:  float = 0.0
    corporate_overhead_mm:    float = 0.0   # negative value; deducted from EV
    total_ev_mm:              float = 0.0
    net_debt_mm:              float = 0.0
    equity_value_mm:          float = 0.0
    diluted_shares_mm:        float = 0.0
    equity_per_share:         float = 0.0
    premium_discount_pct:     float = 0.0   # vs. current price (if provided)
    warnings: list[str]               = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "segment_evs":             self.segment_evs,
            "total_segment_ev_mm":     self.total_segment_ev_mm,
            "non_operating_assets_mm": self.non_operating_assets_mm,
            "corporate_overhead_mm":   self.corporate_overhead_mm,
            "total_ev_mm":             self.total_ev_mm,
            "net_debt_mm":             self.net_debt_mm,
            "equity_value_mm":         self.equity_value_mm,
            "diluted_shares_mm":       self.diluted_shares_mm,
            "equity_per_share":        self.equity_per_share,
            "premium_discount_pct":    self.premium_discount_pct,
            "warnings":                self.warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Core SOTP engine
# ─────────────────────────────────────────────────────────────────────────────

def compute_sotp_valuation(
    segments: list[SegmentValuation],
    net_debt_mm: float,
    diluted_shares_mm: float,
    non_operating_assets_mm: float = 0.0,
    corporate_overhead_mm: float = 0.0,
    current_price: float | None = None,
) -> SOTPResult:
    """
    Sum-of-the-Parts (SOTP) valuation.

    Sums segment-level EVs (from multiples or DCF), adds non-operating assets,
    deducts corporate overhead and net debt, then divides by diluted shares.

    Args:
        segments               : List of SegmentValuation objects.
        net_debt_mm            : Net debt = IBD + preferred + NCI − cash − ST investments ($M).
                                 Positive = net debt; negative = net cash.
        diluted_shares_mm      : Diluted shares outstanding (millions).
        non_operating_assets_mm: Non-core assets not in any segment (equity investments,
                                  held-for-sale, IP, etc.) ($M, positive = adds to EV).
        corporate_overhead_mm  : Corporate overhead not allocated to segments ($M, positive
                                  = cost/liability; will be DEDUCTED from EV).
        current_price          : Optional current market price for premium/discount calc.

    Returns:
        SOTPResult with full bridge from segments to equity per share.

    Reference: Macabacus "Sum-of-the-Parts Analysis" methodology.
    """
    result = SOTPResult()
    warnings: list[str] = []

    if not segments:
        warnings.append("No segments provided — SOTP result is zero.")
        result.warnings = warnings
        return result

    # 1) Sum segment EVs
    seg_ev_dicts = [s.to_dict() for s in segments]
    total_seg_ev = sum(s.segment_ev for s in segments)
    result.segment_evs = seg_ev_dicts
    result.total_segment_ev_mm = total_seg_ev

    # 2) Non-operating assets
    result.non_operating_assets_mm = non_operating_assets_mm

    # 3) Corporate overhead (deducted as a liability / negative value)
    if corporate_overhead_mm < 0:
        warnings.append(
            "corporate_overhead_mm should be positive (it represents a cost to deduct). "
            f"Received {corporate_overhead_mm:.0f}m — using absolute value."
        )
        corporate_overhead_mm = abs(corporate_overhead_mm)
    result.corporate_overhead_mm = -corporate_overhead_mm  # negative in bridge

    # 4) Total EV
    total_ev = total_seg_ev + non_operating_assets_mm - corporate_overhead_mm
    result.total_ev_mm = total_ev

    if total_ev <= 0:
        warnings.append(f"Total SOTP EV is non-positive (${total_ev:.0f}m) — check segment assumptions.")

    # 5) Equity value
    result.net_debt_mm = net_debt_mm
    equity_value = total_ev - net_debt_mm
    result.equity_value_mm = equity_value

    if equity_value < 0:
        warnings.append(
            f"Equity value is negative (${equity_value:.0f}m). "
            "Net debt exceeds total SOTP EV — company may be distressed."
        )

    # 6) Equity per share
    if diluted_shares_mm <= 0:
        warnings.append("diluted_shares_mm ≤ 0 — cannot compute equity per share.")
        equity_per_share = 0.0
    else:
        equity_per_share = safe_divide(equity_value, diluted_shares_mm, 0.0)

    result.diluted_shares_mm = diluted_shares_mm
    result.equity_per_share = equity_per_share

    # 7) Premium/discount vs. current price
    if current_price is not None and current_price > 0:
        prem_disc = safe_divide(equity_per_share - current_price, current_price, 0.0)
        result.premium_discount_pct = prem_disc
        if prem_disc > 0.50:
            warnings.append(
                f"SOTP implies {prem_disc:.0%} premium to current price (${current_price:.2f}). "
                "Verify segment multiples."
            )
        elif prem_disc < -0.50:
            warnings.append(
                f"SOTP implies {abs(prem_disc):.0%} discount to current price (${current_price:.2f}). "
                "Verify segment multiples."
            )

    result.warnings = warnings

    logger.info(
        "SOTP: %d segments, total EV $%.0fm, equity/share $%.2f",
        len(segments), total_ev, equity_per_share,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helper: assign overhead by revenue weight
# ─────────────────────────────────────────────────────────────────────────────

def allocate_overhead_by_revenue(
    segments: list[SegmentValuation],
    total_overhead_mm: float,
    segment_revenues_mm: list[float],
) -> list[float]:
    """
    Allocate corporate overhead across segments proportionally to revenue.

    Per Macabacus: "Allocate corporate overhead to divisions based on percent
    of revenues, EBIT, or industry norms for each segment."

    Args:
        segments             : List of segments (same order as revenues).
        total_overhead_mm    : Total corporate overhead to distribute ($M).
        segment_revenues_mm  : Revenue of each segment (same order as segments).

    Returns:
        list[float] — overhead allocated to each segment ($M).

    Reference: Macabacus "Sum-of-the-Parts Analysis" methodology.
    """
    if not segments or len(segments) != len(segment_revenues_mm):
        return [0.0] * len(segments)

    total_rev = sum(r for r in segment_revenues_mm if r > 0)
    if total_rev <= 0:
        return [total_overhead_mm / len(segments)] * len(segments)

    return [
        total_overhead_mm * safe_divide(rev, total_rev, 0.0)
        for rev in segment_revenues_mm
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience alias
# ─────────────────────────────────────────────────────────────────────────────

sotp_valuation = compute_sotp_valuation
