"""
webapp/data/confidence.py
─────────────────────────
Confidence Score Engine for DCF valuation reliability.

Scores 0–100. Each dimension carries a max_points allocation.
Returns both the aggregate score and a per-dimension breakdown
so the UI can show a detailed scorecard.

Design sources:
  - Damodaran on DCF limitations (Valuation: Measuring and Managing the Value of Companies)
  - CFA Institute on model quality indicators
  - Internal heuristics for common DCF failure modes
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ConfidenceDimension:
    """One scored dimension of model confidence."""
    name: str
    score: int          # points earned
    max_points: int     # points available
    status: str         # "pass" | "warn" | "fail"
    comment: str


@dataclass
class ConfidenceResult:
    """Aggregate confidence result."""
    total: int
    grade: str          # A / B / C / D / F
    label: str          # "High" | "Moderate" | "Low" | "Very Low"
    color: str          # CSS class suffix
    dimensions: list[ConfidenceDimension] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dcf_suitable: bool = True
    suitability_note: str = ""

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "grade": self.grade,
            "label": self.label,
            "color": self.color,
            "dcf_suitable": self.dcf_suitable,
            "suitability_note": self.suitability_note,
            "warnings": self.warnings,
            "dimensions": [
                {
                    "name": d.name,
                    "score": d.score,
                    "max_points": d.max_points,
                    "pct": round(d.score / d.max_points * 100) if d.max_points else 0,
                    "status": d.status,
                    "comment": d.comment,
                }
                for d in self.dimensions
            ],
        }


def _grade(score: int) -> tuple[str, str, str]:
    """Return (letter_grade, label, color_class)."""
    if score >= 80:
        return "A", "High Confidence", "green"
    elif score >= 65:
        return "B", "Moderate Confidence", "amber"
    elif score >= 50:
        return "C", "Low Confidence", "orange"
    elif score >= 35:
        return "D", "Very Low Confidence", "red"
    else:
        return "F", "Unreliable — Do Not Use", "red"


def _coeff_variation(series: list[float]) -> float:
    """Coefficient of variation (std / |mean|). Returns 0 if mean is 0."""
    if len(series) < 2:
        return 0.0
    mean = sum(series) / len(series)
    if abs(mean) < 1e-9:
        return 1.0
    variance = sum((x - mean) ** 2 for x in series) / len(series)
    std = variance ** 0.5
    return std / abs(mean)


def score_confidence(data: dict) -> ConfidenceResult:
    """
    Compute a confidence score for the DCF model in *data*.

    Expected keys (all present in our sample/FMP data structure):
        tv_pct, wacc, terminal_growth, historical, flags,
        ebit_margin_base, beta, intrinsic_value, price, scenarios
    """
    dims: list[ConfidenceDimension] = []
    warnings: list[str] = []
    dcf_suitable = True
    suitability_note = ""

    hist = data.get("historical", {})
    years_avail = len(hist.get("years", []))
    fcf_series   = hist.get("fcf", [])
    rev_series   = hist.get("revenue", [])
    margin_series = hist.get("ebit_margin", [])

    # Detect data-source limits (yfinance returns max 4-5 annual years)
    _dq = data.get("data_quality", {})
    _source = _dq.get("source", "")
    _is_yf = "yahoo" in _source.lower() or "yfinance" in _source.lower() or data.get("is_live", False)
    _has_qrecon = _dq.get("has_quarterly_recon", False)

    # ── 1. Data availability (15 pts) ────────────────────────────────────────
    if years_avail >= 10:
        d1 = ConfidenceDimension("Data Availability", 15, 15, "pass",
            f"{years_avail} years of financial history — excellent coverage.")
    elif years_avail >= 7:
        d1 = ConfidenceDimension("Data Availability", 10, 15, "pass",
            f"{years_avail} years of history — sufficient but a full decade is preferable.")
    elif years_avail >= 5:
        d1 = ConfidenceDimension("Data Availability", 7, 15, "warn",
            f"Only {years_avail} years available. Short history increases forecast uncertainty.")
    elif years_avail >= 4 and _is_yf:
        # yfinance platform only exposes ~4 annual years — not a data quality failure
        _recon_note = " Quarterly data reconstructed for additional coverage." if _has_qrecon else " Quarterly reconstruction attempted."
        d1 = ConfidenceDimension("Data Availability", 7, 15, "warn",
            f"{years_avail} annual years from Yahoo Finance (platform limit).{_recon_note}")
    elif years_avail >= 3:
        d1 = ConfidenceDimension("Data Availability", 4, 15, "warn",
            f"Only {years_avail} years of data — forecasts are highly uncertain.")
        warnings.append("Short history: fewer than 5 years of financial data available.")
    else:
        d1 = ConfidenceDimension("Data Availability", 1, 15, "fail",
            "Fewer than 3 years of data. DCF is unreliable.")
        warnings.append("Critical: Insufficient historical data for reliable DCF.")
    dims.append(d1)

    # ── 2. FCF positivity (10 pts) ────────────────────────────────────────────
    if fcf_series:
        neg_fcf = sum(1 for f in fcf_series if f < 0)
        if neg_fcf == 0:
            d2 = ConfidenceDimension("FCF Quality", 10, 10, "pass",
                "Free cash flow has been consistently positive — model inputs are reliable.")
        elif neg_fcf <= 2:
            d2 = ConfidenceDimension("FCF Quality", 7, 10, "warn",
                f"{neg_fcf} year(s) of negative FCF detected. Normalisation applied.")
            warnings.append(f"{neg_fcf} years of negative FCF in history. Review normalisation.")
        elif neg_fcf <= 4:
            d2 = ConfidenceDimension("FCF Quality", 4, 10, "warn",
                f"{neg_fcf} years of negative FCF — high uncertainty in terminal FCF estimate.")
            warnings.append("Multiple years of negative FCF: terminal value estimates are speculative.")
        else:
            d2 = ConfidenceDimension("FCF Quality", 0, 10, "fail",
                f"{neg_fcf}/{years_avail} years had negative FCF. DCF may not be appropriate.")
            warnings.append("CRITICAL: Predominantly negative FCF history. Consider EV/Revenue multiples instead.")
            dcf_suitable = False
            suitability_note = "Company has predominantly negative free cash flow history. DCF reliability is very low."
    else:
        d2 = ConfidenceDimension("FCF Quality", 3, 10, "warn", "FCF data unavailable — cannot verify positivity.")
    dims.append(d2)

    # ── 3. Revenue stability (10 pts) ─────────────────────────────────────────
    if len(rev_series) >= 4:
        cv = _coeff_variation(rev_series)
        if cv < 0.15:
            d3 = ConfidenceDimension("Revenue Stability", 10, 10, "pass",
                f"Revenue coefficient of variation {cv:.1%} — highly stable, predictable top line.")
        elif cv < 0.30:
            d3 = ConfidenceDimension("Revenue Stability", 7, 10, "pass",
                f"Revenue CV {cv:.1%} — moderate variability, typical for growth companies.")
        elif cv < 0.50:
            d3 = ConfidenceDimension("Revenue Stability", 4, 10, "warn",
                f"Revenue CV {cv:.1%} — high variability increases forecast uncertainty.")
            warnings.append("High revenue variability: treat near-term forecasts with caution.")
        else:
            d3 = ConfidenceDimension("Revenue Stability", 2, 10, "warn",
                f"Revenue CV {cv:.1%} — extreme variability. This is a high-growth or cyclical business.")
            warnings.append("Extreme revenue variability: DCF assumptions may need wide scenario range.")
    else:
        d3 = ConfidenceDimension("Revenue Stability", 3, 10, "warn", "Insufficient data for stability analysis.")
    dims.append(d3)

    # ── 4. Margin stability (10 pts) ──────────────────────────────────────────
    if len(margin_series) >= 4:
        margin_range = max(margin_series) - min(margin_series)
        if margin_range < 3:
            d4 = ConfidenceDimension("Margin Stability", 10, 10, "pass",
                f"EBIT margin range of {margin_range:.1f}pp — highly stable, supports reliable normalisation.")
        elif margin_range < 7:
            d4 = ConfidenceDimension("Margin Stability", 7, 10, "warn",
                f"EBIT margin range {margin_range:.1f}pp — moderate cyclicality. Terminal margin assumption carries risk.")
        elif margin_range < 15:
            d4 = ConfidenceDimension("Margin Stability", 4, 10, "warn",
                f"EBIT margin range {margin_range:.1f}pp — high volatility. Terminal margin is a key source of model error.")
            warnings.append("High margin volatility: verify terminal margin assumption against industry benchmarks.")
        else:
            d4 = ConfidenceDimension("Margin Stability", 1, 10, "fail",
                f"EBIT margin range {margin_range:.1f}pp — extreme swings. Terminal margin assumptions are highly speculative.")
            warnings.append("Extreme margin volatility: consider scenario analysis rather than a single base case.")
    else:
        d4 = ConfidenceDimension("Margin Stability", 3, 10, "warn", "Insufficient margin history for stability analysis.")
    dims.append(d4)

    # ── 5. WACC–g spread (10 pts) ─────────────────────────────────────────────
    wacc = data.get("wacc", 9.0)
    g    = data.get("terminal_growth", 2.5)
    spread = wacc - g
    if spread >= 5.0:
        d5 = ConfidenceDimension("WACC–g Spread", 10, 10, "pass",
            f"Spread of {spread:.1f}pp — very safe. Terminal value is not in a sensitive zone.")
    elif spread >= 3.0:
        d5 = ConfidenceDimension("WACC–g Spread", 8, 10, "pass",
            f"Spread of {spread:.1f}pp — healthy. Sensitivity to terminal growth is manageable.")
    elif spread >= 1.5:
        d5 = ConfidenceDimension("WACC–g Spread", 5, 10, "warn",
            f"Spread of {spread:.1f}pp — approaching a sensitive zone. Small changes in g have large impact.")
        warnings.append(f"WACC–g spread is only {spread:.1f}pp. Terminal value is highly sensitive.")
    elif spread >= 0.5:
        d5 = ConfidenceDimension("WACC–g Spread", 2, 10, "fail",
            f"Spread of {spread:.1f}pp — dangerously thin. Terminal value is mathematically unstable.")
        warnings.append("CRITICAL: WACC–g spread below 1%. Terminal value is mathematically unstable.")
    else:
        d5 = ConfidenceDimension("WACC–g Spread", 0, 10, "fail",
            f"Spread {spread:.1f}pp ≤ 0 — terminal value formula breaks down. Model is invalid.")
        warnings.append("CRITICAL: WACC ≤ terminal growth rate. Model produces infinite or negative values.")
    dims.append(d5)

    # ── 6. Terminal value as % of EV (10 pts) ─────────────────────────────────
    tv_pct = data.get("tv_pct", 70)
    if tv_pct < 55:
        d6 = ConfidenceDimension("Terminal Value Weight", 10, 10, "pass",
            f"TV is {tv_pct:.0f}% of EV — model is well-supported by near-term cash flows.")
    elif tv_pct < 70:
        d6 = ConfidenceDimension("Terminal Value Weight", 7, 10, "warn",
            f"TV is {tv_pct:.0f}% of EV — slightly elevated but typical for mature compounders.")
    elif tv_pct < 80:
        d6 = ConfidenceDimension("Terminal Value Weight", 4, 10, "warn",
            f"TV is {tv_pct:.0f}% of EV — high terminal dependence. Model is sensitive to WACC and g assumptions.")
        warnings.append(f"Terminal value is {tv_pct:.0f}% of EV. Sensitivity analysis is essential.")
    else:
        d6 = ConfidenceDimension("Terminal Value Weight", 2, 10, "fail",
            f"TV is {tv_pct:.0f}% of EV — extreme terminal dependence. Near-term cash flows barely matter.")
        warnings.append(f"CRITICAL: Terminal value is {tv_pct:.0f}% of EV. DCF reliability is very low.")
    dims.append(d6)

    # ── 7. Peer valuation alignment (10 pts) ──────────────────────────────────
    iv = data.get("intrinsic_value", 0)
    price = data.get("price", 0)
    analyst_low  = data.get("analyst_low", 0)
    analyst_high = data.get("analyst_high", 1e9)
    analyst_median = data.get("analyst_median", 0)

    if analyst_median > 0:
        pct_vs_analyst = abs(iv - analyst_median) / analyst_median
        in_range = analyst_low <= iv <= analyst_high
        if in_range and pct_vs_analyst < 0.10:
            d7 = ConfidenceDimension("Peer / Analyst Alignment", 10, 10, "pass",
                f"IV ${iv:.0f} is within analyst range (${analyst_low:.0f}–${analyst_high:.0f}) and within 10% of consensus ${analyst_median:.0f}.")
        elif in_range:
            d7 = ConfidenceDimension("Peer / Analyst Alignment", 7, 10, "pass",
                f"IV ${iv:.0f} falls within analyst range but deviates {pct_vs_analyst:.0%} from ${analyst_median:.0f} consensus.")
        else:
            d7 = ConfidenceDimension("Peer / Analyst Alignment", 4, 10, "warn",
                f"IV ${iv:.0f} is outside analyst range (${analyst_low:.0f}–${analyst_high:.0f}). Review key assumptions.")
            warnings.append(f"Model IV ${iv:.0f} is outside analyst target range. Investigate divergence.")
    else:
        d7 = ConfidenceDimension("Peer / Analyst Alignment", 5, 10, "warn",
            "No analyst target data available for cross-check.")
    dims.append(d7)

    # ── 8. Forecast aggressiveness (10 pts) ────────────────────────────────────
    ebit_base   = data.get("ebit_margin_base", 10)
    ebit_target = data.get("ebit_margin_target", 10)
    margin_expansion = ebit_target - ebit_base
    rev_growth_near = data.get("revenue_growth_near", 5)

    score8 = 10
    comment8 = []
    status8 = "pass"
    if margin_expansion > 10:
        score8 -= 4
        comment8.append(f"Margin expansion of {margin_expansion:.1f}pp is aggressive.")
        warnings.append(f"Model requires {margin_expansion:.1f}pp of EBIT margin expansion — verify with industry benchmarks.")
        status8 = "warn"
    elif margin_expansion > 5:
        score8 -= 2
        comment8.append(f"Margin expansion of {margin_expansion:.1f}pp is achievable but requires execution.")
        status8 = "warn"

    if rev_growth_near > 20:
        score8 -= 3
        comment8.append(f"Near-term revenue growth of {rev_growth_near:.1f}% is very high.")
        status8 = "warn"
    elif rev_growth_near > 10:
        score8 -= 1
        comment8.append(f"Revenue growth of {rev_growth_near:.1f}% is elevated.")

    if not comment8:
        comment8.append(f"Revenue growth {rev_growth_near:.1f}% and margin expansion {margin_expansion:.1f}pp are reasonable.")

    d8 = ConfidenceDimension("Forecast Aggressiveness", max(0, score8), 10, status8, " ".join(comment8))
    dims.append(d8)

    # ── 9. Beta / risk profile (5 pts) ────────────────────────────────────────
    beta = data.get("beta", 1.0)
    sector = data.get("sector", "")
    industry = data.get("industry", "")

    # Check if DCF is inherently unsuitable for this company type
    unsuitable_keywords = ["bank", "insurance", "reit", "financial services", "saving"]
    if any(kw in industry.lower() or kw in sector.lower() for kw in unsuitable_keywords):
        d9 = ConfidenceDimension("Business Type Suitability", 0, 5, "fail",
            f"Banks, insurers, and REITs require dividend discount or residual income models, not FCF-based DCF.")
        dcf_suitable = False
        suitability_note = (
            f"This appears to be a financial services company ({sector} / {industry}). "
            "Standard DCF using unlevered FCF is not appropriate. "
            "Consider Dividend Discount Model (DDM) or Excess Return Model instead."
        )
    elif beta > 2.0:
        d9 = ConfidenceDimension("Business Type Suitability", 2, 5, "warn",
            f"Beta of {beta:.2f} is very high — WACC is sensitive to beta estimation error.")
    elif beta > 1.5:
        d9 = ConfidenceDimension("Business Type Suitability", 3, 5, "warn",
            f"Beta of {beta:.2f} is elevated — higher execution risk than typical.")
    else:
        d9 = ConfidenceDimension("Business Type Suitability", 5, 5, "pass",
            f"Beta of {beta:.2f} is in a normal range — standard DCF assumptions apply.")
    dims.append(d9)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    total = sum(d.score for d in dims)
    max_total = sum(d.max_points for d in dims)
    normalized = round(total / max_total * 100)

    grade, label, color = _grade(normalized)

    if not dcf_suitable:
        normalized = min(normalized, 35)
        grade, label, color = "D", "Very Low Confidence", "red"

    return ConfidenceResult(
        total=normalized,
        grade=grade,
        label=label,
        color=color,
        dimensions=dims,
        warnings=warnings,
        dcf_suitable=dcf_suitable,
        suitability_note=suitability_note,
    )
