"""Unit tests for the query dispatcher (:mod:`app.bots.dispatcher`).

These cover the dispatcher's shared-dispatch responsibilities, all with fakes
for the orchestrator and adapter so no network or database is touched:

* the 30-second processing deadline produces the could-not-process message and
  delivers it (Req 2.5),
* a delivery that keeps failing is retried exactly two more times (three
  attempts total) and then logged with timestamp, tool, and reason (Req 2.6,
  2.7),
* a proxy/connectivity failure surfaced by the adapter still results in a
  logged final failure (Req 2.6, 2.7),
* an error-log writer that itself raises never interrupts processing (Req 2.8).

Validates: Requirements 2.5, 2.6, 2.7, 12.6.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.bots.dispatcher import (
    COULD_NOT_PROCESS_MESSAGE,
    SEND_OPERATION,
    QueryDispatcher,
)
from app.core.models import GeneratedResponse, QueryContext


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeOrchestrator:
    """Orchestrator stand-in returning a preset response (or hanging/raising).

    * Default: returns ``response`` immediately.
    * ``hang=True``: sleeps far past any test deadline so ``wait_for`` must
      cancel it (simulates a query that never returns in time, Req 2.5).
    * ``raises``: raise this exception from ``handle_query``.
    """

    def __init__(
        self,
        *,
        response: GeneratedResponse | None = None,
        hang: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self.response = response or GeneratedResponse(
            text="an answer", citations=["Doc A"], status="success"
        )
        self.hang = hang
        self.raises = raises
        self.calls = 0
        self.cancelled = False

    async def handle_query(self, ctx: QueryContext) -> GeneratedResponse:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.hang:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("hang should have been cancelled by the deadline")
        return self.response


class FakeAdapter:
    """Frontend adapter stand-in recording sends and failing on demand.

    ``fail_times`` controls how many leading ``send`` calls raise ``error``
    before a subsequent call succeeds; set it high (or use ``always_fail``) to
    exhaust every retry.
    """

    tool_name = "telegram"

    def __init__(
        self,
        *,
        fail_times: int = 0,
        error: Exception | None = None,
        format_prefix: str = "FORMATTED: ",
    ) -> None:
        self._fail_times = fail_times
        self._error = error or RuntimeError("delivery boom")
        self._format_prefix = format_prefix
        self.sent: list[tuple[dict, str]] = []
        self.send_attempts = 0
        self.format_calls = 0

    def format(self, response: GeneratedResponse) -> str:
        self.format_calls += 1
        return f"{self._format_prefix}{response.text}"

    async def send(self, conversation_ref: dict, text: str) -> None:
        self.send_attempts += 1
        if self.send_attempts <= self._fail_times:
            raise self._error
        self.sent.append((conversation_ref, text))


class FakeErrorLog:
    """Error-log sink recording inserts, optionally raising on insert."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.entries: list[tuple[str, str, str]] = []

    def insert(self, tool: str, operation: str, error_detail: str) -> int:
        if self._raises is not None:
            raise self._raises
        self.entries.append((tool, operation, error_detail))
        return len(self.entries)


def _ctx() -> QueryContext:
    return QueryContext(
        query_id="q-1",
        tool="telegram",
        conversation_ref={"chat_id": 42},
        text="what is the pto policy?",
        received_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_successful_query_is_formatted_and_delivered():
    """A produced response is formatted and delivered once (Req 2.3)."""
    orch = FakeOrchestrator(
        response=GeneratedResponse(text="hello", citations=[], status="success")
    )
    adapter = FakeAdapter()
    dispatcher = QueryDispatcher(orch, adapter)

    await dispatcher.dispatch(_ctx())

    assert adapter.send_attempts == 1
    assert adapter.sent == [({"chat_id": 42}, "FORMATTED: hello")]
    assert adapter.format_calls == 1


# ---------------------------------------------------------------------------
# Req 2.5 - 30s processing deadline
# ---------------------------------------------------------------------------


async def test_deadline_delivers_could_not_process_message():
    """When processing exceeds the deadline, deliver could-not-process (Req 2.5)."""
    orch = FakeOrchestrator(hang=True)
    adapter = FakeAdapter()
    # Tiny deadline so the test does not actually wait 30 seconds.
    dispatcher = QueryDispatcher(orch, adapter, deadline_s=0.05)

    await dispatcher.dispatch(_ctx())

    # The could-not-process message is delivered verbatim (not formatted).
    assert adapter.sent == [({"chat_id": 42}, COULD_NOT_PROCESS_MESSAGE)]
    # The hung pipeline was cancelled by the deadline.
    assert orch.cancelled is True
    # No response was formatted since none was produced.
    assert adapter.format_calls == 0


async def test_pipeline_failure_delivers_could_not_process_message():
    """A raising pipeline also yields the could-not-process message (Req 2.5)."""
    orch = FakeOrchestrator(raises=RuntimeError("pipeline exploded"))
    adapter = FakeAdapter()
    dispatcher = QueryDispatcher(orch, adapter)

    await dispatcher.dispatch(_ctx())

    assert adapter.sent == [({"chat_id": 42}, COULD_NOT_PROCESS_MESSAGE)]


# ---------------------------------------------------------------------------
# Req 2.6 / 2.7 - delivery retries then logging
# ---------------------------------------------------------------------------


async def test_delivery_retries_exactly_two_more_times_then_logs():
    """Failed delivery retries exactly 2 more times, then logs (Req 2.6/2.7)."""
    orch = FakeOrchestrator()
    adapter = FakeAdapter(fail_times=99, error=RuntimeError("boom"))
    error_log = FakeErrorLog()
    dispatcher = QueryDispatcher(orch, adapter, error_log=error_log)

    await dispatcher.dispatch(_ctx())

    # Exactly three attempts: the first plus two additional retries (Req 2.6).
    assert adapter.send_attempts == 3
    assert adapter.sent == []  # nothing ever delivered
    # Exactly one error-log entry with tool, send operation, and reason (Req 2.7).
    assert len(error_log.entries) == 1
    tool, operation, reason = error_log.entries[0]
    assert tool == "telegram"
    assert operation == SEND_OPERATION
    assert "boom" in reason


async def test_delivery_succeeds_on_third_attempt_no_log():
    """Two failures then success delivers without logging an error (Req 2.6)."""
    orch = FakeOrchestrator(
        response=GeneratedResponse(text="ok", citations=[], status="success")
    )
    adapter = FakeAdapter(fail_times=2, error=RuntimeError("transient"))
    error_log = FakeErrorLog()
    dispatcher = QueryDispatcher(orch, adapter, error_log=error_log)

    await dispatcher.dispatch(_ctx())

    assert adapter.send_attempts == 3
    assert adapter.sent == [({"chat_id": 42}, "FORMATTED: ok")]
    assert error_log.entries == []


async def test_proxy_connectivity_failure_is_logged(monkeypatch):
    """A proxy/connectivity delivery failure surfaces and is logged (Req 2.6/2.7/12.6)."""
    from app.bots.telegram import TelegramProxyError

    orch = FakeOrchestrator()
    adapter = FakeAdapter(
        fail_times=99, error=TelegramProxyError("Outbound_Proxy unreachable")
    )
    error_log = FakeErrorLog()
    dispatcher = QueryDispatcher(orch, adapter, error_log=error_log)

    await dispatcher.dispatch(_ctx())

    assert adapter.send_attempts == 3
    assert len(error_log.entries) == 1
    tool, operation, reason = error_log.entries[0]
    assert tool == "telegram"
    assert operation == SEND_OPERATION
    assert "Outbound_Proxy unreachable" in reason


# ---------------------------------------------------------------------------
# Req 2.8 - logging failure never interrupts processing
# ---------------------------------------------------------------------------


async def test_error_log_failure_is_swallowed():
    """An error-log writer that raises never propagates (Req 2.8)."""
    orch = FakeOrchestrator()
    adapter = FakeAdapter(fail_times=99, error=RuntimeError("boom"))
    error_log = FakeErrorLog(raises=RuntimeError("db is down"))
    dispatcher = QueryDispatcher(orch, adapter, error_log=error_log)

    # Must not raise despite both delivery and error logging failing.
    await dispatcher.dispatch(_ctx())

    assert adapter.send_attempts == 3
