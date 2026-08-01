"""Unit tests for Teams and WhatsApp end-user feedback (👍/👎).

Covers the shared payload helpers, the platform-specific button/reply parsing,
that a successful answer is followed by a feedback prompt, and that a button
press is recorded and acknowledged with the shared narrative. Network calls
are captured with respx (WhatsApp) or an injected token provider + respx
(Teams); no real endpoints are touched.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.bots.base import (
    FEEDBACK_ACK,
    encode_feedback_payload,
    parse_feedback_payload,
)
from app.bots.teams import (
    TeamsAdapter,
    build_feedback_card,
    extract_feedback_submit,
)
from app.bots.whatsapp import WhatsAppAdapter, extract_feedback_reply
from app.core.models import GeneratedResponse


# ---------------------------------------------------------------------------
# Shared payload helpers
# ---------------------------------------------------------------------------


def test_feedback_payload_roundtrip_and_rejects_bad():
    assert parse_feedback_payload(encode_feedback_payload(7, "up")) == (7, "up")
    assert parse_feedback_payload(encode_feedback_payload(9, "down")) == (9, "down")
    for bad in (None, "", "fb:7", "fb:x:up", "fb:7:maybe", "x:7:up"):
        assert parse_feedback_payload(bad) is None


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def test_teams_extract_feedback_submit():
    activity = {
        "type": "message",
        "value": {"action": "feedback", "query_log_id": 12, "verdict": "down"},
    }
    assert extract_feedback_submit(activity) == (12, "down")
    # A normal text message is not a feedback submit.
    assert extract_feedback_submit({"type": "message", "text": "hi"}) is None
    # Malformed value is ignored.
    assert extract_feedback_submit({"value": {"action": "feedback"}}) is None


def test_teams_feedback_card_encodes_verdicts():
    card = build_feedback_card(7)
    actions = card["content"]["actions"]
    data = {a["title"]: a["data"] for a in actions}
    assert data["👍"] == {"action": "feedback", "query_log_id": 7, "verdict": "up"}
    assert data["👎"]["verdict"] == "down"


class _FakeStore:
    def load(self, integration):
        return {"app_id": "a" * 36, "app_password": "secret-pw"}


def _teams_adapter(recorder=None):
    return TeamsAdapter(
        credential_store=_FakeStore(),
        http_client=httpx.AsyncClient(),
        token_provider=lambda: _async("tok"),
        feedback_recorder=recorder,
    )


async def _async(value):
    return value


@respx.mock
async def test_teams_success_answer_followed_by_feedback_card():
    """A successful answer sends the reply, then the feedback card."""
    posts = respx.route(
        method="POST",
        url__regex=r"https://smba\.example/v3/conversations/conv1/activities.*",
    ).mock(return_value=httpx.Response(200, json={"id": "x"}))

    adapter = _teams_adapter(recorder=lambda qid, v: True)
    activity = {
        "type": "message",
        "text": "年假有几天？",
        "serviceUrl": "https://smba.example",
        "conversation": {"id": "conv1"},
        "id": "act1",
    }

    async def dispatcher(ref, text):
        return GeneratedResponse(text="20 days", status="success", query_log_id=7)

    try:
        await adapter.handle_activity(activity, dispatcher)
    finally:
        await adapter.aclose()

    # Two POSTs: the answer, then the feedback card.
    assert posts.call_count == 2
    card_body = json.loads(posts.calls[-1].request.content)
    assert card_body["attachments"][0]["content"]["actions"][0]["title"] == "👍"


@respx.mock
async def test_teams_button_press_records_and_acknowledges():
    """A card submit records the verdict and replies with the shared ack."""
    posts = respx.route(
        method="POST",
        url__regex=r"https://smba\.example/v3/conversations/conv1/activities.*",
    ).mock(return_value=httpx.Response(200, json={"id": "x"}))
    recorded = []

    adapter = _teams_adapter(recorder=lambda qid, v: recorded.append((qid, v)) or True)
    submit = {
        "type": "message",
        "value": {"action": "feedback", "query_log_id": 7, "verdict": "up"},
        "serviceUrl": "https://smba.example",
        "conversation": {"id": "conv1"},
        "id": "act2",
    }

    async def dispatcher(ref, text):  # must NOT be called for a feedback submit
        raise AssertionError("dispatcher should not run for feedback")

    try:
        await adapter.handle_activity(submit, dispatcher)
    finally:
        await adapter.aclose()

    assert recorded == [(7, "up")]
    assert posts.call_count == 1
    assert json.loads(posts.calls[0].request.content)["text"] == FEEDBACK_ACK["up"]


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


def test_whatsapp_extract_feedback_reply():
    notification = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PNID"},
                            "messages": [
                                {
                                    "from": "16505551234",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": encode_feedback_payload(7, "down"),
                                            "title": "👎 No",
                                        },
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    qid, verdict, ref = extract_feedback_reply(notification)
    assert (qid, verdict) == (7, "down")
    assert ref["to"] == "16505551234" and ref["phone_number_id"] == "PNID"
    # A plain text notification is not a feedback reply.
    assert extract_feedback_reply({"entry": []}) is None


def _wa_adapter(recorder=None):
    return WhatsAppAdapter(
        client=httpx.AsyncClient(),
        credentials_resolver=lambda: {
            "access_token": "tok",
            "phone_number_id": "PNID",
            "verify_token": "vt",
        },
        feedback_recorder=recorder,
        backoff_base_s=0.0,
    )


def _wa_text_notification(text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PNID"},
                            "messages": [
                                {
                                    "from": "16505551234",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


@respx.mock
async def test_whatsapp_success_answer_followed_by_prompt():
    """A successful answer sends the text reply, then the interactive prompt."""
    posts = respx.post(
        "https://graph.facebook.com/v20.0/PNID/messages"
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "x"}]}))

    adapter = _wa_adapter(recorder=lambda qid, v: True)

    async def dispatcher(ref, text):
        return GeneratedResponse(text="20 days", status="success", query_log_id=7)

    try:
        await adapter.handle_notification(_wa_text_notification("年假？"), dispatcher)
    finally:
        await adapter.aclose()

    assert posts.call_count == 2  # answer, then interactive prompt
    prompt = json.loads(posts.calls[-1].request.content)
    assert prompt["type"] == "interactive"
    ids = [b["reply"]["id"] for b in prompt["interactive"]["action"]["buttons"]]
    assert ids == [encode_feedback_payload(7, "up"), encode_feedback_payload(7, "down")]


@respx.mock
async def test_whatsapp_button_reply_records_and_acknowledges():
    """A button reply records the verdict and replies with the shared ack."""
    posts = respx.post(
        "https://graph.facebook.com/v20.0/PNID/messages"
    ).mock(return_value=httpx.Response(200, json={"messages": [{"id": "x"}]}))
    recorded = []

    adapter = _wa_adapter(recorder=lambda qid, v: recorded.append((qid, v)) or True)
    notification = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PNID"},
                            "messages": [
                                {
                                    "from": "16505551234",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": encode_feedback_payload(7, "up"),
                                            "title": "👍 Yes",
                                        },
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    async def dispatcher(ref, text):  # must NOT run for a feedback reply
        raise AssertionError("dispatcher should not run for feedback")

    try:
        await adapter.handle_notification(notification, dispatcher)
    finally:
        await adapter.aclose()

    assert recorded == [(7, "up")]
    assert posts.call_count == 1
    body = json.loads(posts.calls[0].request.content)
    assert body["text"]["body"] == FEEDBACK_ACK["up"]
