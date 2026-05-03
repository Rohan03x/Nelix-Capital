import sys
sys.path.insert(0, '.')

# ── Test transactions.py ──────────────────────────────────────────────────────
from auto_valuation.data.transactions import (
    compute_transaction_multiples, compute_transaction_comps_result,
)

txns = [
    {"target": "Co A", "acquirer": "Big Corp", "date": "2022-01-01",
     "ev_mm": 12000, "ebitda_mm": 600, "revenue_mm": 3000, "control_premium_pct": 0.25},
    {"target": "Co B", "acquirer": "Mega Inc",  "date": "2023-06-01",
     "ev_mm": 8000,  "ebitda_mm": 500, "revenue_mm": 2000, "control_premium_pct": 0.20},
    {"target": "Co C", "acquirer": "Acme Ltd",  "date": "2021-09-15",
     "ev_mm": 15000, "ebitda_mm": 800, "revenue_mm": 4000, "control_premium_pct": 0.30},
]
mults = compute_transaction_multiples(txns)
assert mults["deal_count"] == 3
assert mults["ev_ebitda"]["n"] == 3
med = mults["ev_ebitda"]["median"]
assert 16 < med < 22, f"Unexpected EV/EBITDA median {med}"
print(f"transactions.py: EV/EBITDA median={med:.2f}x  PASS")

comps_res = compute_transaction_comps_result(700, 3500, mults)
assert "ev_from_ebitda" in comps_res
assert "blended_ev_range" in comps_res
lo = comps_res["blended_ev_range"]["low"]
hi = comps_res["blended_ev_range"]["high"]
print(f"transactions comps result: EV low={lo:,.0f}mm  high={hi:,.0f}mm  PASS")

# ── Test comps.py ─────────────────────────────────────────────────────────────
from auto_valuation.data.comps import (
    compute_peer_multiples, compute_peer_set_stats, apply_comps_to_subject, build_football_field
)
peers_raw = [
    dict(peer_ticker="PEER1", market_cap_mm=10000, net_debt_mm=500,
         revenue_ltm=3000, ebitda_ltm=600, ebit_ltm=450,
         fcf_ltm=300, net_income_ltm=350),
    dict(peer_ticker="PEER2", market_cap_mm=8000,  net_debt_mm=-200,
         revenue_ltm=2500, ebitda_ltm=500, ebit_ltm=400,
         fcf_ltm=250, net_income_ltm=280),
]
peer_mults = [compute_peer_multiples(**p) for p in peers_raw]
stats = compute_peer_set_stats(peer_mults)
assert stats["ev_ebitda_ltm"]["n"] == 2
ev_med = stats["ev_ebitda_ltm"]["median"]
print(f"comps.py: EV/EBITDA peer median={ev_med:.2f}x  PASS")

subj = apply_comps_to_subject(stats, 3500, 700, 550, 350, 300)
assert "comps_ev_low_mm" in subj
print(f"comps implied EV: low={subj['comps_ev_low_mm']:,.0f}mm  high={subj['comps_ev_high_mm']:,.0f}mm  PASS")

ff = build_football_field(80000, 120000, 75000, 115000, 70000, 125000, net_debt=2000, shares_mm=1500)
assert len(ff) >= 3
print(f"football field rows={len(ff)}  PASS")

# ── Test sensitivity/analysis.py ─────────────────────────────────────────────
from auto_valuation.sensitivity.analysis import (
    _frange, scenario_summary_table, TornadoBar
)
rng = _frange(0.07, 0.09, 0.01)
assert len(rng) == 3, f"Expected 3 got {len(rng)}"
print(f"sensitivity _frange: {rng}  PASS")

# ── Test output/report.py ─────────────────────────────────────────────────────
from auto_valuation.output.report import format_valuation_summary
from auto_valuation.forecast.dcf import DCFResult
dcf = DCFResult(ticker="TEST", scenario="base")
dcf.enterprise_value   = 100000
dcf.pv_ufcfs           = 50000
dcf.pv_terminal_value  = 50000
dcf.tv_pct_of_ev       = 0.5
dcf.wacc               = 0.088
dcf.terminal_growth    = 0.025
dcf.terminal_ufcf      = 8000
dcf.terminal_value_ggm = 125000
dcf.forecast_years_data = []
dcf.warnings = []
summary = format_valuation_summary("TEST", dcf, 2000, 1500, current_price=60.0)
assert summary["enterprise_value"] == 100000
assert abs(summary["intrinsic_price"] - (100000-2000)/1500) < 0.01
print(f"report.py format_valuation_summary: price={summary['intrinsic_price']:.2f}  PASS")

# ── Test output/excel.py (import only; write to temp) ─────────────────────────
import tempfile, os
from auto_valuation.output.excel import write_excel_output
with tempfile.TemporaryDirectory() as td:
    out = write_excel_output(
        output_path=os.path.join(td, "test_output.xlsx"),
        ticker="TEST",
        dcf_result=dcf,
        net_debt=2000,
        shares_mm=1500,
        current_price=60.0,
        football_field=ff,
        scenario_table=[],
        assumptions={"wacc": 0.088, "terminal_growth": 0.025},
    )
    assert os.path.exists(out), "Excel file not created"
    sz = os.path.getsize(out)
    print(f"output/excel.py: wrote {sz:,} bytes to {os.path.basename(out)}  PASS")

print()
print("=== Phase 3 smoke test — ALL PASSED ===")
