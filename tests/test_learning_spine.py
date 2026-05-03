from __future__ import annotations

from dataclasses import replace
from datetime import date
import json

import pytest

from auto_valuation.learning.ledger import LedgerReader, LedgerWriter, PredictionRecord
from auto_valuation.learning.maintenance import run_live_evidence_bootstrap, run_scheduled_learning_maintenance
from auto_valuation.learning.postmortem import QuinquennialStore, run_annual_postmortem
from auto_valuation.validation.shared_brain import collect_operational_diagnostics


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


def _make_fundamentals_for_periods(periods: list[tuple[str, float, float, float | None]]) -> dict:
    income_yearly: dict[str, dict] = {}
    cash_flow_yearly: dict[str, dict] = {}
    for period_end, revenue_mm, ebit_margin, ufcf_mm in periods:
        revenue = revenue_mm * 1_000_000
        ebit = revenue * ebit_margin
        income_yearly[period_end] = {
            "date": period_end,
            "totalRevenue": revenue,
            "ebit": ebit,
        }
        cash_payload = {"date": period_end}
        if ufcf_mm is not None:
            cash_payload.update(
                {
                    "freeCashFlow": ufcf_mm * 1_000_000,
                    "totalCashFromOperatingActivities": (ufcf_mm + 5.0) * 1_000_000,
                    "capitalExpenditures": -5_000_000,
                }
            )
        cash_flow_yearly[period_end] = cash_payload
    return {
        "Financials": {
            "Income_Statement": {"yearly": income_yearly},
            "Cash_Flow": {"yearly": cash_flow_yearly},
        }
    }


def _make_bootstrap_fundamentals(
    annuals: list[dict[str, float | str]],
    *,
    company_name: str = "Acme Corp",
    sector: str = "Technology",
    industry: str = "Software",
) -> dict:
    income_yearly: dict[str, dict] = {}
    cash_flow_yearly: dict[str, dict] = {}
    balance_yearly: dict[str, dict] = {}
    for annual in annuals:
        period_end = str(annual["period_end"])
        filing_date = str(annual.get("filing_date") or period_end)
        revenue_mm = float(annual["revenue_mm"])
        ebit_margin = float(annual.get("ebit_margin") or 0.15)
        ufcf_mm = float(annual.get("ufcf_mm") or 0.0)
        gross_margin_pct = float(annual.get("gross_margin_pct") or 56.0)
        da_mm = float(annual.get("da_mm") or 4.0)
        capex_mm = float(annual.get("capex_mm") or 5.0)
        sbc_mm = float(annual.get("sbc_mm") or 1.2)
        shares_mm = float(annual.get("shares_mm") or 10.0)
        net_debt_mm = float(annual.get("net_debt_mm") or 18.0)
        total_debt_mm = float(annual.get("total_debt_mm") or max(net_debt_mm + 5.0, 0.0))
        total_assets_mm = float(annual.get("total_assets_mm") or max(revenue_mm * 0.9, 50.0))
        pretax_mm = float(annual.get("pretax_mm") or (revenue_mm * ebit_margin * 0.9))
        tax_mm = float(annual.get("tax_mm") or (pretax_mm * 0.21))
        receivables_mm = float(annual.get("receivables_mm") or (revenue_mm * 0.12))
        inventory_mm = float(annual.get("inventory_mm") or (revenue_mm * 0.05))
        payables_mm = float(annual.get("payables_mm") or (revenue_mm * 0.07))
        revenue = revenue_mm * 1_000_000
        ebit = revenue * ebit_margin
        gross_profit = revenue * (gross_margin_pct / 100.0)

        income_yearly[period_end] = {
            "date": period_end,
            "filing_date": filing_date,
            "totalRevenue": revenue,
            "ebit": ebit,
            "grossProfit": gross_profit,
            "incomeBeforeTax": pretax_mm * 1_000_000,
            "incomeTaxExpense": tax_mm * 1_000_000,
        }
        cash_flow_yearly[period_end] = {
            "date": period_end,
            "filing_date": filing_date,
            "freeCashFlow": ufcf_mm * 1_000_000,
            "totalCashFromOperatingActivities": (ufcf_mm + capex_mm) * 1_000_000,
            "capitalExpenditures": -(capex_mm * 1_000_000),
            "depreciationAndAmortization": da_mm * 1_000_000,
            "stockBasedCompensation": sbc_mm * 1_000_000,
        }
        balance_yearly[period_end] = {
            "date": period_end,
            "filing_date": filing_date,
            "totalAssets": total_assets_mm * 1_000_000,
            "commonStockSharesOutstanding": shares_mm * 1_000_000,
            "netDebt": net_debt_mm * 1_000_000,
            "shortLongTermDebtTotal": total_debt_mm * 1_000_000,
            "cashAndShortTermInvestments": (total_debt_mm - net_debt_mm) * 1_000_000,
            "netReceivables": receivables_mm * 1_000_000,
            "inventory": inventory_mm * 1_000_000,
            "accountsPayable": payables_mm * 1_000_000,
        }

    return {
        "General": {"Name": company_name, "Sector": sector, "Industry": industry},
        "Technicals": {"Beta": 1.05},
        "Financials": {
            "Income_Statement": {"yearly": income_yearly},
            "Cash_Flow": {"yearly": cash_flow_yearly},
            "Balance_Sheet": {"yearly": balance_yearly},
        },
    }


def _make_price_history(prices: dict[str, float]) -> list[dict[str, float | str]]:
    return [
        {"date": price_date, "close": close, "source_field": "adjusted_close"}
        for price_date, close in sorted(prices.items())
    ]


def test_partial_realized_labels_are_append_only(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer.append(_make_prediction_record())

    first = writer.backfill_actuals(
        "pred-1",
        actual_revenue_mm=102.0,
        postmortem_notes="Revenue arrived before the rest of the filing.",
        label_as_of_date=date(2026, 2, 15),
        aligned_period_end=date(2025, 12, 31),
        source_name="eodhd_fundamentals",
        source_kind="fundamentals",
        source_payload={"income_statement_date": "2025-12-31"},
    )
    second = writer.backfill_actuals(
        "pred-1",
        actual_revenue_mm=102.0,
        postmortem_notes="Revenue arrived before the rest of the filing.",
        label_as_of_date=date(2026, 2, 15),
        aligned_period_end=date(2025, 12, 31),
        source_name="eodhd_fundamentals",
        source_kind="fundamentals",
        source_payload={"income_statement_date": "2025-12-31"},
    )

    refreshed = reader.query(ticker="ACME", horizon_year=2025, scenario="base")[0]
    outcomes = reader.query_realized_outcomes(record_id="pred-1")

    assert first is True
    assert second is False
    assert refreshed.actual_revenue_mm == pytest.approx(102.0)
    assert refreshed.actual_ebit_margin is None
    assert len(outcomes) == 1
    assert outcomes[0].label_status == "partial"
    assert "actual_ebit_margin" in outcomes[0].unknown_targets


def test_maintenance_uses_strict_alignment_across_symbols(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    writer.append(replace(_make_prediction_record(), record_id="pred-acme", forecast_horizon_year=2024))
    writer.append(
        replace(
            _make_prediction_record(),
            record_id="pred-bolt",
            ticker="BOLT",
            company_name="Bolt Co",
            run_date=date(2024, 7, 1),
            forecast_horizon_year=2025,
            fiscal_year_end_month=6,
            fiscal_year_end_day=30,
        )
    )

    fundamentals_map = {
        "ACME": _make_fundamentals_for_periods([("2024-12-31", 910.0, 0.112, 92.0)]),
        "BOLT": _make_fundamentals_for_periods([("2025-06-30", 140.0, 0.18, 15.0)]),
    }

    first = run_scheduled_learning_maintenance(
        fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
        ledger_reader=reader,
        ledger_writer=writer,
        state_path=state_path,
        interval_hours=0,
        max_tickers=5,
        as_of_date=date(2026, 1, 31),
    )
    second = run_scheduled_learning_maintenance(
        fundamentals_provider=lambda ticker: fundamentals_map.get(ticker),
        ledger_reader=reader,
        ledger_writer=writer,
        state_path=state_path,
        interval_hours=0,
        max_tickers=5,
        as_of_date=date(2026, 1, 31),
    )

    assert first.backfilled_records == 2
    assert first.matured_records == 2
    assert sorted(first.tickers_processed) == ["ACME", "BOLT"]
    assert first.maintenance_run_id is not None
    assert second.backfilled_records == 0
    assert len(reader.query_maintenance_runs()) == 2


def test_maintenance_rejects_misaligned_period_end(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    writer.append(
        replace(
            _make_prediction_record(),
            record_id="pred-bolt",
            ticker="BOLT",
            company_name="Bolt Co",
            run_date=date(2024, 7, 1),
            forecast_horizon_year=2025,
            fiscal_year_end_month=6,
            fiscal_year_end_day=30,
        )
    )

    result = run_scheduled_learning_maintenance(
        fundamentals_provider=lambda _ticker: _make_fundamentals_for_periods([("2025-12-31", 140.0, 0.18, 15.0)]),
        ledger_reader=reader,
        ledger_writer=writer,
        state_path=state_path,
        interval_hours=0,
        max_tickers=5,
        as_of_date=date(2026, 1, 31),
    )
    refreshed = reader.query(ticker="BOLT", horizon_year=2025, scenario="base")[0]

    assert result.backfilled_records == 0
    assert result.matured_records == 0
    assert refreshed.actual_revenue_mm is None


def test_postmortem_prefers_aligned_realized_outcome_context(tmp_path):
    db_path = tmp_path / "predictions.db"
    writer = LedgerWriter(db_path=db_path, export_dir=tmp_path / "ledger")
    reader = LedgerReader(db_path=db_path)
    writer.append(_make_prediction_record())
    writer.backfill_actuals(
        "pred-1",
        actual_revenue_mm=108.0,
        actual_ufcf_mm=9.8,
        label_as_of_date=date(2026, 2, 1),
        aligned_period_end=date(2025, 12, 31),
        source_name="eodhd_fundamentals",
        source_kind="fundamentals",
        structural_break_hints=["revenue_drop_gt_20pct"],
        unknown_targets=["actual_ebit_margin", "actual_ev_mm", "actual_price_at_horizon", "macro_backdrop"],
        source_payload={"income_statement_date": "2025-12-31"},
        postmortem_notes="Partial filing only.",
    )

    records = run_annual_postmortem(
        "ACME",
        2025,
        ledger_reader=reader,
        ledger_writer=writer,
        actual_fetcher=lambda _ticker, _year: {},
    )

    assert len(records) == 1
    assert records[0].realized_label_status == "partial"
    assert records[0].realized_outcome_id is not None
    assert "actual_ev_mm" in records[0].realized_unknown_targets
    assert records[0].aligned_period_end == date(2025, 12, 31)


def test_bootstrap_materializes_staged_realized_evidence_and_postmortems(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    postmortem_db = tmp_path / "quinquennial.db"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer.append(_make_prediction_record())

    fundamentals = _make_bootstrap_fundamentals(
        [
            {
                "period_end": "2025-12-31",
                "filing_date": "2026-01-28",
                "revenue_mm": 108.0,
                "ebit_margin": 0.18,
                "ufcf_mm": 9.8,
                "shares_mm": 10.0,
                "net_debt_mm": 18.0,
            }
        ]
    )
    prices = _make_price_history({"2025-12-30": 11.25})

    result = run_live_evidence_bootstrap(
        tickers=["ACME"],
        fundamentals_provider=lambda _ticker: fundamentals,
        price_history_provider=lambda _ticker, _start, _end: prices,
        ledger_reader=reader,
        ledger_writer=writer,
        report_store=QuinquennialStore(postmortem_db),
        state_path=state_path,
        as_of_date=date(2026, 2, 1),
        replay_enabled=False,
    )

    refreshed = reader.query(ticker="ACME", horizon_year=2025, scenario="base")[0]
    outcomes = reader.query_realized_outcomes(record_id="pred-1")
    postmortems = reader.query_postmortems(record_id="pred-1")

    assert result.realized_outcomes_created == 2
    assert result.partial_realized_outcomes_created == 1
    assert result.full_realized_outcomes_created == 1
    assert result.annual_postmortems_created == 1
    assert refreshed.actual_price_at_horizon == pytest.approx(11.25)
    assert refreshed.actual_revenue_mm == pytest.approx(108.0)
    assert refreshed.actual_ev_mm == pytest.approx(130.5)
    assert len(outcomes) == 2
    assert {outcome.source_kind for outcome in outcomes} == {"market_price", "blended_realized_evidence"}
    assert len(postmortems) == 1


def test_bootstrap_honors_filing_date_availability(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer.append(_make_prediction_record())

    fundamentals = _make_bootstrap_fundamentals(
        [
            {
                "period_end": "2025-12-31",
                "filing_date": "2026-03-15",
                "revenue_mm": 108.0,
                "ebit_margin": 0.18,
                "ufcf_mm": 9.8,
                "shares_mm": 10.0,
                "net_debt_mm": 18.0,
            }
        ]
    )
    prices = _make_price_history({"2025-12-30": 11.25})

    result = run_live_evidence_bootstrap(
        tickers=["ACME"],
        fundamentals_provider=lambda _ticker: fundamentals,
        price_history_provider=lambda _ticker, _start, _end: prices,
        ledger_reader=reader,
        ledger_writer=writer,
        state_path=state_path,
        as_of_date=date(2026, 2, 1),
        replay_enabled=False,
    )

    refreshed = reader.query(ticker="ACME", horizon_year=2025, scenario="base")[0]
    outcomes = reader.query_realized_outcomes(record_id="pred-1")

    assert result.realized_outcomes_created == 1
    assert result.full_realized_outcomes_created == 0
    assert result.partial_realized_outcomes_created == 1
    assert result.missing_labels["missing_aligned_actuals"] == 1
    assert refreshed.actual_price_at_horizon == pytest.approx(11.25)
    assert refreshed.actual_revenue_mm is None
    assert len(outcomes) == 1
    assert outcomes[0].source_kind == "market_price"


def test_bootstrap_replay_populates_quinquennial_evidence_and_validator_diagnostics(tmp_path):
    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    state_path = tmp_path / "maintenance.json"
    postmortem_db = tmp_path / "quinquennial.db"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)

    annuals = []
    prices: dict[str, float] = {}
    for offset, year in enumerate(range(2017, 2025)):
        annuals.append(
            {
                "period_end": f"{year}-12-31",
                "filing_date": f"{year + 1}-01-28",
                "revenue_mm": 80.0 + (offset * 8.0),
                "ebit_margin": 0.12 + (offset * 0.005),
                "ufcf_mm": 6.0 + offset,
                "shares_mm": 10.0,
                "net_debt_mm": 12.0,
            }
        )
        prices[f"{year}-12-30"] = 9.0 + offset
        prices[f"{year + 1}-01-28"] = 9.2 + offset

    fundamentals = _make_bootstrap_fundamentals(annuals)
    price_history = _make_price_history(prices)

    result = run_live_evidence_bootstrap(
        tickers=["ACME"],
        fundamentals_provider=lambda _ticker: fundamentals,
        price_history_provider=lambda _ticker, _start, _end: price_history,
        ledger_reader=reader,
        ledger_writer=writer,
        report_store=QuinquennialStore(postmortem_db),
        state_path=state_path,
        as_of_date=date(2025, 2, 15),
        max_replay_predictions_per_ticker=5,
        replay_enabled=True,
    )
    second = run_live_evidence_bootstrap(
        tickers=["ACME"],
        fundamentals_provider=lambda _ticker: fundamentals,
        price_history_provider=lambda _ticker, _start, _end: price_history,
        ledger_reader=reader,
        ledger_writer=writer,
        report_store=QuinquennialStore(postmortem_db),
        state_path=state_path,
        as_of_date=date(2025, 2, 15),
        max_replay_predictions_per_ticker=5,
        replay_enabled=True,
    )

    diagnostics = collect_operational_diagnostics(
        db_path=db_path,
        export_dir=export_dir,
        postmortem_db_path=postmortem_db,
        state_path=state_path,
    )

    assert result.replay_predictions_created == 5
    assert result.realized_outcomes_created == 10
    assert result.annual_postmortems_created == 5
    assert result.quinquennial_reports_created == 1
    assert second.replay_predictions_created == 0
    assert second.realized_outcomes_created == 0
    assert second.annual_postmortems_created == 0
    assert second.quinquennial_reports_created == 0
    assert diagnostics.prediction_records == 5
    assert diagnostics.postmortem_records == 5
    assert diagnostics.matured_without_postmortem == 0
    assert diagnostics.quinquennial_reports == 1
    assert diagnostics.maintenance_stale is False


def test_bootstrap_resolver_expands_fallback_universe_from_cache_and_seed_list(tmp_path, monkeypatch):
    from auto_valuation.learning import live_evidence_bootstrap as live_bootstrap

    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "eodhd_fund_samsung.json").write_text(
        json.dumps({"data": {"General": {"PrimaryTicker": "005930.KO", "Code": "005930", "Exchange": "KO"}}}),
        encoding="utf-8",
    )
    (cache_dir / "eodhd_fund_rio.json").write_text(
        json.dumps({"data": {"General": {"Code": "RIO", "Exchange": "LSE"}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(live_bootstrap, "WEBAPP_CACHE_DIR", cache_dir)
    monkeypatch.setattr(live_bootstrap, "_load_supported_bootstrap_tickers", lambda: ["DEMO1", "DEMO2"])

    tickers = live_bootstrap._resolve_bootstrap_tickers(None, reader=reader, max_tickers=24)

    assert "005930.KO" in tickers
    assert "RIO.LSE" in tickers
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "DEMO1" in tickers
    assert len(tickers) >= 20


def test_bootstrap_resolver_prefers_symbol_universe_priority_candidates(tmp_path, monkeypatch):
    from auto_valuation.learning import live_evidence_bootstrap as live_bootstrap

    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    writer.append(replace(_make_prediction_record(), ticker="LEDGER1", record_id="ledger-1"))

    monkeypatch.setattr(live_bootstrap, "_load_universe_priority_tickers", lambda limit: ["UNIV1", "UNIV2", "UNIV3"])
    tickers = live_bootstrap._resolve_bootstrap_tickers(None, reader=reader, max_tickers=5)

    assert tickers[:3] == ["UNIV1", "UNIV2", "UNIV3"]
    assert "LEDGER1" in tickers
