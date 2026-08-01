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
cleared with one click. Answers end with their ``Sources:`` citations; the
generation status and latency are not shown in the chat (they remain
available per query in Analytics).

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
    ApiClient,
    ApiError,
    api_base_url,
    build_url,
    inject_base_css,
    render_sidebar_nav,
    section_header,
)
from admin_ui.ui.auth import require_login

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

#: Query-log endpoint used to restore past questions across sessions.
QUERIES_PATH = "/queries"

#: Query_Log ``tool`` value that marks web Test Chat queries.
WEBCHAT_TOOL = "webchat"

#: Maximum number of past questions listed in the sidebar history.
RECENT_QUESTIONS_LIMIT = 20

#: History labels are truncated to this many characters as a fallback; the
#: CSS single-line ellipsis handles the visual cut at the column edge.
SIDEBAR_LABEL_MAX = 40

#: Height of the scrollable history panel in pixels.
HISTORY_PANEL_HEIGHT_PX = 460


def _inject_history_css() -> None:
    """Style the history topic buttons as a compact, left-aligned list.

    Streamlit gives keyed widgets a stable ``st-key-<key>`` wrapper class, so
    the ``reask_*`` buttons can be restyled (left-aligned, single-line
    ellipsis, quiet hover) without affecting any other button on the page.
    """
    import streamlit as st

    st.markdown(
        """
        <style>
          .ik-history-title {
            font-size: 0.8rem; font-weight: 600; letter-spacing: 0.05em;
            text-transform: uppercase; color: #64748b; margin: 4px 0 8px 4px;
          }
          /* Pin the history block to the viewport while the (taller) chat
             column scrolls. Two ingredients, both required:
             1. Stretch every ancestor between the column and the block to
                the full column height, giving the sticky element room to
                travel (otherwise its containing block is its own height and
                sticky never engages).
             2. Keep the block itself content-sized: Streamlit's own emotion
                CSS sets flex: 1 1 0% on it, which would stretch it to fill
                that tall parent and leave no travel room — hence !important. */
          div[data-testid="stColumn"]:has(div.st-key-ik_history_sticky)
            div:has(div.st-key-ik_history_sticky) {
            height: 100%;
          }
          div.st-key-ik_history_sticky {
            position: sticky !important;
            top: 4.5rem !important;   /* clears Streamlit's fixed header */
            height: fit-content !important;
            flex: 0 0 auto !important;
          }
          div[class*="st-key-reask_"] { width: 100%; }
          div[class*="st-key-reask_"] button {
            display: block; width: 100%;
            text-align: left; justify-content: flex-start;
            padding: 6px 10px; min-height: 0;
            border-radius: 8px; color: #334155;
          }
          div[class*="st-key-reask_"] button:hover {
            background: #e2e8f0; color: #0f172a;
          }
          div[class*="st-key-reask_"] button p {
            font-size: 0.88rem; line-height: 1.3;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def recent_webchat_questions(entries: Any) -> list[tuple[str, str]]:
    """Extract past web-chat questions from a ``/queries`` payload.

    Pure helper: keeps only entries whose tool is :data:`WEBCHAT_TOOL`,
    de-duplicates by question text (keeping the most recent occurrence,
    given newest-first input), and trims timestamps to whole seconds.

    Args:
        entries: The decoded ``/queries`` response (newest first).

    Returns:
        Up to :data:`RECENT_QUESTIONS_LIMIT` ``(timestamp, question)`` pairs,
        newest first.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("tool") != WEBCHAT_TOOL:
            continue
        text = str(entry.get("query_text", "") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ts = str(entry.get("ts", "") or "").replace("T", " ").split(".")[0]
        out.append((ts, text))
        if len(out) >= RECENT_QUESTIONS_LIMIT:
            break
    return out


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


def format_stream_tail(final: dict[str, Any]) -> str:
    """Build the sources footer from the terminal event.

    Pure helper mirroring the IM ``Sources:`` formatting. The status and
    latency are intentionally not surfaced in the chat (they remain available
    in Analytics for every query).

    Args:
        final: The terminal ``done`` event payload (may be empty on failure).

    Returns:
        The sources markdown line, or ``""`` when there are no citations.
    """
    citations = [c for c in (final.get("citations") or []) if c]
    return (
        "**Sources:** " + ", ".join(str(c) for c in citations) if citations else ""
    )


def _main() -> None:
    """Render the Test Chat screen."""
    import streamlit as st

    st.set_page_config(page_title="AIA IntelliKnow KMS — Test Chat", page_icon="💬")
    inject_base_css()
    require_login()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Test Chat", module="test_chat")
    st.caption(
        "Test the knowledge base right here — every message runs the same "
        "pipeline as Telegram / Teams / WhatsApp and is recorded in Analytics."
    )

    history: list[dict[str, Any]] = st.session_state.setdefault(HISTORY_KEY, [])

    _inject_history_css()

    # ChatGPT-style layout: history topics in a narrow left column, the
    # conversation in the wide right column.
    col_hist, col_chat = st.columns([1, 3], gap="medium")

    with col_hist:
        # Keyed wrapper so CSS can pin the whole history block (title +
        # scrollable list) to the viewport while the conversation scrolls.
        sticky = st.container(key="ik_history_sticky")
        with sticky:
            st.markdown(
                '<div class="ik-history-title">Recent</div>',
                unsafe_allow_html=True,
            )
            panel = st.container(height=HISTORY_PANEL_HEIGHT_PX, border=False)
            _render_recent_questions(panel)

    with col_chat:
        if history and st.button("🗑️ Clear conversation"):
            st.session_state[HISTORY_KEY] = []
            st.rerun()

        # Replay the conversation so far.
        for message in history:
            with st.chat_message(message["role"]):
                st.markdown(message["text"])

    # A click on a history topic feeds the pipeline exactly like a typed
    # message. The input itself stays pinned to the bottom of the page.
    prompt = st.chat_input("Ask the knowledge base…") or st.session_state.pop(
        "test_chat_reask", None
    )
    if not prompt:
        return

    with col_chat:
        history.append({"role": "user", "text": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Stream the answer token by token (SSE); the terminal event lands in
        # ``final`` so the sources footer renders at the end.
        final: dict[str, Any] = {}
        with st.chat_message("assistant"):
            streamed = st.write_stream(_stream_answer(prompt, final))
            text = streamed if isinstance(streamed, str) else "".join(streamed)
            sources = format_stream_tail(final)
            if sources:
                st.markdown(sources)
                text += "\n\n" + sources

        history.append({"role": "assistant", "text": text})


def sidebar_label(text: str, limit: int = SIDEBAR_LABEL_MAX) -> str:
    """Truncate a question for its sidebar entry (ChatGPT-style list).

    Args:
        text: The full question text.
        limit: Maximum characters before an ellipsis is appended.

    Returns:
        The label, truncated with ``…`` when longer than ``limit``.
    """
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_recent_questions(container) -> None:
    """Render past questions as a ChatGPT-style history list.

    The chat transcript itself is session-scoped (answers are not persisted),
    but questions survive in the Query_Log, so the history column lists the
    most recent web-chat questions across sessions. Clicking one re-runs it
    through the pipeline (the answer is regenerated, not replayed). Degrades
    silently when the log is empty and to a small note when the backend is
    unreachable.

    Args:
        container: The Streamlit container (e.g. a column) to render into.
    """
    import streamlit as st

    client = ApiClient()
    try:
        entries = client.get(QUERIES_PATH, params={"limit": 50})
    except ApiError:
        container.caption("History unavailable.")
        return
    finally:
        client.close()

    questions = recent_webchat_questions(entries)
    if not questions:
        container.caption("No questions yet.")
        return

    for i, (ts, text) in enumerate(questions):
        if container.button(
            sidebar_label(text),
            key=f"reask_{i}",
            help=f"{ts} — click to ask again",
            type="tertiary",
            use_container_width=True,
        ):
            st.session_state["test_chat_reask"] = text
            st.rerun()


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
