from __future__ import annotations

import pytest

from auto_valuation.assumptions.overrides import validate_overrides
from auto_valuation.utils.error import ConfigError


def test_eodhd_api_key_required_in_production(monkeypatch):
    import webapp.data.eodhd_client as eodhd_client

    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("EOD_API_KEY", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    with pytest.raises(RuntimeError, match="EODHD_API_KEY"):
        eodhd_client._api_key()


def test_eodhd_api_key_from_environment(monkeypatch):
    import webapp.data.eodhd_client as eodhd_client

    monkeypatch.setenv("EODHD_API_KEY", "test-key")
    monkeypatch.delenv("VERCEL", raising=False)

    assert eodhd_client._api_key() == "test-key"


def test_override_dos_rejects_deep_payload():
    payload = {"pension": {}}
    cursor = payload["pension"]
    for index in range(12):
        cursor["child"] = {}
        cursor = cursor["child"]

    with pytest.raises(ConfigError, match="nesting depth"):
        validate_overrides(payload, "TEST")


def test_override_dos_rejects_large_string():
    with pytest.raises(ConfigError, match="string value"):
        validate_overrides({"scenario": "x" * 1001}, "TEST")


def test_csrf_required_when_enabled(monkeypatch):
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, CSRF_ENABLED=True, RATELIMIT_ENABLED=False)

    with webapp_module.app.test_client() as client:
        response = client.post("/api/recompute", json={"ticker": "NKE", "overrides": {}})

    assert response.status_code == 400
    assert response.get_json()["reason"] == "csrf_required"


def test_rate_limit_when_enabled(monkeypatch):
    import webapp.app as webapp_module

    webapp_module.app.config.update(TESTING=True, CSRF_ENABLED=False, RATELIMIT_ENABLED=True)
    webapp_module._RATE_LIMIT_BUCKETS.clear()
    monkeypatch.setattr(
        webapp_module,
        "_safe_dashboard_data",
        lambda ticker, **kwargs: {
            "ticker": ticker,
            "intrinsic_value": 1.0,
            "confidence_score": 50,
            "historical": {"years": [], "revenue": [], "gross_margin": [], "ebit_margin": [], "fcf": [], "roic": []},
            "knowledge_model": {},
        },
    )

    with webapp_module.app.test_client() as client:
        statuses = [client.get("/api/dashboard/NKE").status_code for _ in range(11)]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
