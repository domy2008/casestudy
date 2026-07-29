"""Unit tests for startup credential loading (Req 11.4, 11.5).

Covers the missing-credential path: an integration whose credentials are absent
from the Credential_Store is reported by field *name* only (never its value)
and marked unavailable in the `integrations` table, while a fully-configured
integration is marked available and its secret values are never logged.
"""

from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from app.config import load_settings
from app.db import bootstrap
from app.kb.store import IntegrationRepository
from app.security.credentials import CredentialStore
from app.security.logfilter import RedactingFilter
from app.security.startup import load_startup_credentials

# Realistic, schema-valid sample credentials.
VALID_TELEGRAM = {"bot_token": "123456789:AAHfakeTelegramTokenValue_0123456789"}
VALID_DASHSCOPE = {"api_key": "sk-abcdef0123456789abcdef"}
VALID_TEAMS = {
    "app_id": "12345678-1234-1234-1234-1234567890ab",
    "app_password": "s3cr3tPassword!",
}


@pytest.fixture()
def env(tmp_path):
    """Provide isolated settings pointing at a temp DATA_DIR with a master key."""
    return load_settings(
        {
            "DATA_DIR": str(tmp_path),
            "CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
        }
    )


def test_missing_credential_names_field_and_marks_unavailable(env, caplog):
    """A missing integration is reported by field name only and marked unavailable."""
    conn = bootstrap(env)
    store = CredentialStore(env)
    # Configure Telegram + DashScope, but deliberately omit Teams.
    store.save("telegram", VALID_TELEGRAM)
    store.save("dashscope", VALID_DASHSCOPE)

    integration_repo = IntegrationRepository(conn)

    with caplog.at_level(logging.ERROR):
        result = load_startup_credentials(store, integration_repo)

    # Teams is missing: reported by field NAME, and its values never appear.
    assert "teams" in result.missing
    assert set(result.missing["teams"]) == {"app_id", "app_password"}
    assert "teams" in result.unavailable

    # Telegram + DashScope loaded, so Telegram is available.
    assert "telegram" in result.loaded
    assert "dashscope" in result.loaded
    assert "telegram" not in result.unavailable

    # Persisted availability in the integrations table.
    teams_row = integration_repo.get("teams")
    assert teams_row is not None
    assert teams_row["status"] == "Disconnected"
    assert teams_row["active"] == 0

    telegram_row = integration_repo.get("telegram")
    assert telegram_row is not None
    assert telegram_row["active"] == 1

    # The error log names the missing fields...
    log_text = caplog.text
    assert "teams" in log_text
    assert "app_id" in log_text
    assert "app_password" in log_text

    # ...but never leaks any configured credential value.
    assert VALID_TELEGRAM["bot_token"] not in log_text
    assert VALID_DASHSCOPE["api_key"] not in log_text

    conn.close()


def test_all_credentials_present_marks_all_available_and_seeds_filter(env):
    """With every credential present, both tools are available and secrets seed the filter."""
    conn = bootstrap(env)
    store = CredentialStore(env)
    store.save("telegram", VALID_TELEGRAM)
    store.save("teams", VALID_TEAMS)
    store.save("dashscope", VALID_DASHSCOPE)

    integration_repo = IntegrationRepository(conn)
    filt = RedactingFilter()

    result = load_startup_credentials(store, integration_repo, redacting_filter=filt)

    assert result.missing == {}
    assert result.unavailable == set()
    assert integration_repo.get("telegram")["active"] == 1
    assert integration_repo.get("teams")["active"] == 1

    # The filter now redacts the loaded secret values.
    assert filt.redact(VALID_TELEGRAM["bot_token"]) == "***"
    assert filt.redact(VALID_DASHSCOPE["api_key"]) == "***"

    conn.close()


def test_missing_dashscope_makes_all_frontends_unavailable(env, caplog):
    """Missing DashScope credentials render every frontend tool unavailable."""
    conn = bootstrap(env)
    store = CredentialStore(env)
    store.save("telegram", VALID_TELEGRAM)
    store.save("teams", VALID_TEAMS)
    # DashScope intentionally not saved.

    integration_repo = IntegrationRepository(conn)

    with caplog.at_level(logging.ERROR):
        result = load_startup_credentials(store, integration_repo)

    assert "dashscope" in result.missing
    assert result.missing["dashscope"] == ["api_key"]
    assert result.unavailable == {"telegram", "teams"}
    assert integration_repo.get("telegram")["active"] == 0
    assert integration_repo.get("teams")["active"] == 0

    conn.close()
