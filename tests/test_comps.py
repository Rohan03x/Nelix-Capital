"""
tests/test_comps.py — Unit tests for comps, transactions, and data-bridge modules.

Covers:
  - compute_peer_multiples
  - compute_peer_set_stats (p25 / median / p75)
  - apply_comps_to_subject
  - build_football_field
  - apply_manual_proforma_adjustments
  - check_peer_proforma_events
  - compute_transaction_multiples
  - compute_transaction_comps_result
  - compute_net_debt (bridge)
  - compute_equity_value (bridge)
  - working_capital helpers
  - dilution: treasury_stock_method, add_rsu_dilution, compute_fully_diluted_shares
"""

from __future__ import annotations

import pytest

from auto_valuation.data.comps import (
    compute_peer_multiples,
    compute_peer_set_stats,
    apply_comps_to_subject,
    build_football_field,
    apply_manual_proforma_adjustments,
    check_peer_proforma_events,
)
from auto_valuation.data.transactions import (
    _percentile,
    compute_transaction_multiples,
    compute_transaction_comps_result,
)
from auto_valuation.data.bridge import compute_net_debt, compute_equity_value
from auto_valuation.model.working_capital import (
    compute_dso, compute_dio, compute_dpo, compute_cwc_days,
    compute_nowc_from_bs, compute_delta_nowc,
)
from auto_valuation.model.dilution import (
    treasury_stock_method,
    add_rsu_dilution,
    compute_fully_diluted_shares,
    compute_price_per_share,
)


# ─────────────────────────────────────────────────────────────────────────────
# Percentile helper
# ─────────────────────────────────────────────────────────────────────────────

class TestPercentile:
    def test_single_value(self):
        assert _percentile([42.0], 50) == 42.0

    def test_two_values_median(self):
        assert _percentile([10.0, 20.0], 50) == 15.0

    def test_sorted_ascending(self):
        vals = [3.0, 1.0, 2.0]
        assert _percentile(vals, 0)  == 1.0
        assert _percentile(vals, 100) == 3.0

    def test_p25_p75(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        p25 = _percentile(vals, 25)
        p75 = _percentile(vals, 75)
        assert p25 < p75
        assert p25 == pytest.approx(1.75, abs=0.01)
        assert p75 == pytest.approx(3.25, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# compute_peer_multiples
# ─────────────────────────────────────────────────────────────────────────────

class TestComputePeerMultiples:
    def _peer(self, **kwargs):
        defaults = dict(
            peer_ticker="TEST",
            market_cap_mm=10000,
            net_debt_mm=500,
            revenue_ltm=3000,
            ebitda_ltm=600,
            ebit_ltm=450,
            fcf_ltm=300,
            net_income_ltm=350,
        )
        defaults.update(kwargs)
        return compute_peer_multiples(**defaults)

    def test_ev_computed(self):
        m = self._peer(market_cap_mm=10000, net_debt_mm=500)
        assert m["ev"] == 10500

    def test_ev_ebitda(self):
        m = self._peer(market_cap_mm=10000, net_debt_mm=500, ebitda_ltm=600)
        assert abs(m["ev_ebitda_ltm"] - 10500 / 600) < 0.001

    def test_zero_ebitda_returns_none(self):
        m = self._peer(ebitda_ltm=0)
        assert m["ev_ebitda_ltm"] is None

    def test_ntm_multiples_computed(self):
        m = self._peer(revenue_ntm=3300, ebitda_ntm=660)
        assert m.get("ev_revenue_ntm") is not None
        assert m.get("ev_ebitda_ntm") is not None

    def test_negative_net_debt_reduces_ev(self):
        m_pos = self._peer(net_debt_mm=2000)
        m_neg = self._peer(net_debt_mm=-2000)
        assert m_pos["ev"] > m_neg["ev"]


# ─────────────────────────────────────────────────────────────────────────────
# compute_peer_set_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestPeerSetStats:
    @pytest.fixture
    def three_peers(self):
        raw = [
            dict(peer_ticker="A", market_cap_mm=10000, net_debt_mm=500,
                 revenue_ltm=3000, ebitda_ltm=600, ebit_ltm=450,
                 fcf_ltm=300, net_income_ltm=350),
            dict(peer_ticker="B", market_cap_mm=8000,  net_debt_mm=-200,
                 revenue_ltm=2500, ebitda_ltm=500, ebit_ltm=400,
                 fcf_ltm=250, net_income_ltm=280),
            dict(peer_ticker="C", market_cap_mm=12000, net_debt_mm=1000,
                 revenue_ltm=4000, ebitda_ltm=800, ebit_ltm=650,
                 fcf_ltm=400, net_income_ltm=450),
        ]
        return [compute_peer_multiples(**p) for p in raw]

    def test_n_counts(self, three_peers):
        stats = compute_peer_set_stats(three_peers)
        assert stats["ev_ebitda_ltm"]["n"] == 3

    def test_median_between_p25_p75(self, three_peers):
        stats = compute_peer_set_stats(three_peers)
        s = stats["ev_ebitda_ltm"]
        assert s["p25"] <= s["median"] <= s["p75"]

    def test_empty_peers(self):
        stats = compute_peer_set_stats([])
        assert stats["ev_ebitda_ltm"]["n"] == 0
        assert stats["ev_ebitda_ltm"]["median"] is None

    def test_excludes_none_multiples(self, three_peers):
        # Add a peer with zero ebitda
        bad = compute_peer_multiples(
            peer_ticker="D", market_cap_mm=5000, net_debt_mm=0,
            revenue_ltm=1000, ebitda_ltm=0, ebit_ltm=100,
            fcf_ltm=80, net_income_ltm=70,
        )
        stats = compute_peer_set_stats(three_peers + [bad])
        assert stats["ev_ebitda_ltm"]["n"] == 3   # "D" excluded (zero ebitda)


# ─────────────────────────────────────────────────────────────────────────────
# apply_comps_to_subject
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyCompsToSubject:
    @pytest.fixture
    def stats(self):
        raw = [
            dict(peer_ticker="A", market_cap_mm=10000, net_debt_mm=500,
                 revenue_ltm=3000, ebitda_ltm=600, ebit_ltm=450,
                 fcf_ltm=300, net_income_ltm=350),
            dict(peer_ticker="B", market_cap_mm=8000, net_debt_mm=0,
                 revenue_ltm=2500, ebitda_ltm=500, ebit_ltm=380,
                 fcf_ltm=260, net_income_ltm=290),
        ]
        return compute_peer_set_stats([compute_peer_multiples(**p) for p in raw])

    def test_has_ev_range(self, stats):
        res = apply_comps_to_subject(stats, 3500, 700, 550, 350, 300)
        assert "comps_ev_low_mm"  in res
        assert "comps_ev_high_mm" in res

    def test_high_gt_low(self, stats):
        res = apply_comps_to_subject(stats, 3500, 700, 550, 350, 300)
        assert res["comps_ev_high_mm"] >= res["comps_ev_low_mm"]

    def test_zero_subject_ebitda_excluded(self, stats):
        res = apply_comps_to_subject(stats, 3500, 0, 0, 0, 0)
        # Only ev_from_revenue expected (ebitda=0 excluded)
        assert "implied_ev_from_ev_ebitda_ltm" not in res


# ─────────────────────────────────────────────────────────────────────────────
# build_football_field
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildFootballField:
    def test_returns_rows(self):
        rows = build_football_field(80000, 120000, 75000, 115000)
        assert len(rows) >= 2

    def test_includes_transactions_row(self):
        rows = build_football_field(80000, 120000, 75000, 115000,
                                     transactions_ev_low=65000, transactions_ev_high=130000)
        methods = [r["method"] for r in rows]
        assert any("Transaction" in m for m in methods)

    def test_includes_current_price_row(self):
        rows = build_football_field(80000, 120000, 75000, 115000,
                                     net_debt=5000, shares_mm=1500, current_price=60.0)
        methods = [r["method"] for r in rows]
        assert any("Current" in m for m in methods)

    def test_price_derived_from_ev(self):
        rows = build_football_field(150000, 150000, 150000, 150000,
                                     net_debt=0, shares_mm=1500)
        dcf_row = next(r for r in rows if "DCF" in r["method"])
        expected_price = 150000 / 1500
        assert abs(dcf_row["price_low"] - expected_price) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# apply_manual_proforma_adjustments
# ─────────────────────────────────────────────────────────────────────────────

class TestProformaAdjustments:
    def test_adjustment_applied(self):
        peers = [{"ticker": "AAPL", "revenue_ltm": 400000, "ebitda_ltm": 120000}]
        adj = {"AAPL": {"revenue_ltm_adjustment_mm": -10000, "note": "Divested segment"}}
        result = apply_manual_proforma_adjustments(peers, adj)
        assert result[0]["revenue_ltm"] == 390000

    def test_no_adjustment_unchanged(self):
        peers = [{"ticker": "MSFT", "revenue_ltm": 200000}]
        result = apply_manual_proforma_adjustments(peers, {})
        assert result[0]["revenue_ltm"] == 200000

    def test_proforma_note_added(self):
        peers = [{"ticker": "GOOG", "ebitda_ltm": 80000}]
        adj = {"GOOG": {"ebitda_ltm_adjustment_mm": -5000, "note": "One-time"}}
        result = apply_manual_proforma_adjustments(peers, adj)
        assert "proforma_notes" in result[0]


# ─────────────────────────────────────────────────────────────────────────────
# check_peer_proforma_events
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckPeerProformaEvents:
    def test_no_data_returns_empty(self):
        flagged = check_peer_proforma_events(["AAPL", "MSFT"], recent_8k_data=None)
        assert flagged == {}

    def test_merger_keyword_flagged(self):
        data = {"AAPL": [{"title": "Merger Agreement Signed", "description": ""}]}
        flagged = check_peer_proforma_events(["AAPL"], data)
        assert "AAPL" in flagged

    def test_routine_8k_not_flagged(self):
        data = {"MSFT": [{"title": "Quarterly Earnings Release", "description": ""}]}
        flagged = check_peer_proforma_events(["MSFT"], data)
        assert "MSFT" not in flagged


# ─────────────────────────────────────────────────────────────────────────────
# compute_transaction_multiples
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionMultiples:
    @pytest.fixture
    def three_deals(self):
        return [
            {"target": "A", "ev_mm": 12000, "ebitda_mm": 600, "revenue_mm": 3000,
             "control_premium_pct": 0.25},
            {"target": "B", "ev_mm": 8000,  "ebitda_mm": 500, "revenue_mm": 2000,
             "control_premium_pct": 0.20},
            {"target": "C", "ev_mm": 15000, "ebitda_mm": 800, "revenue_mm": 4000,
             "control_premium_pct": 0.30},
        ]

    def test_deal_count(self, three_deals):
        mults = compute_transaction_multiples(three_deals)
        assert mults["deal_count"] == 3

    def test_ev_ebitda_n(self, three_deals):
        mults = compute_transaction_multiples(three_deals)
        assert mults["ev_ebitda"]["n"] == 3

    def test_median_in_range(self, three_deals):
        mults = compute_transaction_multiples(three_deals)
        med = mults["ev_ebitda"]["median"]
        assert 13 < med < 22

    def test_zero_ebitda_excluded(self):
        deals = [
            {"ev_mm": 5000, "ebitda_mm": 0,   "revenue_mm": 1000},
            {"ev_mm": 8000, "ebitda_mm": 400,  "revenue_mm": 2000},
        ]
        mults = compute_transaction_multiples(deals)
        assert mults["ev_ebitda"]["n"] == 1

    def test_empty_deals(self):
        mults = compute_transaction_multiples([])
        assert mults["deal_count"] == 0
        assert mults["ev_ebitda"]["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# compute_transaction_comps_result
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionCompsResult:
    @pytest.fixture
    def mults(self):
        deals = [
            {"ev_mm": 12000, "ebitda_mm": 600, "revenue_mm": 3000},
            {"ev_mm": 8000,  "ebitda_mm": 500, "revenue_mm": 2000},
            {"ev_mm": 15000, "ebitda_mm": 800, "revenue_mm": 4000},
        ]
        return compute_transaction_multiples(deals)

    def test_ev_from_ebitda(self, mults):
        res = compute_transaction_comps_result(700, 3500, mults)
        assert "ev_from_ebitda" in res
        assert res["ev_from_ebitda"]["mid"] > 0

    def test_blended_range(self, mults):
        res = compute_transaction_comps_result(700, 3500, mults)
        assert "blended_ev_range" in res
        assert res["blended_ev_range"]["high"] >= res["blended_ev_range"]["low"]

    def test_zero_ebitda_no_ev_from_ebitda(self, mults):
        res = compute_transaction_comps_result(0, 3500, mults)
        assert "ev_from_ebitda" not in res


# ─────────────────────────────────────────────────────────────────────────────
# compute_net_debt (bridge)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeNetDebt:
    def test_basic(self):
        bs = {
            "short_term_debt": 500,
            "long_term_debt":  5000,
            "capitalLeaseObligations": 200,
            "preferred_stock": 0,
            "nci":             0,
            "cash":            3000,
            "shortTermInvestments": 500,
        }
        nd = compute_net_debt(bs)
        # 500+5000+200 − 3000 − 500 = 2200
        assert abs(nd - 2200) < 0.01

    def test_net_cash_position(self):
        bs = {"short_term_debt": 0, "long_term_debt": 1000, "cash": 5000}
        nd = compute_net_debt(bs)
        assert nd < 0   # net cash

    def test_empty_bs(self):
        nd = compute_net_debt({})
        assert nd == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Working capital helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkingCapital:
    def test_dso(self):
        # DSO = AR / Revenue × 365
        dso = compute_dso(accounts_receivable=1000, revenue=10000)
        assert abs(dso - 36.5) < 0.01

    def test_dio(self):
        # DIO = Inventory / COGS × 365
        dio = compute_dio(inventory=500, cogs=5000)
        assert abs(dio - 36.5) < 0.01

    def test_dpo(self):
        # DPO = AP / COGS × 365
        dpo = compute_dpo(accounts_payable=300, cogs=5000)
        assert abs(dpo - 21.9) < 0.1

    def test_cwc_days(self):
        cwc = compute_cwc_days(dso=40, dio=30, dpo=25)
        assert cwc == 45   # 40 + 30 − 25

    def test_nowc_from_bs(self):
        bs = {"accounts_receivable": 1000, "inventory": 500, "accounts_payable": 300}
        nowc = compute_nowc_from_bs(bs)
        assert nowc == 1200   # 1000 + 500 − 300

    def test_delta_nowc(self):
        delta = compute_delta_nowc(nowc_current=1500, nowc_prior=1200)
        assert delta == 300   # increase = cash outflow


# ─────────────────────────────────────────────────────────────────────────────
# Dilution
# ─────────────────────────────────────────────────────────────────────────────

class TestDilution:
    def test_tsm_no_options(self):
        shares = treasury_stock_method(
            basic_shares_mm=1000, options_outstanding_mm=0,
            options_avg_strike=0, current_price=100,
        )
        assert shares == 1000

    def test_tsm_in_the_money(self):
        # 10mm options with strike 50, price 100
        # proceeds = 10 × 50 = 500mm; buyback = 500/100 = 5mm; net dilution = 5mm
        shares = treasury_stock_method(
            basic_shares_mm=1000, options_outstanding_mm=10,
            options_avg_strike=50, current_price=100,
        )
        assert abs(shares - 1005) < 0.01

    def test_tsm_out_of_money(self):
        # options above current price → no dilution
        shares = treasury_stock_method(
            basic_shares_mm=1000, options_outstanding_mm=10,
            options_avg_strike=150, current_price=100,
        )
        assert shares == 1000

    def test_rsu_dilution(self):
        # 10mm unvested RSUs, 40% withheld for tax → 6mm net shares
        shares = add_rsu_dilution(
            shares_mm=1000, unvested_rsus_mm=10, assumed_tax_withhold_pct=0.40
        )
        assert abs(shares - 1006) < 0.01

    def test_fully_diluted_dict_keys(self):
        d = compute_fully_diluted_shares(
            basic_shares_mm=1000,
            options_outstanding_mm=10,
            options_avg_strike=50,
            current_price=100,
            unvested_rsus_mm=5,
        )
        assert "fully_diluted_mm" in d
        assert d["fully_diluted_mm"] > 1000

    def test_price_per_share(self):
        p = compute_price_per_share(equity_value_mm=150_000, fully_diluted_shares_mm=1500)
        assert abs(p - 100.0) < 0.001

    def test_price_per_share_zero_shares(self):
        p = compute_price_per_share(equity_value_mm=100_000, fully_diluted_shares_mm=0)
        assert p == 0.0
