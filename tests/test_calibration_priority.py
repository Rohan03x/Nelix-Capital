from __future__ import annotations

from auto_valuation.learning.calibration_priority import build_calibration_priority_index, calibration_priority_for_symbol
from auto_valuation.learning.ledger import LedgerReader, LedgerWriter
from auto_valuation.learning.postmortem import PostmortemRecord


def test_calibration_priority_uses_postmortem_error_history(tmp_path):
    from tests.test_learning_spine import _make_prediction_record

    db_path = tmp_path / "predictions.db"
    export_dir = tmp_path / "ledger"
    writer = LedgerWriter(db_path=db_path, export_dir=export_dir)
    reader = LedgerReader(db_path=db_path, export_dir=export_dir)
    prediction = _make_prediction_record()
    writer.append(prediction)
    writer.append_postmortem(
        PostmortemRecord(
            postmortem_id="pm-1",
            record_id=prediction.record_id,
            ticker=prediction.ticker,
            forecast_horizon_year=prediction.forecast_horizon_year,
            postmortem_date=prediction.run_date,
            actual_revenue_mm=80.0,
            actual_ebit_margin=0.08,
            actual_ufcf_mm=6.0,
            actual_ev_mm=85.0,
            actual_price_at_horizon=8.5,
            revenue_error_pct=-24.0,
            margin_error_bps=-260.0,
            ev_error_pct=-18.0,
            price_return_error_pct=-21.0,
            primary_miss_driver="enterprise_value",
            structural_break_detected=True,
        )
    )

    index = build_calibration_priority_index(reader)
    signal = calibration_priority_for_symbol(
        {"ticker": prediction.ticker, "sector": prediction.sector, "industry": prediction.industry},
        index,
    )

    assert signal["score"] > 0
    assert signal["mode"] == "ticker"
    assert signal["direct_samples"] == 1
    assert signal["structural_break_rate"] == 1.0