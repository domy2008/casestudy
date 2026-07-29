"""Unit tests for the integration status monitor (Req 3.1, 3.3, 3.5).

These cover the three behaviors the status monitor is responsible for, with no
real network and no real waiting:

* Status transitions Connected/Error/Disconnected are derived from the check
  outcome and persisted to the ``integrations`` table (Req 3.1).
* A failed API call records an ``integration_error_log`` entry carrying
  timestamp, tool, operation, and error detail (Req 3.3).
* A connectivity check that exceeds the 30-second cap is terminated and yields
  a timeout-failure result (Req 3.5).

Adapters are replaced with trivial fakes exposing ``tool_name`` and an async
``check_connectivity``; the SQLite store runs real against a temp DATA_DIR.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app.bots.monitor import (
    CHECK_OPERATION,
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_ERROR,
    IntegrationStatusMonitor,
)
from app.config import load_settings
from app.core.models import ConnectivityResult
from app.db import bootstrap
from app.kb.store import IntegrationErrorLogRepository, IntegrationRepository


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Adapter stand-in returning a preset ConnectivityResult (or raising)."""

    def __init__(
        self,
        tool_name: str,
        *,
        ok: bool = True,
        detail: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.tool_name = tool_name
        self._ok = ok
        self._detail = detail
        self._raises = raises
        self.calls = 0

    async def check_connectivity(self) -> ConnectivityResult:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return ConnectivityResult(
            tool=self.tool_name,
            ok=self._ok,
            detail=self._detail,
            checked_at=datetime.now(timezone.utc),
        )


class _HangingAdapter:
    """Adapter whose check never completes within any sane test window."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def check_connectivity(self) -> ConnectivityResult:
        # Sleep far longer than the (tiny, injected) test timeout so wait_for
        # must terminate it. Never returns on its own.
        await asyncio.sleep(3600)
        raise AssertionError("check should have been cancelled by the timeout")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repos(tmp_path):
    """Provide fresh integration + error-log repositories over a temp DB."""
    settings = load_settings(
        {
            "DATA_DIR": str(tmp_path),
            "CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
        }
    )
    conn = bootstrap(settings)
    integration_repo = IntegrationRepository(conn)
    error_log_repo = IntegrationErrorLogRepository(conn)
    try:
        yield integration_repo, error_log_repo
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Req 3.1 - status transitions
# ---------------------------------------------------------------------------


async def test_successful_check_sets_connected(repos):
    """An active tool whose check succeeds is marked Connected (Req 3.1)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", True)

    monitor = IntegrationStatusMonitor(
        [_FakeAdapter("telegram", ok=True, detail="getMe ok")],
        integration_repo,
        error_log_repo,
    )
    results = await monitor.check_once()

    assert results["telegram"].ok is True
    assert integration_repo.get("telegram")["status"] == STATUS_CONNECTED
    # A successful check records no error-log entry.
    assert error_log_repo.list_recent(tool="telegram") == []


async def test_failed_check_sets_error_and_logs(repos):
    """An active tool whose check fails is marked Error and logged (Req 3.1/3.3)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("teams", True)

    monitor = IntegrationStatusMonitor(
        [_FakeAdapter("teams", ok=False, detail="token endpoint unreachable")],
        integration_repo,
        error_log_repo,
    )
    results = await monitor.check_once()

    assert results["teams"].ok is False
    assert integration_repo.get("teams")["status"] == STATUS_ERROR

    # Req 3.3: an error-log entry with ts, tool, operation, and detail.
    entries = error_log_repo.list_recent(tool="teams")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "teams"
    assert entry["operation"] == CHECK_OPERATION
    assert entry["error_detail"] == "token endpoint unreachable"
    assert entry["ts"]  # timestamp present


async def test_deactivated_tool_sets_disconnected_without_check(repos):
    """A deactivated integration is Disconnected and its adapter is not called (Req 3.1)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", False)
    adapter = _FakeAdapter("telegram", ok=True)

    monitor = IntegrationStatusMonitor(
        [adapter], integration_repo, error_log_repo
    )
    results = await monitor.check_once()

    assert results["telegram"] is None
    assert adapter.calls == 0  # no connectivity check performed
    assert integration_repo.get("telegram")["status"] == STATUS_DISCONNECTED


async def test_status_transitions_track_latest_outcome(repos):
    """The stored status reflects the most recent check outcome (Req 3.1)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", True)

    ok_adapter = _FakeAdapter("telegram", ok=True)
    monitor = IntegrationStatusMonitor(
        [ok_adapter], integration_repo, error_log_repo
    )
    await monitor.check_once()
    assert integration_repo.get("telegram")["status"] == STATUS_CONNECTED

    # Swap in a failing adapter; the next iteration flips to Error.
    monitor = IntegrationStatusMonitor(
        [_FakeAdapter("telegram", ok=False, detail="getMe failed")],
        integration_repo,
        error_log_repo,
    )
    await monitor.check_once()
    assert integration_repo.get("telegram")["status"] == STATUS_ERROR


async def test_check_that_raises_is_treated_as_error(repos):
    """An adapter check that raises is converted to an Error status + log (Req 3.1/3.3)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", True)

    monitor = IntegrationStatusMonitor(
        [_FakeAdapter("telegram", raises=RuntimeError("boom"))],
        integration_repo,
        error_log_repo,
    )
    results = await monitor.check_once()

    assert results["telegram"].ok is False
    assert integration_repo.get("telegram")["status"] == STATUS_ERROR
    entries = error_log_repo.list_recent(tool="telegram")
    assert len(entries) == 1
    assert "boom" in entries[0]["error_detail"]


# ---------------------------------------------------------------------------
# Req 3.5 - 30s test cap terminates with a timeout failure
# ---------------------------------------------------------------------------


async def test_run_test_times_out_and_reports_failure(repos):
    """A check exceeding the cap is terminated with a timeout-failure result (Req 3.5)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", True)

    monitor = IntegrationStatusMonitor(
        [_HangingAdapter("telegram")],
        integration_repo,
        error_log_repo,
        # Tiny cap so the test never waits for the real 30 seconds.
        test_timeout_s=0.05,
    )
    result = await monitor.run_test("telegram")

    assert result.ok is False
    assert result.timed_out is True
    assert "timed out" in result.detail.lower()

    # The timeout is persisted as Error and recorded in the error log (Req 3.3).
    assert integration_repo.get("telegram")["status"] == STATUS_ERROR
    entries = error_log_repo.list_recent(tool="telegram")
    assert len(entries) == 1
    assert entries[0]["operation"] == CHECK_OPERATION


async def test_run_test_success_marks_connected(repos):
    """A test-button check that succeeds within the cap marks Connected (Req 3.2)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("teams", True)

    monitor = IntegrationStatusMonitor(
        [_FakeAdapter("teams", ok=True, detail="Bot Framework token acquired")],
        integration_repo,
        error_log_repo,
    )
    result = await monitor.run_test("teams")

    assert result.ok is True
    assert result.timed_out is False
    assert integration_repo.get("teams")["status"] == STATUS_CONNECTED


# ---------------------------------------------------------------------------
# Loop wiring - single-iteration + sleep seam
# ---------------------------------------------------------------------------


async def test_run_loop_iterates_then_stops(repos):
    """The loop runs check_once each cycle and honours the stop event (Req 3.6)."""
    integration_repo, error_log_repo = repos
    integration_repo.set_active("telegram", True)
    adapter = _FakeAdapter("telegram", ok=True)

    stop_event = asyncio.Event()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        # Record the requested interval, then stop the loop after one cycle so
        # the test never waits on the wall clock.
        sleeps.append(seconds)
        stop_event.set()

    monitor = IntegrationStatusMonitor(
        [adapter],
        integration_repo,
        error_log_repo,
        interval_s=60,
        sleep=fake_sleep,
    )
    await monitor.run(stop_event=stop_event)

    assert adapter.calls == 1
    assert sleeps == [60]
    assert integration_repo.get("telegram")["status"] == STATUS_CONNECTED
