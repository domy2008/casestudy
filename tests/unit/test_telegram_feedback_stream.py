"""Unit tests for Telegram feedback buttons and streaming delivery.

Covers the pure helpers (:func:`build_feedback_keyboard`,
:func:`parse_feedback_callback`), the ``callback_query`` handling in the
polling dispatch (verdict recorded + acknowledged), and the send-then-edit
streaming delivery (initial send, final edit carrying the formatted message
plus the feedback keyboard). All Bot API traffic is intercepted with respx.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.bots.telegram import (
    TELEGRAM_API_BASE,
    TelegramAdapter,
    build_feedback_keyboard,
    parse_feedback_callback,
)
from app.core.models import GeneratedResponse

TOKEN = "TEST-TOKEN"
API = f"{TELEGRAM_API_BASE}/bot{TOKEN}"


def _adapter(**kwargs) -> TelegramAdapter:
    return TelegramAdapter(
        client=httpx.AsyncClient(),
        token_resolver=lambda: TOKEN,
        backoff_base_s=0.0,
        **kwargs,
    )


def test_feedback_callback_parsing():
    """Round-trips the keyboard payloads; rejects malformed data."""
    keyboard = build_feedback_keyboard(42)
    buttons = keyboard["inline_keyboard"][0]
    assert parse_feedback_callback(buttons[0]["callback_data"]) == (42, "up")
    assert parse_feedback_callback(buttons[1]["callback_data"]) == (42, "down")

    for bad in (None, "", "fb:42", "fb:x:up", "fb:42:maybe", "other:42:up"):
        assert parse_feedback_callback(bad) is None


@respx.mock
async def test_callback_query_records_and_acknowledges():
    """A 👍 press records the verdict, answers the callback, removes buttons."""
    recorded: list[tuple[int, str]] = []
    answer = respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    unmark = respx.post(f"{API}/editMessageReplyMarkup").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    adapter = _adapter(
        feedback_recorder=lambda qid, verdict: recorded.append((qid, verdict)) or True
    )
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "data": "fb:7:up",
            "message": {"message_id": 99, "chat": {"id": 42}},
        },
    }
    try:
        await adapter._dispatch_update(update, handler=None)
    finally:
        await adapter.aclose()

    from app.bots.base import FEEDBACK_ACK

    assert recorded == [(7, "up")]
    assert answer.call_count == 1
    assert json.loads(answer.calls[0].request.content)["text"] == FEEDBACK_ACK["up"]
    assert unmark.call_count == 1  # one vote per answer: buttons removed


@respx.mock
async def test_send_stream_edits_and_attaches_feedback_keyboard():
    """Streaming: initial send, final edit with Sources + feedback buttons."""
    send = respx.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 55}}
        )
    )
    edit = respx.post(f"{API}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    async def events():
        yield ("delta", "Employees receive 20 days of ")
        yield ("delta", "annual leave per year.")
        yield (
            "done",
            GeneratedResponse(
                text="Employees receive 20 days of annual leave per year.",
                citations=["leave_policy.txt"],
                status="success",
                query_log_id=7,
            ),
        )

    adapter = _adapter(feedback_recorder=lambda qid, verdict: True)
    try:
        response = await adapter.send_stream({"chat_id": 42}, events())
    finally:
        await adapter.aclose()

    assert response.status == "success"
    assert send.call_count == 1  # one initial message, then edits only
    final = json.loads(edit.calls[-1].request.content)
    assert "20 days of annual leave" in final["text"]
    assert "Sources:" in final["text"] and "leave_policy.txt" in final["text"]
    assert final["reply_markup"] == build_feedback_keyboard(7)


@respx.mock
async def test_send_stream_no_feedback_keyboard_without_recorder_or_id():
    """No recorder wired (or no query_log_id) → final message has no buttons."""
    respx.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 55}}
        )
    )
    edit = respx.post(f"{API}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    async def events():
        yield ("delta", "A sufficiently long first chunk of text.")
        yield ("done", GeneratedResponse(text="Answer.", status="success"))

    adapter = _adapter()  # no feedback_recorder
    try:
        await adapter.send_stream({"chat_id": 42}, events())
    finally:
        await adapter.aclose()

    assert "reply_markup" not in json.loads(edit.calls[-1].request.content)
