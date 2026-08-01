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
import base64
import binascii
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.models import ConnectivityResult

logger = logging.getLogger(__name__)
from app.security.credentials import (
    CREDENTIAL_SCHEMAS,
    CredentialStore,
    CredentialValidationError,
)

__all__ = [
    "ConnectivityChecker",
    "integrations_router",
    "documents_router",
    "spaces_router",
    "system_router",
    "router",
    "build_router",
    "get_settings_dependency",
    "get_connection",
    "get_credential_store",
    "get_integration_repo",
    "get_error_log_repo",
    "get_connectivity_checker",
    "get_document_repo",
    "get_intent_space_repo",
    "get_settings_repo",
    "get_document_service",
    "get_document_processor",
    "get_search_index",
    "get_analytics_service",
    "validate_space_config",
    "validate_threshold",
    "SPACE_NAME_MAX",
    "SPACE_DESCRIPTION_MAX",
    "SPACE_KEYWORDS_MAX",
    "SPACE_KEYWORD_MAX",
    "THRESHOLD_MIN",
    "THRESHOLD_MAX",
    "DOCUMENT_STATUSES_REPORTED",
]

# Frontend_Tools that support connectivity testing and status/error tracking
# (the credential-only "dashscope" integration is not a Frontend_Tool).
FRONTEND_TOOLS: frozenset[str] = frozenset({"telegram", "teams", "whatsapp"})

# Hard cap for an end-to-end connectivity check (Req 3.2, 3.5).
TEST_TIMEOUT_SECONDS: float = 30.0

# Intent_Space configuration bounds (Req 6.2, 6.5).
SPACE_NAME_MIN: int = 1
SPACE_NAME_MAX: int = 50
SPACE_DESCRIPTION_MAX: int = 500
SPACE_KEYWORDS_MAX: int = 50
SPACE_KEYWORD_MIN: int = 1
SPACE_KEYWORD_MAX: int = 50

# Confidence_Threshold bounds (Req 7.4, 7.9).
THRESHOLD_MIN: int = 0
THRESHOLD_MAX: int = 100

# Document statuses the Dashboard always reports a count for (Req 9.5), so a
# status with zero documents still surfaces as ``0``.
DOCUMENT_STATUSES_REPORTED: tuple[str, ...] = ("Pending", "Processed", "Error")

# Rolling window (hours) for the Dashboard's query-activity section (Req 9.5).
DASHBOARD_QUERY_WINDOW_HOURS: int = 24


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
) -> Iterator[sqlite3.Connection]:
    """Provide a SQLite connection for repository-backed endpoints.

    Opens a fresh per-request connection against the configured database and
    closes it once the request finishes.

    ``check_same_thread=False`` is required because FastAPI resolves this sync
    dependency on a worker thread, while ``async def`` path operations run
    their body on the event-loop thread. Without it, any ``async`` endpoint
    (e.g. the AI keyword-suggestion route) touching this connection would raise
    ``sqlite3.ProgrammingError`` (created in a different thread) → HTTP 500. The
    connection is never shared between concurrent requests, so cross-thread use
    is safe here.

    Tests override this dependency to hand back a temp-DB connection, so this
    default is never exercised under test.

    Args:
        settings: The active settings, providing the database path.

    Yields:
        An open :class:`sqlite3.Connection` with row access configured; it is
        closed when the request completes.
    """
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


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


def get_document_repo(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the documents repository.

    Args:
        conn: The injected SQLite connection.

    Returns:
        A ``DocumentRepository`` bound to ``conn``.
    """
    from app.kb.store import DocumentRepository

    return DocumentRepository(conn)


def get_intent_space_repo(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the Intent_Space + keyword repository.

    Args:
        conn: The injected SQLite connection.

    Returns:
        An ``IntentSpaceRepository`` bound to ``conn``.
    """
    from app.kb.store import IntentSpaceRepository

    return IntentSpaceRepository(conn)


def get_settings_repo(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the key/value settings repository.

    Args:
        conn: The injected SQLite connection.

    Returns:
        A ``SettingsRepository`` bound to ``conn``.
    """
    from app.kb.store import SettingsRepository

    return SettingsRepository(conn)


def get_search_index(
    conn: sqlite3.Connection = Depends(get_connection),
    settings: Settings = Depends(get_settings_dependency),
):
    """Provide the FAISS-backed :class:`~app.kb.search.SearchIndex`.

    Args:
        conn: The injected SQLite connection (source of truth for embeddings).
        settings: The active settings, providing the FAISS index directory.

    Returns:
        A ``SearchIndex`` bound to ``conn`` and ``settings``.
    """
    from app.kb.search import SearchIndex

    return SearchIndex(conn, settings)


def get_document_processor():
    """Provide the background document processor, if one is wired.

    The processor owns the AI mocking seam and is wired in ``main.py`` (a later
    task). Until then — and in tests unless overridden — this returns ``None``
    and the document endpoints simply skip background scheduling. Tests override
    this dependency with a fake exposing an awaitable ``process(document_id)``
    to assert that (re)processing is scheduled.

    Returns:
        ``None`` by default (no processor wired).
    """
    return None


def get_document_service(
    conn: sqlite3.Connection = Depends(get_connection),
    settings: Settings = Depends(get_settings_dependency),
    search_index=Depends(get_search_index),
    processor=Depends(get_document_processor),
):
    """Provide the document lifecycle service (upload/delete/reassign).

    Args:
        conn: The injected SQLite connection.
        settings: The active settings (uploads/FAISS directories).
        search_index: The shared FAISS index manager, so delete/reassign rebuild
            the same on-disk indexes used elsewhere in the request.
        processor: Optional background processor enabling the Update path.

    Returns:
        A ready-to-use ``DocumentLifecycleService``.
    """
    from app.kb.service import DocumentLifecycleService

    return DocumentLifecycleService(
        conn,
        settings=settings,
        search_index=search_index,
        processor=processor,
    )


def get_analytics_service(
    conn: sqlite3.Connection = Depends(get_connection),
):
    """Provide the analytics service (history, metrics, accuracy, export).

    Args:
        conn: The injected SQLite connection.

    Returns:
        An ``AnalyticsService`` bound to ``conn``.
    """
    from app.analytics.service import AnalyticsService

    return AnalyticsService(conn)


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


#: Canned query pushed through the real pipeline by the integration test
#: endpoint, so the Admin test button verifies the full query flow (classify →
#: retrieve → generate), not just tool-API reachability.
SAMPLE_TEST_QUERY = "Integration test: what topics does the knowledge base cover?"

#: Tool label recorded in the Query_Log for sample test queries, keeping them
#: distinguishable from real End_User traffic in analytics.
SAMPLE_TEST_TOOL_SUFFIX = "-test"


async def _run_sample_query(request: Request, tool: str) -> str:
    """Send :data:`SAMPLE_TEST_QUERY` through the shared query pipeline.

    Args:
        request: The current request, source of ``app.state.orchestrator``.
        tool: The Frontend_Tool under test; logged as ``{tool}-test``.

    Returns:
        A one-line human-readable outcome to append to the test detail.

    Raises:
        RuntimeError: If the pipeline answers with status ``"failed"``.
    """
    from app.core.models import QueryContext

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return "Sample query skipped: query pipeline not started."

    ctx = QueryContext(
        query_id=str(uuid.uuid4()),
        tool=f"{tool}{SAMPLE_TEST_TOOL_SUFFIX}",
        conversation_ref={"channel": "integration_test"},
        text=SAMPLE_TEST_QUERY,
        received_at=datetime.now(),
    )
    started = time.monotonic()
    response = await orchestrator.handle_query(ctx)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status == "failed":
        raise RuntimeError(f"Sample query failed after {elapsed_ms} ms.")
    return f"Sample query answered in {elapsed_ms} ms (status: {response.status})."


@integrations_router.post(
    "/{tool}/test",
    response_model=ConnectivityResultResponse,
    summary="Run an end-to-end test: connectivity check + sample query (30s cap)",
)
async def test_integration(
    request: Request,
    tool: str = Path(..., description="Frontend_Tool key (telegram/teams)."),
    checker: ConnectivityChecker = Depends(get_connectivity_checker),
    integration_repo=Depends(get_integration_repo),
    error_repo=Depends(get_error_log_repo),
) -> ConnectivityResultResponse:
    """Execute a connectivity check for ``tool``, capped at 30 seconds.

    The check runs the injected connectivity checker under an
    :func:`asyncio.wait_for` deadline. If it does not finish within
    :data:`TEST_TIMEOUT_SECONDS`, it is terminated and a timeout failure is
    reported (Req 3.5). On a successful check, a sample query is additionally
    sent through the shared query pipeline so the test verifies the full
    end-to-end flow, and its outcome is appended to the result detail. Either
    way the tool's stored status is updated (Connected on success, Error
    otherwise) and any failure is written to the integration error log
    (Req 3.3); the result is returned for display (Req 3.2).
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

    # On a reachable tool, additionally verify the query pipeline end to end
    # with a sample query (kept under the same overall deadline).
    if result.ok:
        try:
            sample_detail = await asyncio.wait_for(
                _run_sample_query(request, tool), timeout=TEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            result.ok = False
            sample_detail = (
                f"Sample query timed out after {int(TEST_TIMEOUT_SECONDS)} seconds."
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via the result detail
            result.ok = False
            sample_detail = str(exc)
        result.detail = f"{result.detail} {sample_detail}".strip()

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
# Validation helpers (pure — reused by endpoints and property tests)
# ---------------------------------------------------------------------------


def validate_space_config(
    name: str | None,
    description: str | None,
    keywords: list[str] | None,
) -> list[FieldErrorModel]:
    """Validate an Intent_Space configuration, one error per offending field.

    Pure function (no I/O). A configuration is valid — an empty list — **iff**
    the name is 1–50 characters after trimming, the description is at most 500
    characters, and there are at most 50 keywords each 1–50 characters long
    (Req 6.2, 6.5). Case-insensitive name uniqueness (Req 6.6) is a stateful
    concern checked against the store by the endpoints, not here.

    Args:
        name: The proposed space name.
        description: The proposed description (``None`` treated as empty).
        keywords: The proposed keyword list (``None`` treated as empty).

    Returns:
        A list of :class:`FieldErrorModel`; empty when the configuration is
        acceptable.
    """
    errors: list[FieldErrorModel] = []

    trimmed = (name or "").strip()
    if len(trimmed) < SPACE_NAME_MIN:
        errors.append(
            FieldErrorModel(field="name", message="Name is required.")
        )
    elif len(trimmed) > SPACE_NAME_MAX:
        errors.append(
            FieldErrorModel(
                field="name",
                message=(
                    f"Name must be at most {SPACE_NAME_MAX} characters "
                    f"(got {len(trimmed)})."
                ),
            )
        )

    if len(description or "") > SPACE_DESCRIPTION_MAX:
        errors.append(
            FieldErrorModel(
                field="description",
                message=(
                    f"Description must be at most {SPACE_DESCRIPTION_MAX} "
                    f"characters (got {len(description or '')})."
                ),
            )
        )

    kw_list = list(keywords or [])
    if len(kw_list) > SPACE_KEYWORDS_MAX:
        errors.append(
            FieldErrorModel(
                field="keywords",
                message=(
                    f"At most {SPACE_KEYWORDS_MAX} keywords are allowed "
                    f"(got {len(kw_list)})."
                ),
            )
        )
    for kw in kw_list:
        length = len(kw)
        if length < SPACE_KEYWORD_MIN or length > SPACE_KEYWORD_MAX:
            errors.append(
                FieldErrorModel(
                    field="keywords",
                    message=(
                        f"Keyword {kw!r} must be between {SPACE_KEYWORD_MIN} "
                        f"and {SPACE_KEYWORD_MAX} characters (got {length})."
                    ),
                )
            )

    return errors


def validate_threshold(raw: Any) -> tuple[bool, int | None]:
    """Validate a Confidence_Threshold submission (Req 7.4, 7.9).

    Pure function. The value is accepted **iff** it is a whole number in the
    inclusive range ``[0, 100]``. Booleans are rejected (``bool`` is an ``int``
    subclass), non-integer floats are rejected, and numeric strings are
    accepted when they parse to an in-range integer.

    Args:
        raw: The submitted threshold value.

    Returns:
        A ``(ok, value)`` pair: on success ``ok`` is ``True`` and ``value`` is
        the accepted integer; on failure ``ok`` is ``False`` and ``value`` is
        ``None``.
    """
    if isinstance(raw, bool):
        return False, None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return False, None
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return False, None
    else:
        return False, None

    if value < THRESHOLD_MIN or value > THRESHOLD_MAX:
        return False, None
    return True, value


def _validation_error(tool_or_area: str, errors: list[FieldErrorModel]) -> HTTPException:
    """Build a 400 :class:`HTTPException` listing every offending field.

    The body shape mirrors the integrations validation error so the Admin_UI's
    shared error extractor renders per-field messages uniformly.

    Args:
        tool_or_area: The area key echoed back (e.g. ``"spaces"``).
        errors: The field errors to surface.

    Returns:
        A ready-to-raise :class:`HTTPException` with status 400.
    """
    body = ValidationErrorResponse(
        tool=tool_or_area,
        message="Submission was rejected; no changes were saved.",
        errors=errors,
    )
    return HTTPException(status_code=400, detail=body.model_dump())


def _space_view(
    space: dict[str, Any],
    *,
    document_count: int,
    accuracy_rate: float | None,
    keywords: list[str],
) -> dict[str, Any]:
    """Render a space row plus computed fields as the API's space view.

    Args:
        space: The raw ``intent_spaces`` row.
        document_count: Number of documents currently associated with the space.
        accuracy_rate: Classification accuracy percentage, or ``None`` for N/A.
        keywords: The space's keyword list.

    Returns:
        A JSON-serializable dict consumed by the Admin_UI space cards.
    """
    return {
        "id": int(space["id"]),
        "name": space["name"],
        "description": space.get("description", "") or "",
        "is_general": bool(space.get("is_general", 0)),
        "is_default": bool(space.get("is_default", 0)),
        "document_count": document_count,
        "accuracy_rate": accuracy_rate,
        "keywords": keywords,
    }


def _query_log_view(entry: Any) -> dict[str, Any]:
    """Render a :class:`~app.core.models.QueryLogEntry` as a JSON dict.

    Args:
        entry: The query-log entry to serialize.

    Returns:
        A dict carrying every field the Admin_UI history table reads.
    """
    ts = entry.ts
    return {
        "id": entry.id,
        "ts": ts if isinstance(ts, str) else (ts.isoformat(sep=" ") if ts else None),
        "query_text": entry.query_text,
        "detected_space_id": entry.detected_space_id,
        "confidence": entry.confidence,
        "response_status": entry.response_status,
        "latency_ms": entry.latency_ms,
        "tool": entry.tool,
        "verified_space_id": entry.verified_space_id,
    }


# ---------------------------------------------------------------------------
# Documents sub-router (Task 13.2)
# ---------------------------------------------------------------------------

documents_router = APIRouter(prefix="/documents", tags=["documents"])


class UploadDocumentRequest(BaseModel):
    """Body for ``POST /documents`` (the KB screen's base64 JSON upload)."""

    name: str = Field(..., description="Original file name (its extension sets the format).")
    format: str | None = Field(
        None, description="Client-declared format token (informational)."
    )
    size_bytes: int | None = Field(
        None, ge=0, description="Declared size for the size gate; defaults to the decoded length."
    )
    content_b64: str = Field(..., description="Base64-encoded file bytes.")
    space_id: int | None = Field(
        None, description="Optional Intent_Space to associate (defaults to General)."
    )


@documents_router.post("", summary="Accept and store a document upload")
@documents_router.post("/", include_in_schema=False)
def create_document(
    payload: UploadDocumentRequest,
    background_tasks: BackgroundTasks,
    service=Depends(get_document_service),
    processor=Depends(get_document_processor),
    doc_repo=Depends(get_document_repo),
) -> dict[str, Any]:
    """Validate an upload, store it Pending, and schedule background processing.

    The base64 content is decoded and handed to
    :meth:`~app.kb.service.DocumentLifecycleService.accept_upload`, which
    validates the format and size (≤50 MB) before writing anything (Req
    4.1/4.2/4.3). On acceptance a ``Pending`` document row is created and, when
    a background processor is wired, ingestion is scheduled so it starts shortly
    after upload (Req 5.1). A rejected upload persists nothing and returns a 400
    whose message names the supported formats / maximum size.
    """
    from app.kb.service import UploadRejected

    try:
        content = base64.b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=400, detail="content_b64 is not valid base64."
        )

    try:
        document_id = service.accept_upload(
            payload.name,
            content,
            declared_size=payload.size_bytes,
            space_id=payload.space_id,
        )
    except UploadRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "saved": False,
                "message": str(exc),
                "errors": [
                    {"field": e.field, "message": e.message} for e in exc.errors
                ],
            },
        )

    if processor is not None:
        background_tasks.add_task(processor.process, document_id)

    created = doc_repo.get(document_id) or {"id": document_id, "status": "Pending"}
    return created


class SuggestSpaceRequest(BaseModel):
    """Body for ``POST /documents/suggest-space`` (pre-upload suggestion)."""

    name: str = Field(..., description="File name; its extension picks the loader.")
    content_b64: str = Field(..., description="Base64-encoded file bytes.")


#: How much extracted text is fed to the space-suggestion prompt.
SPACE_SUGGESTION_EXCERPT_CHARS = 1500


@documents_router.post(
    "/suggest-space", summary="AI-suggest the Intent_Space for a document"
)
async def suggest_document_space(
    request: Request,
    payload: SuggestSpaceRequest,
    space_repo=Depends(get_intent_space_repo),
) -> dict[str, Any]:
    """Suggest which Intent_Space an about-to-be-uploaded document belongs to.

    Decodes the file, extracts its text deterministically with the matching
    format loader, and asks the classifier which space fits best, so the KB
    upload form can pre-select the space (the Admin always confirms). On an
    unsupported/corrupt file a 400 names the problem; on an AI failure the
    suggestion is ``null`` so the caller simply keeps the manual default.
    """
    import tempfile
    from pathlib import Path as _Path

    from app.kb.loaders import LoaderError, load_document

    try:
        content = base64.b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64.")

    suffix = _Path(payload.name).suffix or ".txt"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = _Path(tmp.name)
        try:
            extracted = load_document(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    except LoaderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Body text first; fall back to flattened table cells for sheet-like files.
    excerpt = (extracted.text or "").strip()
    if not excerpt and extracted.tables:
        excerpt = " ".join(
            cell for table in extracted.tables for row in table for cell in row
        )
    excerpt = excerpt[:SPACE_SUGGESTION_EXCERPT_CHARS]

    ai_client = getattr(request.app.state, "dashscope_client", None)
    if ai_client is None or not excerpt:
        return {"suggestion": None}

    from app.ai.prompts import build_document_space_messages
    from app.core.orchestrator import Orchestrator

    spaces = space_repo.list()
    space_specs = [
        (s["id"], s["name"], s.get("description", ""), space_repo.get_keywords(s["id"]))
        for s in spaces
    ]
    valid_ids = {int(s["id"]) for s in spaces}
    messages = build_document_space_messages(space_specs, payload.name, excerpt)
    try:
        raw = await ai_client.classify(messages)
        parsed = Orchestrator._parse_classification(raw, valid_ids)
    except Exception:  # noqa: BLE001 - AI failure → no suggestion, not a 500
        logger.warning("space suggestion AI call failed for %s", payload.name)
        parsed = None

    if parsed is None or parsed[0] is None:
        return {"suggestion": None}

    space_id, confidence = parsed
    space = space_repo.get(space_id)
    return {
        "suggestion": {
            "space_id": space_id,
            "space_name": space["name"] if space else f"#{space_id}",
            "confidence": confidence,
        }
    }


@documents_router.get("", summary="List documents with optional filters")
@documents_router.get("/", include_in_schema=False)
def list_documents(
    name: str | None = Query(None, description="Case-insensitive name substring."),
    format: str | None = Query(None, description="Exact format token filter."),
    space_id: int | None = Query(None, description="Intent_Space id filter."),
    date_from: str | None = Query(
        None, description="Inclusive lower bound on upload date (YYYY-MM-DD)."
    ),
    date_to: str | None = Query(
        None, description="Inclusive upper bound on upload date (YYYY-MM-DD)."
    ),
    doc_repo=Depends(get_document_repo),
) -> list[dict[str, Any]]:
    """Return documents matching every applied filter, newest first (Req 4.5/4.6)."""
    return doc_repo.list(
        name=name,
        format=format,
        space_id=space_id,
        uploaded_from=date_from,
        uploaded_to=date_to,
    )


@documents_router.delete("/{document_id}", summary="Delete a document")
def delete_document(
    document_id: int = Path(..., description="The document to delete."),
    service=Depends(get_document_service),
    doc_repo=Depends(get_document_repo),
) -> dict[str, Any]:
    """Delete a document and every trace of it, rebuilding its index (Req 4.8)."""
    if doc_repo.get(document_id) is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    service.delete(document_id)
    return {"deleted": True, "id": document_id}


@documents_router.post("/{document_id}/update", summary="Trigger re-processing")
def update_document(
    document_id: int,
    background_tasks: BackgroundTasks,
    doc_repo=Depends(get_document_repo),
    processor=Depends(get_document_processor),
) -> dict[str, Any]:
    """Mark a document Pending and schedule re-processing (Req 4.9).

    The document is returned to ``Pending`` (clearing any prior error) so the
    Update action re-parses and re-embeds it; when a background processor is
    wired the ingestion pipeline is scheduled to run.
    """
    if doc_repo.get(document_id) is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    doc_repo.set_status(document_id, "Pending", error_message=None)
    if processor is not None:
        background_tasks.add_task(processor.process, document_id)
    return {"id": document_id, "status": "Pending", "message": "Re-processing scheduled."}


class AssignSpaceRequest(BaseModel):
    """Body for ``PUT /documents/{id}/space`` (Req 5.6)."""

    space_id: int = Field(..., description="Destination Intent_Space id.")


@documents_router.put("/{document_id}/space", summary="Reassign a document's Intent_Space")
def assign_document_space(
    document_id: int,
    payload: AssignSpaceRequest,
    service=Depends(get_document_service),
) -> dict[str, Any]:
    """Reassign a document to another Intent_Space, rebuilding indexes (Req 5.6)."""
    from app.kb.service import DocumentNotFound

    try:
        service.reassign_space(document_id, payload.space_id)
    except DocumentNotFound:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": document_id, "space_id": payload.space_id, "message": "Reassigned."}


# ---------------------------------------------------------------------------
# Intent_Space sub-router (Task 13.3)
# ---------------------------------------------------------------------------

spaces_router = APIRouter(prefix="/spaces", tags=["spaces"])


class SpaceCreateRequest(BaseModel):
    """Body for ``POST /spaces``."""

    name: str = Field(..., description="Space name (1–50 chars, unique NOCASE).")
    description: str = Field("", description="Optional description (≤500 chars).")
    keywords: list[str] = Field(
        default_factory=list, description="≤50 keywords, each 1–50 chars."
    )


class SpaceUpdateRequest(BaseModel):
    """Body for ``PUT /spaces/{id}`` (any field omitted is left unchanged)."""

    name: str | None = Field(None, description="New name (1–50 chars, unique NOCASE).")
    description: str | None = Field(None, description="New description (≤500 chars).")
    keywords: list[str] | None = Field(
        None, description="New keyword list (≤50, each 1–50 chars)."
    )


def _space_document_count(doc_repo, space_id: int) -> int:
    """Count documents currently associated with a space."""
    return len(doc_repo.list(space_id=space_id))


@spaces_router.get("", summary="List Intent_Spaces with counts and accuracy")
@spaces_router.get("/", include_in_schema=False)
def list_spaces(
    space_repo=Depends(get_intent_space_repo),
    doc_repo=Depends(get_document_repo),
    analytics=Depends(get_analytics_service),
) -> list[dict[str, Any]]:
    """Return every Intent_Space with its document count, accuracy, and keywords.

    ``accuracy_rate`` comes from
    :meth:`~app.analytics.service.AnalyticsService.accuracy_by_space` and is
    ``null`` (rendered N/A) when the space has no Admin-verified queries.
    """
    accuracy = analytics.accuracy_by_space()
    result: list[dict[str, Any]] = []
    for space in space_repo.list():
        sid = int(space["id"])
        result.append(
            _space_view(
                space,
                document_count=_space_document_count(doc_repo, sid),
                accuracy_rate=accuracy.get(sid),
                keywords=space_repo.get_keywords(sid),
            )
        )
    return result


@spaces_router.post("", summary="Create an Intent_Space")
@spaces_router.post("/", include_in_schema=False)
def create_space(
    payload: SpaceCreateRequest,
    space_repo=Depends(get_intent_space_repo),
    doc_repo=Depends(get_document_repo),
) -> dict[str, Any]:
    """Create an Intent_Space after validation and uniqueness checks.

    Validation errors → 400 listing each offending field (Req 6.2/6.5); a name
    that duplicates an existing space case-insensitively → 409 (Req 6.6).
    """
    errors = validate_space_config(payload.name, payload.description, payload.keywords)
    if errors:
        raise _validation_error("spaces", errors)

    name = payload.name.strip()
    if space_repo.get_by_name(name) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An Intent_Space named {name!r} already exists.",
        )

    try:
        space_id = space_repo.create(name, description=payload.description or "")
    except sqlite3.IntegrityError:
        # Defensive: the NOCASE UNIQUE constraint tripped despite the check.
        raise HTTPException(
            status_code=409,
            detail=f"An Intent_Space named {name!r} already exists.",
        )
    if payload.keywords:
        space_repo.set_keywords(space_id, payload.keywords)

    space = space_repo.get(space_id)
    return _space_view(
        space,
        document_count=0,
        accuracy_rate=None,
        keywords=space_repo.get_keywords(space_id),
    )


@spaces_router.put("/{space_id}", summary="Edit an Intent_Space")
def update_space(
    space_id: int,
    payload: SpaceUpdateRequest,
    space_repo=Depends(get_intent_space_repo),
    doc_repo=Depends(get_document_repo),
    analytics=Depends(get_analytics_service),
) -> dict[str, Any]:
    """Edit a space's name/description/keywords with the same validation rules.

    Case-insensitive uniqueness is enforced against every *other* space
    (Req 6.6); validation failures → 400 (Req 6.2/6.5).
    """
    existing = space_repo.get(space_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Intent_Space {space_id} not found.")

    # Fields left as None keep their current value; validate the effective set.
    eff_name = existing["name"] if payload.name is None else payload.name
    eff_desc = (
        existing.get("description", "") if payload.description is None else payload.description
    )
    eff_keywords = (
        space_repo.get_keywords(space_id) if payload.keywords is None else payload.keywords
    )

    errors = validate_space_config(eff_name, eff_desc, eff_keywords)
    if errors:
        raise _validation_error("spaces", errors)

    if payload.name is not None:
        trimmed = payload.name.strip()
        clash = space_repo.get_by_name(trimmed)
        if clash is not None and int(clash["id"]) != space_id:
            raise HTTPException(
                status_code=409,
                detail=f"An Intent_Space named {trimmed!r} already exists.",
            )
        eff_name = trimmed

    try:
        space_repo.update(
            space_id,
            name=None if payload.name is None else eff_name,
            description=None if payload.description is None else eff_desc,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"An Intent_Space named {eff_name!r} already exists.",
        )
    if payload.keywords is not None:
        space_repo.set_keywords(space_id, payload.keywords)

    space = space_repo.get(space_id)
    accuracy = analytics.accuracy_by_space()
    return _space_view(
        space,
        document_count=_space_document_count(doc_repo, space_id),
        accuracy_rate=accuracy.get(space_id),
        keywords=space_repo.get_keywords(space_id),
    )


@spaces_router.delete("/{space_id}", summary="Delete an Intent_Space")
def delete_space(
    space_id: int,
    space_repo=Depends(get_intent_space_repo),
    doc_repo=Depends(get_document_repo),
    search_index=Depends(get_search_index),
) -> dict[str, Any]:
    """Delete a space, reassigning its documents to General first (Req 6.3/6.7).

    Deleting the General_Space is refused with a 400 (Req 6.7). For any other
    space, every associated document is reassigned to the General_Space and both
    the vacated and the General indexes are rebuilt so the moved documents stay
    searchable in General, after which the space (and its keywords) is deleted.
    """
    space = space_repo.get(space_id)
    if space is None:
        raise HTTPException(status_code=404, detail=f"Intent_Space {space_id} not found.")
    if bool(space.get("is_general", 0)):
        raise HTTPException(
            status_code=400,
            detail="The General_Space cannot be deleted.",
        )

    general = space_repo.get_general()
    if general is None:
        raise HTTPException(
            status_code=500, detail="General_Space is missing; cannot reassign documents."
        )
    general_id = int(general["id"])

    reassigned = doc_repo.reassign_space_documents(space_id, general_id)
    # Rebuild both indexes from the authoritative rows: General now holds the
    # moved documents; the vacated space's index is dropped.
    search_index.rebuild_space(general_id)
    search_index.rebuild_space(space_id)
    space_repo.delete(space_id)

    return {
        "deleted": True,
        "id": space_id,
        "reassigned_to": general_id,
        "reassigned_count": reassigned,
    }


#: Maximum keyword suggestions returned per request.
KEYWORD_SUGGESTION_MAX = 10

#: How many recent misrouted queries feed one suggestion request.
KEYWORD_SUGGESTION_QUERY_LIMIT = 50


@spaces_router.post(
    "/{space_id}/suggest-keywords",
    summary="AI keyword suggestions from misclassified queries",
)
async def suggest_space_keywords(
    request: Request,
    space_id: int,
    conn: sqlite3.Connection = Depends(get_connection),
    space_repo=Depends(get_intent_space_repo),
) -> dict[str, Any]:
    """Suggest new routing keywords for a space from its misrouted queries.

    Collects recent queries an Admin verified as belonging to this space but
    that were detected into a different one, and asks the AI model to propose
    new keywords (existing keywords are never re-suggested). Returns an empty
    suggestion list with an explanatory message when there are no misrouted
    queries yet or when the AI call fails.
    """
    space = space_repo.get(space_id)
    if space is None:
        raise HTTPException(status_code=404, detail=f"Space {space_id} not found.")

    rows = conn.execute(
        "SELECT DISTINCT query_text FROM query_log "
        "WHERE verified_space_id = ? AND detected_space_id != ? "
        "ORDER BY ts DESC LIMIT ?",
        (space_id, space_id, KEYWORD_SUGGESTION_QUERY_LIMIT),
    ).fetchall()
    misrouted = [r["query_text"] for r in rows]
    if not misrouted:
        return {
            "keywords": [],
            "based_on": 0,
            "message": (
                "No misrouted queries verified for this space yet. Verify "
                "misclassified queries in Analytics first."
            ),
        }

    ai_client = getattr(request.app.state, "dashscope_client", None)
    if ai_client is None:
        raise HTTPException(status_code=503, detail="AI client is not available yet.")

    from app.ai.prompts import build_keyword_suggestion_messages

    existing = space_repo.get_keywords(space_id)
    messages = build_keyword_suggestion_messages(
        space["name"], space.get("description", ""), existing, misrouted
    )
    try:
        raw = await ai_client.chat_completion(messages, json_mode=True)
        parsed = json.loads(raw)
        candidates = parsed.get("keywords", [])
    except Exception:  # noqa: BLE001 - AI failure → empty suggestion, not a 500
        logger.warning("keyword suggestion AI call failed for space %s", space_id)
        return {
            "keywords": [],
            "based_on": len(misrouted),
            "message": "Suggestion generation failed; please try again.",
        }

    existing_lower = {k.lower() for k in existing}
    suggestions: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        keyword = candidate.strip()
        if (
            SPACE_KEYWORD_MIN <= len(keyword) <= SPACE_KEYWORD_MAX
            and keyword.lower() not in existing_lower
            and keyword.lower() not in {s.lower() for s in suggestions}
        ):
            suggestions.append(keyword)
        if len(suggestions) >= KEYWORD_SUGGESTION_MAX:
            break

    return {"keywords": suggestions, "based_on": len(misrouted), "message": None}


# ---------------------------------------------------------------------------
# Settings / analytics / dashboard sub-router (Task 13.7)
# ---------------------------------------------------------------------------

system_router = APIRouter(tags=["system"])

CONFIDENCE_THRESHOLD_KEY = "confidence_threshold"
DEFAULT_CONFIDENCE_THRESHOLD = "70"


@system_router.get(
    "/settings/confidence-threshold", summary="Read the Confidence_Threshold"
)
def get_confidence_threshold(
    settings_repo=Depends(get_settings_repo),
) -> dict[str, Any]:
    """Return the current Confidence_Threshold value (Req 7.4)."""
    raw = settings_repo.get(CONFIDENCE_THRESHOLD_KEY, DEFAULT_CONFIDENCE_THRESHOLD)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(DEFAULT_CONFIDENCE_THRESHOLD)
    return {"value": value, "confidence_threshold": value}


@system_router.put(
    "/settings/confidence-threshold", summary="Update the Confidence_Threshold"
)
def put_confidence_threshold(
    payload: dict[str, Any] = Body(..., description="{'value': <0-100 integer>}"),
    settings_repo=Depends(get_settings_repo),
) -> dict[str, Any]:
    """Persist a new Confidence_Threshold, accepting only ``[0, 100]`` (Req 7.4/7.9).

    An out-of-range or non-integer value is rejected with a 400 range error and
    the previously stored value is retained (nothing is written).
    """
    ok, value = validate_threshold(payload.get("value"))
    if not ok or value is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Confidence threshold must be a whole number between "
                f"{THRESHOLD_MIN} and {THRESHOLD_MAX}."
            ),
        )
    settings_repo.set(CONFIDENCE_THRESHOLD_KEY, str(value))
    return {"value": value, "confidence_threshold": value, "saved": True}


@system_router.get("/queries", summary="Query history with filters")
def list_queries(
    start: str | None = Query(None, description="Inclusive lower bound on timestamp."),
    end: str | None = Query(None, description="Inclusive upper bound on timestamp."),
    space_ids: list[int] | None = Query(
        None, description="Restrict to these detected Intent_Space ids."
    ),
    tool: str | None = Query(None, description="Restrict to a single Frontend_Tool."),
    limit: int = Query(50, ge=1, le=500, description="Maximum entries (newest first)."),
    analytics=Depends(get_analytics_service),
) -> list[dict[str, Any]]:
    """Return Query_Log entries matching the filters, newest first (Req 7.7/10.4)."""
    entries = analytics.history(
        start=start, end=end, space_ids=space_ids, tool=tool, limit=limit
    )
    return [_query_log_view(e) for e in entries]


class VerifyQueryRequest(BaseModel):
    """Body for ``POST /queries/{id}/verify`` (Req 10.3)."""

    verified_space_id: int = Field(..., description="Admin-confirmed Intent_Space id.")


@system_router.post("/queries/{query_id}/verify", summary="Verify a query's Intent_Space")
def verify_query(
    query_id: int,
    payload: VerifyQueryRequest,
    analytics=Depends(get_analytics_service),
) -> dict[str, Any]:
    """Record the Admin-verified Intent_Space for a query (Req 10.3)."""
    analytics.verify_query(query_id, payload.verified_space_id)
    return {
        "id": query_id,
        "verified_space_id": payload.verified_space_id,
        "verified": True,
    }


@system_router.get("/analytics/top-documents", summary="Most accessed documents")
def analytics_top_documents(
    start: str | None = Query(None),
    end: str | None = Query(None),
    n: int = Query(10, ge=1, le=100),
    analytics=Depends(get_analytics_service),
) -> list[dict[str, Any]]:
    """Return the top-``n`` accessed documents for the range (Req 10.2)."""
    return [
        {"name": name, "count": count}
        for name, count in analytics.top_documents(start, end, n)
    ]


@system_router.get("/analytics/top-spaces", summary="Most common Intent_Spaces")
def analytics_top_spaces(
    start: str | None = Query(None),
    end: str | None = Query(None),
    n: int = Query(10, ge=1, le=100),
    analytics=Depends(get_analytics_service),
) -> list[dict[str, Any]]:
    """Return the top-``n`` most common Intent_Spaces for the range (Req 10.2)."""
    return [
        {"name": name, "count": count}
        for name, count in analytics.top_spaces(start, end, n)
    ]


@system_router.get("/analytics/accuracy", summary="Per-space classification accuracy")
def analytics_accuracy(
    analytics=Depends(get_analytics_service),
) -> dict[str, Any]:
    """Return per-Intent_Space accuracy with sample sizes (Req 10.3).

    ``items`` carries one row per space with its ``accuracy`` (``null`` = N/A,
    i.e. no verified queries), and the ``correct``/``verified`` counts behind
    that rate. Top-level ``verified``/``unverified``/``total`` give overall
    verification coverage so a rate over a tiny sample is not misread. The
    legacy flat ``{space_id: pct}`` mapping is preserved for backward
    compatibility.
    """
    detail = analytics.accuracy_detail_by_space()
    totals = analytics.verification_totals()
    payload: dict[str, Any] = {
        "items": [
            {
                "space_id": sid,
                "accuracy": d["accuracy"],
                "correct": d["correct"],
                "verified": d["verified"],
                "unverified": d["unverified"],
            }
            for sid, d in detail.items()
        ],
        "verified": totals["verified"],
        "unverified": totals["unverified"],
        "total": totals["total"],
    }
    # Backward-compatible flat mapping (older UI reads {space_id: pct}).
    for sid, d in detail.items():
        payload[str(sid)] = d["accuracy"]
    return payload


@system_router.get("/analytics/export", summary="Export filtered history + metrics as CSV")
def analytics_export(
    start: str | None = Query(None),
    end: str | None = Query(None),
    space_ids: list[int] | None = Query(None),
    tool: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    analytics=Depends(get_analytics_service),
) -> Response:
    """Return a CSV export of the filtered Query_Log plus metrics (Req 10.5/10.8).

    Any failure surfaces as a 500 with the error message; because export is
    read-only the stored history is left unchanged.
    """
    from app.analytics.service import ExportError, Filters

    filters = Filters(start=start, end=end, space_ids=space_ids, tool=tool, limit=limit)
    try:
        data = analytics.export_csv(filters)
    except ExportError as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")
    return Response(content=data, media_type="text/csv")


def _dashboard_integrations(integration_repo) -> dict[str, str]:
    """Build the per-tool integration-status section of the dashboard."""
    return {row["tool"]: row["status"] for row in integration_repo.list()}


def _dashboard_documents(conn: sqlite3.Connection) -> dict[str, int]:
    """Build the document-counts-by-status section, covering every status."""
    counts = {status: 0 for status in DOCUMENT_STATUSES_REPORTED}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
    ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def _dashboard_queries_24h(conn: sqlite3.Connection) -> dict[str, int]:
    """Build the 24-hour query-activity section (total / success / failed)."""
    cutoff = (
        datetime.now() - timedelta(hours=DASHBOARD_QUERY_WINDOW_HOURS)
    ).isoformat(sep=" ")
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN response_status = 'Success' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN response_status = 'Failed' THEN 1 ELSE 0 END) AS failed
        FROM query_log
        WHERE ts >= ?
        """,
        (cutoff,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "success": int(row["success"] or 0),
        "failed": int(row["failed"] or 0),
    }


@system_router.get("/analytics/gaps", summary="Top unanswered questions (knowledge gaps)")
def analytics_gaps(
    start: str | None = Query(None),
    end: str | None = Query(None),
    n: int = Query(10, ge=1, le=100),
    analytics=Depends(get_analytics_service),
) -> list[dict[str, Any]]:
    """Return the most frequent unanswered (no-match) questions.

    Surfaces the queries the knowledge base could not answer so the Admin
    knows which documents to upload next. See
    :meth:`~app.analytics.service.AnalyticsService.knowledge_gaps`.
    """
    return analytics.knowledge_gaps(start, end, n)


@system_router.get("/analytics/feedback", summary="End-user feedback summary")
def analytics_feedback(
    analytics=Depends(get_analytics_service),
) -> dict[str, Any]:
    """Return 👍/👎 counts and the overall satisfaction percentage.

    ``satisfaction_pct`` is ``null`` (N/A) when no feedback has been recorded.
    """
    return analytics.feedback_summary()


@system_router.get("/dashboard/summary", summary="Dashboard summary (partial-failure tolerant)")
def dashboard_summary(
    conn: sqlite3.Connection = Depends(get_connection),
    integration_repo=Depends(get_integration_repo),
) -> dict[str, Any]:
    """Aggregate the three dashboard sections, each computed independently.

    Integration status, document counts by status, and 24-hour query activity
    are each computed in isolation; if one section's computation fails, an
    ``{"error": <reason>}`` marker is returned for that section while the others
    still return their data (Req 9.5/9.7).
    """
    summary: dict[str, Any] = {}

    try:
        summary["integrations"] = _dashboard_integrations(integration_repo)
    except Exception as exc:  # noqa: BLE001 - per-section isolation (Req 9.7)
        summary["integrations"] = {"error": str(exc) or "integration status unavailable"}

    try:
        summary["documents"] = _dashboard_documents(conn)
    except Exception as exc:  # noqa: BLE001 - per-section isolation (Req 9.7)
        summary["documents"] = {"error": str(exc) or "document counts unavailable"}

    try:
        summary["queries_24h"] = _dashboard_queries_24h(conn)
    except Exception as exc:  # noqa: BLE001 - per-section isolation (Req 9.7)
        summary["queries_24h"] = {"error": str(exc) or "query activity unavailable"}

    return summary


# ---------------------------------------------------------------------------
# Top-level router assembly
# ---------------------------------------------------------------------------


def build_router() -> APIRouter:
    """Build the top-level admin router with all area sub-routers mounted.

    Mounts the integrations, documents, Intent_Space, and
    settings/analytics/dashboard sub-routers into one router that ``main.py``
    includes on the FastAPI app.

    Returns:
        The assembled :class:`fastapi.APIRouter`.
    """
    admin = APIRouter()
    admin.include_router(integrations_router)
    admin.include_router(documents_router)
    admin.include_router(spaces_router)
    admin.include_router(system_router)
    return admin


# Importable, ready-to-mount router (e.g. ``app.include_router(admin_router)``).
router = build_router()
