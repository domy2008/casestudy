# Feature: intelliknow-kms, Integration: Telegram query flow (task 17.1)
"""End-to-end Telegram query flow over the real wiring.

Scenarios (design task 17.1), with DashScope faked at the client seam and the
Telegram Bot API intercepted via ``respx``:

* happy path: inbound message → gate → classify → FAISS retrieval over a
  really-ingested corpus → RAG → reply delivered to ``sendMessage``, routed to
  the matched (above-threshold) Intent_Space;
* below-threshold classification falls back to the General_Space and delivers
  the no-match message when General holds no documents;
* an AI failure during generation delivers the could-not-process message and
  writes a ``Failed`` Query_Log entry.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.ai.prompts import NO_MATCH_MESSAGE
from app.bots.dispatcher import QueryDispatcher
from app.bots.telegram import TELEGRAM_API_BASE, TelegramAdapter
from app.kb.store import IntegrationErrorLogRepository, IntentSpaceRepository
from app.rag.generator import COULD_NOT_PROCESS_MESSAGE

from .conftest import HR_SPACE_ID, make_query_context

TOKEN = "TEST-TOKEN"
SEND_URL = f"{TELEGRAM_API_BASE}/bot{TOKEN}/sendMessage"

LEAVE_QUERY = "How many days of annual leave do employees receive per year?"
LEAVE_DOC_BODY = (
    "Annual leave policy: employees receive 20 days of annual leave per "
    "year, accrued monthly and approved by the direct manager."
)


def _adapter(stack) -> TelegramAdapter:
    """A real TelegramAdapter whose HTTP layer respx intercepts."""
    return TelegramAdapter(
        settings=stack.settings,
        client=httpx.AsyncClient(),
        token_resolver=lambda: TOKEN,
        backoff_base_s=0.0,
    )


def _sent_texts(route) -> list[str]:
    """Extract the message bodies POSTed to ``sendMessage``."""
    return [json.loads(call.request.content)["text"] for call in route.calls]


async def _run_message(stack, adapter, text: str) -> None:
    """Drive one raw Telegram update through gate → dispatcher → delivery."""
    dispatcher = QueryDispatcher(
        stack.orchestrator, adapter, error_log=IntegrationErrorLogRepository(stack.conn)
    )

    async def handler(conversation_ref: dict, query_text: str) -> None:
        ctx = make_query_context(query_text)
        ctx.conversation_ref.update(conversation_ref)
        await dispatcher.dispatch(ctx)

    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": text}}
    await adapter._dispatch_update(update, handler)


@respx.mock
async def test_telegram_happy_path_routes_above_threshold_and_delivers(stack):
    """Message → classify → retrieve → RAG → cited reply on the matched space."""
    doc_id = await stack.ingest(name="leave_policy.txt", body=LEAVE_DOC_BODY)
    assert doc_id > 0
    stack.ai.classify_json = f'{{"space_id": {HR_SPACE_ID}, "confidence": 95}}'
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    adapter = _adapter(stack)
    try:
        await _run_message(stack, adapter, LEAVE_QUERY)
    finally:
        await adapter.aclose()

    # Reply delivered once, grounded in the corpus, citing the source doc.
    assert route.call_count == 1
    sent = _sent_texts(route)[0]
    assert "20 days of annual leave" in sent
    assert "Sources:" in sent and "leave_policy.txt" in sent

    # Routed to the matched HR space with a Success log entry (Req 7.2, 7.6).
    row = stack.conn.execute("SELECT * FROM query_log").fetchone()
    assert row["detected_space_id"] == HR_SPACE_ID
    assert row["confidence"] == 95.0
    assert row["response_status"] == "Success"


@respx.mock
async def test_below_threshold_falls_back_to_general_space(stack):
    """Confidence below the threshold routes to General → no-match reply."""
    await stack.ingest(name="leave_policy.txt", body=LEAVE_DOC_BODY)
    stack.ai.classify_json = f'{{"space_id": {HR_SPACE_ID}, "confidence": 40}}'
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    adapter = _adapter(stack)
    try:
        await _run_message(stack, adapter, LEAVE_QUERY)
    finally:
        await adapter.aclose()

    # General holds no documents → the clear no-match message (Req 8.3/8.4).
    assert NO_MATCH_MESSAGE in _sent_texts(route)[0]
    general_id = int(IntentSpaceRepository(stack.conn).get_general()["id"])
    row = stack.conn.execute("SELECT * FROM query_log").fetchone()
    assert row["detected_space_id"] == general_id
    assert row["response_status"] == "Success"


@respx.mock
async def test_generation_failure_delivers_error_and_logs_failed(stack):
    """An AI failure during generation → error reply + Failed log (Req 8.9)."""
    await stack.ingest(name="leave_policy.txt", body=LEAVE_DOC_BODY)
    stack.ai.generate_error = RuntimeError("dashscope down")
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    adapter = _adapter(stack)
    try:
        await _run_message(stack, adapter, LEAVE_QUERY)
    finally:
        await adapter.aclose()

    assert COULD_NOT_PROCESS_MESSAGE in _sent_texts(route)[0]
    row = stack.conn.execute("SELECT * FROM query_log").fetchone()
    assert row["response_status"] == "Failed"
