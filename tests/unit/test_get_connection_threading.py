"""Regression tests for the real ``admin.get_connection`` dependency.

The rest of the admin test-suite overrides ``get_connection`` with a shared
temp-DB connection, so the *real* dependency — the one used in production — is
never exercised there. These tests pin two properties of the real dependency:

1. Connections it yields are usable from a thread other than the one that
   created them. FastAPI resolves this sync dependency on a worker thread but
   runs ``async def`` path operations on the event-loop thread; without
   ``check_same_thread=False`` every such endpoint (e.g. the AI
   keyword-suggestion route) raised ``sqlite3.ProgrammingError`` → HTTP 500.
2. Hitting an ``async`` admin endpoint that reads through ``get_connection``
   returns a normal response instead of a 500.
"""

from __future__ import annotations

import concurrent.futures

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.config import load_settings
from app.db import bootstrap


def _settings(tmp_path):
    """Bootstrapped settings pointing at a fresh temp database."""
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "CREDENTIAL_MASTER_KEY": "unused"}
    )
    bootstrap(settings).close()
    return settings


def test_get_connection_yields_cross_thread_usable_connection(tmp_path):
    """A yielded connection must be usable off its creating thread (Req: no 500)."""
    settings = _settings(tmp_path)

    gen = admin.get_connection(settings)
    conn = next(gen)
    try:
        # Use the connection from a *different* thread than the one above.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            count = pool.submit(
                lambda: conn.execute(
                    "SELECT COUNT(*) FROM intent_spaces"
                ).fetchone()[0]
            ).result()
        assert count >= 1  # seeded General/HR/Legal/Finance
    finally:
        gen.close()  # runs the dependency's finally → conn.close()


def test_async_endpoint_reading_through_get_connection_is_not_500(tmp_path):
    """The async suggest-keywords route returns 200 via the real dependency."""
    settings = _settings(tmp_path)

    class _FakeAI:
        async def chat_completion(self, messages, **kwargs) -> str:
            return "{}"

    app = FastAPI()
    app.include_router(admin.router)
    # Point the real get_connection at the temp DB; do NOT override it.
    app.dependency_overrides[admin.get_settings_dependency] = lambda: settings
    app.state.dashscope_client = _FakeAI()
    client = TestClient(app)

    # General space always exists after bootstrap (id 1). No misrouted queries
    # yet, so the handler returns an empty suggestion — but only if the
    # cross-thread SELECT succeeds.
    resp = client.post("/spaces/1/suggest-keywords")

    assert resp.status_code == 200
    assert resp.json()["keywords"] == []
