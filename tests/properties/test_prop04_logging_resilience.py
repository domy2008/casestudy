# Feature: intelliknow-kms, Property 4: Logging failures never alter the response
"""Property 4: Logging failures never alter the response.

*For any* query, running the dispatch pipeline with an integration error-log
writer that raises exceptions produces the same delivered response as running it
with a working writer, and processing continues uninterrupted.

This exercises the dispatcher's swallow-and-continue guard (Req 2.8) together
with the response-path fire-and-forget logging invariant (Req 10.6): a failing
logger must never change what the End_User receives, nor cause the dispatch to
raise. We drive the dispatcher across a wide space of inputs — successful
responses, no-match responses, failed responses, and forced delivery failures —
and assert that a raising error-log writer yields byte-for-byte the same
delivered messages as a working one.

Validates: Requirements 2.8, 10.6.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.bots.dispatcher import QueryDispatcher
from app.core.models import GeneratedResponse, QueryContext


# ---------------------------------------------------------------------------
# Fakes (no network, no database)
# ---------------------------------------------------------------------------


class _Orchestrator:
    """Returns a preset response for every query."""

    def __init__(self, response: GeneratedResponse) -> None:
        self._response = response

    async def handle_query(self, ctx: QueryContext) -> GeneratedResponse:
        return self._response


class _Adapter:
    """Records every delivered message; can be forced to always fail sends."""

    tool_name = "telegram"

    def __init__(self, *, always_fail: bool) -> None:
        self._always_fail = always_fail
        self.sent: list[tuple[dict, str]] = []

    def format(self, response: GeneratedResponse) -> str:
        footer = ""
        if response.citations:
            footer = " | sources: " + ", ".join(response.citations)
        return f"[{response.status}] {response.text}{footer}"

    async def send(self, conversation_ref: dict, text: str) -> None:
        if self._always_fail:
            raise RuntimeError("delivery failed")
        self.sent.append((conversation_ref, text))


class _RaisingErrorLog:
    """Error-log writer whose insert always raises (the failing writer)."""

    def insert(self, tool: str, operation: str, error_detail: str) -> int:
        raise RuntimeError("error log is unavailable")


class _WorkingErrorLog:
    """Error-log writer that records inserts (the control writer)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def insert(self, tool: str, operation: str, error_detail: str) -> int:
        self.entries.append((tool, operation, error_detail))
        return len(self.entries)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_statuses = st.sampled_from(["success", "no_match", "failed"])
_citation_lists = st.lists(
    st.text(min_size=1, max_size=20), min_size=0, max_size=4, unique=True
)


@st.composite
def _responses(draw) -> GeneratedResponse:
    return GeneratedResponse(
        text=draw(st.text(min_size=0, max_size=200)),
        citations=draw(_citation_lists),
        status=draw(_statuses),
    )


def _ctx(text: str) -> QueryContext:
    return QueryContext(
        query_id="q",
        tool="telegram",
        conversation_ref={"chat_id": 7},
        text=text,
        received_at=datetime.now(timezone.utc),
    )


async def _run(response: GeneratedResponse, query_text: str, *, always_fail: bool,
               error_log) -> list[tuple[dict, str]]:
    """Dispatch one query and return the list of delivered messages."""
    adapter = _Adapter(always_fail=always_fail)
    dispatcher = QueryDispatcher(
        _Orchestrator(response), adapter, error_log=error_log
    )
    await dispatcher.dispatch(_ctx(query_text))
    return adapter.sent


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    response=_responses(),
    query_text=st.text(min_size=1, max_size=100),
    delivery_fails=st.booleans(),
)
def test_logging_failure_does_not_alter_delivered_response(
    response: GeneratedResponse, query_text: str, delivery_fails: bool
) -> None:
    """A raising error-log writer delivers identically to a working one (Req 2.8/10.6)."""
    delivered_with_raising = asyncio.run(
        _run(
            response,
            query_text,
            always_fail=delivery_fails,
            error_log=_RaisingErrorLog(),
        )
    )
    delivered_with_working = asyncio.run(
        _run(
            response,
            query_text,
            always_fail=delivery_fails,
            error_log=_WorkingErrorLog(),
        )
    )

    # The delivered response is identical regardless of whether logging failed:
    # the logger can never alter what the End_User receives, and dispatch never
    # raised (we reached this assertion).
    assert delivered_with_raising == delivered_with_working

    # When delivery itself failed, nothing was delivered in either run; when it
    # succeeded, exactly one identical message was delivered.
    if delivery_fails:
        assert delivered_with_raising == []
    else:
        assert len(delivered_with_raising) == 1
