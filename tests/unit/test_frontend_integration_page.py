"""Unit tests for the Frontend Integration screen's pure helpers (task 18.3).

These exercise the server-free decision logic of
``admin_ui/pages/1_Frontend_Integration.py``:

* per-tool credential field definitions (Req 1.4, 11.2),
* mapping a validation :class:`ApiError` to per-field messages (Req 1.4), and
* deriving the displayed connection status from stored-ness + the latest
  connectivity result (Req 3.1).

The page module is imported by file path because its filename begins with a
digit (Streamlit's page-ordering convention), which is not a valid Python
identifier for a normal import. Importing it here also asserts the module is
**headless-safe** — it must import without a running Streamlit server.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from admin_ui.ui.components import ApiError

# Path to the page module (filename starts with a digit → import by path).
_PAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "admin_ui"
    / "pages"
    / "1_Frontend_Integration.py"
)


def _load_page_module() -> ModuleType:
    """Import the Frontend Integration page module by file path (headless)."""
    spec = importlib.util.spec_from_file_location(
        "frontend_integration_page", _PAGE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass introspection can resolve the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page = _load_page_module()


# ---------------------------------------------------------------------------
# credential_fields
# ---------------------------------------------------------------------------


def test_telegram_has_bot_token_field() -> None:
    """Telegram exposes exactly the bot_token field, rendered as a secret."""
    fields = page.credential_fields("telegram")
    assert [f.name for f in fields] == ["bot_token"]
    assert fields[0].secret is True


def test_teams_has_app_id_and_password_fields() -> None:
    """Teams exposes app_id (plain) and app_password (secret) in order."""
    fields = page.credential_fields("teams")
    assert [f.name for f in fields] == ["app_id", "app_password"]
    by_name = {f.name: f for f in fields}
    assert by_name["app_id"].secret is False
    assert by_name["app_password"].secret is True


def test_unknown_tool_has_no_fields() -> None:
    """An unknown tool yields an empty field tuple rather than raising."""
    assert page.credential_fields("nope") == ()


# ---------------------------------------------------------------------------
# extract_field_errors
# ---------------------------------------------------------------------------


def test_extract_field_errors_from_nested_detail() -> None:
    """Per-field errors nested under FastAPI ``detail`` are mapped by field."""
    err = ApiError(
        "rejected",
        status_code=400,
        payload={
            "detail": {
                "tool": "teams",
                "saved": False,
                "message": "rejected",
                "errors": [
                    {"field": "app_id", "message": "has an invalid format"},
                    {"field": "app_password", "message": "is required"},
                ],
            }
        },
    )
    assert page.extract_field_errors(err) == {
        "app_id": "has an invalid format",
        "app_password": "is required",
    }


def test_extract_field_errors_from_flat_payload() -> None:
    """Errors on a flat payload (no ``detail`` wrapper) are also mapped."""
    err = ApiError(
        "rejected",
        status_code=400,
        payload={"errors": [{"field": "bot_token", "message": "is required"}]},
    )
    assert page.extract_field_errors(err) == {"bot_token": "is required"}


def test_extract_field_errors_without_payload_is_empty() -> None:
    """A transport-level error (no structured payload) maps to no fields."""
    err = ApiError("Could not reach the backend", status_code=None)
    assert page.extract_field_errors(err) == {}


# ---------------------------------------------------------------------------
# connection_status
# ---------------------------------------------------------------------------


def test_status_not_configured_when_no_credentials() -> None:
    """Without stored credentials the status is 'Not configured'."""
    label, detail = page.connection_status(False, None)
    assert label == "Not configured"
    assert detail


def test_status_unknown_when_configured_but_untested() -> None:
    """Configured but never tested this session → 'Unknown' (prompt to test)."""
    label, _ = page.connection_status(True, None)
    assert label == "Unknown"


def test_status_connected_on_successful_test() -> None:
    """A successful connectivity result yields 'Connected' with its detail."""
    label, detail = page.connection_status(
        True, {"ok": True, "detail": "getMe OK"}
    )
    assert label == "Connected"
    assert detail == "getMe OK"


def test_status_timed_out_is_distinct_error() -> None:
    """A timed-out check is reported as a distinct timeout error state."""
    label, _ = page.connection_status(
        True, {"ok": False, "timed_out": True, "detail": "timed out"}
    )
    assert label == "Error (timed out)"


def test_status_error_on_failed_test() -> None:
    """A failed (non-timeout) check yields the plain 'Error' state."""
    label, detail = page.connection_status(
        True, {"ok": False, "timed_out": False, "detail": "401 Unauthorized"}
    )
    assert label == "Error"
    assert detail == "401 Unauthorized"


# ---------------------------------------------------------------------------
# Screen wiring sanity
# ---------------------------------------------------------------------------


def test_screen_targets_the_frontend_tools() -> None:
    """The screen manages Telegram, Teams, and WhatsApp, with the right key."""
    assert page.TOOLS == ("telegram", "teams", "whatsapp")
    assert page.SCREEN_KEY == "frontend_integration"
    assert page.ERROR_LOG_LIMIT == 50


@pytest.mark.parametrize("tool", ["telegram", "teams", "whatsapp"])
def test_every_tool_has_a_title(tool: str) -> None:
    """Each managed tool has a human-readable display title."""
    assert page.TOOL_TITLES[tool]
