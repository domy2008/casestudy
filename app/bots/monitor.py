"""Integration status monitor: periodic health loop + on-demand e2e test.

This module owns the Frontend_Tool connectivity lifecycle described in the
design's "Frontend Integration Module → Status monitor" section:

* A **60-second background loop** (:meth:`IntegrationStatusMonitor.run`) that,
  on each iteration, performs a lightweight connectivity check per configured
  tool via that adapter's ``check_connectivity`` (``getMe`` for Telegram, Bot
  Framework token acquisition for Teams) and updates the stored integration
  status to ``Connected``/``Error``/``Disconnected`` (Req 3.1, 3.6). A
  deactivated integration is recorded as ``Disconnected`` without a check
  (Req 3.1).

* A **per-adapter end-to-end test entrypoint**
  (:meth:`IntegrationStatusMonitor.run_test`) used by the Admin test button.
  It caps the check at 30 seconds and, if the check does not complete in time,
  terminates it and returns a timeout-failure
  :class:`~app.core.models.ConnectivityResult` (Req 3.2, 3.5).

* On any failed check (including a timeout), an ``integration_error_log`` entry
  is recorded with timestamp, tool, operation, and error detail (Req 3.3).

Everything the monitor touches is injectable — the adapters, the
:class:`~app.kb.store.IntegrationRepository`, the
:class:`~app.kb.store.IntegrationErrorLogRepository`, and clock/sleep seams — so
the loop can be exercised in tests without real waiting. A single-iteration
method (:meth:`IntegrationStatusMonitor.check_once`) is exposed for exactly that
purpose.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone

from app.bots.base import FrontendAdapter
from app.core.models import ConnectivityResult
from app.kb.store import IntegrationErrorLogRepository, IntegrationRepository

__all__ = [
    "MONITOR_INTERVAL_SECONDS",
    "CONNECTIVITY_TEST_TIMEOUT_SECONDS",
    "CHECK_OPERATION",
    "STATUS_CONNECTED",
    "STATUS_ERROR",
    "STATUS_DISCONNECTED",
    "IntegrationStatusMonitor",
]

logger = logging.getLogger(__name__)

# Re-evaluation cadence for the background loop (Req 3.6: "60 seconds or less").
MONITOR_INTERVAL_SECONDS = 60

# Hard cap for a single admin-triggered end-to-end connectivity test
# (Req 3.2/3.5). A check that runs past this is terminated and reported as a
# timeout failure.
CONNECTIVITY_TEST_TIMEOUT_SECONDS = 30

# Operation label recorded in the integration error log for a failed check
# (Req 3.3). The underlying per-tool call (getMe / token acquisition) lives
# inside each adapter's check_connectivity.
CHECK_OPERATION = "connectivity_check"

# Stored integration status values (design: integrations.status).
STATUS_CONNECTED = "Connected"
STATUS_ERROR = "Error"
STATUS_DISCONNECTED = "Disconnected"

# Injectable time seams.
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _default_clock() -> datetime:
    """Return the current UTC time (default :data:`Clock`)."""
    return datetime.now(timezone.utc)


class IntegrationStatusMonitor:
    """Monitor Frontend_Tool connectivity and persist Connected/Error/Disconnected.

    The monitor is constructed with one :class:`~app.bots.base.FrontendAdapter`
    per Frontend_Tool plus the integration status and error-log repositories.
    Its :meth:`run` method drives the periodic loop; :meth:`check_once` runs a
    single iteration (the test seam); and :meth:`run_test` performs a
    30-second-capped end-to-end check for the Admin test button.

    Attributes:
        tools: The Frontend_Tool identifiers this monitor manages.
    """

    def __init__(
        self,
        adapters: Mapping[str, FrontendAdapter] | Iterable[FrontendAdapter],
        integration_repo: IntegrationRepository,
        error_log_repo: IntegrationErrorLogRepository,
        *,
        interval_s: float = MONITOR_INTERVAL_SECONDS,
        test_timeout_s: float = CONNECTIVITY_TEST_TIMEOUT_SECONDS,
        clock: Clock = _default_clock,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Create an integration status monitor.

        Args:
            adapters: Either a mapping of ``tool_name -> adapter`` or an iterable
                of adapters (keyed by each adapter's ``tool_name``). One entry
                per configured Frontend_Tool.
            integration_repo: Repository used to read the active flag and to
                persist the Connected/Error/Disconnected status per tool.
            error_log_repo: Repository used to record a failed-check entry
                (ts, tool, operation, detail) (Req 3.3).
            interval_s: Seconds between background loop iterations
                (Req 3.6; default 60).
            test_timeout_s: Hard cap in seconds for an end-to-end test check
                (Req 3.2/3.5; default 30).
            clock: Callable returning the current time; injectable for tests.
            sleep: Awaitable sleep used between loop iterations; injectable for
                tests so the loop never waits for real time.
        """
        if isinstance(adapters, Mapping):
            self._adapters: dict[str, FrontendAdapter] = dict(adapters)
        else:
            self._adapters = {a.tool_name: a for a in adapters}
        self._integration_repo = integration_repo
        self._error_log_repo = error_log_repo
        self._interval_s = interval_s
        self._test_timeout_s = test_timeout_s
        self._clock = clock
        self._sleep = sleep

    @property
    def tools(self) -> tuple[str, ...]:
        """Return the Frontend_Tool identifiers this monitor manages."""
        return tuple(self._adapters)

    # --- background loop -------------------------------------------------

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Run the periodic connectivity loop until stopped (Req 3.6).

        Each iteration calls :meth:`check_once` and then sleeps for
        ``interval_s`` seconds. The loop exits before the next iteration once
        ``stop_event`` is set; when ``stop_event`` is ``None`` it runs until the
        task is cancelled. An unexpected error in a single iteration is logged
        and swallowed so the loop keeps running.

        Args:
            stop_event: Optional event; when set, the loop stops before the next
                iteration.
        """
        while stop_event is None or not stop_event.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad cycle must not kill the loop
                logger.exception("Integration status monitor iteration failed")
            await self._sleep(self._interval_s)

    async def check_once(self) -> dict[str, ConnectivityResult | None]:
        """Run one connectivity check for every managed tool (test seam).

        For each tool, updates the stored status per Req 3.1 and returns the
        outcome. A deactivated tool is recorded as ``Disconnected`` and yields a
        ``None`` result (no check performed); an active tool yields the
        :class:`~app.core.models.ConnectivityResult` of its check.

        Returns:
            A mapping of ``tool_name`` to the :class:`ConnectivityResult` of its
            check, or ``None`` when the tool was deactivated and skipped.
        """
        results: dict[str, ConnectivityResult | None] = {}
        for tool in self._adapters:
            results[tool] = await self._check_tool(tool)
        return results

    async def _check_tool(self, tool: str) -> ConnectivityResult | None:
        """Check a single active tool and persist its status (Req 3.1/3.3).

        Args:
            tool: The Frontend_Tool identifier to check.

        Returns:
            The :class:`ConnectivityResult` of the check, or ``None`` if the
            integration is deactivated (recorded as ``Disconnected``).
        """
        if not self._is_active(tool):
            # Deactivated integration: Disconnected, no check performed (Req 3.1).
            self._integration_repo.set_status(
                tool, STATUS_DISCONNECTED, last_check_ts=self._clock()
            )
            return None

        adapter = self._adapters[tool]
        result = await self._safe_check(adapter)
        self._apply_result(result)
        return result

    # --- admin test button ----------------------------------------------

    async def run_test(self, tool: str) -> ConnectivityResult:
        """Run a 30-second-capped end-to-end connectivity test (Req 3.2/3.5).

        Executes the tool's ``check_connectivity`` under a hard time cap. If the
        check does not complete within :attr:`test_timeout_s`, it is terminated
        and a timeout-failure :class:`ConnectivityResult` is returned. The
        outcome (success, failure, or timeout) is persisted to the integration
        status, and any failure is recorded in the integration error log
        (Req 3.1/3.3).

        Args:
            tool: The Frontend_Tool identifier to test.

        Returns:
            The :class:`ConnectivityResult` of the check. On timeout,
            ``timed_out`` is ``True``, ``ok`` is ``False``, and ``detail``
            explains the timeout.

        Raises:
            KeyError: If ``tool`` has no registered adapter.
        """
        adapter = self._adapters[tool]
        try:
            result = await asyncio.wait_for(
                adapter.check_connectivity(), timeout=self._test_timeout_s
            )
        except asyncio.TimeoutError:
            # The pending check has been cancelled by wait_for on timeout.
            result = ConnectivityResult(
                tool=tool,
                ok=False,
                detail=(
                    "Connectivity check timed out after "
                    f"{self._test_timeout_s:g}s and was terminated"
                ),
                timed_out=True,
                checked_at=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via the result object
            result = ConnectivityResult(
                tool=tool,
                ok=False,
                detail=str(exc),
                checked_at=self._clock(),
            )
        self._apply_result(result)
        return result

    # --- internals -------------------------------------------------------

    def _is_active(self, tool: str) -> bool:
        """Return whether the integration row for ``tool`` is active.

        A missing row (no configuration/no check yet) counts as inactive, which
        the loop records as ``Disconnected`` per Req 3.1.

        Args:
            tool: The Frontend_Tool identifier.
        """
        row = self._integration_repo.get(tool)
        return row is not None and bool(row["active"])

    async def _safe_check(self, adapter: FrontendAdapter) -> ConnectivityResult:
        """Call an adapter's ``check_connectivity``, never raising.

        The adapters are designed to return a :class:`ConnectivityResult` rather
        than raise, but a defensive guard here converts any unexpected exception
        into a failure result so a single tool cannot break the loop.

        Args:
            adapter: The Frontend_Tool adapter to check.

        Returns:
            The adapter's :class:`ConnectivityResult`, or a synthesized failure
            result if the check raised.
        """
        try:
            return await adapter.check_connectivity()
        except Exception as exc:  # noqa: BLE001 - convert to a failure result
            return ConnectivityResult(
                tool=adapter.tool_name,
                ok=False,
                detail=str(exc),
                checked_at=self._clock(),
            )

    def _apply_result(self, result: ConnectivityResult) -> None:
        """Persist a check outcome to status + error log (Req 3.1/3.3).

        Sets the integration status to ``Connected`` on success or ``Error`` on
        failure, and records a failed check in the integration error log with
        timestamp, tool, operation, and error detail. Error-log recording is
        wrapped so a logging failure never interrupts monitoring (mirrors the
        swallow-and-continue guard of Req 2.8).

        Args:
            result: The connectivity check outcome to apply.
        """
        checked_at = result.checked_at or self._clock()
        status = STATUS_CONNECTED if result.ok else STATUS_ERROR
        self._integration_repo.set_status(
            result.tool, status, last_check_ts=checked_at
        )
        if not result.ok:
            detail = result.detail or (
                "connectivity check timed out"
                if result.timed_out
                else "connectivity check failed"
            )
            try:
                self._error_log_repo.insert(
                    result.tool, CHECK_OPERATION, detail, ts=checked_at
                )
            except Exception:  # noqa: BLE001 - logging must never break monitoring
                logger.exception(
                    "Failed to record integration error-log entry for %s",
                    result.tool,
                )
