"""Admin UI — Test Chat screen.

A chat window that lets the Admin test the knowledge base directly on this
page, without needing an IM client (Telegram/Teams/WhatsApp). Each message is
sent to the backend's ``POST /chat/query`` endpoint, which runs the exact same
Orchestrator pipeline (classify → route → retrieve → generate → log) that
serves the IM frontends, so answers here match what End_Users receive.

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

from typing import Any

from admin_ui.ui.components import (
    ApiClient,
    ApiError,
    inject_base_css,
    render_sidebar_nav,
    section_header,
)

#: Navigation key for this screen.
SCREEN_KEY = "test_chat"

#: Backend endpoint consumed by this screen (see ``app/api/chat.py``).
CHAT_QUERY_PATH = "/chat/query"

#: Session-state key holding the conversation history.
HISTORY_KEY = "test_chat_history"

#: Status → badge caption shown under each assistant answer.
STATUS_LABELS: dict[str, str] = {
    "success": "✅ success",
    "no_match": "🔍 no match",
    "failed": "⚠️ failed",
}


def format_assistant_message(payload: dict[str, Any] | None) -> tuple[str, str]:
    """Turn a ``/chat/query`` response into display text and a status caption.

    Pure helper (no Streamlit): appends a ``Sources:`` footer when citations
    are present — mirroring the IM formatting — and builds the status/latency
    caption line.

    Args:
        payload: The parsed JSON response body, or ``None``.

    Returns:
        A ``(text, caption)`` pair ready for rendering.
    """
    if not isinstance(payload, dict):
        return "The backend returned an unexpected response.", STATUS_LABELS["failed"]

    text = str(payload.get("text") or "").strip() or "(empty answer)"
    citations = [c for c in (payload.get("citations") or []) if c]
    if citations:
        text += "\n\n**Sources:** " + ", ".join(str(c) for c in citations)

    status = str(payload.get("status") or "success")
    caption = STATUS_LABELS.get(status, status)
    latency = payload.get("latency_ms")
    if isinstance(latency, int):
        caption += f" · {latency} ms"
    return text, caption


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

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                payload = ApiClient().post(CHAT_QUERY_PATH, json={"text": prompt})
                text, caption = format_assistant_message(payload)
            except ApiError as exc:
                text, caption = f"Request failed: {exc.message}", STATUS_LABELS["failed"]
        st.markdown(text)
        st.caption(caption)

    history.append({"role": "assistant", "text": text, "caption": caption})


if __name__ == "__main__":
    _main()
