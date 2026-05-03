"""Deterministic shared-brain evaluation harness and operational diagnostics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from auto_valuation.forecast.dcf import run_dcf
from auto_valuation.learning.confidence import build_ranked_confidence_model
from auto_valuation.learning._layered_calibrator import CalibrationObservation
from auto_valuation.learning.ledger import DEFAULT_DB_PATH, DEFAULT_EXPORT_DIR, LedgerReader
from auto_valuation.learning.maintenance import MAINTENANCE_STATE_PATH
from auto_valuation.learning.postmortem import POSTMORTEM_DB_PATH
from webapp.data.knowledge_model import refine_live_assumptions


@dataclass(frozen=True)
class TimedObservation:
    realized_year: int
    observation: CalibrationObservation


@dataclass(frozen=True)
class ValidationCase:
    ticker: str
    prediction_year: int
    actual_price_at_horizon: float
    actual_revenue_growth_pct: float
    actual_ebit_margin_pct: float
    actual_ufcf_mm: float
    diluted_shares_mm: float
    net_debt_mm: float
    inputs: dict[str, Any]


@dataclass(frozen=True)
class CaseRun:
    revenue_growth_near_pct: float
    first_year_ebit_margin_pct: float
    target_margin_pct: float
    valuation_per_share: float
    first_year_ufcf_mm: float
    wacc_pct: float
    terminal_growth_pct: float
    calibration_confidence: float
    assumption_confidence: float
    valuation_confidence: float
    confidence_signal: float
    expected_valuation_error_pct: float
    global_learning_enabled: bool
    global_learning_scope: str | None
    global_learning_confidence: float
    pattern_match: str | None
    pattern_match_score: float
    sparse_fallback: bool
    revenue_growth_error_pp: float
    ebit_margin_error_pp: float
    ufcf_error_pct: float
    valuation_error_pct: float


@dataclass(frozen=True)
class CaseComparison:
    ticker: str
    prediction_year: int
    observations_used: int
    time_aware: bool
    analog_consistent: bool
    baseline: CaseRun
    shared: CaseRun


@dataclass(frozen=True)
class MetricComparison:
    baseline_mae: float
    shared_mae: float
    improvement: float
    relative_improvement_pct: float


@dataclass(frozen=True)
class ErrorDistribution:
    mean: float
    p50: float
    p90: float
    worst: float


@dataclass(frozen=True)
class OperationalDiagnostics:
    status: str
    ledger_accessible: bool
    prediction_records: int
    matured_records: int
    postmortem_records: int
    matured_without_postmortem: int
    quinquennial_reports: int
    maintenance_state_exists: bool
    maintenance_last_run_at: str | None
    maintenance_stale: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AcceptanceSummary:
    status: str
    benchmark_passed: bool
    summary: str
    remaining_gaps: list[str] = field(default_factory=list)


@dataclass
class SharedBrainEvaluationResult:
    case_count: int
    time_aware_violations: int
    metrics: dict[str, MetricComparison]
    valuation_error_distribution: dict[str, ErrorDistribution]
    confidence_ranking_accuracy: float
    confidence_bucket_gap: float
    analog_consistency_rate: float
    sparse_fallback_cases: int
    performance_ms: float
    performance_budget_ms: float
    cases: list[CaseComparison] = field(default_factory=list)
    operational_diagnostics: OperationalDiagnostics | None = None
    acceptance: AcceptanceSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _build_statement_inputs(case: ValidationCase) -> dict[str, list[dict[str, float | str]]]:
    inputs = case.inputs
    revenues = list(inputs["revenues"])
    ebit_margins = list(inputs["ebit_margins"])
    capexes = list(inputs["capexes"])
    das = list(inputs["das"])
    sbcs = list(inputs["sbcs"])
    gross_margin_pct = float(inputs["gross_margin_base_pct"])
    dso = float(inputs["dso"])
    dio = float(inputs["dio"])
    dpo = float(inputs["dpo"])
    base_revenue = max(float(inputs["revenue_base"]), 1.0)
    base_assets = max(float(inputs["total_assets"]), 1.0)
    asset_turnover = max(base_revenue / base_assets, 0.25)

    start_year = case.prediction_year - len(revenues)
    years = list(range(start_year, case.prediction_year))[-min(5, len(revenues)):]
    slice_len = len(years)

    income_stmts: list[dict[str, float | str]] = []
    cash_flows: list[dict[str, float | str]] = []
    balance_sheets: list[dict[str, float | str]] = []
    for year, revenue, margin, capex, da, sbc in reversed(
        list(zip(years, revenues[-slice_len:], ebit_margins[-slice_len:], capexes[-slice_len:], das[-slice_len:], sbcs[-slice_len:]))
    ):
        cogs = revenue * max(0.0, 1.0 - gross_margin_pct / 100.0)
        income_stmts.append(
            {
                "calendarYear": str(year),
                "revenue": revenue,
                "ebit": revenue * margin / 100.0,
                "operatingIncome": revenue * margin / 100.0,
                "grossProfit": revenue * gross_margin_pct / 100.0,
            }
        )
        cash_flows.append(
            {
                "calendarYear": str(year),
                "depreciationAndAmortization": da,
                "capitalExpenditure": -abs(capex),
                "stockBasedCompensation": sbc,
            }
        )
        balance_sheets.append(
            {
                "calendarYear": str(year),
                "totalAssets": revenue / asset_turnover,
                "netReceivables": revenue * dso / 365.0,
                "inventory": cogs * dio / 365.0,
                "accountPayables": cogs * dpo / 365.0,
            }
        )
    return {
        "income_stmts": income_stmts,
        "cash_flows": cash_flows,
        "balance_sheets": balance_sheets,
    }


def _project_from_assumptions(
    case: ValidationCase,
    *,
    revenue_growth_near_pct: float,
    target_margin_pct: float,
    wacc_pct: float,
    terminal_growth_pct: float,
    tax_rate_pct: float,
    da_pct: float,
    capex_pct: float,
    sbc_pct: float,
) -> dict[str, float]:
    statements = _build_statement_inputs(case)
    result = run_dcf(
        ticker=case.ticker,
        scenario="base",
        income_stmts=statements["income_stmts"],
        cash_flows=statements["cash_flows"],
        balance_sheets=statements["balance_sheets"],
        wacc=wacc_pct / 100.0,
        terminal_growth=terminal_growth_pct / 100.0,
        near_term_growth=revenue_growth_near_pct / 100.0,
        target_ebit_margin=target_margin_pct / 100.0,
        forecast_years=5,
        tax_rate_override=tax_rate_pct / 100.0,
        da_pct_override=da_pct / 100.0,
        capex_pct_override=capex_pct / 100.0,
        sbc_pct_override=sbc_pct / 100.0,
        mid_year_convention=True,
    )
    first_year = result.forecast_years_data[0] if result.forecast_years_data else None
    valuation_per_share = 0.0
    if case.diluted_shares_mm > 0:
        valuation_per_share = max(0.0, (result.enterprise_value - case.net_debt_mm) / case.diluted_shares_mm)
    return {
        "valuation_per_share": round(valuation_per_share, 4),
        "first_year_ufcf_mm": round(first_year.ufcf if first_year else 0.0, 4),
        "first_year_ebit_margin_pct": round((first_year.ebit_margin * 100.0) if first_year else 0.0, 4),
    }


def _confidence_signal(payload: dict[str, Any]) -> float:
    confidence_model = dict(payload.get("confidence_model") or build_ranked_confidence_model(payload))
    valuation_confidence = dict(confidence_model.get("valuation_confidence") or {})
    return round(float(valuation_confidence.get("score") or confidence_model.get("ranking_signal") or 0.0), 4)


def _run_from_payload(case: ValidationCase, payload: dict[str, Any]) -> CaseRun:
    confidence_model = dict(payload.get("confidence_model") or build_ranked_confidence_model(payload))
    assumption_confidence = dict(confidence_model.get("assumption_confidence") or {})
    valuation_confidence = dict(confidence_model.get("valuation_confidence") or {})
    expected_error_band = dict(valuation_confidence.get("expected_error_pct") or {})
    projection = _project_from_assumptions(
        case,
        revenue_growth_near_pct=float(payload.get("revenue_growth_near") or 0.0),
        target_margin_pct=float(payload.get("ebit_margin_target") or 0.0),
        wacc_pct=float(payload.get("wacc") or 0.0),
        terminal_growth_pct=float(payload.get("terminal_growth") or 0.0),
        tax_rate_pct=float(payload.get("tax_rate_pct") or case.inputs["tax_rate_pct"]),
        da_pct=float(payload.get("da_pct") or case.inputs["da_pct"]),
        capex_pct=float(payload.get("capex_pct") or case.inputs["capex_pct"]),
        sbc_pct=float(payload.get("sbc_pct") or case.inputs["sbc_pct"]),
    )
    global_learning = dict(payload.get("global_learning") or {})
    valuation_error_pct = 0.0
    if case.actual_price_at_horizon > 0:
        valuation_error_pct = abs(projection["valuation_per_share"] - case.actual_price_at_horizon) / case.actual_price_at_horizon * 100.0
    ufcf_error_pct = 0.0
    if abs(case.actual_ufcf_mm) > 1e-9:
        ufcf_error_pct = abs(projection["first_year_ufcf_mm"] - case.actual_ufcf_mm) / abs(case.actual_ufcf_mm) * 100.0
    return CaseRun(
        revenue_growth_near_pct=round(float(payload.get("revenue_growth_near") or 0.0), 4),
        first_year_ebit_margin_pct=projection["first_year_ebit_margin_pct"],
        target_margin_pct=round(float(payload.get("ebit_margin_target") or 0.0), 4),
        valuation_per_share=projection["valuation_per_share"],
        first_year_ufcf_mm=projection["first_year_ufcf_mm"],
        wacc_pct=round(float(payload.get("wacc") or 0.0), 4),
        terminal_growth_pct=round(float(payload.get("terminal_growth") or 0.0), 4),
        calibration_confidence=round(float(payload.get("calibration_confidence") or 0.0), 4),
        assumption_confidence=round(float(assumption_confidence.get("score") or 0.0), 4),
        valuation_confidence=round(float(valuation_confidence.get("score") or 0.0), 4),
        confidence_signal=round(float(valuation_confidence.get("score") or confidence_model.get("ranking_signal") or 0.0), 4),
        expected_valuation_error_pct=round(float(expected_error_band.get("p50") or 0.0), 4),
        global_learning_enabled=bool(global_learning.get("enabled")),
        global_learning_scope=global_learning.get("scope"),
        global_learning_confidence=round(float(global_learning.get("confidence") or 0.0), 4),
        pattern_match=payload.get("pattern_match"),
        pattern_match_score=round(float(payload.get("pattern_match_score") or 0.0), 4),
        sparse_fallback=int(payload.get("calibration_cohort_size") or 0) < 5,
        revenue_growth_error_pp=round(abs(float(payload.get("revenue_growth_near") or 0.0) - case.actual_revenue_growth_pct), 4),
        ebit_margin_error_pp=round(abs(projection["first_year_ebit_margin_pct"] - case.actual_ebit_margin_pct), 4),
        ufcf_error_pct=round(ufcf_error_pct, 4),
        valuation_error_pct=round(valuation_error_pct, 4),
    )


def evaluate_case(case: ValidationCase, observations: list[TimedObservation]) -> CaseComparison:
    usable_observations = [
        item.observation
        for item in observations
        if item.realized_year < case.prediction_year
    ]
    baseline_payload = refine_live_assumptions(**case.inputs, observations=[])
    shared_payload = refine_live_assumptions(**case.inputs, observations=usable_observations)
    repeat_payload = refine_live_assumptions(**case.inputs, observations=usable_observations)
    analog_consistent = (
        shared_payload.get("pattern_match") == repeat_payload.get("pattern_match")
        and round(float(shared_payload.get("pattern_match_score") or 0.0), 6)
        == round(float(repeat_payload.get("pattern_match_score") or 0.0), 6)
    )
    return CaseComparison(
        ticker=case.ticker,
        prediction_year=case.prediction_year,
        observations_used=len(usable_observations),
        time_aware=True,
        analog_consistent=analog_consistent,
        baseline=_run_from_payload(case, baseline_payload),
        shared=_run_from_payload(case, shared_payload),
    )


def _metric_summary(results: list[CaseComparison], attribute: str) -> MetricComparison:
    baseline_values = [float(getattr(result.baseline, attribute)) for result in results]
    shared_values = [float(getattr(result.shared, attribute)) for result in results]
    baseline_mae = round(_mean(baseline_values), 4)
    shared_mae = round(_mean(shared_values), 4)
    improvement = round(baseline_mae - shared_mae, 4)
    relative = round((improvement / baseline_mae) * 100.0, 2) if baseline_mae else 0.0
    return MetricComparison(
        baseline_mae=baseline_mae,
        shared_mae=shared_mae,
        improvement=improvement,
        relative_improvement_pct=relative,
    )


def _distribution(values: list[float]) -> ErrorDistribution:
    return ErrorDistribution(
        mean=round(_mean(values), 4),
        p50=round(_percentile(values, 0.50), 4),
        p90=round(_percentile(values, 0.90), 4),
        worst=round(max(values) if values else 0.0, 4),
    )


def _confidence_ranking_accuracy(results: list[CaseComparison]) -> float:
    pairs = 0
    hits = 0
    for index, left in enumerate(results):
        for right in results[index + 1 :]:
            left_conf = left.shared.confidence_signal
            right_conf = right.shared.confidence_signal
            if abs(left_conf - right_conf) < 1e-9:
                continue
            pairs += 1
            higher, lower = (left, right) if left_conf > right_conf else (right, left)
            if higher.shared.valuation_error_pct <= lower.shared.valuation_error_pct:
                hits += 1
    return round(hits / pairs, 4) if pairs else 1.0


def _confidence_bucket_gap(results: list[CaseComparison]) -> float:
    ordered = sorted(results, key=lambda item: item.shared.confidence_signal)
    midpoint = max(1, len(ordered) // 2)
    lower = ordered[:midpoint]
    upper = ordered[-midpoint:]
    lower_error = _mean([item.shared.valuation_error_pct for item in lower])
    upper_error = _mean([item.shared.valuation_error_pct for item in upper])
    return round(lower_error - upper_error, 4)


def evaluate_shared_brain(
    cases: list[ValidationCase],
    observations: list[TimedObservation],
    *,
    performance_budget_ms: float = 1500.0,
) -> SharedBrainEvaluationResult:
    started = perf_counter()
    results = [evaluate_case(case, observations) for case in cases]
    elapsed_ms = (perf_counter() - started) * 1000.0

    metrics = {
        "revenue_growth": _metric_summary(results, "revenue_growth_error_pp"),
        "ebit_margin": _metric_summary(results, "ebit_margin_error_pp"),
        "ufcf_error_pct": _metric_summary(results, "ufcf_error_pct"),
        "valuation_error_pct": _metric_summary(results, "valuation_error_pct"),
    }
    valuation_distribution = {
        "baseline": _distribution([item.baseline.valuation_error_pct for item in results]),
        "shared": _distribution([item.shared.valuation_error_pct for item in results]),
    }

    return SharedBrainEvaluationResult(
        case_count=len(results),
        time_aware_violations=sum(1 for item in results if not item.time_aware),
        metrics=metrics,
        valuation_error_distribution=valuation_distribution,
        confidence_ranking_accuracy=_confidence_ranking_accuracy(results),
        confidence_bucket_gap=_confidence_bucket_gap(results),
        analog_consistency_rate=round(
            sum(1 for item in results if item.analog_consistent) / max(len(results), 1),
            4,
        ),
        sparse_fallback_cases=sum(1 for item in results if item.shared.sparse_fallback),
        performance_ms=round(elapsed_ms, 2),
        performance_budget_ms=performance_budget_ms,
        cases=results,
    )


def collect_operational_diagnostics(
    *,
    db_path: str | Path | None = None,
    export_dir: str | Path | None = None,
    postmortem_db_path: str | Path | None = None,
    state_path: str | Path | None = None,
    as_of: datetime | None = None,
    stale_after_hours: int = 48,
) -> OperationalDiagnostics:
    warnings: list[str] = []
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    export_dir = Path(export_dir) if export_dir else DEFAULT_EXPORT_DIR
    postmortem_db_path = Path(postmortem_db_path) if postmortem_db_path else POSTMORTEM_DB_PATH
    state_path = Path(state_path) if state_path else MAINTENANCE_STATE_PATH
    as_of = as_of or datetime.now(timezone.utc)

    try:
        reader = LedgerReader(db_path=db_path, export_dir=export_dir)
        prediction_records = reader.query()
        postmortem_records = reader.query_postmortems()
    except Exception as exc:
        return OperationalDiagnostics(
            status="fail",
            ledger_accessible=False,
            prediction_records=0,
            matured_records=0,
            postmortem_records=0,
            matured_without_postmortem=0,
            quinquennial_reports=0,
            maintenance_state_exists=state_path.exists(),
            maintenance_last_run_at=None,
            maintenance_stale=False,
            warnings=[f"Prediction ledger unavailable: {exc}"],
        )

    matured_record_ids = {
        record.record_id
        for record in prediction_records
        if any(
            value is not None
            for value in (
                record.actual_revenue_mm,
                record.actual_ebit_margin,
                record.actual_ufcf_mm,
                record.actual_ev_mm,
                record.actual_price_at_horizon,
            )
        )
    }
    postmortem_record_ids = {str(item.get("record_id") or "") for item in postmortem_records}
    matured_without_postmortem = sum(1 for record_id in matured_record_ids if record_id not in postmortem_record_ids)

    quinquennial_reports = 0
    if postmortem_db_path.exists():
        with sqlite3.connect(postmortem_db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM quinquennial_reports").fetchone()
            quinquennial_reports = int(row[0] or 0) if row else 0

    maintenance_last_run_at: str | None = None
    maintenance_stale = False
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        maintenance_last_run_at = state.get("maintenance_last_run_at") or state.get("last_run_at")
        if maintenance_last_run_at:
            parsed = datetime.fromisoformat(str(maintenance_last_run_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            maintenance_stale = stale_after_hours > 0 and (as_of - parsed) > timedelta(hours=stale_after_hours)
    elif prediction_records:
        warnings.append("Scheduled learning maintenance state is missing.")

    if not prediction_records:
        warnings.append("No prediction records exist yet; live acceptance remains data-limited.")
    if prediction_records and not postmortem_records:
        warnings.append("No postmortems have been recorded yet.")
    if matured_without_postmortem:
        warnings.append(f"{matured_without_postmortem} matured prediction(s) are still missing postmortems.")
    if maintenance_stale:
        warnings.append("Scheduled learning maintenance appears stale.")

    status = "warn" if warnings else "pass"
    return OperationalDiagnostics(
        status=status,
        ledger_accessible=True,
        prediction_records=len(prediction_records),
        matured_records=len(matured_record_ids),
        postmortem_records=len(postmortem_records),
        matured_without_postmortem=matured_without_postmortem,
        quinquennial_reports=quinquennial_reports,
        maintenance_state_exists=state_path.exists(),
        maintenance_last_run_at=maintenance_last_run_at,
        maintenance_stale=maintenance_stale,
        warnings=warnings,
    )


def summarize_acceptance(
    evaluation: SharedBrainEvaluationResult,
    diagnostics: OperationalDiagnostics | None = None,
) -> AcceptanceSummary:
    benchmark_passed = (
        evaluation.time_aware_violations == 0
        and evaluation.metrics["revenue_growth"].shared_mae <= evaluation.metrics["revenue_growth"].baseline_mae
        and evaluation.metrics["ebit_margin"].shared_mae <= evaluation.metrics["ebit_margin"].baseline_mae
        and evaluation.metrics["valuation_error_pct"].shared_mae <= evaluation.metrics["valuation_error_pct"].baseline_mae
        and evaluation.analog_consistency_rate >= 0.99
        and evaluation.performance_ms <= evaluation.performance_budget_ms
    )

    gaps: list[str] = []
    if not benchmark_passed:
        if evaluation.metrics["revenue_growth"].shared_mae > evaluation.metrics["revenue_growth"].baseline_mae:
            gaps.append("Revenue-growth accuracy did not beat baseline.")
        if evaluation.metrics["ebit_margin"].shared_mae > evaluation.metrics["ebit_margin"].baseline_mae:
            gaps.append("EBIT-margin accuracy did not beat baseline.")
        if evaluation.metrics["valuation_error_pct"].shared_mae > evaluation.metrics["valuation_error_pct"].baseline_mae:
            gaps.append("Valuation error did not improve versus baseline.")
        if evaluation.performance_ms > evaluation.performance_budget_ms:
            gaps.append("Validation runtime exceeded the regression budget.")

    if diagnostics is None:
        status = "provisional" if benchmark_passed else "gap"
    else:
        if diagnostics.prediction_records < 5:
            gaps.append("The live prediction ledger is still too thin for a full real-world acceptance claim.")
        if diagnostics.postmortem_records < 5:
            gaps.append("Too few postmortems exist to prove live out-of-sample improvement.")
        if diagnostics.matured_without_postmortem > 0:
            gaps.append("Some matured predictions still lack postmortems.")
        if diagnostics.maintenance_stale:
            gaps.append("Scheduled learning maintenance is stale.")

        if benchmark_passed and diagnostics.prediction_records >= 5 and diagnostics.postmortem_records >= 5 and diagnostics.matured_without_postmortem == 0 and not diagnostics.maintenance_stale:
            status = "accepted"
        elif benchmark_passed:
            status = "provisional"
        else:
            status = "gap"

    if status == "accepted":
        summary = "Packaged out-of-sample validation passes and the live ledger has enough maintained evidence to treat the shared-brain path as accepted."
    elif status == "provisional":
        summary = "The shared-brain path is integrated, measurable, and better on the packaged out-of-sample benchmark, but the live ledger is still too data-limited for a full acceptance claim."
    else:
        summary = "The repository now measures the shared-brain path, but at least one validation or operational gap still blocks end-state acceptance."

    deduped_gaps = list(dict.fromkeys(gaps))
    return AcceptanceSummary(
        status=status,
        benchmark_passed=benchmark_passed,
        summary=summary,
        remaining_gaps=deduped_gaps,
    )


def _observation(
    *,
    realized_year: int,
    sector: str,
    industry: str,
    predicted_growth: float,
    actual_growth: float,
    predicted_margin: float,
    actual_margin: float,
    predicted_wacc: float,
    actual_wacc: float,
    predicted_terminal_growth: float,
    actual_terminal_growth: float,
    predicted_beta: float,
    actual_beta: float,
) -> TimedObservation:
    return TimedObservation(
        realized_year=realized_year,
        observation=CalibrationObservation(
            sector=sector,
            industry=industry,
            data_vintage_years=6,
            market_cap_regime="large",
            macro_regime="neutral",
            predicted_revenue_growth=predicted_growth,
            actual_revenue_growth=actual_growth,
            predicted_ebit_margin=predicted_margin,
            actual_ebit_margin=actual_margin,
            predicted_wacc=predicted_wacc,
            actual_wacc=actual_wacc,
            predicted_terminal_growth=predicted_terminal_growth,
            actual_terminal_growth=actual_terminal_growth,
            predicted_beta=predicted_beta,
            actual_beta=actual_beta,
        ),
    )


def build_default_validation_observations() -> list[TimedObservation]:
    observations: list[TimedObservation] = []
    years = [2020, 2021, 2022, 2023, 2024]
    sector_specs = [
        ("Technology", "Software", 0.145, 0.126, 0.310, 0.287, 0.088, 0.092, 0.030, 0.028, 1.18, 1.10),
        ("Industrials", "Manufacturing", 0.076, 0.066, 0.160, 0.169, 0.084, 0.081, 0.025, 0.026, 0.95, 0.90),
        ("Consumer Staples", "Beverages", 0.060, 0.054, 0.185, 0.190, 0.076, 0.074, 0.023, 0.024, 0.74, 0.71),
        ("Health Care", "Medical Devices", 0.098, 0.090, 0.228, 0.221, 0.081, 0.082, 0.026, 0.025, 0.86, 0.84),
    ]
    for sector, industry, pred_g, act_g, pred_margin, act_margin, pred_wacc, act_wacc, pred_g_term, act_g_term, pred_beta, act_beta in sector_specs:
        for offset, year in enumerate(years):
            drift = (offset - 2) * 0.002
            observations.append(
                _observation(
                    realized_year=year,
                    sector=sector,
                    industry=industry,
                    predicted_growth=pred_g + drift,
                    actual_growth=act_g + drift * 0.5,
                    predicted_margin=pred_margin + drift * 0.5,
                    actual_margin=act_margin + drift * 0.4,
                    predicted_wacc=pred_wacc,
                    actual_wacc=act_wacc,
                    predicted_terminal_growth=pred_g_term,
                    actual_terminal_growth=act_g_term,
                    predicted_beta=pred_beta,
                    actual_beta=act_beta,
                )
            )
    return observations


def _make_case(
    *,
    ticker: str,
    prediction_year: int,
    sector: str,
    industry: str,
    market_cap: float,
    shares_mm: float,
    revenues: list[float],
    ebit_margins: list[float],
    gross_margin_base_pct: float,
    baseline_growth_pct: float,
    baseline_target_margin_pct: float,
    baseline_wacc_pct: float,
    baseline_terminal_growth_pct: float,
    actual_growth_pct: float,
    actual_target_margin_pct: float,
    actual_wacc_pct: float,
    actual_terminal_growth_pct: float,
    beta: float,
    tax_rate_pct: float,
    dso: float,
    dio: float,
    dpo: float,
    capex_pct: float,
    da_pct: float,
    sbc_pct: float,
    debt_ratio: float,
    cash_ratio: float,
    asset_turnover: float,
    operating_cf_ratio: float,
    fcf_ratio: float,
) -> ValidationCase:
    revenue_base = revenues[-1]
    total_assets = round(revenue_base / max(asset_turnover, 0.25), 2)
    total_debt = round(revenue_base * debt_ratio, 2)
    cash = round(revenue_base * cash_ratio, 2)
    net_debt = round(total_debt - cash, 2)
    operating_cf = round(revenue_base * operating_cf_ratio, 2)
    fcf = round(revenue_base * fcf_ratio, 2)
    capexes = [round(revenue * capex_pct / 100.0, 2) for revenue in revenues]
    das = [round(revenue * da_pct / 100.0, 2) for revenue in revenues]
    sbcs = [round(revenue * sbc_pct / 100.0, 2) for revenue in revenues]
    pretax_incomes = [round(revenue * margin / 100.0 * 0.88, 2) for revenue, margin in zip(revenues, ebit_margins)]
    tax_provisions = [round(max(pretax, 0.0) * tax_rate_pct / 100.0, 2) for pretax in pretax_incomes]
    total_capital = market_cap + total_debt
    equity_weight = market_cap / total_capital * 100.0 if total_capital > 0 else 85.0
    debt_weight = 100.0 - equity_weight
    inputs = {
        "ticker": ticker,
        "sector": sector,
        "industry": industry,
        "market_cap": market_cap,
        "revenues": revenues,
        "ebit_margins": ebit_margins,
        "gross_margin_base_pct": gross_margin_base_pct,
        "revenue_growth_near": baseline_growth_pct,
        "terminal_growth": baseline_terminal_growth_pct,
        "ebit_margin_base_pct": ebit_margins[-1],
        "ebit_margin_target": baseline_target_margin_pct,
        "beta": beta,
        "wacc": baseline_wacc_pct,
        "rf_rate": 4.0,
        "erp": 5.0,
        "kd_post": round(max(2.5, baseline_wacc_pct * 0.35), 2),
        "e_wt": round(equity_weight, 2),
        "d_wt": round(debt_weight, 2),
        "total_assets": total_assets,
        "total_debt": total_debt,
        "revenue_base": revenue_base,
        "operating_cf": operating_cf,
        "fcf": fcf,
        "capex_pct": capex_pct,
        "capexes": capexes,
        "da_pct": da_pct,
        "das": das,
        "sbc_pct": sbc_pct,
        "sbcs": sbcs,
        "tax_rate_pct": tax_rate_pct,
        "pretax_incomes": pretax_incomes,
        "tax_provisions": tax_provisions,
        "dso": dso,
        "dio": dio,
        "dpo": dpo,
    }
    provisional_case = ValidationCase(
        ticker=ticker,
        prediction_year=prediction_year,
        actual_price_at_horizon=0.0,
        actual_revenue_growth_pct=actual_growth_pct,
        actual_ebit_margin_pct=0.0,
        actual_ufcf_mm=0.0,
        diluted_shares_mm=shares_mm,
        net_debt_mm=net_debt,
        inputs=inputs,
    )
    actual_projection = _project_from_assumptions(
        provisional_case,
        revenue_growth_near_pct=actual_growth_pct,
        target_margin_pct=actual_target_margin_pct,
        wacc_pct=actual_wacc_pct,
        terminal_growth_pct=actual_terminal_growth_pct,
        tax_rate_pct=tax_rate_pct,
        da_pct=da_pct,
        capex_pct=capex_pct,
        sbc_pct=sbc_pct,
    )
    return ValidationCase(
        ticker=ticker,
        prediction_year=prediction_year,
        actual_price_at_horizon=round(actual_projection["valuation_per_share"], 2),
        actual_revenue_growth_pct=actual_growth_pct,
        actual_ebit_margin_pct=round(actual_projection["first_year_ebit_margin_pct"], 2),
        actual_ufcf_mm=round(actual_projection["first_year_ufcf_mm"], 2),
        diluted_shares_mm=shares_mm,
        net_debt_mm=net_debt,
        inputs=inputs,
    )


def build_default_validation_cases() -> list[ValidationCase]:
    return [
        _make_case(
            ticker="CLOUD",
            prediction_year=2025,
            sector="Technology",
            industry="Software",
            market_cap=62_000.0,
            shares_mm=950.0,
            revenues=[180.0, 205.0, 232.0, 265.0, 302.0, 340.0],
            ebit_margins=[24.0, 24.8, 25.4, 26.1, 27.0, 27.8],
            gross_margin_base_pct=69.0,
            baseline_growth_pct=14.8,
            baseline_target_margin_pct=33.0,
            baseline_wacc_pct=8.8,
            baseline_terminal_growth_pct=3.0,
            actual_growth_pct=12.7,
            actual_target_margin_pct=29.4,
            actual_wacc_pct=9.2,
            actual_terminal_growth_pct=2.8,
            beta=1.24,
            tax_rate_pct=21.0,
            dso=41.0,
            dio=5.0,
            dpo=28.0,
            capex_pct=4.2,
            da_pct=2.8,
            sbc_pct=3.0,
            debt_ratio=0.14,
            cash_ratio=0.06,
            asset_turnover=0.92,
            operating_cf_ratio=0.20,
            fcf_ratio=0.16,
        ),
        _make_case(
            ticker="CHIP",
            prediction_year=2026,
            sector="Technology",
            industry="Software",
            market_cap=58_000.0,
            shares_mm=820.0,
            revenues=[140.0, 161.0, 186.0, 214.0, 246.0, 279.0],
            ebit_margins=[22.0, 22.7, 23.8, 24.9, 25.8, 26.6],
            gross_margin_base_pct=66.0,
            baseline_growth_pct=13.9,
            baseline_target_margin_pct=31.0,
            baseline_wacc_pct=8.7,
            baseline_terminal_growth_pct=3.0,
            actual_growth_pct=12.1,
            actual_target_margin_pct=28.8,
            actual_wacc_pct=9.0,
            actual_terminal_growth_pct=2.8,
            beta=1.18,
            tax_rate_pct=20.0,
            dso=38.0,
            dio=6.0,
            dpo=27.0,
            capex_pct=4.5,
            da_pct=3.1,
            sbc_pct=2.6,
            debt_ratio=0.12,
            cash_ratio=0.05,
            asset_turnover=0.90,
            operating_cf_ratio=0.18,
            fcf_ratio=0.14,
        ),
        _make_case(
            ticker="GEAR",
            prediction_year=2025,
            sector="Industrials",
            industry="Manufacturing",
            market_cap=28_000.0,
            shares_mm=640.0,
            revenues=[220.0, 233.0, 246.0, 261.0, 277.0, 293.0],
            ebit_margins=[12.4, 12.9, 13.5, 14.0, 14.4, 14.8],
            gross_margin_base_pct=37.0,
            baseline_growth_pct=7.2,
            baseline_target_margin_pct=16.4,
            baseline_wacc_pct=8.4,
            baseline_terminal_growth_pct=2.5,
            actual_growth_pct=6.3,
            actual_target_margin_pct=18.0,
            actual_wacc_pct=8.1,
            actual_terminal_growth_pct=2.6,
            beta=0.96,
            tax_rate_pct=24.0,
            dso=51.0,
            dio=68.0,
            dpo=42.0,
            capex_pct=3.4,
            da_pct=2.3,
            sbc_pct=0.8,
            debt_ratio=0.18,
            cash_ratio=0.04,
            asset_turnover=0.86,
            operating_cf_ratio=0.15,
            fcf_ratio=0.10,
        ),
        _make_case(
            ticker="RAIL",
            prediction_year=2026,
            sector="Industrials",
            industry="Manufacturing",
            market_cap=31_000.0,
            shares_mm=710.0,
            revenues=[175.0, 184.0, 194.0, 205.0, 216.0, 228.0],
            ebit_margins=[13.1, 13.5, 13.8, 14.2, 14.6, 15.0],
            gross_margin_base_pct=39.0,
            baseline_growth_pct=6.8,
            baseline_target_margin_pct=16.7,
            baseline_wacc_pct=8.2,
            baseline_terminal_growth_pct=2.5,
            actual_growth_pct=5.9,
            actual_target_margin_pct=17.6,
            actual_wacc_pct=8.0,
            actual_terminal_growth_pct=2.6,
            beta=0.92,
            tax_rate_pct=24.0,
            dso=48.0,
            dio=61.0,
            dpo=40.0,
            capex_pct=3.2,
            da_pct=2.2,
            sbc_pct=0.7,
            debt_ratio=0.16,
            cash_ratio=0.04,
            asset_turnover=0.88,
            operating_cf_ratio=0.14,
            fcf_ratio=0.10,
        ),
        _make_case(
            ticker="FIZZ",
            prediction_year=2025,
            sector="Consumer Staples",
            industry="Beverages",
            market_cap=24_000.0,
            shares_mm=520.0,
            revenues=[160.0, 169.0, 178.0, 188.0, 198.0, 209.0],
            ebit_margins=[16.6, 17.0, 17.4, 17.8, 18.1, 18.4],
            gross_margin_base_pct=48.0,
            baseline_growth_pct=6.0,
            baseline_target_margin_pct=19.2,
            baseline_wacc_pct=7.6,
            baseline_terminal_growth_pct=2.3,
            actual_growth_pct=5.3,
            actual_target_margin_pct=20.1,
            actual_wacc_pct=7.4,
            actual_terminal_growth_pct=2.4,
            beta=0.72,
            tax_rate_pct=23.0,
            dso=34.0,
            dio=43.0,
            dpo=36.0,
            capex_pct=2.6,
            da_pct=1.9,
            sbc_pct=0.6,
            debt_ratio=0.15,
            cash_ratio=0.05,
            asset_turnover=1.05,
            operating_cf_ratio=0.17,
            fcf_ratio=0.12,
        ),
        _make_case(
            ticker="PANTRY",
            prediction_year=2026,
            sector="Consumer Staples",
            industry="Beverages",
            market_cap=21_000.0,
            shares_mm=490.0,
            revenues=[130.0, 137.0, 145.0, 154.0, 162.0, 171.0],
            ebit_margins=[15.8, 16.2, 16.6, 17.1, 17.6, 18.0],
            gross_margin_base_pct=46.0,
            baseline_growth_pct=5.8,
            baseline_target_margin_pct=18.7,
            baseline_wacc_pct=7.7,
            baseline_terminal_growth_pct=2.3,
            actual_growth_pct=5.2,
            actual_target_margin_pct=19.6,
            actual_wacc_pct=7.5,
            actual_terminal_growth_pct=2.4,
            beta=0.74,
            tax_rate_pct=23.0,
            dso=33.0,
            dio=41.0,
            dpo=35.0,
            capex_pct=2.5,
            da_pct=1.8,
            sbc_pct=0.5,
            debt_ratio=0.14,
            cash_ratio=0.05,
            asset_turnover=1.08,
            operating_cf_ratio=0.16,
            fcf_ratio=0.11,
        ),
        _make_case(
            ticker="MEDX",
            prediction_year=2025,
            sector="Health Care",
            industry="Medical Devices",
            market_cap=37_000.0,
            shares_mm=600.0,
            revenues=[190.0, 205.0, 220.0, 236.0, 252.0, 269.0],
            ebit_margins=[20.4, 20.9, 21.4, 21.8, 22.1, 22.5],
            gross_margin_base_pct=58.0,
            baseline_growth_pct=9.4,
            baseline_target_margin_pct=24.1,
            baseline_wacc_pct=8.1,
            baseline_terminal_growth_pct=2.6,
            actual_growth_pct=8.8,
            actual_target_margin_pct=23.2,
            actual_wacc_pct=8.2,
            actual_terminal_growth_pct=2.5,
            beta=0.88,
            tax_rate_pct=22.0,
            dso=45.0,
            dio=27.0,
            dpo=29.0,
            capex_pct=3.0,
            da_pct=2.0,
            sbc_pct=1.0,
            debt_ratio=0.13,
            cash_ratio=0.06,
            asset_turnover=0.82,
            operating_cf_ratio=0.18,
            fcf_ratio=0.13,
        ),
        _make_case(
            ticker="DEVICE",
            prediction_year=2026,
            sector="Health Care",
            industry="Medical Devices",
            market_cap=35_000.0,
            shares_mm=585.0,
            revenues=[165.0, 178.0, 191.0, 205.0, 220.0, 236.0],
            ebit_margins=[19.7, 20.2, 20.8, 21.1, 21.5, 21.9],
            gross_margin_base_pct=56.0,
            baseline_growth_pct=9.1,
            baseline_target_margin_pct=23.8,
            baseline_wacc_pct=8.0,
            baseline_terminal_growth_pct=2.6,
            actual_growth_pct=8.4,
            actual_target_margin_pct=23.0,
            actual_wacc_pct=8.1,
            actual_terminal_growth_pct=2.5,
            beta=0.84,
            tax_rate_pct=22.0,
            dso=43.0,
            dio=25.0,
            dpo=28.0,
            capex_pct=2.9,
            da_pct=1.9,
            sbc_pct=0.9,
            debt_ratio=0.12,
            cash_ratio=0.05,
            asset_turnover=0.84,
            operating_cf_ratio=0.17,
            fcf_ratio=0.12,
        ),
    ]


def evaluate_default_suite(
    *,
    include_diagnostics: bool = True,
    performance_budget_ms: float = 1500.0,
) -> SharedBrainEvaluationResult:
    report = evaluate_shared_brain(
        build_default_validation_cases(),
        build_default_validation_observations(),
        performance_budget_ms=performance_budget_ms,
    )
    if include_diagnostics:
        report.operational_diagnostics = collect_operational_diagnostics()
    report.acceptance = summarize_acceptance(report, report.operational_diagnostics)
    return report