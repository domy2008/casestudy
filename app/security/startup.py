"""Startup credential loading for the IntelliKnow KMS.

Requirement 11.4 requires that on startup the system load the Frontend_Tool
API keys (Telegram, Teams, WhatsApp) and the DashScope AK/SK from the
Credential_Store as
the *primary* credential source. Requirement 11.5 requires that any credential
that is missing or cannot be loaded is reported by field *name* only (never its
value) and that the dependent integration is marked unavailable.

:func:`load_startup_credentials` performs that work:

* It reads each integration's credentials from the
  :class:`~app.security.credentials.CredentialStore` and validates them with the
  pure :func:`~app.security.credentials.validate_credentials` function (reused,
  never re-implemented).
* For any integration whose required fields are missing, empty, or
  unloadable, it logs an error naming the offending field(s) — and only the
  field names — and records the integration as unavailable.
* Frontend integrations (Telegram, Teams) additionally depend on the DashScope
  AI backend; if DashScope credentials are unavailable, every frontend tool is
  marked unavailable because it cannot serve answers.
* The resulting integration availability is persisted via the injected
  :class:`~app.kb.store.IntegrationRepository` (status ``Disconnected`` +
  ``active = 0`` when unavailable, ``active = 1`` when available), matching the
  design's "marked unavailable" behavior.
* Successfully-loaded credential values are handed to an optional
  :class:`~app.security.logfilter.RedactingFilter` so they are scrubbed from
  all subsequent log output (Req 11.3).

This module is called from :mod:`app.main` during application startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.security.credentials import (
    CREDENTIAL_SCHEMAS,
    CredentialStore,
    validate_credentials,
)
from app.security.logfilter import RedactingFilter

__all__ = ["StartupCredentialResult", "load_startup_credentials"]

# Module logger. The RedactingFilter installed on the root logger scrubs any
# credential value; this module additionally only ever logs field *names*.
_logger = logging.getLogger(__name__)

# Frontend_Tool integrations that surface as rows in the `integrations` table
# and can be marked available/unavailable (Req 11.5).
FRONTEND_TOOLS: tuple[str, ...] = ("telegram", "teams", "whatsapp")

# The AI integration every frontend tool depends on to answer queries. It is
# not a frontend tool itself, so it has no `integrations` row, but its absence
# renders the frontend tools unable to serve responses.
AI_INTEGRATION: str = "dashscope"


@dataclass
class StartupCredentialResult:
    """Outcome of loading credentials at startup.

    Attributes:
        loaded: Mapping of integration key to the validated field values that
            were successfully loaded from the Credential_Store.
        unavailable: The set of Frontend_Tool keys marked unavailable because
            their own credentials or the shared DashScope credentials were
            missing/unloadable.
        missing: Mapping of integration key to the list of field *names* that
            were missing, empty, or format-invalid. Values are never included
            (Req 11.5).
    """

    loaded: dict[str, dict[str, str]] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    missing: dict[str, list[str]] = field(default_factory=dict)


def _load_integration(
    store: CredentialStore, integration: str
) -> tuple[dict[str, str] | None, list[str]]:
    """Load and validate one integration's credentials.

    Args:
        store: The Credential_Store to read from.
        integration: The integration key to load.

    Returns:
        A ``(fields, missing_field_names)`` tuple. ``fields`` is the loaded
        mapping when every required field is present and valid, otherwise
        ``None``. ``missing_field_names`` lists the offending field names (empty
        when valid). Any load/decrypt error is treated as "all required fields
        unloadable".
    """
    try:
        fields = store.load(integration)
    except Exception:
        # Unloadable (e.g. decrypt failure): report all required fields by name.
        return None, list(CREDENTIAL_SCHEMAS[integration].keys())

    errors = validate_credentials(integration, fields or {})
    if errors:
        return None, [e.field for e in errors]
    return fields, []


def load_startup_credentials(
    store: CredentialStore,
    integration_repo: "object",
    *,
    redacting_filter: RedactingFilter | None = None,
    logger: logging.Logger | None = None,
) -> StartupCredentialResult:
    """Load startup credentials and mark unavailable integrations.

    Reads Telegram, Teams, and DashScope credentials from the Credential_Store
    (the primary source, Req 11.4). For any integration whose required fields
    are missing/empty/unloadable, logs an error naming only the affected
    field(s) (Req 11.5) and marks the dependent Frontend_Tool(s) unavailable.
    Frontend tools also depend on DashScope, so missing DashScope credentials
    mark every frontend tool unavailable.

    Availability is persisted through ``integration_repo`` (an
    :class:`~app.kb.store.IntegrationRepository`): unavailable tools are set to
    status ``Disconnected`` with ``active = 0``; available tools are set
    ``active = 1``. Loaded credential values are registered with
    ``redacting_filter`` (if given) so they are scrubbed from logs (Req 11.3).

    Args:
        store: The Credential_Store to read from.
        integration_repo: Repository exposing ``set_status`` / ``set_active``
            for persisting per-tool availability.
        redacting_filter: Optional log-redaction filter to seed with the loaded
            secret values.
        logger: Optional logger to use; defaults to this module's logger.

    Returns:
        A :class:`StartupCredentialResult` summarizing what loaded, what is
        missing (by field name), and which frontend tools are unavailable.
    """
    log = logger or _logger
    result = StartupCredentialResult()

    # Load every integration (frontend tools + the shared AI backend).
    for integration in (*FRONTEND_TOOLS, AI_INTEGRATION):
        fields, missing = _load_integration(store, integration)
        if fields is not None:
            result.loaded[integration] = fields
        else:
            result.missing[integration] = missing
            # Log by field NAME only — never the value (Req 11.5).
            log.error(
                "Startup credential load failed for integration '%s': "
                "missing or unloadable field(s): %s",
                integration,
                ", ".join(missing) if missing else "(unknown)",
            )

    ai_available = AI_INTEGRATION in result.loaded

    # Determine and persist frontend-tool availability.
    for tool in FRONTEND_TOOLS:
        tool_ready = tool in result.loaded and ai_available
        if tool_ready:
            _mark_available(integration_repo, tool)
        else:
            result.unavailable.add(tool)
            _mark_unavailable(integration_repo, tool)
            if tool in result.loaded and not ai_available:
                # Tool's own creds are fine; it is unavailable solely because
                # the shared DashScope backend could not be loaded.
                log.error(
                    "Integration '%s' marked unavailable: DashScope "
                    "credentials are unavailable.",
                    tool,
                )

    # Seed the redaction filter with the loaded secret values (Req 11.3).
    if redacting_filter is not None:
        values = [
            value
            for fields in result.loaded.values()
            for value in fields.values()
            if isinstance(value, str) and value
        ]
        if values:
            redacting_filter.add_secrets(values)

    return result


def _mark_unavailable(integration_repo: "object", tool: str) -> None:
    """Persist a Frontend_Tool as unavailable (Disconnected, inactive)."""
    try:
        integration_repo.set_status(tool, "Disconnected", active=False)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive; storage failure
        _logger.error("Failed to mark integration '%s' unavailable.", tool)


def _mark_available(integration_repo: "object", tool: str) -> None:
    """Persist a Frontend_Tool as available/active (status left to the monitor).

    The 60s status monitor performs the first live connectivity check and sets
    Connected/Error; startup only records that the tool is configured/active.
    """
    try:
        integration_repo.set_active(tool, True)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive; storage failure
        _logger.error("Failed to mark integration '%s' available.", tool)
