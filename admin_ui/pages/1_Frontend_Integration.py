"""Frontend Integration admin screen (task 18.3).

This is the second screen of the Streamlit multipage app (it lives under
``pages/`` so Streamlit lists it in the sidebar after the Dashboard — Req 9.1).
It lets the Admin manage the Telegram, Microsoft Teams, and WhatsApp
integrations end to end using the shared :class:`~admin_ui.ui.components.ApiClient` against the
backend admin REST API:

* **Masked credential forms** per Frontend_Tool with per-field validation
  errors and a save confirmation — ``GET``/``PUT
  /integrations/{tool}/credentials`` (Req 1.4, 1.7, 11.2).
* **Connection status** per tool, derived from whether credentials are stored
  and the most recent connectivity check (Req 3.1).
* **Test button** that runs the 30-second-capped connectivity check and shows
  the outcome — ``POST /integrations/{tool}/test`` (Req 3.2).
* **The 50 most recent error-log entries** per tool — ``GET
  /integrations/{tool}/errors?limit=50`` (Req 3.4).

Import-safety and testability
-----------------------------
Every ``streamlit`` call lives inside :func:`_main` (invoked only under
``__main__``), and ``streamlit`` is imported lazily there, so importing this
module never requires a Streamlit runtime. The decision logic — which fields a
tool has, how a validation error maps to per-field messages, and how a
connection status is derived — is factored into small **pure functions**
(:func:`credential_fields`, :func:`extract_field_errors`,
:func:`connection_status`) that depend only on the standard library and the
:class:`~admin_ui.ui.components.ApiError` data type, so they are unit-testable
without a server (mirroring the pure-helper split in ``ui/components.py``).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Make the project root importable when Streamlit runs this page directly.
_root = _os.path.abspath(__file__)
for _ in range(4):
    _root = _os.path.dirname(_root)
    if _os.path.isdir(_os.path.join(_root, "admin_ui")):
        break
if _root not in _sys.path:
    _sys.path.insert(0, _root)

from dataclasses import dataclass
from typing import Any

from admin_ui.ui.components import (
    ApiClient,
    ApiError,
    inject_base_css,
    render_metric_card,
    render_sidebar_nav,
    section_header,
)

#: Navigation key for this screen (matches a ``SCREENS`` entry in components).
SCREEN_KEY = "frontend_integration"

#: Module identifier used to pick the blue accent colour (Req 9.4).
MODULE = "frontend_integration"

#: Error-log page size requested from the backend (Req 3.4).
ERROR_LOG_LIMIT = 50


@dataclass(frozen=True)
class CredentialField:
    """One editable credential field for a Frontend_Tool's form.

    Attributes:
        name: The backend field key (matches the credential schema, e.g.
            ``"bot_token"``); used as the JSON key on save and as the key into
            masked reads and per-field validation errors.
        label: Human-readable label shown beside the input.
        secret: When ``True`` the input is rendered as a password field so the
            typed value is obscured on screen.
        help_text: Optional hint shown under the input (e.g. the expected
            format) to guide a correct submission.
    """

    name: str
    label: str
    secret: bool = True
    help_text: str = ""


#: The Frontend_Tools shown on this screen, in display order. Telegram, Teams,
#: and WhatsApp are Frontend_Tools with connectivity/error tracking (DashScope
#: is a credential-only integration and is configured elsewhere).
TOOLS: tuple[str, ...] = ("telegram", "teams", "whatsapp")

#: Display titles for each tool.
TOOL_TITLES: dict[str, str] = {
    "telegram": "Telegram",
    "teams": "Microsoft Teams",
    "whatsapp": "WhatsApp",
}

#: The credential fields each tool exposes. These mirror the backend credential
#: schema (telegram → ``bot_token``; teams → ``app_id`` + ``app_password``;
#: whatsapp → ``access_token`` + ``phone_number_id`` + ``verify_token``) but
#: are declared here so the admin-UI container stays independent of the backend
#: package. The backend remains the single source of truth for validation; this
#: screen simply surfaces whatever per-field errors it returns.
TOOL_FIELDS: dict[str, tuple[CredentialField, ...]] = {
    "telegram": (
        CredentialField(
            "bot_token",
            "Bot Token",
            secret=True,
            help_text="From BotFather, e.g. 123456:ABC-DEF… (digits:token).",
        ),
    ),
    "teams": (
        CredentialField(
            "app_id",
            "App ID",
            secret=False,
            help_text="The Bot Framework app (client) ID — a 36-char GUID.",
        ),
        CredentialField(
            "app_password",
            "App Password",
            secret=True,
            help_text="The Bot Framework app client secret (≥8 characters).",
        ),
    ),
    "whatsapp": (
        CredentialField(
            "access_token",
            "Access Token",
            secret=True,
            help_text="Meta Graph API system-user access token (≥20 chars).",
        ),
        CredentialField(
            "phone_number_id",
            "Phone Number ID",
            secret=False,
            help_text="The WhatsApp Business phone number ID (numeric).",
        ),
        CredentialField(
            "verify_token",
            "Verify Token",
            secret=True,
            help_text="Your chosen webhook verify token (≥8 chars); must match "
            "the value entered in the Meta App webhook configuration.",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Pure helpers (server-free, unit-testable)
# ---------------------------------------------------------------------------


def credential_fields(tool: str) -> tuple[CredentialField, ...]:
    """Return the credential fields for ``tool``.

    Args:
        tool: The Frontend_Tool key (``"telegram"`` / ``"teams"``).

    Returns:
        The tuple of :class:`CredentialField` definitions for the tool, or an
        empty tuple when the tool is unknown.
    """
    return TOOL_FIELDS.get(tool, ())


def extract_field_errors(error: ApiError) -> dict[str, str]:
    """Map a validation :class:`ApiError` to per-field messages (Req 1.4).

    The backend's validated-save endpoint returns a 400 whose body carries an
    ``errors`` list of ``{"field", "message"}`` objects (surfaced on
    :attr:`ApiError.payload`, itself possibly nested under ``detail``). This
    extracts those into a ``field → message`` mapping the form can render
    inline next to each input. When the error carries no structured per-field
    detail (e.g. a transport failure), an empty mapping is returned.

    Args:
        error: The :class:`ApiError` raised by a failed credential save.

    Returns:
        A mapping of field name to its validation message; empty when no
        per-field detail is available.
    """
    payload = error.payload
    if not isinstance(payload, dict):
        return {}
    # The payload may be the error body itself or a FastAPI {"detail": {...}}.
    detail = payload.get("detail", payload)
    if not isinstance(detail, dict):
        return {}
    errors = detail.get("errors")
    if not isinstance(errors, list):
        return {}
    mapped: dict[str, str] = {}
    for item in errors:
        if isinstance(item, dict):
            field = item.get("field")
            message = item.get("message")
            if isinstance(field, str) and isinstance(message, str):
                mapped[field] = message
    return mapped


def connection_status(
    configured: bool, test_result: dict[str, Any] | None
) -> tuple[str, str]:
    """Derive a display connection status for a tool (Req 3.1).

    The screen has three signals available from the in-scope endpoints: whether
    credentials are stored (from the masked read), and — once the Admin runs a
    check — the most recent connectivity result. This folds them into a single
    label and a supporting detail line:

    * no credentials stored → ``"Not configured"``;
    * stored but never tested this session → ``"Unknown"`` (prompt to test);
    * last test succeeded → ``"Connected"``;
    * last test timed out → ``"Error (timed out)"``;
    * last test failed → ``"Error"``.

    Args:
        configured: ``True`` when the tool has stored credentials.
        test_result: The most recent connectivity result payload for the tool
            (as returned by the test endpoint), or ``None`` if untested.

    Returns:
        A ``(label, detail)`` pair; ``detail`` is a short supporting sentence.
    """
    if not configured:
        return ("Not configured", "Enter and save credentials to enable this tool.")
    if not test_result:
        return ("Unknown", "Run a connectivity test to check the connection.")
    if test_result.get("ok"):
        detail = str(test_result.get("detail") or "Connectivity check succeeded.")
        return ("Connected", detail)
    if test_result.get("timed_out"):
        detail = str(test_result.get("detail") or "Connectivity check timed out.")
        return ("Error (timed out)", detail)
    detail = str(test_result.get("detail") or "Connectivity check failed.")
    return ("Error", detail)


# ---------------------------------------------------------------------------
# Streamlit rendering (server-only; imports streamlit lazily)
# ---------------------------------------------------------------------------


def _session_key(tool: str, suffix: str) -> str:
    """Build a stable ``st.session_state`` key scoped to a tool."""
    return f"frontend_integration:{tool}:{suffix}"


def _render_credentials_form(st: Any, client: ApiClient, tool: str) -> None:
    """Render the masked credential read + validated save form for a tool."""
    # Masked read (Req 11.2): show what is currently stored, values masked.
    try:
        masked = client.get(f"/integrations/{tool}/credentials")
    except ApiError as exc:
        masked = None
        st.error(f"Could not load stored credentials: {exc.message}")

    configured = bool(masked and masked.get("configured"))
    stored_values: dict[str, str] = (masked or {}).get("credentials", {}) or {}

    if configured:
        st.caption("Currently stored (masked):")
        for field in credential_fields(tool):
            shown = stored_values.get(field.name, "—")
            st.markdown(f"- **{field.label}**: `{shown}`")
    else:
        st.caption("No credentials stored yet.")

    # Per-field errors from the previous save attempt (if any) so they render
    # inline next to their inputs after the form reruns.
    errors_key = _session_key(tool, "field_errors")
    field_errors: dict[str, str] = st.session_state.get(errors_key, {})

    with st.form(_session_key(tool, "form")):
        submitted_values: dict[str, str] = {}
        for field in credential_fields(tool):
            submitted_values[field.name] = st.text_input(
                field.label,
                type="password" if field.secret else "default",
                help=field.help_text or None,
                key=_session_key(tool, f"input:{field.name}"),
            )
            if field.name in field_errors:
                st.error(f"{field.label}: {field_errors[field.name]}")
        save = st.form_submit_button("Save credentials")

    if save:
        try:
            result = client.put(
                f"/integrations/{tool}/credentials", json=submitted_values
            )
        except ApiError as exc:
            new_errors = extract_field_errors(exc)
            st.session_state[errors_key] = new_errors
            if new_errors:
                st.error(
                    "Credentials were not saved. Fix the highlighted fields "
                    "and try again."
                )
            else:
                st.error(f"Credentials were not saved: {exc.message}")
        else:
            # Clear any stale per-field errors and confirm the save (Req 1.7).
            st.session_state[errors_key] = {}
            st.success(
                result.get("message", f"Credentials for {tool} were saved.")
            )


def _render_status_and_test(st: Any, client: ApiClient, tool: str) -> None:
    """Render the connection-status card and the connectivity Test button."""
    result_key = _session_key(tool, "test_result")

    # Determine configured-ness from a fresh masked read for status accuracy.
    configured = False
    try:
        masked = client.get(f"/integrations/{tool}/credentials")
        configured = bool(masked and masked.get("configured"))
    except ApiError:
        # Status falls back to "unknown/not configured"; the form section above
        # already surfaced the read error to the Admin.
        configured = False

    last_result = st.session_state.get(result_key)
    label, detail = connection_status(configured, last_result)
    render_metric_card(
        "Connection status", label, module=MODULE, help_text=detail
    )

    if st.button("Test connection", key=_session_key(tool, "test_btn")):
        with st.spinner(f"Running connectivity check for {tool}…"):
            try:
                result = client.post(f"/integrations/{tool}/test")
            except ApiError as exc:
                st.session_state[result_key] = {
                    "ok": False,
                    "timed_out": False,
                    "detail": exc.message,
                }
                st.error(f"Connectivity check failed: {exc.message}")
            else:
                st.session_state[result_key] = result
                if result.get("ok"):
                    st.success(
                        result.get("detail") or "Connectivity check succeeded."
                    )
                elif result.get("timed_out"):
                    st.warning(
                        result.get("detail") or "Connectivity check timed out."
                    )
                else:
                    st.error(
                        result.get("detail") or "Connectivity check failed."
                    )


def _render_error_log(st: Any, client: ApiClient, tool: str) -> None:
    """Render the 50 most recent error-log entries for a tool (Req 3.4)."""
    st.markdown("**Recent errors** (most recent first)")
    try:
        entries = client.get(
            f"/integrations/{tool}/errors", params={"limit": ERROR_LOG_LIMIT}
        )
    except ApiError as exc:
        st.error(f"Could not load the error log: {exc.message}")
        return

    if not entries:
        st.caption("No errors logged for this tool.")
        return

    rows = [
        {
            "Time": entry.get("ts", ""),
            "Operation": entry.get("operation", ""),
            "Detail": entry.get("error_detail", ""),
        }
        for entry in entries
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_tool(st: Any, client: ApiClient, tool: str) -> None:
    """Render the full panel (credentials, status/test, errors) for one tool."""
    section_header(TOOL_TITLES.get(tool, tool.title()), module=MODULE)
    _render_status_and_test(st, client, tool)
    _render_credentials_form(st, client, tool)
    _render_error_log(st, client, tool)


def _main() -> None:
    """Render the Frontend Integration screen."""
    import streamlit as st

    st.set_page_config(
        page_title="IntelliKnow KMS — Frontend Integration", page_icon="🔌"
    )
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Frontend Integration", module=MODULE)
    st.caption(
        "Manage Telegram, Microsoft Teams, and WhatsApp credentials, check "
        "connectivity, and review recent integration errors."
    )

    client = ApiClient()
    try:
        tabs = st.tabs([TOOL_TITLES.get(tool, tool.title()) for tool in TOOLS])
        for tab, tool in zip(tabs, TOOLS):
            with tab:
                _render_tool(st, client, tool)
    finally:
        client.close()


if __name__ == "__main__":
    _main()
