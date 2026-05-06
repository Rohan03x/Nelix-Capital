"""Annual and quinquennial postmortems for adaptive DCF learning."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .attribution import ErrorDriver, aggregate_attributions, attribute_postmortem
from .ledger import (
    LedgerReader,
    LedgerWriter,
    PredictionRecord,
    REALIZED_VALUE_FIELDS,
    RealizedOutcomeRecord,
    prediction_horizon_target_date,
)
from .storage_paths import learning_db_dir


POSTMORTEM_DB_PATH = learning_db_dir() / "postmortems.db"


@dataclass(frozen=True)
class PostmortemRecord:
    postmortem_id: str
    record_id: str
    ticker: str
    forecast_horizon_year: int
    postmortem_date: date
    actual_revenue_mm: float | None
    actual_ebit_margin: float | None
    actual_ufcf_mm: float | None
    actual_ev_mm: float | None
    actual_price_at_horizon: float | None
    revenue_error_pct: float
    margin_error_bps: float
    ev_error_pct: float
    price_return_error_pct: float
    primary_miss_driver: str
    surprise_flags: list[str] = field(default_factory=list)
    error_attribution: list[tuple[ErrorDriver | str, float]] = field(default_factory=list)
    structural_break_detected: bool = False
    # BRAIN_IMPROVEMENT_PLAN.md (H4) — graded break score (0.0=clean, 1.0=severe)
    structural_break_score: float = 0.0
    model_bias_signal: str = "neutral"
    postmortem_notes: str | None = None
    prediction_snapshot: dict[str, Any] = field(default_factory=dict)
    macro_backdrop_at_prediction: dict[str, float] = field(default_factory=dict)
    macro_backdrop_at_horizon: dict[str, float] = field(default_factory=dict)
    actual_wacc: float | None = None
    actual_terminal_growth: float | None = None
    # Layer 6 — sub-driver error tracking
    da_error_pct: float | None = None
    capex_error_pct: float | None = None
    tax_rate_error_bps: float | None = None
    sbc_error_pct: float | None = None
    terminal_g_error_bps: float | None = None
    wacc_error_bps: float | None = None
    near_term_margin_error_bps: float | None = None
    terminal_margin_error_bps: float | None = None
    # Gap 6 — EV error decomposition via first-order DCF partial derivatives
    # {driver: delta_ev_mm} — how many $M each driver contributed to the EV miss
    ev_partial_attribution: dict[str, float] = field(default_factory=dict)
    realized_outcome_id: str | None = None
    realized_label_status: str = "pending"
    realized_unknown_targets: list[str] = field(default_factory=list)
    horizon_target_date: date | None = None
    aligned_period_end: date | None = None
    alignment_method: str | None = None
    realized_source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["postmortem_date"] = self.postmortem_date.isoformat()
        payload["horizon_target_date"] = self.horizon_target_date.isoformat() if self.horizon_target_date else None
        payload["aligned_period_end"] = self.aligned_period_end.isoformat() if self.aligned_period_end else None
        payload["error_attribution"] = [
            ((driver.value if isinstance(driver, ErrorDriver) else driver), contribution)
            for driver, contribution in self.error_attribution
        ]
        return payload


@dataclass(frozen=True)
class QuinquennialReport:
    report_id: str
    ticker: str
    base_year: int
    created_at: date
    annual_records: list[PostmortemRecord] = field(default_factory=list)
    trajectory_analysis: dict[str, Any] = field(default_factory=dict)
    assumption_drift_diagnosis: dict[str, Any] = field(default_factory=dict)
    structural_breaks: list[dict[str, Any]] = field(default_factory=list)
    compounding_error_attribution: list[tuple[ErrorDriver | str, float]] = field(default_factory=list)
    cross_industry_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["annual_records"] = [record.to_dict() for record in self.annual_records]
        payload["compounding_error_attribution"] = [
            ((driver.value if isinstance(driver, ErrorDriver) else driver), contribution)
            for driver, contribution in self.compounding_error_attribution
        ]
        return payload


class QuinquennialStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else POSTMORTEM_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quinquennial_reports (
                    report_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    base_year INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, report: QuinquennialReport) -> QuinquennialReport:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM quinquennial_reports WHERE ticker = ? AND base_year = ?",
                (report.ticker, report.base_year),
            )
            conn.execute(
                "INSERT OR REPLACE INTO quinquennial_reports(report_id, ticker, base_year, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    report.report_id,
                    report.ticker,
                    report.base_year,
                    report.created_at.isoformat(),
                    json.dumps(report.to_dict(), default=str),
                ),
            )
            conn.commit()
        return report

    def has_report(self, ticker: str, base_year: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM quinquennial_reports WHERE ticker = ? AND base_year = ? LIMIT 1",
                (ticker, base_year),
            ).fetchone()
        return row is not None


def should_run_quinquennial(years_since_ipo: int) -> bool:
    return years_since_ipo >= 5 and years_since_ipo % 5 == 0


def _safe_pct_error(actual: float | None, predicted: float | None) -> float:
    if actual is None or predicted is None or abs(predicted) < 1e-9:
        return 0.0
    return ((actual - predicted) / predicted) * 100.0


def _postmortem_from_payload(payload: dict[str, Any]) -> PostmortemRecord:
    return PostmortemRecord(
        postmortem_id=str(payload.get("postmortem_id") or ""),
        record_id=str(payload.get("record_id") or ""),
        ticker=str(payload.get("ticker") or ""),
        forecast_horizon_year=int(payload.get("forecast_horizon_year") or 0),
        postmortem_date=_parse_date_value(payload.get("postmortem_date")) or date.today(),
        actual_revenue_mm=payload.get("actual_revenue_mm"),
        actual_ebit_margin=payload.get("actual_ebit_margin"),
        actual_ufcf_mm=payload.get("actual_ufcf_mm"),
        actual_ev_mm=payload.get("actual_ev_mm"),
        actual_price_at_horizon=payload.get("actual_price_at_horizon"),
        revenue_error_pct=float(payload.get("revenue_error_pct") or 0.0),
        margin_error_bps=float(payload.get("margin_error_bps") or 0.0),
        ev_error_pct=float(payload.get("ev_error_pct") or 0.0),
        price_return_error_pct=float(payload.get("price_return_error_pct") or 0.0),
        primary_miss_driver=str(payload.get("primary_miss_driver") or "revenue"),
        surprise_flags=list(payload.get("surprise_flags") or []),
        error_attribution=[tuple(item) for item in (payload.get("error_attribution") or [])],
        structural_break_detected=bool(payload.get("structural_break_detected")),
        model_bias_signal=str(payload.get("model_bias_signal") or "neutral"),
        postmortem_notes=payload.get("postmortem_notes"),
        prediction_snapshot=dict(payload.get("prediction_snapshot") or {}),
        macro_backdrop_at_prediction=dict(payload.get("macro_backdrop_at_prediction") or {}),
        macro_backdrop_at_horizon=dict(payload.get("macro_backdrop_at_horizon") or {}),
        actual_wacc=payload.get("actual_wacc"),
        actual_terminal_growth=payload.get("actual_terminal_growth"),
        da_error_pct=payload.get("da_error_pct"),
        capex_error_pct=payload.get("capex_error_pct"),
        tax_rate_error_bps=payload.get("tax_rate_error_bps"),
        sbc_error_pct=payload.get("sbc_error_pct"),
        terminal_g_error_bps=payload.get("terminal_g_error_bps"),
        wacc_error_bps=payload.get("wacc_error_bps"),
        near_term_margin_error_bps=payload.get("near_term_margin_error_bps"),
        terminal_margin_error_bps=payload.get("terminal_margin_error_bps"),
        ev_partial_attribution=dict(payload.get("ev_partial_attribution") or {}),
        realized_outcome_id=payload.get("realized_outcome_id"),
        realized_label_status=str(payload.get("realized_label_status") or "pending"),
        realized_unknown_targets=list(payload.get("realized_unknown_targets") or []),
        horizon_target_date=_parse_date_value(payload.get("horizon_target_date")),
        aligned_period_end=_parse_date_value(payload.get("aligned_period_end")),
        alignment_method=payload.get("alignment_method"),
        realized_source=dict(payload.get("realized_source") or {}),
    )


def _primary_miss_driver(revenue_error_pct: float, margin_error_bps: float, ev_error_pct: float, price_error_pct: float) -> str:
    candidates = {
        "revenue": abs(revenue_error_pct),
        "margin": abs(margin_error_bps) / 100.0,
        "enterprise_value": abs(ev_error_pct),
        "price_return": abs(price_error_pct),
    }
    return max(candidates, key=candidates.get)


def _model_bias_signal(errors: list[float]) -> str:
    if not errors:
        return "neutral"
    mean_error = sum(errors) / len(errors)
    if mean_error <= -10.0:
        return "optimistic"
    if mean_error >= 10.0:
        return "pessimistic"
    return "neutral"


# ---------------------------------------------------------------------------
# Reverse-DCF: solve for market-implied terminal growth given EV and UFCF
# ---------------------------------------------------------------------------

def _solve_implied_terminal_growth(
    actual_ev_mm: float,
    pv_explicit_ufcfs: float,
    terminal_ufcf: float,
    wacc: float,
    discount_factor_at_terminal: float,
    *,
    g_low: float = -0.10,
    g_high_cap: float | None = None,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float | None:
    """
    Solve for g in: actual_ev = pv_explicit + terminal_ufcf*(1+g)/(wacc-g)*df_terminal
    using bisection. Returns None if not solvable.
    """
    import math
    if terminal_ufcf is None or abs(terminal_ufcf) < 1e-9 or discount_factor_at_terminal < 1e-9:
        return None
    if wacc <= 0.0:
        return None
    g_high = g_high_cap if g_high_cap is not None else wacc - 0.005
    g_high = min(g_high, wacc - 0.001)
    if g_low >= g_high:
        return None

    def _tv_pv(g: float) -> float:
        denom = wacc - g
        if abs(denom) < 1e-9:
            return float("inf")
        tv = terminal_ufcf * (1.0 + g) / denom
        return pv_explicit_ufcfs + tv * discount_factor_at_terminal

    try:
        f_low = _tv_pv(g_low) - actual_ev_mm
        f_high = _tv_pv(g_high) - actual_ev_mm
    except Exception:
        return None

    if f_low * f_high > 0:
        # Try shrinking g_high
        for _g in [g_high - 0.01, g_high - 0.02, g_high - 0.03]:
            if _g <= g_low:
                break
            try:
                _f = _tv_pv(_g) - actual_ev_mm
                if f_low * _f <= 0:
                    g_high = _g
                    f_high = _f
                    break
            except Exception:
                continue
        else:
            return None

    for _ in range(max_iter):
        g_mid = (g_low + g_high) / 2.0
        try:
            f_mid = _tv_pv(g_mid) - actual_ev_mm
        except Exception:
            return None
        if abs(f_mid) < tol or (g_high - g_low) / 2.0 < tol:
            return g_mid
        if f_low * f_mid <= 0:
            g_high = g_mid
        else:
            g_low = g_mid
            f_low = f_mid
    return (g_low + g_high) / 2.0


def _compute_implied_wacc_and_tg(
    prediction: Any,
    actuals: dict[str, Any],
) -> tuple[float | None, float | None]:
    """
    Given a prediction snapshot and realized actuals, compute:
    - actual_wacc: best estimate of the cost of capital implied by actual outcomes
      (use the DCF WACC from prediction; override if actuals provide it directly)
    - actual_terminal_growth: reverse-solved from Gordon Growth Model using
      actual EV, actual UFCF, and prediction's WACC
    """
    # 1. WACC: use prediction's wacc as proxy (it's the best available without
    #    re-running a full WACC build from horizon-date fundamentals)
    pred_wacc: float | None = getattr(prediction, "predicted_wacc", None)
    actual_wacc_direct: float | None = actuals.get("actual_wacc")
    resolved_wacc = actual_wacc_direct if actual_wacc_direct is not None else pred_wacc

    # 2. Terminal growth from reverse DCF
    actual_ev = actuals.get("actual_ev_mm")
    actual_ufcf = actuals.get("actual_ufcf_mm")
    if actual_ev is None or actual_ufcf is None or resolved_wacc is None:
        return resolved_wacc, None
    if actual_ev <= 0 or resolved_wacc <= 0:
        return resolved_wacc, None

    # Use terminal UFCF as current actual_ufcf (year N)
    # The explicit PV is unavailable at postmortem time, so use actual_ev
    # as the full PV target with pv_explicit=0 and terminal_ufcf = actual_ufcf.
    # This solves: actual_ev = ufcf*(1+g)/(wacc-g) * 1.0 → simpler form.
    # Rearranging: actual_ev * (wacc - g) = ufcf * (1 + g)
    #              actual_ev * wacc - actual_ev * g = ufcf + ufcf * g
    #              g * (actual_ev + ufcf) = actual_ev * wacc - ufcf
    #              g = (actual_ev * wacc - actual_ufcf) / (actual_ev + actual_ufcf)
    denom = actual_ev + actual_ufcf
    if abs(denom) < 1e-9:
        return resolved_wacc, None
    g_implied = (actual_ev * resolved_wacc - actual_ufcf) / denom
    # Clamp to [-0.10, wacc - 0.005] for sanity
    g_implied = max(-0.10, min(g_implied, resolved_wacc - 0.005))
    return resolved_wacc, g_implied


def fetch_actual_financials_for_year(ticker: str, horizon_year: int) -> dict[str, Any]:
    return {
        "actual_revenue_mm": None,
        "actual_ebit_margin": None,
        "actual_ufcf_mm": None,
        "actual_ev_mm": None,
        "actual_price_at_horizon": None,
        "macro_backdrop": {},
        "surprise_flags": [],
        "structural_break_hints": [],
        "unknown_targets": list(REALIZED_VALUE_FIELDS),
        "notes": f"No default actuals provider configured for {ticker} FY{horizon_year}.",
    }


def _compute_ev_partial_attribution(
    predicted_ev_mm: float | None,
    actual_ev_mm: float | None,
    predicted_wacc: float | None,
    actual_wacc: float | None,
    predicted_tg: float | None,
    actual_tg: float | None,
    predicted_ufcf_margin: float | None,
    actual_ebit_margin: float | None,
    predicted_ebit_margin: float | None,
    predicted_revenue_mm: float | None,
    actual_revenue_mm: float | None,
) -> dict[str, float]:
    """Compute first-order partial-derivative decomposition of the EV miss ($M).

    For TV = last_ufcf * (1+g) / (WACC - g):
      ∂TV/∂WACC = -TV / (WACC - g)
      ∂TV/∂g    = TV * WACC / (WACC - g)^2  [but TV/(WACC-g) = last_ufcf*(1+g)/(WACC-g)^2]
      ∂TV/∂ufcf = (1+g) / (WACC - g)

    Returns {driver: delta_ev_mm}, positive = over-prediction, negative = under-prediction.
    """
    if predicted_ev_mm is None or actual_ev_mm is None:
        return {}
    total_ev_error = actual_ev_mm - predicted_ev_mm

    wacc = predicted_wacc or 0.10
    g = predicted_tg or 0.03
    if wacc <= g or (wacc - g) < 1e-6:
        return {"total_ev_miss_mm": round(total_ev_error, 2)}

    # Proxy terminal UFCF from predicted EV using Gordon Growth reversal
    # TV = last_ufcf * (1+g) / (WACC-g)  and  PV(TV) ≈ predicted_ev * tv_fraction
    # Use tv_fraction ≈ 0.6 as typical DCF terminal value weight
    tv_fraction = 0.60
    pv_tv_estimate = (predicted_ev_mm or 0.0) * tv_fraction
    denom = wacc - g
    tv_estimate = pv_tv_estimate  # simplified: assume discount factor ≈ 1 for this linear approx
    last_ufcf_estimate = tv_estimate * denom / (1.0 + g) if (1.0 + g) > 1e-9 else 0.0

    # Partial derivatives
    dtv_dwacc = -tv_estimate / denom if abs(denom) > 1e-9 else 0.0
    dtv_dg = tv_estimate * wacc / (denom * denom) if abs(denom) > 1e-9 else 0.0
    dtv_dufcf = (1.0 + g) / denom if abs(denom) > 1e-9 else 0.0

    delta_wacc = (actual_wacc - wacc) if actual_wacc is not None else 0.0
    delta_g = (actual_tg - g) if actual_tg is not None else 0.0
    # UFCF margin error → proxy as ebit_margin error * (1 - tax) * revenue
    tax_proxy = 0.22
    rev_mm = actual_revenue_mm or predicted_revenue_mm or 0.0
    if actual_ebit_margin is not None and predicted_ebit_margin is not None:
        delta_ufcf_margin = (actual_ebit_margin - predicted_ebit_margin) * (1.0 - tax_proxy)
    elif actual_revenue_mm is not None and predicted_revenue_mm is not None and predicted_revenue_mm > 0:
        delta_revenue_pct = (actual_revenue_mm - predicted_revenue_mm) / predicted_revenue_mm
        delta_ufcf_margin = delta_revenue_pct * (predicted_ufcf_margin or 0.0)
    else:
        delta_ufcf_margin = 0.0
    delta_ufcf_mm = delta_ufcf_margin * rev_mm

    wacc_contribution = dtv_dwacc * delta_wacc
    tg_contribution = dtv_dg * delta_g
    ufcf_contribution = dtv_dufcf * delta_ufcf_mm / rev_mm if rev_mm > 1e-3 else 0.0
    # Revenue error: explicit PV ≈ (1-tv_fraction) of EV; treated as separate driver
    if actual_revenue_mm is not None and predicted_revenue_mm is not None and predicted_revenue_mm > 0:
        revenue_pct_error = (actual_revenue_mm - predicted_revenue_mm) / predicted_revenue_mm
        revenue_contribution = (predicted_ev_mm or 0.0) * (1.0 - tv_fraction) * revenue_pct_error
    else:
        revenue_contribution = 0.0

    explained = wacc_contribution + tg_contribution + ufcf_contribution + revenue_contribution
    residual = total_ev_error - explained

    return {
        "wacc_delta_ev_mm": round(wacc_contribution, 2),
        "terminal_g_delta_ev_mm": round(tg_contribution, 2),
        "ufcf_margin_delta_ev_mm": round(ufcf_contribution, 2),
        "revenue_delta_ev_mm": round(revenue_contribution, 2),
        "residual_delta_ev_mm": round(residual, 2),
        "total_ev_miss_mm": round(total_ev_error, 2),
    }


def run_annual_postmortem(
    ticker: str,
    horizon_year: int,
    *,
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    actual_fetcher: Callable[[str, int], dict[str, Any]] | None = None,
    persist: bool = True,
    skip_existing: bool = True,
) -> list[PostmortemRecord]:
    ledger_reader = ledger_reader or LedgerReader()
    ledger_writer = ledger_writer or LedgerWriter(ledger_reader.db_path)
    actual_fetcher = actual_fetcher or fetch_actual_financials_for_year

    predictions = ledger_reader.query(ticker=ticker, horizon_year=horizon_year)
    if not predictions:
        return []

    fallback_actuals = actual_fetcher(ticker, horizon_year)
    bias_errors: list[float] = []
    results: list[PostmortemRecord] = []
    existing_record_ids: set[str] = set()
    if persist and skip_existing:
        for prediction in predictions:
            if ledger_reader.query_postmortems(record_id=prediction.record_id):
                existing_record_ids.add(prediction.record_id)

    for prediction in predictions:
        if prediction.record_id in existing_record_ids:
            continue

        actuals = _resolve_actuals_for_prediction(
            prediction,
            ledger_reader=ledger_reader,
            fallback_actuals=fallback_actuals,
        )
        if not _has_any_actual_labels(actuals):
            continue

        revenue_error_pct = _safe_pct_error(actuals.get("actual_revenue_mm"), prediction.predicted_revenue_mm)
        actual_margin = actuals.get("actual_ebit_margin")
        margin_error_bps = 0.0 if actual_margin is None else (actual_margin - prediction.predicted_ebit_margin) * 10_000
        ev_error_pct = _safe_pct_error(actuals.get("actual_ev_mm"), prediction.predicted_ev_mm)
        predicted_return_pct = _safe_pct_error(prediction.predicted_price_per_share, prediction.actual_price_at_prediction)
        actual_return_pct = _safe_pct_error(actuals.get("actual_price_at_horizon"), prediction.actual_price_at_prediction)
        price_return_error_pct = actual_return_pct - predicted_return_pct

        bias_errors.append(ev_error_pct)
        structural_break_hints = list(actuals.get("structural_break_hints") or [])
        # Compute implied WACC and terminal g via reverse-DCF / Gordon Growth
        _implied_wacc, _implied_tg = _compute_implied_wacc_and_tg(prediction, actuals)

        # Sub-driver error tracking (Layer 6)
        _pred_da = prediction.prediction_snapshot.get("predicted_da_pct") if hasattr(prediction, "prediction_snapshot") else None
        _actual_da = actuals.get("actual_da_pct")
        _pred_capex = prediction.prediction_snapshot.get("predicted_capex_pct") if hasattr(prediction, "prediction_snapshot") else None
        _actual_capex = actuals.get("actual_capex_pct")
        _pred_tax = getattr(prediction, "predicted_tax_rate", None)
        _actual_tax = actuals.get("actual_tax_rate")
        _pred_sbc = prediction.prediction_snapshot.get("predicted_sbc_pct") if hasattr(prediction, "prediction_snapshot") else None
        _actual_sbc = actuals.get("actual_sbc_pct")
        _pred_tg = getattr(prediction, "predicted_terminal_growth", None) or prediction.prediction_snapshot.get("predicted_terminal_growth")
        _pred_wacc = getattr(prediction, "predicted_wacc", None) or prediction.prediction_snapshot.get("predicted_wacc")

        _da_error_pct = _safe_pct_error(_actual_da, _pred_da) if _actual_da is not None and _pred_da is not None else None
        _capex_error_pct = _safe_pct_error(_actual_capex, _pred_capex) if _actual_capex is not None and _pred_capex is not None else None
        _tax_error_bps = ((_actual_tax or 0.0) - (_pred_tax or 0.0)) * 10_000 if _actual_tax is not None and _pred_tax is not None else None
        _sbc_error_pct = _safe_pct_error(_actual_sbc, _pred_sbc) if _actual_sbc is not None and _pred_sbc is not None else None
        _tg_error_bps = ((_implied_tg or 0.0) - (_pred_tg or 0.0)) * 10_000 if _implied_tg is not None and _pred_tg is not None else None
        _wacc_error_bps = ((_implied_wacc or 0.0) - (_pred_wacc or 0.0)) * 10_000 if _implied_wacc is not None and _pred_wacc is not None else None
        _near_margin_error_bps = margin_error_bps  # identical to margin_error_bps for now

        # Layer G — terminal margin error: predicted target EBIT margin vs actual horizon margin
        _pred_target_margin = (
            prediction.prediction_snapshot.get("target_ebit_margin")
            or prediction.prediction_snapshot.get("predicted_target_ebit_margin")
            if hasattr(prediction, "prediction_snapshot")
            else None
        )
        _terminal_margin_error_bps = (
            (_pred_target_margin - actual_margin) * 10_000
            if _pred_target_margin is not None and actual_margin is not None
            else None
        )

        # Gap 6 — EV error decomposition via first-order DCF partial derivatives
        _ev_partial_attribution = _compute_ev_partial_attribution(
            predicted_ev_mm=prediction.predicted_ev_mm,
            actual_ev_mm=actuals.get("actual_ev_mm"),
            predicted_wacc=_pred_wacc,
            actual_wacc=_implied_wacc,
            predicted_tg=_pred_tg,
            actual_tg=_implied_tg,
            predicted_ufcf_margin=prediction.prediction_snapshot.get("predicted_ufcf_margin")
                if hasattr(prediction, "prediction_snapshot") else None,
            actual_ebit_margin=actual_margin,
            predicted_ebit_margin=prediction.predicted_ebit_margin,
            predicted_revenue_mm=prediction.predicted_revenue_mm,
            actual_revenue_mm=actuals.get("actual_revenue_mm"),
        )

        # Priority 8 — improved multi-variable structural break score.
        # Combines revenue error, margin shock, ev error, hint count, and WACC/tg shifts.
        _wacc_shock = abs(_wacc_error_bps or 0.0) / 500.0  # 500bps WACC move = score 1.0
        _tg_shock = abs(_tg_error_bps or 0.0) / 500.0      # 500bps tg move = score 1.0
        _margin_shock = abs(margin_error_bps) / 1000.0      # 1000bps margin move = score 1.0
        structural_break_score = min(
            1.0,
            abs(revenue_error_pct) / 50.0
            + len(structural_break_hints) * 0.15
            + _margin_shock * 0.30
            + _wacc_shock * 0.15
            + _tg_shock * 0.10
        )
        structural_break = structural_break_score >= 0.50 or bool(structural_break_hints)
        surprise_flags = list(actuals.get("surprise_flags") or [])
        if structural_break and "structural break candidate" not in {flag.lower() for flag in surprise_flags}:
            surprise_flags.append("structural break candidate")

        record = PostmortemRecord(
            postmortem_id=str(uuid.uuid4()),
            record_id=prediction.record_id,
            ticker=prediction.ticker,
            forecast_horizon_year=prediction.forecast_horizon_year,
            postmortem_date=_parse_date_value(actuals.get("label_as_of_date")) or date.today(),
            actual_revenue_mm=actuals.get("actual_revenue_mm"),
            actual_ebit_margin=actual_margin,
            actual_ufcf_mm=actuals.get("actual_ufcf_mm"),
            actual_ev_mm=actuals.get("actual_ev_mm"),
            actual_price_at_horizon=actuals.get("actual_price_at_horizon"),
            revenue_error_pct=revenue_error_pct,
            margin_error_bps=margin_error_bps,
            ev_error_pct=ev_error_pct,
            price_return_error_pct=price_return_error_pct,
            primary_miss_driver=_primary_miss_driver(revenue_error_pct, margin_error_bps, ev_error_pct, price_return_error_pct),
            surprise_flags=surprise_flags,
            structural_break_detected=structural_break,
            structural_break_score=structural_break_score,
            model_bias_signal="neutral",
            postmortem_notes=actuals.get("notes"),
            prediction_snapshot={
                "predicted_revenue_mm": prediction.predicted_revenue_mm,
                "predicted_ebit_margin": prediction.predicted_ebit_margin,
                "predicted_ev_mm": prediction.predicted_ev_mm,
                "predicted_price_per_share": prediction.predicted_price_per_share,
                "predicted_wacc": prediction.predicted_wacc,
                "predicted_terminal_growth": prediction.predicted_terminal_growth,
                "prediction_timestamp": prediction.prediction_timestamp,
                "horizon_target_date": prediction_horizon_target_date(prediction).isoformat(),
            },
            macro_backdrop_at_prediction=dict(prediction.macro_backdrop or {}),
            macro_backdrop_at_horizon=dict(actuals.get("macro_backdrop") or {}),
            actual_wacc=_implied_wacc,
            actual_terminal_growth=_implied_tg,
            da_error_pct=_da_error_pct,
            capex_error_pct=_capex_error_pct,
            tax_rate_error_bps=_tax_error_bps,
            sbc_error_pct=_sbc_error_pct,
            terminal_g_error_bps=_tg_error_bps,
            wacc_error_bps=_wacc_error_bps,
            near_term_margin_error_bps=_near_margin_error_bps,
            terminal_margin_error_bps=_terminal_margin_error_bps,
            ev_partial_attribution=_ev_partial_attribution,
            realized_outcome_id=actuals.get("realized_outcome_id"),
            realized_label_status=str(actuals.get("realized_label_status") or "pending"),
            realized_unknown_targets=list(actuals.get("unknown_targets") or []),
            horizon_target_date=prediction_horizon_target_date(prediction),
            aligned_period_end=_parse_date_value(actuals.get("aligned_period_end")),
            alignment_method=actuals.get("alignment_method"),
            realized_source=dict(actuals.get("realized_source") or {}),
        )
        results.append(record)

    bias_signal = _model_bias_signal(bias_errors)
    bias_history = [item.ev_error_pct for item in results]
    final_results: list[PostmortemRecord] = []
    for record in results:
        attributions = attribute_postmortem(record, bias_history=bias_history)
        enriched = PostmortemRecord(
            **{
                **asdict(record),
                "model_bias_signal": bias_signal,
                "error_attribution": attributions,
            }
        )
        final_results.append(enriched)
        if persist:
            ledger_writer.append_postmortem(enriched)
    return final_results


def run_5year_postmortem(
    ticker: str,
    base_year: int,
    *,
    ledger_reader: LedgerReader | None = None,
    ledger_writer: LedgerWriter | None = None,
    actual_fetcher: Callable[[str, int], dict[str, Any]] | None = None,
    analog_provider: Callable[[str, int], dict[str, Any]] | None = None,
    report_store: QuinquennialStore | None = None,
) -> QuinquennialReport:
    ledger_reader = ledger_reader or LedgerReader()
    ledger_writer = ledger_writer or LedgerWriter(ledger_reader.db_path)
    actual_fetcher = actual_fetcher or fetch_actual_financials_for_year
    report_store = report_store or QuinquennialStore()

    annual_records: list[PostmortemRecord] = []
    for year in range(base_year + 1, base_year + 6):
        created = run_annual_postmortem(
            ticker,
            year,
            ledger_reader=ledger_reader,
            ledger_writer=ledger_writer,
            actual_fetcher=actual_fetcher,
            persist=True,
            skip_existing=True,
        )
        if created:
            annual_records.extend(created)
            continue

        for prediction in ledger_reader.query(ticker=ticker, horizon_year=year, scenario="base"):
            payloads = ledger_reader.query_postmortems(record_id=prediction.record_id)
            if payloads:
                annual_records.append(_postmortem_from_payload(payloads[0]))

    ordered = sorted(annual_records, key=lambda item: item.forecast_horizon_year)
    predicted_revenues = [record.prediction_snapshot.get("predicted_revenue_mm", 0.0) for record in ordered]
    actual_revenues = [record.actual_revenue_mm or 0.0 for record in ordered]
    predicted_margins = [record.prediction_snapshot.get("predicted_ebit_margin", 0.0) for record in ordered]
    actual_margins = [record.actual_ebit_margin or 0.0 for record in ordered]

    def _direction(series: list[float]) -> int:
        if len(series) < 2:
            return 0
        delta = series[-1] - series[0]
        if delta > 0:
            return 1
        if delta < 0:
            return -1
        return 0

    trajectory = {
        "revenue_direction_correct": _direction(predicted_revenues) == _direction(actual_revenues),
        "margin_direction_correct": _direction(predicted_margins) == _direction(actual_margins),
        "predicted_revenue_direction": _direction(predicted_revenues),
        "actual_revenue_direction": _direction(actual_revenues),
        "predicted_margin_direction": _direction(predicted_margins),
        "actual_margin_direction": _direction(actual_margins),
    }

    drift_candidates = {
        "revenue_growth": abs((actual_revenues[-1] if actual_revenues else 0.0) - (predicted_revenues[-1] if predicted_revenues else 0.0)),
        "ebit_margin": abs((actual_margins[-1] if actual_margins else 0.0) - (predicted_margins[-1] if predicted_margins else 0.0)),
    }
    largest_drift = max(drift_candidates, key=drift_candidates.get) if drift_candidates else "revenue_growth"
    assumption_drift = {
        "largest_moved_assumption": largest_drift,
        "drift_magnitudes": drift_candidates,
    }

    structural_breaks = [
        {
            "forecast_horizon_year": record.forecast_horizon_year,
            "revenue_error_pct": record.revenue_error_pct,
            "candidate": record.structural_break_detected,
            "source": record.realized_source,
        }
        for record in ordered
        if record.structural_break_detected
    ]

    cross_industry_comparison = analog_provider(ticker, base_year) if analog_provider else {}
    report = QuinquennialReport(
        report_id=str(uuid.uuid4()),
        ticker=ticker,
        base_year=base_year,
        created_at=date.today(),
        annual_records=ordered,
        trajectory_analysis=trajectory,
        assumption_drift_diagnosis=assumption_drift,
        structural_breaks=structural_breaks,
        compounding_error_attribution=aggregate_attributions(ordered),
        cross_industry_comparison=cross_industry_comparison,
    )
    report_store.save(report)
    return report


def _actuals_from_realized_outcome(outcome: RealizedOutcomeRecord) -> dict[str, Any]:
    return {
        "actual_revenue_mm": outcome.actual_revenue_mm,
        "actual_ebit_margin": outcome.actual_ebit_margin,
        "actual_ufcf_mm": outcome.actual_ufcf_mm,
        "actual_ev_mm": outcome.actual_ev_mm,
        "actual_price_at_horizon": outcome.actual_price_at_horizon,
        "macro_backdrop": dict(outcome.macro_backdrop or {}),
        "surprise_flags": list(outcome.surprise_flags or []),
        "structural_break_hints": list(outcome.structural_break_hints or []),
        "unknown_targets": list(outcome.unknown_targets or []),
        "notes": outcome.evidence_notes,
        "label_as_of_date": outcome.label_as_of_date.isoformat(),
        "aligned_period_end": outcome.aligned_period_end.isoformat() if outcome.aligned_period_end else None,
        "alignment_method": outcome.alignment_method,
        "realized_outcome_id": outcome.outcome_id,
        "realized_label_status": outcome.label_status,
        "realized_source": {
            "source_name": outcome.source_name,
            "source_kind": outcome.source_kind,
            "label_as_of_date": outcome.label_as_of_date.isoformat(),
            "source_payload": dict(outcome.source_payload or {}),
        },
    }


def _resolve_actuals_for_prediction(
    prediction: PredictionRecord,
    *,
    ledger_reader: LedgerReader,
    fallback_actuals: dict[str, Any],
) -> dict[str, Any]:
    resolved = dict(fallback_actuals or {})
    realized_outcome = ledger_reader.get_best_realized_outcome(prediction.record_id, include_partial=True)
    if realized_outcome is None:
        return resolved

    realized_payload = _actuals_from_realized_outcome(realized_outcome)
    for key, value in realized_payload.items():
        if value is None:
            continue
        if isinstance(value, dict) and not value:
            continue
        if isinstance(value, list) and not value:
            continue
        resolved[key] = value

    if "unknown_targets" in realized_payload:
        resolved["unknown_targets"] = list(realized_payload.get("unknown_targets") or resolved.get("unknown_targets") or [])
    return resolved


def _has_any_actual_labels(actuals: dict[str, Any]) -> bool:
    return any(actuals.get(key) is not None for key in REALIZED_VALUE_FIELDS)


def _parse_date_value(value: Any) -> date | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
