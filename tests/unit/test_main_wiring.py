"""Wiring tests for the assembled FastAPI application (``app/main.py``).

These exercise the real product assembly end to end via
:class:`fastapi.testclient.TestClient`, which runs the application's lifespan
(startup + shutdown). Everything is pointed at a throwaway temporary
``DATA_DIR`` and no credentials are configured, so:

* the deployment core (``/health``) still answers,
* the Admin REST API routers are mounted and serve real, seeded data,
* the masked credential read works with nothing stored, and
* the Teams webhook acknowledges a minimal activity without any network call.

Because no credentials are configured in the temp environment, the Telegram
poller stays inactive and the Teams handler short-circuits before any Bot
Connector call, so no real network I/O ever happens.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.config as config
import app.main as main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Build a TestClient over the assembled app against a temp DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AWS_DEFAULT_REGION", "cn-north-1")
    # The settings snapshot is cached; clear it so startup picks up the temp env.
    config.get_settings.cache_clear()
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        config.get_settings.cache_clear()


def test_health_ok(client):
    """The liveness probe still answers after full wiring."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_metadata(client):
    """The root metadata route is preserved."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "intelliknow-kms"


def test_spaces_route_mounted_with_seeds(client):
    """GET /spaces is mounted and returns the four seeded Intent_Spaces."""
    resp = client.get("/spaces")
    assert resp.status_code == 200
    names = {space["name"] for space in resp.json()}
    assert {"General", "HR", "Legal", "Finance"} <= names


def test_dashboard_summary_mounted(client):
    """GET /dashboard/summary is mounted and returns a 200 payload."""
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_masked_credentials_read_unconfigured(client):
    """GET masked credentials returns 200 and reports nothing configured."""
    resp = client.get("/integrations/telegram/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "telegram"
    assert body["configured"] is False
    assert body["credentials"] == {}


def test_teams_webhook_minimal_activity_returns_ok(client):
    """POST /webhooks/teams acknowledges a minimal activity without network I/O."""
    resp = client.post("/webhooks/teams", json={"type": "message", "text": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_teams_webhook_empty_body_returns_ok(client):
    """An empty/malformed Teams webhook body is still acknowledged with 200."""
    resp = client.post("/webhooks/teams", content=b"not-json")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
