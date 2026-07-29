# Feature: intelliknow-kms, Property 11: Space name uniqueness is case-insensitive
"""Property-based test for case-insensitive Intent_Space name uniqueness.

**Property 11: Space name uniqueness is case-insensitive**

For any Intent_Space name, once a space with that name exists, creating another
space whose name differs only by letter case is rejected, and the set of spaces
is left unchanged.

**Validates: Requirements 6.6**

The test drives the real ``POST /spaces`` endpoint through a FastAPI
``TestClient`` backed by a freshly bootstrapped temporary database, so it
exercises the endpoint's uniqueness check end to end.
"""

from __future__ import annotations

import sqlite3
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.api import admin
from app.config import load_settings
from app.db import bootstrap

# Names are prefixed with "z" so they never collide with the seeded spaces
# (General / HR / Legal / Finance), keeping the "first create succeeds" premise.
_base_name = st.text(alphabet="abcXYZ", min_size=0, max_size=10).map(lambda s: "z" + s)
# A per-character case-flip mask applied to build a differing-case variant.
_flip_mask = st.lists(st.booleans(), min_size=0, max_size=11)


def _recase(name: str, mask: list[bool]) -> str:
    """Return ``name`` with the case of masked positions swapped."""
    chars = list(name)
    for i, flip in enumerate(mask):
        if i < len(chars) and flip:
            c = chars[i]
            chars[i] = c.lower() if c.isupper() else c.upper()
    return "".join(chars)


def _build_client(tmp: str) -> tuple[TestClient, sqlite3.Connection]:
    """Build a TestClient over a bootstrapped temp DB with admin routes."""
    settings_obj = load_settings(
        {"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "unused"}
    )
    bootstrap(settings_obj).close()
    conn = sqlite3.connect(str(settings_obj.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: conn
    app.dependency_overrides[admin.get_settings_dependency] = lambda: settings_obj
    return TestClient(app), conn


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=_base_name, mask=_flip_mask)
def test_case_insensitive_name_is_rejected(name: str, mask: list[bool]) -> None:
    """A differing-case duplicate is rejected and the space set is unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        client, conn = _build_client(tmp)
        try:
            first = client.post(
                "/spaces", json={"name": name, "description": "", "keywords": []}
            )
            assert first.status_code == 200, first.text

            count_before = len(client.get("/spaces").json())

            variant = _recase(name, mask)
            resp = client.post(
                "/spaces", json={"name": variant, "description": "", "keywords": []}
            )
            # A case-variant of an existing name must be rejected (Req 6.6).
            assert resp.status_code == 409, (
                f"expected 409 for variant {variant!r} of {name!r}, got "
                f"{resp.status_code}"
            )

            # The set of spaces is unchanged by the rejected create.
            count_after = len(client.get("/spaces").json())
            assert count_after == count_before
        finally:
            conn.close()
