"""Web test-chat API for the Admin UI.

Exposes ``POST /chat/query`` so an Admin can exercise the full query pipeline
(classify → route → retrieve → generate → log) directly from the Admin UI's
Test Chat page, without needing a Telegram/Teams/WhatsApp client. The endpoint
reuses the exact same :class:`~app.core.orchestrator.Orchestrator` instance
that serves the IM frontends, so a web test query behaves — and is logged in
the Query_Log (with tool ``"webchat"``) — just like a real End_User query.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

__all__ = ["chat_router", "ChatQueryRequest", "ChatQueryResponse", "WEBCHAT_TOOL"]

# Tool identifier recorded in the Query_Log for web test-chat queries.
WEBCHAT_TOOL: str = "webchat"

# Inbound validation gate, mirroring the IM adapters (1–4,000 characters).
QUERY_TEXT_MIN: int = 1
QUERY_TEXT_MAX: int = 4000

chat_router = APIRouter(prefix="/chat", tags=["chat"])


class ChatQueryRequest(BaseModel):
    """Body for ``POST /chat/query``."""

    text: str = Field(
        ...,
        description=f"The test query text ({QUERY_TEXT_MIN}–{QUERY_TEXT_MAX} chars).",
    )


class ChatQueryResponse(BaseModel):
    """The generated answer for a web test-chat query."""

    query_id: str = Field(..., description="UUID assigned to this query.")
    text: str = Field(..., description="The generated answer text.")
    citations: list[str] = Field(
        default_factory=list, description="Unique source document names cited."
    )
    status: str = Field(
        ..., description="Generation outcome: success / no_match / failed."
    )
    latency_ms: int = Field(..., description="End-to-end processing latency.")


def _validated_context(payload: ChatQueryRequest, request: Request):
    """Validate a chat submission and resolve the shared Orchestrator.

    Applies the same 1-4,000 character gate as the IM adapters and builds the
    :class:`~app.core.models.QueryContext` for the web chat.

    Args:
        payload: The submitted chat query body.
        request: The current request (source of ``app.state.orchestrator``).

    Returns:
        A ``(ctx, orchestrator)`` pair ready for dispatch.

    Raises:
        HTTPException: 400 when the text fails the length gate; 503 when the
            Orchestrator has not finished starting up yet.
    """
    text = (payload.text or "").strip()
    if not (QUERY_TEXT_MIN <= len(text) <= QUERY_TEXT_MAX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Validation failed for field 'text': must be between "
                f"{QUERY_TEXT_MIN} and {QUERY_TEXT_MAX} characters "
                f"(got {len(text)})."
            ),
        )

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="The query pipeline is not available yet; try again shortly.",
        )

    from app.core.models import QueryContext

    ctx = QueryContext(
        query_id=str(uuid.uuid4()),
        tool=WEBCHAT_TOOL,
        conversation_ref={"channel": "admin_ui"},
        text=text,
        received_at=datetime.now(),
    )
    return ctx, orchestrator


@chat_router.post(
    "/query",
    response_model=ChatQueryResponse,
    summary="Run a test query through the full RAG pipeline",
)
async def chat_query(payload: ChatQueryRequest, request: Request) -> ChatQueryResponse:
    """Answer a test query using the same pipeline as the IM frontends.

    The query is validated with the same 1–4,000 character gate the IM
    adapters apply, then dispatched to the shared Orchestrator. The response
    carries the answer text, its citations, and the generation status so the
    Test Chat page can render them like an IM client would.

    Raises:
        HTTPException: 400 when the text fails the length gate; 503 when the
            Orchestrator has not finished starting up yet.
    """
    ctx, orchestrator = _validated_context(payload, request)

    started = time.monotonic()
    response = await orchestrator.handle_query(ctx)
    latency_ms = int((time.monotonic() - started) * 1000)

    return ChatQueryResponse(
        query_id=ctx.query_id,
        text=response.text,
        citations=list(response.citations or []),
        status=response.status,
        latency_ms=latency_ms,
    )


@chat_router.post(
    "/query/stream",
    summary="Run a test query, streaming the answer as Server-Sent Events",
)
async def chat_query_stream(
    payload: ChatQueryRequest, request: Request
) -> StreamingResponse:
    """Stream a test query's answer as SSE for the web Test Chat page.

    Runs the identical pipeline as ``POST /chat/query`` (same validation, same
    Orchestrator, same Query_Log entry) but delivers the answer incrementally
    so the page can render text within ~1s. Event protocol, one JSON object
    per ``data:`` line:

    * ``{"delta": "<text chunk>"}`` — zero or more, in order.
    * ``{"done": true, "query_id": ..., "citations": [...], "status": ...,
      "latency_ms": ...}`` — exactly one terminal event.

    Raises:
        HTTPException: 400 when the text fails the length gate; 503 when the
            Orchestrator has not finished starting up yet.
    """
    ctx, orchestrator = _validated_context(payload, request)

    async def _events():
        started = time.monotonic()
        async for kind, item in orchestrator.handle_query_stream(ctx):
            if kind == "delta":
                yield f"data: {json.dumps({'delta': item}, ensure_ascii=False)}\n\n"
            else:  # "done" — item is the final GeneratedResponse
                final = {
                    "done": True,
                    "query_id": ctx.query_id,
                    "citations": list(item.citations or []),
                    "status": item.status,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
                yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
