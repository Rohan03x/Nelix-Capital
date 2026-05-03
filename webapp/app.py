"""
webapp/app.py — Flask application for the DCF Valuation Dashboard.
"""

from __future__ import annotations
import copy
import logging
import os
import sys
import json
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session,
)

# Allow imports from parent directory (auto_valuation package)
sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.data.samples import get_dashboard_data, REGISTRY, SUPPORTED_TICKERS
from webapp.data.ticker_search import resolve_search_input, search_tickers


logger = logging.getLogger(__name__)

app = Flask(__name__)

# Make enumerate available in Jinja2 templates
app.jinja_env.globals.update(enumerate=enumerate)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(32))


def _safe_discovery_store():
    try:
        from auto_valuation.learning.discovery import DiscoveryStore

        return DiscoveryStore()
    except Exception:
        return None


def _dashboard_api_payload(data: dict) -> dict:
    payload = dict(data)
    historical = payload.get("historical") or {}
    knowledge_model = payload.get("knowledge_model") or {}

    payload.setdefault("intrinsic_value_per_share", payload.get("intrinsic_value"))
    payload.setdefault("model_confidence_score", payload.get("confidence_score"))
    payload.setdefault("learning_explainability", knowledge_model.get("explainability"))
    payload.setdefault("knowledge_model_summary", knowledge_model.get("summary"))
    payload.setdefault("historical_years", historical.get("years", []))
    payload.setdefault("historical_revenue", historical.get("revenue", []))
    payload.setdefault("historical_gross_margin", historical.get("gross_margin", []))
    payload.setdefault("historical_ebit_margin", historical.get("ebit_margin", []))
    payload.setdefault("historical_fcf", historical.get("fcf", []))
    payload.setdefault("historical_roic", historical.get("roic", []))
    return payload


def _fallback_dashboard_data(ticker: str, reason: str | None = None) -> dict:
    fallback = copy.deepcopy(REGISTRY.get(ticker) or REGISTRY["NKE"])
    fallback["is_demo"] = True
    fallback["data_source"] = "demo-fallback"
    fallback["requested_ticker"] = ticker
    fallback["demo_note"] = (
        f"Live dashboard data is temporarily unavailable for {ticker}. "
        "Showing demo data while production recovers."
    )
    if reason:
        fallback["runtime_warning"] = reason
    return fallback


def _safe_dashboard_data(ticker: str) -> dict:
    try:
        return get_dashboard_data(ticker)
    except Exception as exc:
        logger.exception("Dashboard data failed for %s", ticker)
        return _fallback_dashboard_data(ticker, reason=str(exc))

# ─── Landing page ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", supported=SUPPORTED_TICKERS)


# ─── Valuation entry point ───────────────────────────────────────────────────

@app.route("/valuate", methods=["POST"])
def valuate():
    typed_ticker = request.form.get("ticker", "NKE").strip()
    selected_ticker = request.form.get("selected_ticker", "").strip().upper()
    exchange = request.form.get("exchange", "auto")
    currency = request.form.get("currency", "USD")
    years    = request.form.get("years", "10")

    resolved_ticker = selected_ticker or resolve_search_input(typed_ticker, exchange=exchange)
    ticker = (resolved_ticker or typed_ticker or "NKE").upper().strip()

    # Normalise the ticker to an EODHD-compatible code so the exchange hint
    # is embedded in the ticker param (e.g. "BHP" + exchange="LSE" → "BHP.LSE").
    from webapp.data.eodhd_client import normalize_requested_ticker
    ticker = normalize_requested_ticker(ticker, exchange=exchange)

    discovery_store = _safe_discovery_store()
    if discovery_store is not None:
        discovery_store.record_search_impression(typed_ticker, [], exchange=exchange, selected_ticker=ticker)

    session["ticker"]   = ticker
    session["exchange"] = exchange
    session["currency"] = currency
    session["years"]    = years
    return redirect(url_for("loading", ticker=ticker))


# ─── Loading / progress screen ───────────────────────────────────────────────

@app.route("/loading/<ticker>")
def loading(ticker):
    return render_template("loading.html", ticker=ticker.upper())


# ─── Main dashboard ──────────────────────────────────────────────────────────

@app.route("/dashboard/<ticker>")
def dashboard(ticker):
    ticker = ticker.upper()
    data   = _safe_dashboard_data(ticker)
    return render_template("dashboard.html", data=data)


# ─── API: get dashboard data as JSON ─────────────────────────────────────────

@app.route("/api/dashboard/<ticker>")
def api_dashboard(ticker):
    data = _safe_dashboard_data(ticker.upper())
    return jsonify(_dashboard_api_payload(data))


@app.route("/api/ticker-search")
def api_ticker_search():
    query = request.args.get("q", "")
    exchange = request.args.get("exchange", "auto")
    try:
        limit = max(1, min(int(request.args.get("limit", "12")), 25))
    except ValueError:
        limit = 12
    results = search_tickers(query, limit=limit, exchange=exchange)
    discovery_store = _safe_discovery_store()
    if discovery_store is not None:
        discovery_store.record_search_impression(query, results, exchange=exchange)
    return jsonify({"results": results})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    store = _safe_discovery_store()
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503

    if request.method == "GET":
        return jsonify({"items": store.list_watchlist(limit=30), "ok": True})

    payload = request.get_json(force=True) or {}
    item = store.add_to_watchlist(payload)
    if item is None:
        return jsonify({"items": store.list_watchlist(limit=30), "ok": False, "reason": "invalid-symbol"}), 400
    return jsonify({"items": store.list_watchlist(limit=30), "item": item, "ok": True})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def api_watchlist_delete(ticker):
    store = _safe_discovery_store()
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503
    removed = store.remove_from_watchlist(ticker)
    return jsonify({"items": store.list_watchlist(limit=30), "ok": removed})


@app.route("/api/manual-compare", methods=["GET", "POST"])
def api_manual_compare():
    store = _safe_discovery_store()
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503

    if request.method == "GET":
        subject = request.args.get("subject", "")
        return jsonify({"items": store.list_manual_compares(subject_ticker=subject, limit=8), "ok": True})

    payload = request.get_json(force=True) or {}
    subject = payload.get("subject") or {}
    peers = payload.get("peers") or ([] if payload.get("peer") is None else [payload.get("peer")])
    result = store.record_manual_compare(subject, peers)
    return jsonify({"ok": True, **result})


# ─── API: recompute with overrides ───────────────────────────────────────────

@app.route("/api/recompute", methods=["POST"])
def api_recompute():
    payload   = request.get_json(force=True) or {}
    ticker    = payload.get("ticker", "NKE").upper()
    overrides = payload.get("overrides", {})

    # Coerce numeric overrides
    numeric_keys = [
        "wacc", "g", "revenue_growth_near", "ebit_margin_target",
        "da_pct", "capex_pct", "sbc_pct", "tax_rate", "beta",
    ]
    for k in numeric_keys:
        if k in overrides:
            try:
                overrides[k] = float(overrides[k])
            except (ValueError, TypeError):
                overrides.pop(k, None)

    data = get_dashboard_data(ticker, overrides=overrides)

    return jsonify({
        "intrinsic_value":  data["intrinsic_value"],
        "upside_pct":       data["upside_pct"],
        "recommendation":   data["recommendation"],
        "recommendation_class": data["recommendation_class"],
        "enterprise_value": data["enterprise_value"],
        "equity_value":     data["equity_value"],
        "pv_ufcfs":         data["pv_ufcfs"],
        "pv_terminal":      data["pv_terminal"],
        "tv_pct":           data["tv_pct"],
        "wacc":             data["wacc"],
        "terminal_growth":  data["terminal_growth"],
        "sensitivity": {
            "wacc_labels":   data["sensitivity"]["wacc_labels"],
            "g_labels":      data["sensitivity"]["g_labels"],
            "iv_grid":       data["sensitivity"]["iv_grid"],
            "base_wacc_idx": data["sensitivity"]["base_wacc_idx"],
            "base_g_idx":    data["sensitivity"]["base_g_idx"],
        },
    })


# ─── API: confidence score ────────────────────────────────────────────────────

@app.route("/api/confidence/<ticker>")
def api_confidence(ticker):
    data = get_dashboard_data(ticker.upper())
    return jsonify(data.get("confidence_breakdown") or {"score": data.get("confidence_score", 50)})


# ─── API: reverse DCF ─────────────────────────────────────────────────────────

@app.route("/api/reverse-dcf/<ticker>")
def api_reverse_dcf(ticker):
    data = get_dashboard_data(ticker.upper())
    return jsonify(data.get("reverse_dcf") or {"error": "reverse_dcf not available"})


# ─── API: investment memo ─────────────────────────────────────────────────────

@app.route("/api/memo/<ticker>")
def api_memo(ticker):
    data = get_dashboard_data(ticker.upper())
    return jsonify(data.get("investment_memo") or {"error": "investment_memo not available"})


# ─── API: market expectations ─────────────────────────────────────────────────

@app.route("/api/market-expectations/<ticker>")
def api_market_expectations(ticker):
    data = get_dashboard_data(ticker.upper())
    return jsonify(data.get("market_expectations") or {"error": "market_expectations not available"})


# ─── API: financial scores (Altman Z + Piotroski F) ───────────────────────────

@app.route("/api/financial-scores/<ticker>")
def api_financial_scores(ticker):
    data = get_dashboard_data(ticker.upper())
    return jsonify(data.get("financial_scores") or {"error": "financial_scores not available"})


# ─── API: football field chart data ───────────────────────────────────────────

@app.route("/api/football-field/<ticker>")
def api_football_field(ticker):
    data = get_dashboard_data(ticker.upper())
    ff = {
        "dcf_bear":       data.get("scenarios", {}).get("bear", {}).get("iv", 0),
        "dcf_base":       data.get("intrinsic_value", 0),
        "dcf_bull":       data.get("scenarios", {}).get("bull", {}).get("iv", 0),
        "fifty_two_low":  data.get("fifty_two_week_low", 0),
        "fifty_two_high": data.get("fifty_two_week_high", 0),
        "analyst_low":    data.get("analyst_low", 0),
        "analyst_median": data.get("analyst_median", 0),
        "analyst_high":   data.get("analyst_high", 0),
        "price":          data.get("price", 0),
    }
    return jsonify(ff)


# ─── API: Excel export ───────────────────────────────────────────────────────

@app.route("/api/export/<ticker>")
def api_export(ticker):
    """Build and stream an Excel workbook for *ticker*."""
    import io as _io
    from flask import send_file
    from webapp.data.excel_export import build_excel_bytes
    from webapp.data.samples import get_dashboard_data as _gdd

    ticker = ticker.upper().strip()
    data   = _gdd(ticker)
    xlsx   = build_excel_bytes(data)
    buf    = _io.BytesIO(xlsx)
    buf.seek(0)
    fname  = f"{ticker}_DCF_{data.get('price_date', '')}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port, host="0.0.0.0")
