"""Unit tests for the admin integration endpoints (``app/api/admin.py``).

Uses FastAPI's :class:`~fastapi.testclient.TestClient` with dependency
overrides so every collaborator is a controllable fake:

* a :class:`FakeCredentialStore` standing in for the real Fernet-backed store,
* a :class:`FakeConnectivityChecker` for the test endpoint (including a slow
  variant that exercises the 30-second cap), and
* real SQLite repositories over a temporary bootstrapped database for the
  status/error-log side effects.

Covered scenarios: masked GET, valid PUT (saved confirmation + store updated),
invalid PUT (400 with per-field errors and the store left unchanged), the test
endpoint's success and timeout-failure paths, and error-log listing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.core.models import ConnectivityResult
from app.db import bootstrap
from app.config import load_settings
from app.kb.store import IntegrationErrorLogRepository, IntegrationRepository
from app.security.credentials import CredentialValidationError, validate_credentials


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCredentialStore:
    """In-memory stand-in for :class:`CredentialStore`.

    Reuses the real :func:`validate_credentials` so validation behavior matches
    production exactly, but keeps values in a dict instead of an encrypted file.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def save(self, integration: str, fields: dict) -> None:
        errors = validate_credentials(integration, fields)
        if errors:
            raise CredentialValidationError(errors)
        # Store only schema fields (mirrors the real store's behavior).
        from app.security.credentials import CREDENTIAL_SCHEMAS

        self._data[integration] = {
            name: fields[name] for name in CREDENTIAL_SCHEMAS[integration]
        }

    def load(self, integration: str) -> dict[str, str] | None:
        return self._data.get(integration)

    def masked(self, integration: str) -> dict[str, str]:
        from app.security.credentials import mask_value

        entry = self._data.get(integration)
        if not entry:
            return {}
        return {k: mask_value(v) for k, v in entry.items()}


class FakeConnectivityChecker:
    """Connectivity checker returning a preset result (optionally slow)."""

    def __init__(
        self, result: ConnectivityResult, *, delay: float = 0.0
    ) -> None:
        self._result = result
        self._delay = delay

    async def check(self, tool: str) -> ConnectivityResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_conn(tmp_path):
    """A bootstrapped SQLite connection over a temp data dir.

    FastAPI runs sync endpoints in a worker thread, so the connection is
    opened with ``check_same_thread=False`` to be usable across the test
    thread and the request-handling thread.
    """
    import sqlite3

    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "CREDENTIAL_MASTER_KEY": "unused-in-fake"}
    )
    # Bootstrap schema + seed data, then reopen for cross-thread use.
    bootstrap(settings).close()
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture()
def store() -> FakeCredentialStore:
    return FakeCredentialStore()


def _make_app(
    *,
    store: FakeCredentialStore,
    conn,
    checker: FakeConnectivityChecker | None = None,
) -> FastAPI:
    """Build a FastAPI app mounting the admin router with fakes injected."""
    app = FastAPI()
    app.include_router(admin.router)

    app.dependency_overrides[admin.get_credential_store] = lambda: store
    app.dependency_overrides[admin.get_integration_repo] = (
        lambda: IntegrationRepository(conn)
    )
    app.dependency_overrides[admin.get_error_log_repo] = (
        lambda: IntegrationErrorLogRepository(conn)
    )
    if checker is not None:
        app.dependency_overrides[admin.get_connectivity_checker] = lambda: checker
    return app


# A valid Telegram token per CREDENTIAL_SCHEMAS: r"^\d+:[A-Za-z0-9_-]{30,}$"
VALID_TELEGRAM_TOKEN = "123456789:" + "A" * 35


# ---------------------------------------------------------------------------
# Masked GET
# ---------------------------------------------------------------------------


def test_get_credentials_masked_after_save(store, temp_conn):
    """GET returns masked values revealing at most the last 4 chars."""
    store.save("telegram", {"bot_token": VALID_TELEGRAM_TOKEN})
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.get("/integrations/telegram/credentials")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "telegram"
    assert body["configured"] is True
    masked = body["credentials"]["bot_token"]
    # Same length, only last 4 chars visible, everything else masked.
    assert len(masked) == len(VALID_TELEGRAM_TOKEN)
    assert masked.endswith(VALID_TELEGRAM_TOKEN[-4:])
    assert set(masked[:-4]) == {"*"}


def test_get_credentials_when_unconfigured(store, temp_conn):
    """GET on an unconfigured integration reports not configured, empty dict."""
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.get("/integrations/teams/credentials")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["credentials"] == {}


def test_get_credentials_unknown_integration(store, temp_conn):
    """GET on an unknown integration returns 404."""
    client = TestClient(_make_app(store=store, conn=temp_conn))
    resp = client.get("/integrations/nope/credentials")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Valid PUT
# ---------------------------------------------------------------------------


def test_put_valid_credentials_saves_and_confirms(store, temp_conn):
    """A valid PUT stores the credentials and returns a saved confirmation."""
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.put(
        "/integrations/telegram/credentials",
        json={"bot_token": VALID_TELEGRAM_TOKEN},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert "saved" in body["message"].lower()
    # Store now holds the value; response echoes it masked.
    assert store.load("telegram") == {"bot_token": VALID_TELEGRAM_TOKEN}
    assert body["credentials"]["bot_token"].endswith(VALID_TELEGRAM_TOKEN[-4:])


def test_put_valid_credentials_replaces_previous(store, temp_conn):
    """A valid PUT fully replaces previously stored credentials (Req 1.5)."""
    store.save("telegram", {"bot_token": "111111111:" + "B" * 35})
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.put(
        "/integrations/telegram/credentials",
        json={"bot_token": VALID_TELEGRAM_TOKEN},
    )

    assert resp.status_code == 200
    assert store.load("telegram") == {"bot_token": VALID_TELEGRAM_TOKEN}


# ---------------------------------------------------------------------------
# Invalid PUT
# ---------------------------------------------------------------------------


def test_put_invalid_credentials_returns_per_field_errors(store, temp_conn):
    """An invalid PUT returns 400 with one error per offending field."""
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.put(
        "/integrations/teams/credentials",
        json={"app_id": "not-a-uuid", "app_password": "short"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["saved"] is False
    fields = {e["field"] for e in detail["errors"]}
    assert fields == {"app_id", "app_password"}


def test_put_invalid_leaves_store_unchanged(store, temp_conn):
    """A rejected update keeps prior credentials intact (Req 1.6)."""
    original = {"bot_token": VALID_TELEGRAM_TOKEN}
    store.save("telegram", original)
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.put(
        "/integrations/telegram/credentials",
        json={"bot_token": "invalid-token"},
    )

    assert resp.status_code == 400
    # Store is unchanged.
    assert store.load("telegram") == original


# ---------------------------------------------------------------------------
# Test endpoint
# ---------------------------------------------------------------------------


def test_test_endpoint_success(store, temp_conn):
    """A successful connectivity check returns ok and records Connected."""
    checker = FakeConnectivityChecker(
        ConnectivityResult(tool="telegram", ok=True, detail="getMe ok")
    )
    client = TestClient(_make_app(store=store, conn=temp_conn, checker=checker))

    resp = client.post("/integrations/telegram/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["timed_out"] is False
    # Status persisted as Connected.
    assert IntegrationRepository(temp_conn).get("telegram")["status"] == "Connected"


def test_test_endpoint_failure_logs_error(store, temp_conn):
    """A failed check returns ok=False, records Error, and logs the failure."""
    checker = FakeConnectivityChecker(
        ConnectivityResult(tool="teams", ok=False, detail="401 Unauthorized")
    )
    client = TestClient(_make_app(store=store, conn=temp_conn, checker=checker))

    resp = client.post("/integrations/teams/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["detail"]
    assert IntegrationRepository(temp_conn).get("teams")["status"] == "Error"
    errors = IntegrationErrorLogRepository(temp_conn).list_recent(tool="teams")
    assert len(errors) == 1
    assert errors[0]["operation"] == "test"


def test_test_endpoint_timeout_failure(store, temp_conn, monkeypatch):
    """A check exceeding the cap terminates with a timeout failure (Req 3.5)."""
    # Shrink the cap so the test is fast; the checker sleeps past it.
    monkeypatch.setattr(admin, "TEST_TIMEOUT_SECONDS", 0.05)
    checker = FakeConnectivityChecker(
        ConnectivityResult(tool="telegram", ok=True), delay=1.0
    )
    client = TestClient(_make_app(store=store, conn=temp_conn, checker=checker))

    resp = client.post("/integrations/telegram/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["timed_out"] is True
    assert "timed out" in body["detail"].lower()
    assert IntegrationRepository(temp_conn).get("telegram")["status"] == "Error"


def test_test_endpoint_unknown_tool(store, temp_conn):
    """Connectivity test on a non-Frontend_Tool returns 404."""
    checker = FakeConnectivityChecker(ConnectivityResult(tool="x", ok=True))
    client = TestClient(_make_app(store=store, conn=temp_conn, checker=checker))
    resp = client.post("/integrations/dashscope/test")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Error listing
# ---------------------------------------------------------------------------


def test_list_errors_newest_first_and_limited(store, temp_conn):
    """Error listing returns entries newest-first, capped at the limit."""
    repo = IntegrationErrorLogRepository(temp_conn)
    for i in range(5):
        repo.insert(
            tool="telegram",
            operation="send",
            error_detail=f"err-{i}",
            ts=datetime(2024, 1, 1, 0, 0, i),
        )
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.get("/integrations/telegram/errors", params={"limit": 3})

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    # Newest first: err-4, err-3, err-2.
    assert [r["error_detail"] for r in rows] == ["err-4", "err-3", "err-2"]


def test_list_errors_filters_by_tool(store, temp_conn):
    """Error listing only returns the requested tool's entries."""
    repo = IntegrationErrorLogRepository(temp_conn)
    repo.insert(tool="telegram", operation="send", error_detail="tg")
    repo.insert(tool="teams", operation="send", error_detail="tm")
    client = TestClient(_make_app(store=store, conn=temp_conn))

    resp = client.get("/integrations/teams/errors")

    assert resp.status_code == 200
    rows = resp.json()
    assert [r["tool"] for r in rows] == ["teams"]
