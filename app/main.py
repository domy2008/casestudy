"""FastAPI application assembly.

This module wires the deployed container into the real product: it serves the
Admin REST API consumed by the Streamlit UI, hosts the Microsoft Teams inbound
webhook, runs the Telegram long-polling loop, drives the integration status
monitor, and keeps emitting CloudWatch metrics — all on top of the original
minimal deployment core (``/health``, ``/``, the request-latency middleware,
and the CloudWatch publisher).

Startup (see :func:`lifespan`) is defensive by design so ``from app.main import
app`` works headlessly with no network and no credentials:

  - The SQLite schema/seeds are bootstrapped and a long-lived, thread-safe app
    connection is opened for the background tasks.
  - A :class:`~app.security.logfilter.RedactingFilter` is installed on the root
    logger and startup credentials are loaded from the Credential_Store; a
    missing DashScope key or bot token marks the dependent integration
    unavailable instead of crashing.
  - Shared singletons (DashScope client, FAISS search, analytics, RAG
    generator, orchestrator, bot adapters, dispatchers, status monitor) are
    built and stored on ``app.state``.
  - Background tasks are started: the Telegram poller (only when a token is
    configured), the integration status monitor, and the CloudWatch publisher.
    All are cancelled and their clients/connection closed on shutdown.

Threading note: FastAPI runs sync request handlers in a worker threadpool, and
each opens its own SQLite connection via the admin ``get_connection``
dependency. The background tasks instead use a separate
``check_same_thread=False`` connection held on ``app.state``. These two must
never be crossed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime

from fastapi import Depends, FastAPI, Request

from app.api.admin import (
    get_connection,
    get_connectivity_checker,
    get_document_processor,
)
from app.api.admin import router as admin_router
from app.bots.dispatcher import QueryDispatcher
from app.bots.monitor import IntegrationStatusMonitor
from app.bots.teams import TeamsAdapter
from app.bots.telegram import TelegramAdapter, TelegramConfigError
from app.config import Settings, get_settings
from app.core.models import ConnectivityResult, GeneratedResponse, QueryContext
from app.db import bootstrap
from app.monitoring.publisher import CloudWatchPublisher, MetricSnapshot

logger = logging.getLogger(__name__)


class RequestMetricsSource:
    """Metrics source backed by observed HTTP request latencies.

    The app records each request's latency; each publishing cycle drains the
    accumulated samples into a :class:`MetricSnapshot`. Reports the app itself
    as a healthy integration so the health chain has a real datapoint.
    """

    def __init__(self) -> None:
        self._latencies: deque[float] = deque(maxlen=10_000)

    def record(self, latency_ms: float) -> None:
        """Record one request latency in milliseconds."""
        self._latencies.append(latency_ms)

    def snapshot(self) -> MetricSnapshot:
        """Drain accumulated latencies into a snapshot for the interval."""
        samples = list(self._latencies)
        self._latencies.clear()
        return MetricSnapshot(
            latency_ms_samples=samples,
            error_rate_pct=0.0 if samples else None,
            integration_health={"app": True},
        )


metrics = RequestMetricsSource()


# ---------------------------------------------------------------------------
# Connectivity checker seam wired to the live status monitor
# ---------------------------------------------------------------------------


class _MonitorConnectivityChecker:
    """Adapt the :class:`IntegrationStatusMonitor` to the admin checker seam.

    The admin ``POST /integrations/{tool}/test`` endpoint depends on a
    :class:`~app.api.admin.ConnectivityChecker`. This adapter delegates to the
    monitor's 30-second-capped ``run_test`` so the endpoint drives the same
    end-to-end check the background loop uses.
    """

    def __init__(self, monitor: IntegrationStatusMonitor) -> None:
        self._monitor = monitor

    async def check(self, tool: str) -> ConnectivityResult:
        """Run the monitor's end-to-end connectivity test for ``tool``."""
        return await self._monitor.run_test(tool)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _open_app_connection(settings: Settings) -> sqlite3.Connection:
    """Open the long-lived, thread-safe connection used by background tasks.

    Unlike the per-request connections opened by the admin ``get_connection``
    dependency, this connection is shared across the async background tasks, so
    it is opened with ``check_same_thread=False``.

    Args:
        settings: The active settings providing the database path.

    Returns:
        An open connection with ``Row`` row factory and foreign keys enforced.
    """
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _safe_credential_store(settings: Settings):
    """Build a :class:`CredentialStore`, returning ``None`` if unavailable.

    A missing/invalid ``CREDENTIAL_MASTER_KEY`` must not crash startup — the
    dependent integrations are simply left unavailable. Import is local so the
    module stays importable in minimal contexts.

    Args:
        settings: The active settings (master key + store path).

    Returns:
        A ready :class:`CredentialStore`, or ``None`` when one cannot be built.
    """
    from app.security.credentials import CredentialStore

    try:
        return CredentialStore(settings)
    except Exception:  # noqa: BLE001 - missing/invalid key => no store
        logger.warning(
            "Credential store is unavailable; integrations depending on stored "
            "credentials will be marked unavailable."
        )
        return None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Assemble the product on startup and tear it down cleanly on shutdown."""
    settings = get_settings()

    # 1a. Schema + seeds, plus the long-lived background connection.
    bootstrap(settings).close()
    app_conn = _open_app_connection(settings)
    app.state.settings = settings
    app.state.app_conn = app_conn

    # 1b. Redact secrets from logs and load startup credentials.
    from app.kb.store import (
        IntegrationErrorLogRepository,
        IntegrationRepository,
    )
    from app.security.logfilter import RedactingFilter
    from app.security.startup import load_startup_credentials

    redacting_filter = RedactingFilter()
    logging.getLogger().addFilter(redacting_filter)

    integration_repo = IntegrationRepository(app_conn)
    error_log_repo = IntegrationErrorLogRepository(app_conn)

    credential_store = _safe_credential_store(settings)
    cred_result = None
    if credential_store is not None:
        try:
            cred_result = load_startup_credentials(
                credential_store,
                integration_repo,
                redacting_filter=redacting_filter,
                logger=logger,
            )
        except Exception:  # noqa: BLE001 - startup must survive load failures
            logger.exception("Startup credential loading failed; continuing")

    # 1c. Shared singletons.
    from app.ai.dashscope_client import DashScopeClient
    from app.analytics.service import AnalyticsService
    from app.core.orchestrator import Orchestrator
    from app.kb.search import SearchIndex
    from app.rag.generator import ResponseGenerator

    ai_client = DashScopeClient(settings=settings, credential_store=credential_store)
    search_index = SearchIndex(app_conn, settings)
    analytics = AnalyticsService(app_conn)
    # Document-access rows are correlated with a query_log_id, which the
    # Orchestrator owns and writes; it records document_access itself, so the
    # generator does not double-write here.
    generator = ResponseGenerator(ai_client)
    orchestrator = Orchestrator(
        conn=app_conn,
        ai_client=ai_client,
        search_index=search_index,
        generator=generator,
        analytics=analytics,
    )

    telegram_adapter = TelegramAdapter(
        credential_store=credential_store, settings=settings, error_log=None
    )
    teams_adapter = TeamsAdapter(
        credential_store=credential_store, settings=settings
    )

    telegram_dispatcher = QueryDispatcher(
        orchestrator, telegram_adapter, error_log=error_log_repo
    )
    monitor = IntegrationStatusMonitor(
        {"telegram": telegram_adapter, "teams": teams_adapter},
        integration_repo,
        error_log_repo,
    )

    app.state.dashscope_client = ai_client
    app.state.search_index = search_index
    app.state.analytics = analytics
    app.state.orchestrator = orchestrator
    app.state.telegram_adapter = telegram_adapter
    app.state.teams_adapter = teams_adapter
    app.state.monitor = monitor
    app.state.connectivity_checker = _MonitorConnectivityChecker(monitor)

    # 1d. Background tasks.
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    publisher = CloudWatchPublisher(str(settings.logbuffer_dir), region=settings.aws_region)
    tasks.append(asyncio.create_task(publisher.run_forever(metrics)))
    tasks.append(asyncio.create_task(monitor.run(stop_event=stop_event)))

    telegram_active = cred_result is not None and "telegram" in cred_result.loaded
    if telegram_active:

        async def _telegram_handler(conversation_ref: dict, text: str) -> None:
            ctx = QueryContext(
                query_id=str(uuid.uuid4()),
                tool="telegram",
                conversation_ref=conversation_ref,
                text=text,
                received_at=datetime.now(),
            )
            await telegram_dispatcher.dispatch(ctx)

        async def _run_telegram_poller() -> None:
            try:
                await telegram_adapter.run_polling(
                    _telegram_handler, stop_event=stop_event
                )
            except TelegramConfigError:
                logger.warning(
                    "Telegram polling not started: bot token is not configured."
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a poller failure must not crash startup
                logger.exception("Telegram polling loop exited unexpectedly")

        tasks.append(asyncio.create_task(_run_telegram_poller()))

    try:
        yield
    finally:
        # Shutdown: stop loops, cancel tasks, close clients + connection.
        stop_event.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await ai_client.aclose()
        with contextlib.suppress(Exception):
            await telegram_adapter.aclose()
        with contextlib.suppress(Exception):
            await teams_adapter.aclose()
        with contextlib.suppress(Exception):
            app_conn.close()


app = FastAPI(
    title="IntelliKnow KMS",
    version="0.1.0",
    description="Gen AI-powered Knowledge Management System.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Admin API + dependency wiring
# ---------------------------------------------------------------------------

app.include_router(admin_router)


class _BackgroundDocumentProcessor:
    """Runs document ingestion on its own per-task SQLite connection.

    Document processing is scheduled via FastAPI ``BackgroundTasks``, which run
    on the event-loop thread — a *different* thread from the worker-threadpool
    thread that served the (sync) request. A SQLite connection may only be used
    on the thread that created it, so the processor must NOT reuse the request
    connection. This wrapper therefore opens a fresh ``check_same_thread=False``
    connection inside :meth:`process` (executed on the background task's own
    thread), runs the real :class:`~app.kb.processor.DocumentProcessor` over it,
    and closes it when done. The uploaded ``documents`` row (already committed
    by the request connection) is visible to the fresh connection via WAL.

    Args:
        settings: The active settings (database path, FAISS dir).
        client: The shared DashScope client (safe to share; it is stateless
            per call and does its own async I/O).
    """

    def __init__(self, settings: Settings, client) -> None:
        self._settings = settings
        self._client = client

    async def process(self, document_id: int) -> None:
        """Open a task-local connection and run the ingestion pipeline."""
        from app.kb.processor import DocumentProcessor
        from app.kb.search import SearchIndex

        conn = sqlite3.connect(
            str(self._settings.db_path), check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            processor = DocumentProcessor(
                conn,
                self._client,
                settings=self._settings,
                search_index=SearchIndex(conn, self._settings),
            )
            await processor.process(document_id)
        finally:
            conn.close()


def _document_processor_dependency():
    """Provide a background-safe document processor for uploads/updates.

    Returns a :class:`_BackgroundDocumentProcessor` that opens its own
    connection inside each ``process`` call, so scheduled ingestion never
    reuses a request or app connection across threads.

    Returns:
        A :class:`_BackgroundDocumentProcessor`, or ``None`` before the shared
        DashScope client has been built (i.e. before startup completes).
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    client = getattr(app.state, "dashscope_client", None)
    if client is None:
        return None
    return _BackgroundDocumentProcessor(settings, client)


def _connectivity_checker_dependency():
    """Provide the live connectivity checker backed by the status monitor.

    Returns:
        The :class:`_MonitorConnectivityChecker` built at startup.

    Raises:
        HTTPException: 503 when the monitor is not yet available.
    """
    checker = getattr(app.state, "connectivity_checker", None)
    if checker is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="Connectivity checking is not available yet.",
        )
    return checker


app.dependency_overrides[get_document_processor] = _document_processor_dependency
app.dependency_overrides[get_connectivity_checker] = _connectivity_checker_dependency


# ---------------------------------------------------------------------------
# Middleware + core routes
# ---------------------------------------------------------------------------


@app.middleware("http")
async def record_latency(request: Request, call_next):
    """Measure each request's latency and feed it to the metrics source."""
    start = time.perf_counter()
    response = await call_next(request)
    metrics.record((time.perf_counter() - start) * 1000.0)
    return response


@app.get("/health")
async def health() -> dict:
    """Liveness probe consumed by the Docker healthcheck."""
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    """Basic service metadata."""
    return {"service": "intelliknow-kms", "version": "0.1.0", "status": "running"}


@app.post("/webhooks/teams")
async def teams_webhook(request: Request) -> dict:
    """Handle a Microsoft Teams Bot Framework inbound activity (Req 2.1).

    Parses the activity body and hands it to the Teams adapter, whose
    dispatcher runs the query through the Orchestrator and replies via the Bot
    Connector. Any handler error is contained so the webhook always returns a
    200 acknowledgement rather than crashing.
    """
    try:
        activity = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is acknowledged, not fatal
        activity = {}

    adapter: TeamsAdapter | None = getattr(app.state, "teams_adapter", None)
    orchestrator = getattr(app.state, "orchestrator", None)
    if adapter is None or orchestrator is None:
        return {"status": "ok"}

    async def _dispatch(conversation_ref: dict, text: str) -> GeneratedResponse:
        ctx = QueryContext(
            query_id=str(uuid.uuid4()),
            tool="teams",
            conversation_ref=conversation_ref,
            text=text,
            received_at=datetime.now(),
        )
        return await orchestrator.handle_query(ctx)

    try:
        await adapter.handle_activity(
            activity,
            dispatcher=_dispatch,
            auth_header=request.headers.get("Authorization"),
        )
    except Exception:  # noqa: BLE001 - never let a handler error break the webhook
        logger.exception("Teams webhook handling failed")

    return {"status": "ok"}
