from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from webapp.data.samples import REGISTRY


def test_compliance_pages_render_and_explain_non_advice_status():
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")

    with webapp_module.app.test_client() as client:
        responses = {
            path: client.get(path)
            for path in ("/terms", "/disclosures", "/privacy")
        }

    assert all(response.status_code == 200 for response in responses.values())
    assert "not investment advice" in responses["/terms"].get_data(as_text=True).lower()
    assert "Forecast Risk" in responses["/disclosures"].get_data(as_text=True)
    assert "24 months" in responses["/privacy"].get_data(as_text=True)
    assert "DELETE /api/me" in responses["/privacy"].get_data(as_text=True)


def test_dashboard_includes_compliance_footer(monkeypatch):
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    monkeypatch.setattr(
        webapp_module,
        "_safe_dashboard_data",
        lambda ticker, **kwargs: copy.deepcopy(REGISTRY["NKE"]),
    )

    with webapp_module.app.test_client() as client:
        response = client.get("/dashboard/NKE")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "not investment advice" in html.lower()
    assert 'href="/terms"' in html
    assert 'href="/disclosures"' in html
    assert 'href="/privacy"' in html


def test_delete_me_purges_anonymous_workflow_data(tmp_path, monkeypatch):
    import webapp.app as webapp_module
    from auto_valuation.learning.discovery import DiscoveryStore
    from auto_valuation.learning.universe import SymbolUniverseStore

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    discovery = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)
    monkeypatch.setattr(webapp_module, "_safe_discovery_store", lambda: discovery)
    monkeypatch.setattr(webapp_module, "_persist_external_learning_state", lambda *, force=False: {"enabled": False})

    with webapp_module.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["ticker"] = "NKE"
            sess["currency"] = "USD"
        client.post("/api/watchlist", json={"ticker": "NKE", "company_name": "Nike", "exchange": "US"})
        client.post(
            "/api/manual-compare",
            json={
                "subject": {"ticker": "NKE", "company_name": "Nike", "exchange": "US"},
                "peer": {"ticker": "AAPL", "company_name": "Apple", "exchange": "US"},
            },
        )
        response = client.delete("/api/me")
        with client.session_transaction() as sess:
            session_keys = set(sess.keys())

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["deleted"]["watchlist_items"] == 1
    assert payload["deleted"]["manual_compare_events"] == 1
    assert payload["deleted"]["peer_relationships"] == 1
    assert discovery.list_watchlist(limit=5) == []
    assert discovery.list_manual_compares(subject_ticker="NKE", limit=5) == []
    assert discovery.get_peer_relationship("NKE", "AAPL") is None
    assert "ticker" not in session_keys
    assert "currency" not in session_keys


def test_privacy_cleanup_removes_only_stale_workflow_data(tmp_path, monkeypatch):
    import webapp.app as webapp_module
    from auto_valuation.learning.discovery import DiscoveryStore
    from auto_valuation.learning.universe import SymbolUniverseStore

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    universe = SymbolUniverseStore(tmp_path / "symbol-universe.db")
    discovery = DiscoveryStore(tmp_path / "discovery.db", universe_store=universe)
    monkeypatch.setattr(webapp_module, "_safe_discovery_store", lambda: discovery)
    monkeypatch.setattr(webapp_module, "_persist_external_learning_state", lambda *, force=False: {"enabled": False})

    old_cutoff = (datetime.now(timezone.utc) - timedelta(days=800)).isoformat()
    discovery.add_to_watchlist({"ticker": "OLD", "company_name": "Old Co", "exchange": "US"})
    discovery.record_search_impression("old", [{"ticker": "OLD", "company_name": "Old Co"}], selected_ticker="OLD")
    discovery.record_manual_compare(
        {"ticker": "OLD", "company_name": "Old Co", "exchange": "US"},
        [{"ticker": "AAPL", "company_name": "Apple", "exchange": "US"}],
    )
    discovery._execute("UPDATE watchlist_items SET last_touched_at = :old WHERE ticker = 'OLD'", {"old": old_cutoff})
    discovery._execute("UPDATE search_impressions SET created_at = :old WHERE selected_ticker = 'OLD'", {"old": old_cutoff})
    discovery._execute("UPDATE manual_compare_events SET created_at = :old WHERE subject_ticker = 'OLD'", {"old": old_cutoff})
    discovery._execute("UPDATE peer_relationships SET last_seen_at = :old WHERE subject_ticker = 'OLD'", {"old": old_cutoff})

    discovery.add_to_watchlist({"ticker": "NEW", "company_name": "New Co", "exchange": "US"})
    discovery.record_search_impression("new", [{"ticker": "NEW", "company_name": "New Co"}], selected_ticker="NEW")
    discovery.record_manual_compare(
        {"ticker": "NEW", "company_name": "New Co", "exchange": "US"},
        [{"ticker": "MSFT", "company_name": "Microsoft", "exchange": "US"}],
    )

    with webapp_module.app.test_client() as client:
        response = client.post("/api/internal/privacy/cleanup")

    payload = response.get_json()
    remaining_watchlist = {item["ticker"] for item in discovery.list_watchlist(limit=10)}
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["retention_days"] == 730
    assert payload["deleted"]["watchlist_items"] == 1
    assert payload["deleted"]["search_impressions"] == 1
    assert payload["deleted"]["manual_compare_events"] == 1
    assert payload["deleted"]["peer_relationships"] == 1
    assert remaining_watchlist == {"NEW"}
    assert discovery.list_manual_compares(subject_ticker="OLD", limit=5) == []
    assert discovery.list_manual_compares(subject_ticker="NEW", limit=5)[0]["ticker"] == "MSFT"
    assert discovery.get_peer_relationship("NEW", "MSFT") is not None


def test_eu_suitability_gate_redirects_first_page_visit():
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")

    with webapp_module.app.test_client() as client:
        dashboard = client.get("/dashboard/NKE", headers={"CF-IPCountry": "DE"})
        privacy = client.get("/privacy", headers={"CF-IPCountry": "DE"})

    assert dashboard.status_code == 302
    assert "/suitability?next=/dashboard/NKE" in dashboard.headers["Location"]
    assert privacy.status_code == 200


def test_eu_dashboard_api_requires_suitability_before_data_access():
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")

    with webapp_module.app.test_client() as client:
        response = client.get("/api/dashboard/NKE", headers={"X-Vercel-IP-Country": "FR"})

    assert response.status_code == 403
    assert response.get_json()["reason"] == "suitability_required"


def test_suitability_form_persists_session_and_allows_eu_dashboard(monkeypatch):
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    monkeypatch.setattr(
        webapp_module,
        "_safe_dashboard_data",
        lambda ticker, **kwargs: copy.deepcopy(REGISTRY["NKE"]),
    )

    with webapp_module.app.test_client() as client:
        response = client.post(
            "/suitability",
            headers={"CF-IPCountry": "DE"},
            data={
                "next": "/dashboard/NKE",
                "experience": "intermediate",
                "risk_tolerance": "medium",
                "horizon": "long",
                "acknowledge_non_advice": "on",
            },
        )
        with client.session_transaction() as sess:
            complete = sess.get("mifid_suitability_complete")
            profile = dict(sess.get("mifid_suitability_profile") or {})
        dashboard = client.get("/dashboard/NKE", headers={"CF-IPCountry": "DE"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/NKE")
    assert complete is True
    assert profile["country_code"] == "DE"
    assert profile["risk_tolerance"] == "medium"
    assert dashboard.status_code == 200