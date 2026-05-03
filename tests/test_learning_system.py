from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from auto_valuation.assumptions.engine import AssumptionSet
from auto_valuation.learning.adapter import AdaptedAssumptionSet, adapt_assumptions
from auto_valuation.learning.attribution import ErrorDriver, attribute_postmortem
from auto_valuation.learning._layered_calibrator import CalibrationObservation, CalibrationStore, calibrate
from auto_valuation.learning.confidence import build_ranked_confidence_model, compute_intervals, run_learning_monte_carlo
from auto_valuation.learning.cross_industry import AnalogObservation, find_analogs
from auto_valuation.learning.ledger import LedgerReader, LedgerWriter, PredictionRecord
from auto_valuation.learning.maintenance import run_scheduled_learning_maintenance
from auto_valuation.learning.online_research import ResearchInsight
from auto_valuation.learning.postmortem import PostmortemRecord, QuinquennialStore, run_annual_postmortem, should_run_quinquennial
from auto_valuation.validation.shared_brain import (
    collect_operational_diagnostics,
    evaluate_default_suite,
    evaluate_shared_brain,
    build_default_validation_cases,
    build_default_validation_observations,
)


def _make_prediction_record() -> PredictionRecord:
    return PredictionRecord(
        record_id="pred-1",
        ticker="ACME",
        company_name="Acme Corp",
        sector="Technology",
        industry="Software",
        run_date=date(2024, 1, 15),
        forecast_horizon_year=2025,
        years_since_ipo=6,
        data_vintage_years=6,
        predicted_revenue_mm=100.0,
        predicted_ebit_margin=0.15,
        predicted_ebit_mm=15.0,
        predicted_ufcf_mm=11.0,
        predicted_wacc=0.09,
        predicted_terminal_growth=0.025,
        predicted_ev_mm=150.0,
        predicted_equity_value_mm=140.0,
        predicted_price_per_share=14.0,
        scenario="base",
        near_term_revenue_growth=0.08,
        target_ebit_margin=0.18,
        da_pct_revenue=0.03,
        capex_pct_revenue=0.02,
        beta=1.1,
        erp=0.055,
        rf_rate=0.04,
        actual_price_at_prediction=10.0,
        actual_ev_at_prediction=120.0,
        market_cycle_phase="expansion",
        macro_backdrop={"10y_yield": 0.035, "cpi_yoy": 0.025, "gdp_growth": 0.02},
        market_cap_regime="mid",
        macro_regime="neutral",
        feature_vector=(0.28, 0.16, 0.68, 0.03, 1.1, 0.72, 0.7, 0.55, 0.18, 0.02),
    )


def _make_raw_assumptions() -> AssumptionSet:
    return AssumptionSet(
        revenue_growth_rates=[0.08, 0.07, 0.06, 0.05, 0.045, 0.04, 0.035],
        near_term_growth=0.08,
        long_run_growth=0.03,
        ebit_margin_current=0.14,
        ebit_margin_terminal=0.18,
        ebit_margin_schedule=[0.145, 0.15, 0.16, 0.17, 0.175, 0.18, 0.18],
        effective_tax_rate=0.21,
        capex_pct_revenue=0.03,
        capex_schedule=[0.03] * 7,
        da_pct_revenue=0.02,
        basic_shares_mm=10.0,
    )


def _make_fundamentals_for_actuals(years: list[tuple[int, float, float, float]]) -> dict:
    income_yearly: dict[str, dict] = {}
    cash_flow_yearly: dict[str, dict] = {}
    for year, revenue_mm, ebit_margin, ufcf_mm in years:
        revenue = revenue_mm * 1_000_000
        ebit = revenue * ebit_margin
        key = f"{year}-12-31"
        income_yearly[key] = {
            "date": key,
            "totalRevenue": revenue,
            "ebit": ebit,
        }
        cash_flow_yearly[key] = {
            "date": key,
            "freeCashFlow": ufcf_mm * 1_000_000,
            "totalCashFromOperatingActivities": (ufcf_mm + 5.0) * 1_000_000,
            "capitalExpenditures": -5_000_000,
        }
    return {
        "Financials": {
            "Income_Statement": {"yearly": income_yearly},
            "Cash_Flow": {"yearly": cash_flow_yearly},
        }
    }


def _make_observations(
    count: int,
    *,
    sector: str = "Technology",
    ticker: str = "",
    feature_vector: tuple[float, ...] | None = None,
    structural_break_flag: bool = False,
    predicted_ufcf_margin: float | None = None,
    actual_ufcf_margin: float | None = None,
    predicted_reinvestment_rate: float | None = None,
    actual_reinvestment_rate: float | None = None,
) -> list[CalibrationObservation]:
    return [
        CalibrationObservation(
            sector=sector,
            industry="Software",
            data_vintage_years=6,
            market_cap_regime="mid",
            macro_regime="neutral",
            predicted_revenue_growth=0.08,
            actual_revenue_growth=0.10,
            predicted_ebit_margin=0.18,
            actual_ebit_margin=0.19,
            predicted_wacc=0.09,
            actual_wacc=0.095,
            predicted_terminal_growth=0.03,
            actual_terminal_growth=0.028,
            predicted_beta=1.1,
            actual_beta=1.05,
            ticker=ticker,
            predicted_ufcf_margin=predicted_ufcf_margin,
            actual_ufcf_margin=actual_ufcf_margin,
            predicted_reinvestment_rate=predicted_reinvestment_rate,
            actual_reinvestment_rate=actual_reinvestment_rate,
            structural_break_flag=structural_break_flag,
            feature_vector=feature_vector,
        )
        for _ in range(count)
    ]


def _make_base_dcf_kwargs() -> dict:
    income = [
        {"calendarYear": "2023", "revenue": 100.0, "ebit": 15.0, "operatingIncome": 15.0},
        {"calendarYear": "2022", "revenue": 92.0, "ebit": 13.0, "operatingIncome": 13.0},
        {"calendarYear": "2021", "revenue": 84.0, "ebit": 11.0, "operatingIncome": 11.0},
    ]
    cash_flows = [
        {"calendarYear": "2023", "depreciationAndAmortization": 4.0, "capitalExpenditure": -3.0, "stockBasedCompensation": 1.0},
        {"calendarYear": "2022", "depreciationAndAmortization": 3.8, "capitalExpenditure": -2.9, "stockBasedCompensation": 0.9},
        {"calendarYear": "2021", "depreciationAndAmortization": 3.6, "capitalExpenditure": -2.7, "stockBasedCompensation": 0.8},
    ]
    balance = [
        {"calendarYear": "2023", "totalAssets": 80.0, "netReceivables": 10.0, "inventory": 2.0, "accountPayables": 8.0},
        {"calendarYear": "2022", "totalAssets": 75.0, "netReceivables": 9.0, "inventory": 2.0, "accountPayables": 7.5},
        {"calendarYear": "2021", "totalAssets": 70.0, "netReceivables": 8.5, "inventory": 1.8, "accountPayables": 7.0},
    ]
    return {
        "ticker": "ACME",
        "scenario": "base",
        "income_stmts": income,
        "cash_flows": cash_flows,
        "balance_sheets": balance,
        "wacc": 0.09,
        "terminal_growth": 0.025,
        "near_term_growth": 0.08,
        "target_ebit_margin": 0.18,
        "forecast_years": 5,
        "tax_rate_override": 0.21,
        "mid_year_convention": True,
    }


class TestLearningLedger:
    def test_write_read_and_immutability(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        export_dir = tmp_path / "ledger"
        writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
        reader = LedgerReader(db_path=db_path)
        record = _make_prediction_record()

        writer.append(record)
        loaded = reader.query(ticker="ACME", horizon_year=2025, scenario="base")

        assert len(loaded) == 1
        assert loaded[0] == record
        assert loaded[0].horizon_target_date == date(2025, 12, 31)
        assert loaded[0].horizon_label == "FY2025"
        with pytest.raises(FrozenInstanceError):
            loaded[0].ticker = "DIFF"
        with pytest.raises(ValueError):
            writer.append(record)


class TestAnnualPostmortem:
    def test_computes_error_metrics(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        writer = LedgerWriter(db_path=db_path, export_dir=tmp_path / "ledger")
        reader = LedgerReader(db_path=db_path)
        writer.append(_make_prediction_record())

        def actual_fetcher(_ticker: str, _year: int) -> dict:
            return {
                "actual_revenue_mm": 115.0,
                "actual_ebit_margin": 0.17,
                "actual_ufcf_mm": 12.0,
                "actual_ev_mm": 142.5,
                "actual_price_at_horizon": 13.0,
                "macro_backdrop": {"10y_yield": 0.038, "cpi_yoy": 0.03, "gdp_growth": 0.019},
                "surprise_flags": ["macro shock"],
            }

        records = run_annual_postmortem(
            "ACME",
            2025,
            ledger_reader=reader,
            ledger_writer=writer,
            actual_fetcher=actual_fetcher,
        )

        assert len(records) == 1
        postmortem = records[0]
        assert postmortem.revenue_error_pct == pytest.approx(15.0)
        assert postmortem.margin_error_bps == pytest.approx(200.0)
        assert postmortem.ev_error_pct == pytest.approx(-5.0)
        assert postmortem.primary_miss_driver == "revenue"

    def test_skips_existing_postmortems_when_persisting(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        writer = LedgerWriter(db_path=db_path, export_dir=tmp_path / "ledger")
        reader = LedgerReader(db_path=db_path)
        writer.append(_make_prediction_record())

        def actual_fetcher(_ticker: str, _year: int) -> dict:
            return {
                "actual_revenue_mm": 115.0,
                "actual_ebit_margin": 0.17,
                "actual_ufcf_mm": 12.0,
                "actual_ev_mm": 142.5,
                "actual_price_at_horizon": 13.0,
                "macro_backdrop": {},
                "surprise_flags": [],
            }

        first = run_annual_postmortem(
            "ACME",
            2025,
            ledger_reader=reader,
            ledger_writer=writer,
            actual_fetcher=actual_fetcher,
        )
        second = run_annual_postmortem(
            "ACME",
            2025,
            ledger_reader=reader,
            ledger_writer=writer,
            actual_fetcher=actual_fetcher,
        )

        assert len(first) == 1
        assert second == []
        assert len(reader.query_postmortems(record_id="pred-1")) == 1


class TestLearningMaintenance:
    def test_backfills_and_creates_annual_postmortems_once(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        export_dir = tmp_path / "ledger"
        state_path = tmp_path / "maintenance.json"
        reader = LedgerReader(db_path=db_path, export_dir=export_dir)
        writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
        writer.append(
            replace(
                _make_prediction_record(),
                record_id="pred-2024",
                forecast_horizon_year=2024,
                predicted_revenue_mm=880.0,
                predicted_ebit_margin=0.13,
                predicted_ufcf_mm=87.0,
            )
        )

        fundamentals_map = {
            "ACME": _make_fundamentals_for_actuals([(2024, 910.0, 0.112, 92.0)])
        }

        first = run_scheduled_learning_maintenance(
            fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
            ledger_reader=reader,
            ledger_writer=writer,
            state_path=state_path,
            interval_hours=0,
            max_tickers=5,
        )
        refreshed = reader.query(ticker="ACME", horizon_year=2024, scenario="base")[0]
        second = run_scheduled_learning_maintenance(
            fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
            ledger_reader=reader,
            ledger_writer=writer,
            state_path=state_path,
            interval_hours=0,
            max_tickers=5,
        )

        assert first.backfilled_records == 1
        assert first.annual_postmortems_created == 1
        assert refreshed.actual_revenue_mm == pytest.approx(910.0)
        assert refreshed.actual_ebit_margin == pytest.approx(0.112)
        assert second.backfilled_records == 0
        assert second.annual_postmortems_created == 0

    def test_creates_quinquennial_report_once(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        export_dir = tmp_path / "ledger"
        state_path = tmp_path / "maintenance.json"
        report_store = QuinquennialStore(tmp_path / "postmortems.db")
        reader = LedgerReader(db_path=db_path, export_dir=export_dir)
        writer = LedgerWriter(db_path=db_path, export_dir=export_dir)

        for year, revenue in zip(range(2021, 2026), [101.0, 106.0, 111.0, 117.0, 123.0]):
            writer.append(
                replace(
                    _make_prediction_record(),
                    record_id=f"pred-{year}",
                    run_date=date(year - 1, 1, 15),
                    forecast_horizon_year=year,
                    predicted_revenue_mm=revenue - 3.0,
                    predicted_ebit_margin=0.14,
                    predicted_ufcf_mm=12.0,
                )
            )

        fundamentals_map = {
            "ACME": _make_fundamentals_for_actuals(
                [
                    (2021, 101.0, 0.141, 11.2),
                    (2022, 106.0, 0.142, 11.8),
                    (2023, 111.0, 0.144, 12.5),
                    (2024, 117.0, 0.145, 13.1),
                    (2025, 123.0, 0.146, 13.8),
                ]
            )
        }

        first = run_scheduled_learning_maintenance(
            fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
            ledger_reader=reader,
            ledger_writer=writer,
            report_store=report_store,
            state_path=state_path,
            interval_hours=0,
            max_tickers=5,
        )
        second = run_scheduled_learning_maintenance(
            fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
            ledger_reader=reader,
            ledger_writer=writer,
            report_store=report_store,
            state_path=state_path,
            interval_hours=0,
            max_tickers=5,
        )

        assert first.quinquennial_reports_created == 1
        assert report_store.has_report("ACME", 2020) is True
        assert second.quinquennial_reports_created == 0


class TestAttribution:
    def test_revenue_surprise_is_top_driver_for_known_pattern(self):
        postmortem = PostmortemRecord(
            postmortem_id="pm-1",
            record_id="pred-1",
            ticker="ACME",
            forecast_horizon_year=2025,
            postmortem_date=date.today(),
            actual_revenue_mm=70.0,
            actual_ebit_margin=0.149,
            actual_ufcf_mm=8.0,
            actual_ev_mm=130.0,
            actual_price_at_horizon=11.0,
            revenue_error_pct=-30.0,
            margin_error_bps=-10.0,
            ev_error_pct=-12.0,
            price_return_error_pct=-5.0,
            primary_miss_driver="revenue",
            macro_backdrop_at_prediction={"10y_yield": 0.035, "cpi_yoy": 0.025, "gdp_growth": 0.02},
            macro_backdrop_at_horizon={"10y_yield": 0.036, "cpi_yoy": 0.024, "gdp_growth": 0.018},
        )

        attributions = attribute_postmortem(postmortem)

        assert attributions[0][0] == ErrorDriver.REVENUE_SURPRISE
        assert sum(weight for _, weight in attributions) == pytest.approx(100.0)


class TestCalibrator:
    def test_computes_corrections_and_honors_gating(self, tmp_path):
        raw = _make_raw_assumptions()
        store = CalibrationStore(tmp_path / "calibration.db")
        calibrated = calibrate(
            raw,
            "Technology",
            "Software",
            6,
            "mid",
            "neutral",
            observations=_make_observations(10),
            base_wacc=0.09,
            base_terminal_growth=0.03,
            base_beta=1.1,
            calibration_store=store,
        )
        thin = calibrate(
            raw,
            "Technology",
            "Software",
            6,
            "mid",
            "neutral",
            observations=_make_observations(4),
            base_wacc=0.09,
            base_terminal_growth=0.03,
            base_beta=1.1,
            calibration_store=store,
        )

        assert calibrated.revenue_growth_adj == pytest.approx(0.10)
        assert calibrated.ebit_margin_adj == pytest.approx(0.19)
        assert calibrated.wacc_adj == pytest.approx(0.095)
        assert calibrated.calibration_cohort_size == 10
        assert calibrated.calibration_diagnostics.assumptions["revenue_growth"].dominant_layer == "cohort_memory"
        assert calibrated.scenario_width_multiplier >= 1.0
        assert thin.revenue_growth_adj == pytest.approx(raw.near_term_growth)
        assert thin.calibration_confidence <= 0.35
        assert thin.calibration_diagnostics.assumptions["revenue_growth"].weak_evidence is True

    def test_detects_structural_break_and_exposes_cashflow_learning(self, tmp_path):
        raw = _make_raw_assumptions()
        store = CalibrationStore(tmp_path / "structural.db")
        observations = _make_observations(
            6,
            ticker="ACME",
            feature_vector=(0.30, 0.18, 0.68, 0.03, 1.10, 0.72, 0.70, 0.55, 0.18, 0.02),
            structural_break_flag=True,
            predicted_ufcf_margin=0.12,
            actual_ufcf_margin=0.07,
            predicted_reinvestment_rate=0.01,
            actual_reinvestment_rate=0.05,
        ) + _make_observations(
            4,
            sector="Industrials",
            feature_vector=(0.31, 0.17, 0.67, 0.031, 1.08, 0.70, 0.69, 0.53, 0.17, 0.02),
            structural_break_flag=True,
            predicted_ufcf_margin=0.11,
            actual_ufcf_margin=0.06,
            predicted_reinvestment_rate=0.015,
            actual_reinvestment_rate=0.045,
        )

        calibrated = calibrate(
            raw,
            "Technology",
            "Software",
            6,
            "mid",
            "neutral",
            observations=observations,
            base_wacc=0.09,
            base_terminal_growth=0.03,
            base_beta=1.1,
            calibration_store=store,
            ticker="ACME",
            feature_vector=(0.30, 0.18, 0.68, 0.03, 1.10, 0.72, 0.70, 0.55, 0.18, 0.02),
        )

        assert calibrated.calibration_diagnostics.structural_break.detected is True
        assert calibrated.calibration_diagnostics.structural_break.score >= 0.45
        assert calibrated.scenario_width_multiplier > 1.0
        assert calibrated.ufcf_margin_adj < 0.12
        assert calibrated.reinvestment_rate_adj > 0.01
        assert calibrated.calibration_diagnostics.assumptions["ufcf_margin"].evidence_count > 0
        assert calibrated.calibration_diagnostics.assumptions["reinvestment_rate"].evidence_count > 0


class TestCrossIndustryAnalogs:
    def test_similarity_and_same_sector_exclusion(self):
        subject = (0.30, 0.18, 0.68, 0.03, 1.10, 0.72, 0.70, 0.55, 0.18, 0.02)
        candidates = [
            AnalogObservation(
                ticker="SAME",
                sector="Technology",
                industry="Software",
                vintage_year=6,
                feature_vector=subject,
                outcome_revenue_cagr_5y=0.20,
            ),
            AnalogObservation(
                ticker="ANLG1",
                sector="Consumer Staples",
                industry="Household Products",
                vintage_year=5,
                feature_vector=(0.29, 0.17, 0.67, 0.031, 1.09, 0.70, 0.72, 0.54, 0.17, 0.021),
                outcome_revenue_cagr_5y=0.12,
            ),
            AnalogObservation(
                ticker="ANLG2",
                sector="Industrials",
                industry="Machinery",
                vintage_year=6,
                feature_vector=(0.05, 0.08, 0.20, 0.12, 0.80, 0.30, 2.00, 0.10, 0.40, -0.02),
                outcome_revenue_cagr_5y=0.03,
            ),
        ]

        analogs = find_analogs(
            "ACME",
            subject,
            candidates,
            subject_sector="Technology",
            subject_industry="Software",
            subject_vintage_year=6,
        )

        assert [match.analog.ticker for match in analogs.analogs] == ["ANLG1"]
        assert analogs.analogs[0].similarity_score > 0.85


class TestConfidenceIntervals:
    def test_bands_widen_with_horizon_and_thin_vintage(self):
        calibrated = calibrate(
            _make_raw_assumptions(),
            "Technology",
            "Software",
            6,
            "mid",
            "neutral",
            observations=_make_observations(10),
            base_wacc=0.09,
            base_terminal_growth=0.03,
            base_beta=1.1,
            calibration_store=CalibrationStore(),
        )

        short_history = compute_intervals(calibrated, 1, calibrated.calibration_confidence, 0.2, forecast_years=5, cohort_size=calibrated.calibration_cohort_size)
        long_history = compute_intervals(calibrated, 10, calibrated.calibration_confidence, 0.2, forecast_years=5, cohort_size=calibrated.calibration_cohort_size)

        year1_width = short_history.intervals["revenue_growth"][0].p90 - short_history.intervals["revenue_growth"][0].p10
        year5_width = short_history.intervals["revenue_growth"][4].p90 - short_history.intervals["revenue_growth"][4].p10
        long_year1_width = long_history.intervals["revenue_growth"][0].p90 - long_history.intervals["revenue_growth"][0].p10

        assert year5_width > year1_width
        assert year1_width > long_year1_width


class TestRankedConfidenceModel:
    def test_penalizes_thin_conflicted_and_sensitive_payloads(self):
        strong_payload = {
            "calibration_confidence": 0.78,
            "learning_confidence": 0.74,
            "calibration_cohort_size": 12,
            "history_window_years": 5,
            "pattern_match_score": 0.84,
            "scenario_width_multiplier": 1.0,
            "wacc": 8.7,
            "terminal_growth": 2.7,
            "global_learning": {"confidence": 0.68, "cohort_size": 12, "sector_span": 4},
            "analogs": {
                "count": 3,
                "items": [
                    {"score": 0.91, "similarity": 0.92},
                    {"score": 0.88, "similarity": 0.89},
                    {"score": 0.86, "similarity": 0.87},
                ],
            },
            "layered_learning": {
                "uncertainty": {"weak_evidence": False, "conflict_score": 0.004, "scenario_width_multiplier": 1.0},
                "structural_break": {"score": 0.08},
                "layer_mix": {
                    "company_memory": {"records": 3},
                    "cohort_memory": {"records": 12},
                    "sector_memory": {"records": 16},
                    "analog_memory": {"confidence": 0.82},
                },
            },
            "explainability": {
                "company_memory": {"history_window_years": 5, "completed_years": 8},
                "forecast_layers": [
                    {"company_anchor": 7.3, "sector_anchor": 7.0, "learned_adjustment": 7.2},
                    {"company_anchor": 18.5, "sector_anchor": 18.0, "learned_adjustment": 18.3},
                ],
            },
        }
        weak_payload = {
            "calibration_confidence": 0.31,
            "learning_confidence": 0.28,
            "calibration_cohort_size": 2,
            "history_window_years": 2,
            "pattern_match_score": 0.34,
            "scenario_width_multiplier": 1.8,
            "wacc": 8.0,
            "terminal_growth": 6.3,
            "global_learning": {"confidence": 0.18, "cohort_size": 2, "sector_span": 1},
            "analogs": {
                "count": 1,
                "items": [
                    {"score": 0.51, "similarity": 0.44},
                ],
            },
            "layered_learning": {
                "uncertainty": {"weak_evidence": True, "conflict_score": 0.028, "scenario_width_multiplier": 1.8},
                "structural_break": {"score": 0.67},
                "layer_mix": {
                    "company_memory": {"records": 0},
                    "cohort_memory": {"records": 2},
                    "sector_memory": {"records": 4},
                    "analog_memory": {"confidence": 0.22},
                },
            },
            "explainability": {
                "company_memory": {"history_window_years": 2, "completed_years": 3, "review_due": True},
                "forecast_layers": [
                    {"company_anchor": 9.8, "sector_anchor": 4.1, "learned_adjustment": 1.2},
                    {"company_anchor": 23.0, "sector_anchor": 14.2, "learned_adjustment": 9.0},
                ],
            },
        }

        strong_model = build_ranked_confidence_model(strong_payload)
        weak_model = build_ranked_confidence_model(weak_payload)

        assert strong_model["assumption_confidence"]["score"] > weak_model["assumption_confidence"]["score"]
        assert strong_model["valuation_confidence"]["score"] > weak_model["valuation_confidence"]["score"]
        assert strong_model["valuation_confidence"]["expected_error_pct"]["p50"] < weak_model["valuation_confidence"]["expected_error_pct"]["p50"]
        assert strong_model["ranking_signal"] > weak_model["ranking_signal"]


class TestAdapterIntegration:
    def test_pipeline_returns_adapted_assumptions(self, tmp_path):
        research_insights = [
            ResearchInsight(
                query="AI automation impact on Technology margins 2026",
                source_url="https://example.com/research",
                source_credibility=0.9,
                insight_text="Automation is improving support margins.",
                assumption_impacted="ebit_margin_adj",
                direction="positive",
                magnitude_estimate=0.01,
                confidence=0.8,
                valid_until=date.today() + timedelta(days=7),
            )
        ]
        analog_candidates = [
            AnalogObservation(
                ticker="ANLG1",
                sector="Consumer Staples",
                industry="Household Products",
                vintage_year=6,
                feature_vector=(0.29, 0.17, 0.67, 0.031, 1.09, 0.70, 0.72, 0.54, 0.17, 0.021),
                outcome_revenue_cagr_5y=0.12,
                outcome_margin_change_bps=120.0,
                outcome_ev_multiple_change=1.5,
            )
        ]

        adapted = adapt_assumptions(
            ticker="ACME",
            sector="Technology",
            industry="Software",
            data_vintage_years=6,
            market_cap_regime="mid",
            macro_regime="neutral",
            raw_assumptions=_make_raw_assumptions(),
            research_insights=research_insights,
            observations=_make_observations(10),
            analog_candidates=analog_candidates,
            feature_vector=(0.30, 0.18, 0.68, 0.03, 1.10, 0.72, 0.70, 0.55, 0.18, 0.02),
            base_wacc=0.09,
            base_terminal_growth=0.03,
            base_beta=1.1,
            calibration_store=CalibrationStore(tmp_path / "adapter.db"),
        )

        assert isinstance(adapted, AdaptedAssumptionSet)
        assert adapted.confidence_intervals is not None
        assert adapted.model_confidence_score > 0.0
        assert adapted.analog_set is not None
        assert adapted.research_insights == research_insights
        assert adapted.calibration_diagnostics.assumptions["revenue_growth"].layers
        assert adapted.scenario_width_multiplier >= 1.0


class TestLearningMonteCarlo:
    def test_percentiles_are_ordered(self):
        summary = run_learning_monte_carlo(
            _make_base_dcf_kwargs(),
            samples=100,
            seed=42,
            net_debt=20.0,
            shares_mm=10.0,
        )

        assert summary.ev_p10 < summary.ev_p50 < summary.ev_p90


class TestTemporalSchedule:
    def test_quinquennial_schedule(self):
        assert should_run_quinquennial(5) is True
        assert should_run_quinquennial(10) is True
        assert should_run_quinquennial(15) is True
        assert should_run_quinquennial(20) is True
        assert should_run_quinquennial(2) is False
        assert should_run_quinquennial(3) is False
        assert should_run_quinquennial(4) is False


class TestSharedBrainValidationHarness:
    def test_default_suite_is_time_aware_and_improves_core_metrics(self):
        report = evaluate_default_suite(include_diagnostics=False, performance_budget_ms=2500.0)

        assert report.case_count >= 8
        assert report.time_aware_violations == 0
        assert report.metrics["revenue_growth"].shared_mae <= report.metrics["revenue_growth"].baseline_mae
        assert report.metrics["ebit_margin"].shared_mae <= report.metrics["ebit_margin"].baseline_mae
        assert report.metrics["ufcf_error_pct"].shared_mae <= report.metrics["ufcf_error_pct"].baseline_mae
        assert report.metrics["valuation_error_pct"].shared_mae <= report.metrics["valuation_error_pct"].baseline_mae
        assert report.confidence_ranking_accuracy >= 0.75
        assert report.confidence_bucket_gap > 0.0
        assert report.analog_consistency_rate == pytest.approx(1.0)
        assert report.performance_ms <= 2500.0
        assert report.acceptance is not None
        assert report.acceptance.benchmark_passed is True
        assert report.acceptance.status == "provisional"
        assert report.acceptance.remaining_gaps == []

    def test_sparse_data_fallback_stays_explicit(self):
        report = evaluate_shared_brain(
            build_default_validation_cases()[:2],
            build_default_validation_observations()[:2],
            performance_budget_ms=2500.0,
        )

        assert report.sparse_fallback_cases == report.case_count
        assert all(case.shared.global_learning_enabled is False for case in report.cases)
        assert all(case.shared.calibration_confidence <= 0.35 for case in report.cases)


class TestOperationalDiagnostics:
    def test_repeatable_and_flags_missing_postmortems(self, tmp_path):
        db_path = tmp_path / "predictions.db"
        export_dir = tmp_path / "ledger"
        state_path = tmp_path / "maintenance.json"
        writer = LedgerWriter(db_path=db_path, export_dir=export_dir)

        writer.append(
            replace(
                _make_prediction_record(),
                record_id="pred-diag",
                forecast_horizon_year=2024,
            )
        )
        writer.backfill_actuals(
            "pred-diag",
            actual_revenue_mm=110.0,
            actual_ebit_margin=0.16,
            actual_ufcf_mm=12.0,
        )
        state_path.write_text(
            json.dumps({"last_run_at": "2026-04-30T00:00:00+00:00"}),
            encoding="utf-8",
        )

        as_of = datetime(2026, 5, 3, tzinfo=timezone.utc)
        first = collect_operational_diagnostics(
            db_path=db_path,
            export_dir=export_dir,
            postmortem_db_path=tmp_path / "postmortems.db",
            state_path=state_path,
            as_of=as_of,
            stale_after_hours=24,
        )
        second = collect_operational_diagnostics(
            db_path=db_path,
            export_dir=export_dir,
            postmortem_db_path=tmp_path / "postmortems.db",
            state_path=state_path,
            as_of=as_of,
            stale_after_hours=24,
        )

        assert first == second
        assert first.prediction_records == 1
        assert first.matured_records == 1
        assert first.postmortem_records == 0
        assert first.matured_without_postmortem == 1
        assert first.maintenance_state_exists is True
        assert first.maintenance_stale is True
        assert first.status == "warn"
        assert any("missing postmortems" in warning.lower() for warning in first.warnings)