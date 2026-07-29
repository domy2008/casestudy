"""Admin REST API consumed by the Streamlit admin UI.

This module assembles the FastAPI router tree that the Streamlit Admin_UI
calls over HTTP. It is deliberately organized as a top-level :data:`router`
that *includes* one focused sub-router per functional area, so later tasks can
append their own sub-routers (documents, spaces, settings/analytics/dashboard)
without touching existing code or colliding on endpoint definitions.

Currently implemented area:

* **Integrations** (:data:`integrations_router`) — credential read/save,
  end-to-end connectivity testing, and recent error-log listing for each
  Frontend_Tool. Covers Req 1.2/1.4/1.5/1.6/1.7 (validated credential save with
  per-field errors and a saved confirmation), Req 11.2/11.6 (masked reads that
  reveal at most the last four characters), Req 3.2/3.5 (a 30-second-capped
  connectivity check reporting success or a failure/timeout detail), and
  Req 3.4 (the 50 most recent integration error-log entries).

### Extension points for later tasks (13.2 / 13.3 / 13.7)

Add a new sub-router near the bottom of this file (mirroring
:data:`integrations_router`), give it its own tag and dependency wiring, and
register it with ``router.include_router(...)`` in :func:`build_router` /
alongside the existing ``include_router`` call. Do not add unrelated endpoints
to :data:`integrations_router`.

### Dependency injection

Every collaborator (the :class:`~app.security.credentials.CredentialStore`, the
SQLite-backed repositories, and the connectivity checker used by the test
endpoint) is provided through a FastAPI dependency. Tests override these via
``app.dependency_overrides`` to inject fakes and a temporary database, and no
secret is ever hardcoded here — the master key and paths come from
:class:`app.config.Settings`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.models import ConnectivityResult
from app.security.credentials import (
    CREDENTIAL_SCHEMAS,
    CredentialStore,
    CredentialValidationError,
)

__all__ = [
    "ConnectivityChecker",
    "integrations_router",
    "router",
    "build_router",
    "get_settings_dependency",
    "get_connection",
    "get_credential_store",
    "get_integration_repo",
    "get_error_log_repo",
    "get_connectivity_checker",
]

# Frontend_Tools that support connectivity testing and status/error tracking
# (the credential-only "dashscope" integration is not a Frontend_Tool).
FRONTEND_TOOLS: frozenset[str] = frozenset({"telegram", "teams"})

# Hard cap for an end-to-end connectivity check (Req 3.2, 3.5).
TEST_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Connectivity checker seam (test endpoint)
# ---------------------------------------------------------------------------


@runtime_checkable
class ConnectivityChecker(Protocol):
    """Runs an end-to-end connectivity check for a Frontend_Tool.

    Abstracts the concrete adapter/status-monitor wiring so the ``test``
    endpoint depends only on this small seam. Production supplies an
    implementation backed by the frontend adapters; tests inject a fake that
    returns a canned :class:`~app.core.models.ConnectivityResult` (including a
    slow implementation to exercise the 30-second cap).
    """

    async def check(self, tool: str) -> ConnectivityResult:
        """Perform the connectivity check for ``tool`` and return its result.

        Args:
            tool: The Frontend_Tool to check (e.g. ``"telegram"``/``"teams"``).

        Returns:
            A :class:`~app.core.models.ConnectivityResult` describing the
            outcome (success, or failure/timeout with detail).
        """
        ...


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class FieldErrorModel(BaseModel):
    """One per-field validation error surfaced to the Admin_UI."""

    field: str = Field(..., description="Name of the offending credential field.")
    message: str = Field(..., description="Why the field is invalid.")


class MaskedCredentialsResponse(BaseModel):
    """Masked credential read for a Frontend_Tool (Req 11.2, 11.6)."""

    tool: str = Field(..., description="The integration key.")
    configured: bool = Field(
        ..., description="True when credentials are currently stored."
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Field name → masked value (≤4 trailing chars revealed).",
    )


class SaveCredentialsResponse(BaseModel):
    """Confirmation returned after a successful credential save (Req 1.7)."""

    tool: str = Field(..., description="The integration key.")
    saved: bool = Field(True, description="Always True on a successful save.")
    message: str = Field(..., description="Human-readable saved confirmation.")
    credentials: dict[str, str] = Field(
        default_factory=dict, description="The newly stored values, masked."
    )


class ValidationErrorResponse(BaseModel):
    """400 body listing every offending field (Req 1.4, 1.6)."""

    tool: str = Field(..., description="The integration key.")
    saved: bool = Field(False, description="Always False when validation fails.")
    message: str = Field(..., description="Summary message for the Admin_UI.")
    errors: list[FieldErrorModel] = Field(
        default_factory=list, description="One entry per invalid/missing field."
    )


class ConnectivityResultResponse(BaseModel):
    """Result of an end-to-end connectivity test (Req 3.2, 3.5)."""

    tool: str = Field(..., description="The Frontend_Tool checked.")
    ok: bool = Field(..., description="True when the check succeeded end to end.")
    detail: str = Field("", description="Success detail or failure/timeout reason.")
    timed_out: bool = Field(
        False, description="True when the check exceeded the 30-second cap."
    )
    checked_at: datetime | None = Field(
        None, description="When the check completed."
    )


class ErrorLogEntryResponse(BaseModel):
    """A single integration error-log entry (Req 3.3, 3.4)."""

    id: int
    ts: str
    tool: str
    operation: str
    error_detail: str


# ---------------------------------------------------------------------------
# Dependencies (overridable in tests via app.dependency_overrides)
# ---------------------------------------------------------------------------


def get_settings_dependency() -> Settings:
    """Provide the process-wide settings snapshot.

    Returns:
        The cached :class:`~app.config.Settings` instance.
    """
    return get_settings()


def get_connection(
    settings: Settings = Depends(get_settings_dependency),
) -> sqlite3.Connection:
    """Provide a SQLite connection for repository-backed endpoints.

    The default opens a connection against the configured database. Tests
    override this dependency to hand back a temp-DB connection, so this default
    is never exercised under test.

    Args:
        settings: The active settings, providing the database path.

    Returns:
        An open :class:`sqlite3.Connection` with row access configured.
    """
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_credential_store(
    settings: Settings = Depends(get_settings_dependency),
) -> CredentialStore:
    """Provide the :class:`~app.security.credentials.CredentialStore`.

    Args:
        settings: The active settings, providing the store path and master key.

    Returns:
        A ready-to-use credential store bound to ``settings``.
    """
    return CredentialStore(settings)


def get_integration_repo(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the per-tool integration status repository.

    Imported lazily to keep this module importable in minimal contexts and to
    let tests override the dependency without importing the data layer.

    Args:
        conn: The injected SQLite connection.

    Returns:
        An ``IntegrationRepository`` bound to ``conn``.
    """
    from app.kb.store import IntegrationRepository

    return IntegrationRepository(conn)


def get_error_log_repo(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the integration error-log repository.

    Args:
        conn: The injected SQLite connection.

    Returns:
        An ``IntegrationErrorLogRepository`` bound to ``conn``.
    """
    from app.kb.store import IntegrationErrorLogRepository

    return IntegrationErrorLogRepository(conn)


def get_connectivity_checker() -> ConnectivityChecker:
    """Provide the connectivity checker used by the test endpoint.

    No production checker is wired yet (the status monitor lands in a later
    task), so the default reports the feature as unconfigured. Tests override
    this dependency with a fake checker.

    Raises:
        HTTPException: Always (503), until a real checker is wired in.
    """
    raise HTTPException(
        status_code=503,
        detail="Connectivity checking is not configured on this server.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_known_integration(tool: str) -> None:
    """Reject unknown credential integrations with a 404.

    Args:
        tool: The integration key from the path.

    Raises:
        HTTPException: 404 when ``tool`` has no credential schema.
    """
    if tool not in CREDENTIAL_SCHEMAS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown integration {tool!r}. "
                f"Known: {', '.join(sorted(CREDENTIAL_SCHEMAS))}."
            ),
        )


def _require_frontend_tool(tool: str) -> None:
    """Reject non-Frontend_Tool identifiers with a 404.

    Args:
        tool: The tool key from the path.

    Raises:
        HTTPException: 404 when ``tool`` is not a testable Frontend_Tool.
    """
    if tool not in FRONTEND_TOOLS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown Frontend_Tool {tool!r}. "
                f"Known: {', '.join(sorted(FRONTEND_TOOLS))}."
            ),
        )


# ---------------------------------------------------------------------------
# Integrations sub-router
# ---------------------------------------------------------------------------

integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])


@integrations_router.get(
    "/{tool}/credentials",
    response_model=MaskedCredentialsResponse,
    summary="Read masked integration credentials",
)
def read_credentials(
    tool: str = Path(..., description="Integration key (telegram/teams/dashscope)."),
    store: CredentialStore = Depends(get_credential_store),
) -> MaskedCredentialsResponse:
    """Return the stored credentials for ``tool`` with every value masked.

    Values are masked so at most the last four characters are ever revealed
    (Req 11.2, 11.6). When nothing is stored, ``configured`` is ``False`` and
    ``credentials`` is empty.
    """
    _require_known_integration(tool)
    masked = store.masked(tool)
    return MaskedCredentialsResponse(
        tool=tool, configured=bool(masked), credentials=masked
    )


@integrations_router.put(
    "/{tool}/credentials",
    response_model=SaveCredentialsResponse,
    responses={400: {"model": ValidationErrorResponse}},
    summary="Validate and save integration credentials",
)
def save_credentials(
    tool: str = Path(..., description="Integration key (telegram/teams/dashscope)."),
    fields: dict[str, Any] = Body(
        ...,
        description="Submitted credential field values for this integration.",
    ),
    store: CredentialStore = Depends(get_credential_store),
) -> SaveCredentialsResponse:
    """Validate a credential submission and, if valid, store it atomically.

    Validation happens before any write. On failure the store is left
    unchanged and a 400 is returned whose body lists exactly one error per
    missing/empty/format-invalid field (Req 1.4, 1.6). On success the previous
    credentials are fully replaced (Req 1.5) and a saved confirmation with the
    freshly masked values is returned (Req 1.7).
    """
    _require_known_integration(tool)
    try:
        store.save(tool, fields)
    except CredentialValidationError as exc:
        body = ValidationErrorResponse(
            tool=tool,
            message="Credential submission was rejected; no changes were saved.",
            errors=[
                FieldErrorModel(field=e.field, message=e.message) for e in exc.errors
            ],
        )
        raise HTTPException(status_code=400, detail=body.model_dump())

    return SaveCredentialsResponse(
        tool=tool,
        saved=True,
        message=f"Credentials for {tool} were saved.",
        credentials=store.masked(tool),
    )


@integrations_router.post(
    "/{tool}/test",
    response_model=ConnectivityResultResponse,
    summary="Run an end-to-end connectivity check (30s cap)",
)
async def test_integration(
    tool: str = Path(..., description="Frontend_Tool key (telegram/teams)."),
    checker: ConnectivityChecker = Depends(get_connectivity_checker),
    integration_repo=Depends(get_integration_repo),
    error_repo=Depends(get_error_log_repo),
) -> ConnectivityResultResponse:
    """Execute a connectivity check for ``tool``, capped at 30 seconds.

    The check runs the injected connectivity checker under an
    :func:`asyncio.wait_for` deadline. If it does not finish within
    :data:`TEST_TIMEOUT_SECONDS`, it is terminated and a timeout failure is
    reported (Req 3.5). Either way the tool's stored status is updated
    (Connected on success, Error otherwise) and any failure is written to the
    integration error log (Req 3.3); the result is returned for display
    (Req 3.2).
    """
    _require_frontend_tool(tool)

    try:
        result = await asyncio.wait_for(
            checker.check(tool), timeout=TEST_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        result = ConnectivityResult(
            tool=tool,
            ok=False,
            detail=(
                f"Connectivity check timed out after "
                f"{int(TEST_TIMEOUT_SECONDS)} seconds."
            ),
            timed_out=True,
            checked_at=datetime.now(),
        )

    if result.checked_at is None:
        result.checked_at = datetime.now()

    # Reflect the outcome in the stored status and error log. Guard so a
    # bookkeeping failure never masks the actual test result.
    try:
        integration_repo.set_status(
            tool,
            "Connected" if result.ok else "Error",
            last_check_ts=result.checked_at,
        )
        if not result.ok:
            error_repo.insert(
                tool=tool,
                operation="test",
                error_detail=result.detail or "connectivity check failed",
                ts=result.checked_at,
            )
    except Exception:  # noqa: BLE001 - status/log bookkeeping is best-effort
        pass

    return ConnectivityResultResponse(
        tool=result.tool,
        ok=result.ok,
        detail=result.detail,
        timed_out=result.timed_out,
        checked_at=result.checked_at,
    )


@integrations_router.get(
    "/{tool}/errors",
    response_model=list[ErrorLogEntryResponse],
    summary="List the most recent integration error-log entries",
)
def list_integration_errors(
    tool: str = Path(..., description="Frontend_Tool key (telegram/teams)."),
    limit: int = Query(
        50, ge=1, le=500, description="Maximum entries to return (newest first)."
    ),
    error_repo=Depends(get_error_log_repo),
) -> list[ErrorLogEntryResponse]:
    """Return the ``limit`` most recent error-log entries for ``tool``.

    Entries are ordered newest to oldest and returned regardless of the tool's
    current status (Req 3.4). ``limit`` defaults to 50.
    """
    _require_frontend_tool(tool)
    rows = error_repo.list_recent(tool=tool, limit=limit)
    return [ErrorLogEntryResponse(**row) for row in rows]


# ---------------------------------------------------------------------------
# Top-level router assembly
# ---------------------------------------------------------------------------


def build_router() -> APIRouter:
    """Build the top-level admin router with all area sub-routers mounted.

    Later tasks add their sub-routers here (documents, spaces,
    settings/analytics/dashboard) by defining them above and including them in
    this function.

    Returns:
        The assembled :class:`fastapi.APIRouter`.
    """
    admin = APIRouter()
    admin.include_router(integrations_router)
    return admin


# Importable, ready-to-mount router (e.g. ``app.include_router(admin_router)``).
router = build_router()
