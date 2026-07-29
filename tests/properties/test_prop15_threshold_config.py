# Feature: intelliknow-kms, Property 15: Threshold configuration validation
"""Property-based test for Confidence_Threshold configuration.

**Property 15: Threshold configuration validation**

For any submitted Confidence_Threshold value, the update is accepted if and only
if the value is a whole number in the inclusive range ``[0, 100]``. A rejected
submission retains the previously stored value; an accepted submission is
persisted and read back by a subsequent GET.

**Validates: Requirements 7.4, 7.9**

The test drives the real ``GET/PUT /settings/confidence-threshold`` endpoints
through a FastAPI ``TestClient`` over a bootstrapped temporary database (whose
seeded default is 70), comparing acceptance against an independent reference.
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

# A spread of candidate values: in-range ints, out-of-range ints, non-integer
# floats, numeric strings, and clearly-invalid strings.
_candidates = st.one_of(
    st.integers(min_value=-50, max_value=150),
    st.floats(min_value=-50, max_value=150, allow_nan=False, allow_infinity=False),
    st.integers(min_value=0, max_value=100).map(str),
    st.text(alphabet="abc 12", min_size=0, max_size=4),
)


def _reference_accepts(raw: object) -> tuple[bool, int | None]:
    """Independently decide acceptance and the accepted integer (Req 7.4/7.9)."""
    if isinstance(raw, bool):
        return False, None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not float(raw).is_integer():
            return False, None
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            return False, None
    else:
        return False, None
    if 0 <= value <= 100:
        return True, value
    return False, None


def _build_client(tmp: str) -> tuple[TestClient, sqlite3.Connection]:
    """Build a TestClient over a bootstrapped temp DB with admin routes."""
    settings_obj = load_settings({"DATA_DIR": tmp, "CREDENTIAL_MASTER_KEY": "unused"})
    bootstrap(settings_obj).close()
    conn = sqlite3.connect(str(settings_obj.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.get_connection] = lambda: conn
    app.dependency_overrides[admin.get_settings_dependency] = lambda: settings_obj
    return TestClient(app), conn


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(raw=_candidates)
def test_threshold_accepted_iff_in_range(raw: object) -> None:
    """Accept iff [0,100]; reject retains prior value; accepted persists."""
    with tempfile.TemporaryDirectory() as tmp:
        client, conn = _build_client(tmp)
        try:
            prior = client.get("/settings/confidence-threshold").json()["value"]
            expected_ok, expected_val = _reference_accepts(raw)

            resp = client.put("/settings/confidence-threshold", json={"value": raw})
            now = client.get("/settings/confidence-threshold").json()["value"]

            if expected_ok:
                assert resp.status_code == 200, resp.text
                assert resp.json()["value"] == expected_val
                assert now == expected_val
            else:
                assert resp.status_code == 400, resp.text
                # A rejected submission leaves the prior value intact (Req 7.9).
                assert now == prior
        finally:
            conn.close()
