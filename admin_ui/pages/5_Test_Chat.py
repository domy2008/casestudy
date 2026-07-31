"""Admin UI — Test Chat screen.

A chat window that lets the Admin test the knowledge base directly on this
page, without needing an IM client (Telegram/Teams/WhatsApp). Each message is
sent to the backend's ``POST /chat/query/stream`` SSE endpoint, which runs the
exact same Orchestrator pipeline (classify → route → retrieve → generate →
log) that serves the IM frontends, so answers here match what End_Users
receive — but the answer streams in token by token via ``st.write_stream``,
so text starts appearing within about a second.

Rendered with Streamlit's native chat widgets (``st.chat_message`` /
``st.chat_input``); the conversation lives in ``st.session_state`` and can be
cleared with one click. Answers show their ``Sources:`` citations and the
generation status (success / no_match / failed) plus end-to-end latency.

Import-safety
-------------
As with every screen, all Streamlit calls live inside :func:`_main`, which runs
only under ``__main__``. The helpers below are pure (no Streamlit, no I/O) so
this module imports cleanly in headless contexts (including the test suite).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Make the project root importable when Streamlit runs this page directly.
_root = _os.path.abspath(__file__)
for _ in range(4):
    _root = _os.path.dirname(_root)
    if _os.path.isdir(_os.path.join(_root, "admin_ui")):
        break
if _root not in _sys.path:
    _sys.path.insert(0, _root)

import json
from typing import Any, Iterator

import httpx

from admin_ui.ui.components import (
    api_base_url,
    build_url,
    inject_base_css,
    render_sidebar_nav,
    section_header,
)

#: Navigation key for this screen.
SCREEN_KEY = "test_chat"

#: Backend endpoints consumed by this screen (see ``app/api/chat.py``).
CHAT_QUERY_PATH = "/chat/query"
CHAT_STREAM_PATH = "/chat/query/stream"

#: Streaming timeouts: generous connect (classification + retrieval happen
#: before the first byte) and a per-chunk read cap.
STREAM_CONNECT_TIMEOUT_S = 30.0
STREAM_READ_TIMEOUT_S = 30.0

#: Session-state key holding the conversation history.
HISTORY_KEY = "test_chat_history"

#: Status → badge caption shown under each assistant answer.
STATUS_LABELS: dict[str, str] = {
    "success": "✅ success",
    "no_match": "🔍 no match",
    "failed": "⚠️ failed",
}


def parse_sse_event(line: str) -> dict[str, Any] | None:
    """Parse one SSE line from ``/chat/query/stream`` into its JSON payload.

    Pure helper (no Streamlit, no I/O). Lines look like ``data: {...}``;
    anything else (blank keep-alives, malformed JSON) yields ``None``.

    Args:
        line: One line from the SSE response body.

    Returns:
        The decoded event dict, or ``None`` when the line carries no event.
    """
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line[len("data:"):].strip())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def format_stream_tail(final: dict[str, Any]) -> tuple[str, str]:
    """Build the sources footer and status caption from the terminal event.

    Pure helper: mirrors the IM ``Sources:`` formatting and produces the
    status/latency caption shown under the streamed answer.

    Args:
        final: The terminal ``done`` event payload (may be empty on failure).

    Returns:
        A ``(sources_markdown, caption)`` pair; ``sources_markdown`` is empty
        when there are no citations.
    """
    citations = [c for c in (final.get("citations") or []) if c]
    sources = (
        "**Sources:** " + ", ".join(str(c) for c in citations) if citations else ""
    )
    status = str(final.get("status") or "failed")
    caption = STATUS_LABELS.get(status, status)
    latency = final.get("latency_ms")
    if isinstance(latency, int):
        caption += f" · {latency} ms"
    return sources, caption


def _main() -> None:
    """Render the Test Chat screen."""
    import streamlit as st

    st.set_page_config(page_title="IntelliKnow KMS — Test Chat", page_icon="💬")
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Test Chat", module="test_chat")
    st.caption(
        "Test the knowledge base right here — every message runs the same "
        "pipeline as Telegram / Teams / WhatsApp and is recorded in Analytics."
    )

    history: list[dict[str, Any]] = st.session_state.setdefault(HISTORY_KEY, [])

    if history and st.button("🗑️ Clear conversation"):
        st.session_state[HISTORY_KEY] = []
        st.rerun()

    # Replay the conversation so far.
    for message in history:
        with st.chat_message(message["role"]):
            st.markdown(message["text"])
            if message.get("caption"):
                st.caption(message["caption"])

    prompt = st.chat_input("Ask the knowledge base…")
    if not prompt:
        return

    history.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Stream the answer token by token (SSE); the terminal event lands in
    # ``final`` so the sources footer and status caption render at the end.
    final: dict[str, Any] = {}
    with st.chat_message("assistant"):
        streamed = st.write_stream(_stream_answer(prompt, final))
        text = streamed if isinstance(streamed, str) else "".join(streamed)
        sources, caption = format_stream_tail(final)
        if sources:
            st.markdown(sources)
            text += "\n\n" + sources
        st.caption(caption)

    history.append({"role": "assistant", "text": text, "caption": caption})


def _stream_answer(prompt: str, final: dict[str, Any]) -> Iterator[str]:
    """Yield answer chunks from the SSE endpoint, capturing the final event.

    Opens a streaming POST against ``/chat/query/stream`` and yields each
    ``delta`` as it arrives (consumed by ``st.write_stream``). The terminal
    ``done`` event is written into the caller-supplied ``final`` dict. Any
    transport/HTTP error is surfaced as a final text chunk with a failed
    status, so the page never crashes on a broken stream.

    Args:
        prompt: The user's chat message.
        final: Mutable dict the terminal event payload is stored into.
    """
    url = build_url(api_base_url(), CHAT_STREAM_PATH)
    timeout = httpx.Timeout(
        STREAM_READ_TIMEOUT_S, connect=STREAM_CONNECT_TIMEOUT_S
    )
    try:
        with httpx.stream("POST", url, json={"text": prompt}, timeout=timeout) as r:
            if r.status_code >= 400:
                r.read()
                yield f"Request failed with status {r.status_code}."
                return
            for line in r.iter_lines():
                event = parse_sse_event(line)
                if event is None:
                    continue
                if event.get("done"):
                    final.update(event)
                    return
                delta = event.get("delta")
                if delta:
                    yield str(delta)
    except httpx.HTTPError as exc:
        yield f"Request failed: could not reach the backend ({exc})."


if __name__ == "__main__":
    _main()
