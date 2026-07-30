# Feature: intelliknow-kms, Integration: Teams webhook flow (task 17.1)
"""End-to-end Teams webhook happy path over the real wiring.

A Bot Framework ``message`` activity is handled by the real
:class:`TeamsAdapter` (JWT seam no-op, token provider injected), which runs the
shared inbound gate, dispatches to the real Orchestrator over a
really-ingested corpus, and delivers the markdown reply to the Bot Connector —
whose HTTP endpoint respx intercepts.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.bots.teams import TeamsAdapter
from app.core.models import GeneratedResponse

from .conftest import HR_SPACE_ID, make_query_context

SERVICE_URL = "https://smba.trafficmanager.example"
CONVERSATION_ID = "conv-1"
ACTIVITY_ID = "act-1"
REPLY_URL = (
    f"{SERVICE_URL}/v3/conversations/{CONVERSATION_ID}/activities/{ACTIVITY_ID}"
)

LEAVE_QUERY = "How many days of annual leave do employees receive per year?"
LEAVE_DOC_BODY = (
    "Annual leave policy: employees receive 20 days of annual leave per "
    "year, accrued monthly and approved by the direct manager."
)


class _FakeCredentialStore:
    """Static Teams app credentials for the adapter's credential seam."""

    def load(self, integration: str) -> dict[str, str] | None:
        return {"app_id": "app-id", "app_password": "app-pw"}


async def _token_provider() -> str:
    """Injected Bot Framework bearer token (skips the client-credential grant)."""
    return "test-bearer-token"


@respx.mock
async def test_teams_webhook_happy_path_delivers_markdown_reply(stack):
    """Activity → gate → orchestrator → markdown reply POSTed to the connector."""
    await stack.ingest(name="leave_policy.txt", body=LEAVE_DOC_BODY)
    stack.ai.classify_json = f'{{"space_id": {HR_SPACE_ID}, "confidence": 95}}'
    route = respx.post(REPLY_URL).mock(
        return_value=httpx.Response(200, json={"id": "reply-1"})
    )

    async def dispatcher(conversation_ref: dict, text: str) -> GeneratedResponse:
        ctx = make_query_context(text, tool="teams")
        ctx.conversation_ref.update(conversation_ref)
        return await stack.orchestrator.handle_query(ctx)

    activity = {
        "type": "message",
        "id": ACTIVITY_ID,
        "text": LEAVE_QUERY,
        "serviceUrl": SERVICE_URL,
        "channelId": "msteams",
        "conversation": {"id": CONVERSATION_ID},
        "from": {"id": "user-1", "name": "End User"},
        "recipient": {"id": "bot-1", "name": "IntelliKnow"},
    }

    adapter = TeamsAdapter(
        credential_store=_FakeCredentialStore(),
        settings=stack.settings,
        http_client=httpx.AsyncClient(),
        token_provider=_token_provider,
    )
    try:
        response = await adapter.handle_activity(activity, dispatcher)
    finally:
        await adapter.aclose()

    # The orchestrator produced a grounded, successful answer.
    assert response is not None and response.status == "success"
    assert response.citations == ["leave_policy.txt"]

    # The reply reached the Bot Connector as markdown with bold sources
    # (Req 8.6) and the bearer token from the token seam.
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-bearer-token"
    payload = json.loads(request.content)
    assert payload["type"] == "message"
    assert payload["textFormat"] == "markdown"
    assert "20 days of annual leave" in payload["text"]
    assert "**Sources:**" in payload["text"] and "leave_policy.txt" in payload["text"]

    # Identities are swapped so the bot answers the originating user.
    assert payload["from"] == activity["recipient"]
    assert payload["recipient"] == activity["from"]

    # Exactly one Success Query_Log entry was written (Req 7.6).
    row = stack.conn.execute("SELECT * FROM query_log").fetchone()
    assert row["detected_space_id"] == HR_SPACE_ID
    assert row["response_status"] == "Success"
