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

# Load .env file if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session,
)

# Allow imports from parent directory (auto_valuation package)
sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.data.samples import get_dashboard_data, REGISTRY, SUPPORTED_TICKERS
from webapp.data.ticker_search import available_exchanges, resolve_search_input, search_tickers


logger = logging.getLogger(__name__)

app = Flask(__name__)
_VERCEL_LEARNING_FORCE_HYDRATED = False

# Make enumerate available in Jinja2 templates
app.jinja_env.globals.update(enumerate=enumerate)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(32))

# ─── Security state ──────────────────────────────────────────────────────────
# Keyed by remote address; values are lists of monotonic timestamps.
_RATE_LIMIT_BUCKETS: dict = {}

# Provide a no-op csrf_token() function in templates unless a real CSRF
# library is installed.  Tests that need CSRF can override this.
app.jinja_env.globals.setdefault("csrf_token", lambda: "")

_HISTORICAL_YEARS_UNSET = object()


def _normalize_historical_years_choice(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"", "ipo", "since_ipo", "all", "max"}:
        return "since_ipo"
    if raw in {"10", "15"}:
        return raw
    return "since_ipo"


def _historical_years_limit(value: str | None) -> int | None:
    choice = _normalize_historical_years_choice(value)
    return None if choice == "since_ipo" else int(choice)


def _current_historical_years_limit() -> int | None:
    try:
        return _historical_years_limit(session.get("years", "since_ipo"))
    except RuntimeError:
        return None


def _maybe_start_background_runner() -> None:
    if os.environ.get("VERCEL") or "pytest" in sys.modules:
        return
    try:
        from auto_valuation.learning.background_runner import start_learning_background_runner
        start_learning_background_runner()
    except Exception as exc:
        logger.debug("Could not start background runner: %s", exc)

_maybe_start_background_runner()


def _prewarm_caches() -> None:
    """Pre-load expensive disk caches in a background thread at startup.

    Runs independently of the background runner.  Errors are silenced so
    that a warm-up failure never prevents the server from starting.
    """
    import time as _t
    _t.sleep(0.5)  # brief pause so Flask finishes binding the port first
    try:
        from webapp.data.knowledge_model import _cached_ledger_records  # type: ignore[attr-defined]
        from auto_valuation.learning.historical_replay import get_all_observations
        _cached_ledger_records()            # ~1300ms — DB read, 22k records
        get_all_observations()              # ~835ms  — loads obs_cache.pkl
    except Exception:
        pass
    try:
        from webapp.data.peer_lists import _load_cached_peer_profiles
        _load_cached_peer_profiles()        # ~18ms   — loads _peer_profiles.pkl
    except Exception:
        pass
    try:
        from auto_valuation.learning.calibration_priority import build_calibration_priority_index
        build_calibration_priority_index()  # ~18ms   — loads _calib_priority.pkl
    except Exception:
        pass
    try:
        from auto_valuation.learning.universe import SymbolUniverseStore
        SymbolUniverseStore().summary()     # ~100ms  — with above caches warm
    except Exception:
        pass
    try:
        from webapp.data.ticker_search import seedable_tickers
        seedable_tickers(common_stock_only=True)  # ~775ms — loads search index
    except Exception:
        pass


if not os.environ.get("VERCEL") and "pytest" not in sys.modules:
    import threading as _threading
    _threading.Thread(target=_prewarm_caches, daemon=True, name="cache-prewarm").start()


def _sync_external_learning_state(*, force: bool = False) -> dict[str, object]:
    try:
        from auto_valuation.learning.production_sync import hydrate_external_learning_state

        return dict(hydrate_external_learning_state(force=force) or {})
    except Exception as exc:
        logger.debug("External learning hydrate skipped: %s", exc)
        return {"enabled": False, "reason": str(exc)}


def _persist_external_learning_state(*, force: bool = False) -> dict[str, object]:
    try:
        from auto_valuation.learning.production_sync import persist_external_learning_state

        return dict(persist_external_learning_state(force=force) or {})
    except Exception as exc:
        logger.debug("External learning persist skipped: %s", exc)
        return {"enabled": False, "reason": str(exc)}


def _cron_authorized() -> tuple[bool, str | None]:
    expected = str(os.environ.get("CRON_SECRET") or os.environ.get("LEARNING_CRON_SECRET") or "").strip()
    auth_header = str(request.headers.get("Authorization") or "").strip()
    if not expected:
        if os.environ.get("VERCEL"):
            return False, "cron-secret-missing"
        return True, None
    if auth_header == f"Bearer {expected}":
        return True, None
    return False, "unauthorized"


def _safe_discovery_store():
    _sync_external_learning_state()
    try:
        from auto_valuation.learning.discovery import DiscoveryStore

        return DiscoveryStore()
    except Exception:
        return None


def _seed_watchlist(limit: int = 30) -> list[dict]:
    try:
        from auto_valuation.learning.deployment_seed import watchlist_items

        return watchlist_items(limit=limit)
    except Exception:
        return []


def _seed_manual_compares(subject_ticker: str, limit: int = 8) -> list[dict]:
    try:
        from auto_valuation.learning.deployment_seed import manual_compare_items

        return manual_compare_items(subject_ticker=subject_ticker, limit=limit)
    except Exception:
        return []


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


def _safe_dashboard_data(
    ticker: str,
    overrides: dict | None = None,
    *,
    mutate_learning: bool = True,
    historical_years: int | None | object = _HISTORICAL_YEARS_UNSET,
) -> dict:
    global _VERCEL_LEARNING_FORCE_HYDRATED
    if historical_years is _HISTORICAL_YEARS_UNSET:
        historical_years = _current_historical_years_limit()
    # Always hydrate from remote on Vercel (serverless = fresh empty DBs each request).
    # On local dev, only hydrate when mutating to avoid redundant round-trips.
    if mutate_learning or os.environ.get("VERCEL"):
        force_hydrate = bool(os.environ.get("VERCEL") and not _VERCEL_LEARNING_FORCE_HYDRATED)
        sync_result = _sync_external_learning_state(force=force_hydrate)
        if force_hydrate and sync_result.get("enabled") and not sync_result.get("errors"):
            _VERCEL_LEARNING_FORCE_HYDRATED = True
    try:
        kwargs = {"mutate_learning": mutate_learning, "historical_years": historical_years}
        if overrides is not None:
            kwargs["overrides"] = overrides
        try:
            data = get_dashboard_data(ticker, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            kwargs.pop("historical_years", None)
            try:
                data = get_dashboard_data(ticker, **kwargs)
            except TypeError as legacy_exc:
                if "unexpected keyword argument" not in str(legacy_exc):
                    raise
                data = get_dashboard_data(ticker)
        if mutate_learning:
            _persist_external_learning_state()
        return data
    except Exception as exc:
        logger.exception("Dashboard data failed for %s", ticker)
        return _fallback_dashboard_data(ticker, reason=str(exc))

# ─── Landing page ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", supported=SUPPORTED_TICKERS, exchanges=available_exchanges())


# ─── Valuation entry point ───────────────────────────────────────────────────

@app.route("/valuate", methods=["POST"])
def valuate():
    typed_ticker = request.form.get("ticker", "NKE").strip()
    selected_raw = request.form.get("selected_ticker", "").strip()
    selected_ticker = selected_raw.upper()
    exchange = request.form.get("exchange", "auto")
    currency = request.form.get("currency", "USD")
    years = _normalize_historical_years_choice(request.form.get("years", "since_ipo"))

    typed_upper = typed_ticker.upper().strip()
    resolved_ticker = resolve_search_input(typed_ticker, exchange=exchange)
    if selected_ticker and selected_ticker != typed_upper:
        resolved_ticker = selected_ticker
    elif not resolved_ticker and selected_ticker:
        resolved_ticker = selected_ticker
    ticker = (resolved_ticker or typed_ticker or "NKE").upper().strip()

    # Normalise the ticker to an EODHD-compatible code so the exchange hint
    # is embedded in the ticker param (e.g. "BHP" + exchange="LSE" → "BHP.LSE").
    from webapp.data.eodhd_client import normalize_requested_ticker
    ticker = normalize_requested_ticker(ticker, exchange=exchange)

    session["ticker"]   = ticker
    session["exchange"] = exchange
    session["currency"] = currency
    session["years"]    = years
    return redirect(url_for("loading", ticker=ticker))


# ─── Loading / progress screen ───────────────────────────────────────────────

@app.route("/loading/<ticker>")
def loading(ticker):
    return render_template("loading.html", ticker=ticker.upper())


# ─── EU/MiFID suitability gate ──────────────────────────────────────────────

_EU_EEA_COUNTRY_CODES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
    "IS", "LI", "NO",  # EEA non-EU
}


def _visitor_country() -> str:
    country = (
        request.headers.get("CF-IPCountry")
        or request.headers.get("X-Vercel-IP-Country")
        or ""
    ).strip().upper()
    return country


def _requires_eu_suitability() -> bool:
    """Return True when the visitor is from EU/EEA and hasn't completed suitability."""
    return (
        _visitor_country() in _EU_EEA_COUNTRY_CODES
        and not session.get("mifid_suitability_complete")
    )


# ─── Main dashboard ──────────────────────────────────────────────────────────

@app.route("/dashboard/<ticker>")
def dashboard(ticker):
    ticker = ticker.upper()
    if _requires_eu_suitability():
        return redirect(url_for("suitability", next=f"/dashboard/{ticker}"))
    data   = _safe_dashboard_data(ticker, mutate_learning=False)
    return render_template("dashboard.html", data=data)


# ─── API: get dashboard data as JSON ─────────────────────────────────────────

@app.route("/api/dashboard/<ticker>")
def api_dashboard(ticker):
    import time as _time
    if _requires_eu_suitability():
        return jsonify({"error": "suitability_required", "reason": "suitability_required"}), 403
    if app.config.get("RATELIMIT_ENABLED"):
        key = request.remote_addr or "unknown"
        now = _time.monotonic()
        bucket = _RATE_LIMIT_BUCKETS.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= 10:
            return jsonify({"error": "rate_limited", "reason": "rate_limited"}), 429
        bucket.append(now)
    data = _safe_dashboard_data(ticker.upper(), mutate_learning=False)
    return jsonify(_dashboard_api_payload(data))


# ─── API: trigger learning snapshot (write-mode, fire-and-forget from loading) ─

@app.route("/api/snapshot/<ticker>", methods=["POST"])
def api_snapshot(ticker):
    """Run a write-mode dashboard fetch for the given ticker.

    Triggers bootstrap, backfill, and learning persistence that are
    suppressed on ordinary read-only dashboard/API routes.  Called once per
    ticker from the loading page as a fire-and-forget request so that newly
    visited tickers accumulate learning evidence without slowing down the UI.
    """
    try:
        _safe_dashboard_data(ticker.upper(), mutate_learning=True)
    except Exception as exc:
        logger.debug("Snapshot trigger failed for %s: %s", ticker, exc)
    return jsonify({"ok": True})


@app.route("/api/ticker-search")
def api_ticker_search():
    query = request.args.get("q", "")
    exchange = request.args.get("exchange", "auto")
    try:
        limit = max(1, min(int(request.args.get("limit", "12")), 25))
    except ValueError:
        limit = 12
    results = search_tickers(query, limit=limit, exchange=exchange)
    return jsonify({"results": results})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    store = _safe_discovery_store()
    if request.method == "GET" and store is None:
        items = _seed_watchlist(limit=30)
        return jsonify({"items": items, "ok": True, "seeded": bool(items)})
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503

    if request.method == "GET":
        return jsonify({"items": store.list_watchlist(limit=30), "ok": True})

    payload = request.get_json(force=True) or {}
    item = store.add_to_watchlist(payload)
    if item is None:
        return jsonify({"items": store.list_watchlist(limit=30), "ok": False, "reason": "invalid-symbol"}), 400
    _persist_external_learning_state()
    return jsonify({"items": store.list_watchlist(limit=30), "item": item, "ok": True})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def api_watchlist_delete(ticker):
    store = _safe_discovery_store()
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503
    removed = store.remove_from_watchlist(ticker)
    _persist_external_learning_state()
    return jsonify({"items": store.list_watchlist(limit=30), "ok": removed})


@app.route("/api/manual-compare", methods=["GET", "POST"])
def api_manual_compare():
    store = _safe_discovery_store()
    if request.method == "GET" and store is None:
        subject = request.args.get("subject", "")
        items = _seed_manual_compares(subject_ticker=subject, limit=8)
        return jsonify({"items": items, "ok": True, "seeded": bool(items)})
    if store is None:
        return jsonify({"items": [], "ok": False, "reason": "discovery-unavailable"}), 503

    if request.method == "GET":
        subject = request.args.get("subject", "")
        return jsonify({"items": store.list_manual_compares(subject_ticker=subject, limit=8), "ok": True})

    payload = request.get_json(force=True) or {}
    subject = payload.get("subject") or {}
    peers = payload.get("peers") or ([] if payload.get("peer") is None else [payload.get("peer")])
    result = store.record_manual_compare(subject, peers, event_id=payload.get("event_id"))
    _persist_external_learning_state()
    return jsonify({"ok": True, **result})


@app.route("/api/internal/learning/status", methods=["GET"])
def api_internal_learning_status():
    hydrate_force = str(request.args.get("hydrate") or request.args.get("force") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    payload: dict = {}
    # On Vercel (serverless) hydrate from external storage so counts reflect real data.
    if os.environ.get("VERCEL"):
        payload["sync_in"] = _sync_external_learning_state(force=hydrate_force)
    try:
        from auto_valuation.learning.background_runner import (
            read_background_runner_state,
            get_daily_stats,
        )
        payload["runner"] = read_background_runner_state()
        payload["daily"] = get_daily_stats()
    except Exception as exc:
        payload["runner"] = {"error": str(exc)}

    try:
        import sqlite3
        from auto_valuation.learning._layered_calibrator import CalibrationStore
        cs = CalibrationStore()
        conn = sqlite3.connect(str(cs.db_path))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM calibration_priors")
        prior_count = cur.fetchone()[0]
        cur.execute(
            "SELECT sector || '/' || industry, cohort_size FROM calibration_priors "
            "WHERE cohort_size > 0 ORDER BY cohort_size DESC LIMIT 8"
        )
        cohort_sizes = dict(cur.fetchall())
        conn.close()
        payload["calibration"] = {"prior_count": prior_count, "cohort_sizes_sample": cohort_sizes}
    except Exception as exc:
        payload["calibration"] = {"error": str(exc)}

    try:
        import sqlite3 as _sqlite3
        from auto_valuation.learning.ledger import LedgerReader
        _lr = LedgerReader()
        _lconn = _sqlite3.connect(str(_lr.db_path))
        _lcur = _lconn.cursor()
        ledger_counts: dict = {}
        for _t in ("prediction_records", "realized_outcomes", "postmortem_records", "maintenance_runs"):
            try:
                _lcur.execute(f"SELECT COUNT(*) FROM {_t}")
                ledger_counts[_t] = _lcur.fetchone()[0]
            except Exception:
                pass
        _lconn.close()
        payload["ledger"] = ledger_counts
    except Exception as exc:
        payload["ledger"] = {"error": str(exc)}

    try:
        import torch
        if torch.cuda.is_available():
            gpu = f"CUDA ({torch.cuda.get_device_name(0)})"
        elif torch.backends.mps.is_available():
            gpu = "MPS (Apple Silicon)"
        else:
            gpu = "CPU only"
    except Exception:
        gpu = "torch not available"
    payload["gpu"] = gpu
    payload["cpu_cores"] = os.cpu_count()

    try:
        from auto_valuation.learning.production_sync import get_sync_stats
        payload["sync"] = get_sync_stats()
    except Exception as exc:
        payload["sync"] = {"error": str(exc)}

    try:
        from auto_valuation.learning.deployment_seed import seed_summary
        payload["deployment_seed"] = seed_summary()
    except Exception as exc:
        payload["deployment_seed"] = {"error": str(exc)}

    try:
        from auto_valuation.learning.performance_report import build_learning_performance_report

        report = build_learning_performance_report(chunk_size=1000)
        payload["performance"] = {
            "latest_chunk": report.get("cohorts", {}).get("latest_chunk", {}),
            "latest_stable_only": report.get("cohorts", {}).get("latest_stable_only", {}),
            "calibration": report.get("calibration", {}),
            "throughput": report.get("throughput", {}),
            "targets": report.get("targets", {}),
        }
    except Exception as exc:
        payload["performance"] = {"error": str(exc)}

    try:
        from auto_valuation.learning.scenario_calibrator import get_scenario_calibration_summary
        payload["scenario_calibration"] = get_scenario_calibration_summary()
    except Exception as exc:
        payload["scenario_calibration"] = {"error": str(exc)}

    return jsonify({"ok": True, **payload})


@app.route("/api/internal/learning/cron", methods=["GET", "POST"])
def api_internal_learning_cron():
    authorized, reason = _cron_authorized()
    if not authorized:
        status = 503 if reason == "cron-secret-missing" else 401
        return jsonify({"ok": False, "reason": reason}), status

    sync_in = _sync_external_learning_state(force=True)
    from auto_valuation.learning.background_runner import run_background_learning_cycle

    cycle = run_background_learning_cycle()
    sync_out = _persist_external_learning_state(force=True)
    return jsonify({"ok": True, "sync_in": sync_in, "cycle": cycle, "sync_out": sync_out})


# ─── API: recompute with overrides ───────────────────────────────────────────

@app.route("/api/recompute", methods=["POST"])
def api_recompute():
    if app.config.get("CSRF_ENABLED"):
        body = request.get_json(force=True, silent=True) or {}
        token = request.headers.get("X-CSRF-Token") or body.get("csrf_token")
        if not token:
            return jsonify({"error": "csrf_required", "reason": "csrf_required"}), 400
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

    data = _safe_dashboard_data(ticker, overrides=overrides)

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
    data = _safe_dashboard_data(ticker.upper(), mutate_learning=False)
    return jsonify(data.get("confidence_breakdown") or {"score": data.get("confidence_score", 50)})


# ─── API: reverse DCF ─────────────────────────────────────────────────────────

@app.route("/api/reverse-dcf/<ticker>")
def api_reverse_dcf(ticker):
    data = _safe_dashboard_data(ticker.upper())
    return jsonify(data.get("reverse_dcf") or {"error": "reverse_dcf not available"})


# ─── API: investment memo ─────────────────────────────────────────────────────

@app.route("/api/memo/<ticker>")
def api_memo(ticker):
    data = _safe_dashboard_data(ticker.upper())
    return jsonify(data.get("investment_memo") or {"error": "investment_memo not available"})


# ─── API: market expectations ─────────────────────────────────────────────────

@app.route("/api/market-expectations/<ticker>")
def api_market_expectations(ticker):
    data = _safe_dashboard_data(ticker.upper())
    return jsonify(data.get("market_expectations") or {"error": "market_expectations not available"})


# ─── API: financial scores (Altman Z + Piotroski F) ───────────────────────────

@app.route("/api/financial-scores/<ticker>")
def api_financial_scores(ticker):
    data = _safe_dashboard_data(ticker.upper())
    return jsonify(data.get("financial_scores") or {"error": "financial_scores not available"})


# ─── API: football field chart data ───────────────────────────────────────────

@app.route("/api/football-field/<ticker>")
def api_football_field(ticker):
    data = _safe_dashboard_data(ticker.upper())
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
    ticker = ticker.upper().strip()
    data   = _safe_dashboard_data(ticker)
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


# ─── Compliance pages ────────────────────────────────────────────────────────

@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/disclosures")
def disclosures():
    return render_template("disclosures.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ─── EU suitability form ─────────────────────────────────────────────────────

@app.route("/suitability", methods=["GET", "POST"])
def suitability():
    if request.method == "POST":
        next_url = request.form.get("next") or "/"
        country_code = _visitor_country() or "XX"
        session["mifid_suitability_complete"] = True
        session["mifid_suitability_profile"] = {
            "country_code": country_code,
            "experience": request.form.get("experience", ""),
            "risk_tolerance": request.form.get("risk_tolerance", ""),
            "horizon": request.form.get("horizon", ""),
            "acknowledged_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        }
        return redirect(next_url)
    next_url = request.args.get("next", "/")
    return render_template("suitability.html", next_url=next_url)


# ─── GDPR / right-to-erasure ─────────────────────────────────────────────────

@app.route("/api/me", methods=["DELETE"])
def api_delete_me():
    store = _safe_discovery_store()
    deleted = {"watchlist_items": 0, "manual_compare_events": 0, "peer_relationships": 0}
    if store is not None:
        try:
            deleted = store.purge_all_workflow_data()
        except Exception as exc:
            logger.warning("purge_all_workflow_data failed: %s", exc)
    session.clear()
    _persist_external_learning_state()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/internal/privacy/cleanup", methods=["POST"])
def api_privacy_cleanup():
    store = _safe_discovery_store()
    retention_days = 730
    deleted = {
        "watchlist_items": 0,
        "search_impressions": 0,
        "manual_compare_events": 0,
        "peer_relationships": 0,
    }
    if store is not None:
        try:
            deleted = store.cleanup_stale_workflow_data(retention_days=retention_days)
        except Exception as exc:
            logger.warning("cleanup_stale_workflow_data failed: %s", exc)
    return jsonify({"ok": True, "retention_days": retention_days, "deleted": deleted})


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port, host="0.0.0.0")
