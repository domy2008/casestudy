"""Frontend adapter contract and the shared inbound message-validation gate.

This module defines two things used by every Frontend_Tool integration
(Telegram, Teams):

1. :class:`FrontendAdapter` - the :class:`typing.Protocol` that each adapter
   implements so the rest of the system can send, format, and connectivity-test
   integrations uniformly (see the design's "Frontend Integration Module").

2. The inbound validation gate (:func:`evaluate_inbound`) - a pure, testable
   function that decides whether a raw inbound message should be forwarded to
   the Orchestrator. Per Req 2.1/2.2, a message is forwarded if and only if it
   contains text of 1 to 4,000 characters; otherwise it is rejected and a
   rejection message is delivered back to the originating conversation while
   nothing reaches the Orchestrator.

Keeping the gate as a pure function (raw text in, decision out) lets both
adapters reuse identical logic and lets it be property-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.models import ConnectivityResult, GeneratedResponse

__all__ = [
    "MIN_QUERY_LENGTH",
    "MAX_QUERY_LENGTH",
    "REJECTION_MESSAGE",
    "GateDecision",
    "evaluate_inbound",
    "FrontendAdapter",
]

# Inclusive character bounds for a forwardable query (Req 2.1).
MIN_QUERY_LENGTH = 1
MAX_QUERY_LENGTH = 4000

# Delivered back to the originating conversation when a message is rejected
# (Req 2.2). Phrased to tell the End_User exactly what is supported.
REJECTION_MESSAGE = (
    "Sorry, I can only answer text queries of up to "
    f"{MAX_QUERY_LENGTH:,} characters. Please send your question as text."
)


@dataclass(frozen=True)
class GateDecision:
    """The outcome of the inbound message-validation gate.

    Immutable so callers can treat it as a value. Exactly one of the two
    branches applies: either the message is forwarded (``forward is True`` with
    ``query_text`` set and ``rejection_message`` ``None``) or it is rejected
    (``forward is False`` with ``rejection_message`` set and ``query_text``
    ``None``).

    Attributes:
        forward: ``True`` when the message should be forwarded to the
            Orchestrator; ``False`` when it should be rejected.
        query_text: The validated query text to forward, present only when
            ``forward`` is ``True``; otherwise ``None``.
        rejection_message: The message to deliver back to the originating
            conversation, present only when ``forward`` is ``False``; otherwise
            ``None``.
    """

    forward: bool
    query_text: str | None = None
    rejection_message: str | None = None


def evaluate_inbound(text: str | None) -> GateDecision:
    """Decide whether an inbound message should reach the Orchestrator.

    This is the shared inbound validation gate for all frontend adapters. It is
    a pure function: it performs no I/O and simply maps the extracted message
    text to a :class:`GateDecision`. Adapters are responsible for extracting the
    text payload from their tool-specific message envelope first, passing
    ``None`` when the message carries no text content (e.g. an image or
    sticker).

    The message is forwarded if and only if ``text`` is a string whose length
    is between :data:`MIN_QUERY_LENGTH` and :data:`MAX_QUERY_LENGTH` inclusive
    (Req 2.1). Any other case - no text content, an empty string, or text longer
    than :data:`MAX_QUERY_LENGTH` - is rejected with :data:`REJECTION_MESSAGE`
    and is not forwarded (Req 2.2).

    Args:
        text: The extracted text content of the inbound message, or ``None``
            when the message has no text content.

    Returns:
        A :class:`GateDecision`. When forwarding, ``query_text`` holds the
        validated text and ``rejection_message`` is ``None``. When rejecting,
        ``rejection_message`` holds :data:`REJECTION_MESSAGE` and ``query_text``
        is ``None``.
    """
    if isinstance(text, str) and MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH:
        return GateDecision(forward=True, query_text=text)
    return GateDecision(forward=False, rejection_message=REJECTION_MESSAGE)


@runtime_checkable
class FrontendAdapter(Protocol):
    """Contract implemented once per Frontend_Tool (Telegram, Teams).

    Each adapter knows how to talk to a single messaging platform: send a
    message to a conversation, format a generated response for that platform's
    constraints, and run an end-to-end connectivity check. The inbound
    validation gate (:func:`evaluate_inbound`) is shared and lives outside the
    adapter so behavior is identical across tools.

    Attributes:
        tool_name: The Frontend_Tool identifier, e.g. ``"telegram"`` or
            ``"teams"``.
    """

    tool_name: str

    async def send(self, conversation_ref: dict, text: str) -> None:
        """Deliver a text message to a conversation on this Frontend_Tool.

        Args:
            conversation_ref: Tool-specific reply address identifying the
                destination conversation (e.g. a Telegram ``chat_id`` or a Teams
                conversation reference).
            text: The message body to deliver, already formatted for this tool.

        Returns:
            ``None``. Implementations perform delivery (including any retries)
            as a side effect.
        """
        ...

    def format(self, response: GeneratedResponse) -> str:
        """Render a generated response as a string for this Frontend_Tool.

        Applies tool-specific formatting and length constraints (e.g. plain
        text within Telegram's 4,096-character limit, or Teams-native
        markdown), preserving citations even when the body must be truncated
        (Req 8.5/8.6/8.7).

        Args:
            response: The generated response, including answer text, citations,
                and status.

        Returns:
            The formatted message string ready to pass to :meth:`send`.
        """
        ...

    async def check_connectivity(self) -> ConnectivityResult:
        """Run an end-to-end connectivity check against this Frontend_Tool.

        Used by the Admin test function and the background status monitor to
        determine Connected/Error/Disconnected status (Req 3.1/3.2).

        Returns:
            A :class:`ConnectivityResult` describing whether the check
            succeeded, any failure detail, and whether it timed out.
        """
        ...
