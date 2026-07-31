"""Telegram Frontend_Tool adapter: long-polling intake, sending, and formatting.

Telegram cannot be reached directly from AWS China, so every Telegram Bot API
call in this module is routed through the Outbound_Proxy configured on the
httpx client (``proxy=settings.telegram_proxy_url``) and the integration uses
**long polling** (``getUpdates``) rather than webhooks, which would additionally
require Telegram to reach *us* (equally blocked). See the design's "Key Design
Decisions" and "Frontend Integration Module" sections.

The adapter implements the :class:`app.bots.base.FrontendAdapter` protocol:

* :meth:`TelegramAdapter.run_polling` runs the asyncio ``getUpdates`` loop; each
  update is passed through the shared inbound gate
  (:func:`app.bots.base.evaluate_inbound`) and, when valid, handed to a caller
  supplied handler/dispatcher callback as ``(conversation_ref, text)``
  (Req 2.1/2.2). Rejected messages get the shared rejection reply.
* :meth:`TelegramAdapter.send` posts ``sendMessage`` through the proxy, retrying
  up to :data:`PROXY_MAX_RETRIES` times on proxy connectivity errors and logging
  each proxy failure to the optional integration error log (Req 12.2, 12.6).
* :meth:`TelegramAdapter.format` renders plain text plus a ``Sources:`` footer,
  hard-capped at :data:`TELEGRAM_MAX_MESSAGE_CHARS` with body-only truncation so
  citations always survive and a truncation indicator is appended (Req 8.5/8.7).
* :meth:`TelegramAdapter.check_connectivity` performs a ``getMe`` end-to-end
  check through the proxy (Req 3.1/3.2).

Testability seam: the httpx client and the bot-token resolver are injected
through the constructor, so tests can drive the adapter with ``respx`` and a
fake token and never make a real network call. By default the token is read
from the :class:`~app.security.credentials.CredentialStore` (``telegram`` /
``bot_token``, Req 11.4) and the client is created with the proxy from
:class:`~app.config.Settings`.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol

import httpx

from app.bots.base import FrontendAdapter, evaluate_inbound
from app.config import Settings, get_settings
from app.core.models import ConnectivityResult, GeneratedResponse
from app.security.credentials import CredentialStore

__all__ = [
    "TELEGRAM_API_BASE",
    "TELEGRAM_MAX_MESSAGE_CHARS",
    "TRUNCATION_INDICATOR",
    "SOURCES_HEADER",
    "PROXY_MAX_RETRIES",
    "LONG_POLL_TIMEOUT_SECONDS",
    "STREAM_EDIT_INTERVAL_S",
    "STREAM_MIN_FIRST_CHARS",
    "FEEDBACK_CALLBACK_PREFIX",
    "TelegramConfigError",
    "TelegramProxyError",
    "ErrorLog",
    "QueryHandler",
    "FeedbackRecorder",
    "build_feedback_keyboard",
    "parse_feedback_callback",
    "build_sources_footer",
    "strip_markdown",
    "format_telegram_message",
    "TelegramAdapter",
]

logger = logging.getLogger(__name__)

# Base URL for the Telegram Bot API. Per-call URLs are
# ``{TELEGRAM_API_BASE}/bot{token}/{method}``.
TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram's hard maximum message length in characters (Req 8.5).
TELEGRAM_MAX_MESSAGE_CHARS = 4096

# Appended to a message whose body had to be truncated so it fits the limit
# (Req 8.7). Kept short so it consumes minimal budget.
TRUNCATION_INDICATOR = "\n\n[... response truncated]"

# Header introducing the citation footer.
SOURCES_HEADER = "Sources:"

# Number of times a Telegram API request is retried after a proxy connectivity
# failure, in addition to the initial attempt (Req 12.6; design: "up to 3
# retries").
PROXY_MAX_RETRIES = 3

# How long the server holds a ``getUpdates`` long-poll open before returning an
# empty result set. The httpx client read timeout must exceed this.
LONG_POLL_TIMEOUT_SECONDS = 25

# Default seconds between proxy retries (multiplied by the retry index for a
# simple linear backoff). Tests inject ``0.0`` to run without delay.
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

# httpx exceptions that indicate the Outbound_Proxy (or the connection through
# it) is unreachable, and therefore warrant a retry + proxy-failure log entry
# (Req 12.6). HTTP status errors are NOT in this set - they are not connectivity
# failures and must not be retried here.
_PROXY_ERRORS: tuple[type[Exception], ...] = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)


class TelegramConfigError(RuntimeError):
    """Raised when the Telegram bot token is missing from the Credential_Store."""


class TelegramProxyError(RuntimeError):
    """Raised when a Telegram request fails after exhausting proxy retries.

    Indicates the Outbound_Proxy was unreachable for every attempt (Req 12.6).
    The originating httpx exception is attached via ``__cause__``.
    """


class ErrorLog(Protocol):
    """Minimal sink for integration error-log entries (Req 2.7, 3.3, 12.6)."""

    def record(self, tool: str, operation: str, error_detail: str) -> None:
        """Record one integration error entry."""
        ...


# A forwarded-query handler receives the reply address and validated text.
QueryHandler = Callable[[dict, str], Awaitable[None]]

# A feedback recorder receives (query_log_id, verdict) where verdict is
# "up"/"down"; returns True when the verdict was recorded.
FeedbackRecorder = Callable[[int, str], bool]

# callback_data prefix for the 👍/👎 inline keyboard buttons.
FEEDBACK_CALLBACK_PREFIX = "fb"

# Streaming delivery pacing: minimum seconds between message edits (respects
# Telegram's edit rate limits) and minimum characters before the first send.
STREAM_EDIT_INTERVAL_S = 1.5
STREAM_MIN_FIRST_CHARS = 20


def build_feedback_keyboard(query_log_id: int) -> dict:
    """Build the 👍/👎 inline keyboard markup for an answer message.

    Args:
        query_log_id: The Query_Log row the verdict will be recorded against.

    Returns:
        A Telegram ``reply_markup`` dict with one row of two buttons whose
        ``callback_data`` encodes ``fb:<id>:<verdict>``.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "👍",
                    "callback_data": f"{FEEDBACK_CALLBACK_PREFIX}:{query_log_id}:up",
                },
                {
                    "text": "👎",
                    "callback_data": f"{FEEDBACK_CALLBACK_PREFIX}:{query_log_id}:down",
                },
            ]
        ]
    }


def parse_feedback_callback(data: str | None) -> tuple[int, str] | None:
    """Parse a feedback button's ``callback_data`` into ``(id, verdict)``.

    Args:
        data: The raw ``callback_data`` string from a ``callback_query``.

    Returns:
        ``(query_log_id, verdict)`` for a well-formed ``fb:<id>:<up|down>``
        payload, otherwise ``None``.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != FEEDBACK_CALLBACK_PREFIX:
        return None
    if parts[2] not in ("up", "down"):
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def build_sources_footer(citations: list[str]) -> str:
    """Build the ``Sources:`` footer for a list of citation document names.

    The footer is prefixed with two newlines to separate it from the body and
    lists each citation on its own bullet line. An empty citation list yields
    an empty footer (no header).

    Args:
        citations: Unique source document names to cite. May be empty.

    Returns:
        The footer string (possibly empty). The exact format is
        ``"\\n\\nSources:\\n- <c1>\\n- <c2>..."``.
    """
    if not citations:
        return ""
    lines = "\n".join(f"- {c}" for c in citations)
    return f"\n\n{SOURCES_HEADER}\n{lines}"


# Markdown markers the LLM commonly emits that Telegram/WhatsApp plain-text
# messages render literally (e.g. a visible "**年假**"). Each pattern maps to
# its unstyled replacement; single-asterisk emphasis is deliberately NOT
# stripped because a lone "*" is too likely to be literal content.
_MARKDOWN_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),  # **bold**
    (re.compile(r"__(.+?)__", re.DOTALL), r"\1"),  # __bold__
    (re.compile(r"`([^`\n]+)`"), r"\1"),  # `inline code`
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),  # # headings
)


def strip_markdown(text: str) -> str:
    """Remove Markdown emphasis markers for plain-text frontends (Req 8.5).

    Telegram (without a ``parse_mode``) and WhatsApp render message bodies as
    plain text, so Markdown the model emits — ``**bold**``, ``__bold__``,
    backtick code spans, and ``#`` heading prefixes — shows up as literal
    punctuation noise. This strips the markers while keeping their inner text;
    list dashes and other plain characters are left untouched.

    Args:
        text: The generated answer body, possibly containing Markdown.

    Returns:
        The text with emphasis markers removed.
    """
    for pattern, replacement in _MARKDOWN_MARKERS:
        text = pattern.sub(replacement, text)
    return text


def format_telegram_message(response: GeneratedResponse) -> str:
    """Format a generated response for Telegram within the length limit.

    Produces ``body`` + a ``Sources:`` footer listing the response's citations.
    When the combined message would exceed :data:`TELEGRAM_MAX_MESSAGE_CHARS`,
    truncation is applied to the **body only** so the citation footer survives
    intact, and a :data:`TRUNCATION_INDICATOR` is appended to signal truncation
    (Req 8.5/8.7).

    Last-resort behavior: if the citation footer alone (plus the truncation
    indicator) does not fit within the limit - an extreme case only reachable
    with an unusually large citation list - the footer itself is truncated as a
    last resort so the hard length cap is never violated. In that degenerate
    case not all citations can be preserved; the body-first rule guarantees
    citations survive whenever the footer fits.

    Args:
        response: The generated response to format.

    Returns:
        The formatted message, guaranteed to be at most
        :data:`TELEGRAM_MAX_MESSAGE_CHARS` characters long.
    """
    body = strip_markdown(response.text or "")
    footer = build_sources_footer(response.citations)
    full = body + footer

    if len(full) <= TELEGRAM_MAX_MESSAGE_CHARS:
        return full

    indicator = TRUNCATION_INDICATOR

    # Preferred path: keep the whole footer (all citations) + indicator, and
    # fill the remaining budget with as much body as fits (Req 8.7).
    if len(footer) + len(indicator) <= TELEGRAM_MAX_MESSAGE_CHARS:
        body_budget = TELEGRAM_MAX_MESSAGE_CHARS - len(footer) - len(indicator)
        return body[:body_budget] + indicator + footer

    # Last resort: the footer alone does not fit. Preserve the indicator at the
    # end and keep as much of the footer as possible, never exceeding the cap.
    if len(indicator) <= TELEGRAM_MAX_MESSAGE_CHARS:
        keep = TELEGRAM_MAX_MESSAGE_CHARS - len(indicator)
        return footer[:keep] + indicator
    return footer[:TELEGRAM_MAX_MESSAGE_CHARS]


class TelegramAdapter:
    """Telegram implementation of :class:`app.bots.base.FrontendAdapter`.

    All Bot API traffic flows through an injected httpx client configured with
    the Outbound_Proxy. The bot token is resolved lazily (default: from the
    Credential_Store) so the adapter can be constructed before credentials are
    loaded.

    Attributes:
        tool_name: Always ``"telegram"``.
    """

    tool_name: str = "telegram"

    def __init__(
        self,
        *,
        credential_store: CredentialStore | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        token_resolver: Callable[[], str | None] | None = None,
        error_log: ErrorLog | None = None,
        feedback_recorder: FeedbackRecorder | None = None,
        api_base: str = TELEGRAM_API_BASE,
        proxy_max_retries: int = PROXY_MAX_RETRIES,
        long_poll_timeout: int = LONG_POLL_TIMEOUT_SECONDS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        """Create a Telegram adapter.

        Args:
            credential_store: Source of the ``telegram`` / ``bot_token`` value
                when no explicit ``token_resolver`` is supplied. Defaults to a
                new :class:`~app.security.credentials.CredentialStore`.
            settings: Settings snapshot providing ``telegram_proxy_url`` for the
                default client. Defaults to :func:`app.config.get_settings`.
            client: A pre-built httpx client to use. Primarily a test seam
                (e.g. combined with ``respx``). Takes precedence over
                ``client_factory``.
            http_client: Alias for ``client`` accepted for caller convenience;
                ``client`` wins if both are given.
            client_factory: Factory building the proxied httpx client on first
                use. Defaults to a client configured with the Telegram proxy.
            token_resolver: Callable returning the current bot token (or
                ``None``). Defaults to reading it from the Credential_Store.
            error_log: Optional sink recording each proxy failure with tool,
                operation, and reason (Req 12.6).
            feedback_recorder: Optional callback recording an End_User 👍/👎
                verdict as ``(query_log_id, verdict)``. When present, answer
                messages carry a feedback inline keyboard and
                ``callback_query`` updates are handled.
            api_base: Base URL of the Telegram Bot API (override in tests).
            proxy_max_retries: Retries after a proxy connectivity failure
                (Req 12.6).
            long_poll_timeout: Seconds the ``getUpdates`` long poll stays open.
            backoff_base_s: Base delay between proxy retries (linear backoff).
        """
        self._settings = settings or get_settings()
        self._credential_store = credential_store
        self._client = client or http_client
        self._client_factory = client_factory
        self._token_resolver = token_resolver
        self._error_log = error_log
        self._feedback_recorder = feedback_recorder
        self._api_base = api_base.rstrip("/")
        self._proxy_max_retries = proxy_max_retries
        self._long_poll_timeout = long_poll_timeout
        self._backoff_base_s = backoff_base_s

    # --- seams ----------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the httpx client, building the proxied default on first use."""
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                proxy = self._settings.telegram_proxy_url or None
                # Read timeout must outlast the long poll so getUpdates can
                # block server-side for the full interval.
                timeout = httpx.Timeout(self._long_poll_timeout + 10)
                self._client = httpx.AsyncClient(proxy=proxy, timeout=timeout)
        return self._client

    def _resolve_token(self) -> str | None:
        """Resolve the bot token from the injected resolver or Credential_Store."""
        if self._token_resolver is not None:
            return self._token_resolver()
        if self._credential_store is None:
            self._credential_store = CredentialStore(self._settings)
        creds = self._credential_store.load("telegram")
        if not creds:
            return None
        return creds.get("bot_token")

    # --- Bot API plumbing ----------------------------------------------

    async def _call(
        self,
        method: str,
        *,
        http_verb: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Issue a single Bot API call through the proxy and return its JSON.

        Args:
            method: Bot API method name (e.g. ``"sendMessage"``).
            http_verb: ``"GET"`` or ``"POST"``.
            json: JSON request body for POST calls.
            params: Query parameters for GET calls.

        Returns:
            The decoded JSON response body.

        Raises:
            TelegramConfigError: If no bot token is configured.
            httpx.HTTPError: On transport or HTTP-status failures.
        """
        token = self._resolve_token()
        if not token:
            raise TelegramConfigError(
                "telegram bot_token is not configured in the Credential_Store"
            )
        url = f"{self._api_base}/bot{token}/{method}"
        client = self._get_client()
        if http_verb == "GET":
            resp = await client.get(url, params=params)
        else:
            # Serialize explicitly (standard separators) rather than relying on
            # the client's compact encoding, so the wire body is stable.
            resp = await client.post(
                url,
                content=_json.dumps(json if json is not None else {}),
                headers={"Content-Type": "application/json"},
            )
        resp.raise_for_status()
        return resp.json()

    async def _call_with_proxy_retries(
        self,
        method: str,
        *,
        http_verb: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Call the Bot API, retrying proxy connectivity failures (Req 12.6).

        Retries up to :attr:`_proxy_max_retries` additional times when the
        Outbound_Proxy is unreachable, logging every proxy failure (both to the
        module logger and, when configured, the integration error log). Non
        connectivity errors (e.g. HTTP status errors) propagate immediately
        without retry.

        Args:
            method: Bot API method name.
            http_verb: ``"GET"`` or ``"POST"``.
            json: JSON request body for POST calls.
            params: Query parameters for GET calls.

        Returns:
            The decoded JSON response body from the first successful attempt.

        Raises:
            TelegramProxyError: If every attempt failed with a proxy
                connectivity error.
        """
        last_exc: Exception | None = None
        max_attempts = self._proxy_max_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._call(
                    method, http_verb=http_verb, json=json, params=params
                )
            except _PROXY_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    "Telegram proxy connectivity failure on %s "
                    "(attempt %d/%d): %s",
                    method,
                    attempt,
                    max_attempts,
                    exc,
                )
                if self._error_log is not None:
                    # Logging must never break query processing (Req 2.8).
                    try:
                        self._error_log.record(self.tool_name, method, str(exc))
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to record Telegram proxy error for %s", method
                        )
                if attempt < max_attempts and self._backoff_base_s > 0:
                    await asyncio.sleep(self._backoff_base_s * attempt)
        logger.error(
            "Telegram request %s failed after %d attempts: "
            "proxy connectivity failure",
            method,
            max_attempts,
        )
        raise TelegramProxyError(
            f"Telegram {method} failed after {max_attempts} attempts: "
            "Outbound_Proxy unreachable"
        ) from last_exc

    # --- FrontendAdapter interface -------------------------------------

    async def send(
        self,
        conversation_ref: dict,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> int | None:
        """Deliver a text message to a Telegram chat through the proxy.

        Args:
            conversation_ref: Reply address containing a ``"chat_id"`` key.
            text: The message body (already formatted by :meth:`format`).
            reply_markup: Optional inline keyboard markup (e.g. the feedback
                buttons from :func:`build_feedback_keyboard`).

        Returns:
            The sent message's ``message_id`` when Telegram reports one,
            otherwise ``None``.

        Raises:
            TelegramProxyError: If delivery fails after exhausting proxy
                retries (Req 12.6).
        """
        payload: dict = {"chat_id": conversation_ref["chat_id"], "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        data = await self._call_with_proxy_retries(
            "sendMessage", http_verb="POST", json=payload
        )
        result = data.get("result") if isinstance(data, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None

    # --- streaming delivery (edit-message loop) --------------------------

    # Marks this adapter as able to consume the orchestrator's streaming
    # events; the dispatcher checks this attribute (duck-typed capability).
    supports_streaming: bool = True

    async def send_stream(self, conversation_ref: dict, events) -> GeneratedResponse:
        """Deliver an answer incrementally via a send-then-edit loop.

        Consumes the orchestrator's ``("delta", str)`` / ``("done",
        GeneratedResponse)`` events: the first ~:data:`STREAM_MIN_FIRST_CHARS`
        characters trigger an initial ``sendMessage`` so text appears within
        about a second, subsequent growth is applied with ``editMessageText``
        at most every :data:`STREAM_EDIT_INTERVAL_S` seconds (respecting
        Telegram's edit rate limits), and the terminal event triggers a final
        edit carrying the fully formatted message — ``Sources:`` footer,
        length cap — plus the 👍/👎 feedback keyboard on a successful answer.
        Mid-stream edit failures are swallowed (the final edit still runs);
        a failed final delivery raises so the caller can handle it.

        Args:
            conversation_ref: Reply address containing a ``"chat_id"`` key.
            events: Async iterator of orchestrator streaming events.

        Returns:
            The final :class:`GeneratedResponse` from the ``"done"`` event.

        Raises:
            TelegramProxyError: If the final delivery fails after retries.
        """
        import time as _time

        chat_id = conversation_ref["chat_id"]
        buffer = ""
        message_id: int | None = None
        last_edit = 0.0
        response: GeneratedResponse | None = None

        async for kind, payload in events:
            if kind == "done":
                response = payload
                break
            buffer += payload
            preview = strip_markdown(buffer)[: TELEGRAM_MAX_MESSAGE_CHARS - 2] + " …"
            now = _time.monotonic()
            if message_id is None:
                if len(buffer) >= STREAM_MIN_FIRST_CHARS:
                    # The first visible text; a failure here aborts streaming
                    # so the caller can fall back to an error message.
                    message_id = await self.send(conversation_ref, preview)
                    last_edit = now
            elif now - last_edit >= STREAM_EDIT_INTERVAL_S:
                await self._edit_message_safe(chat_id, message_id, preview)
                last_edit = now

        if response is None:  # defensive: the orchestrator always emits "done"
            response = GeneratedResponse(text=buffer, citations=[], status="failed")

        final_text = self.format(response)
        reply_markup = None
        if (
            self._feedback_recorder is not None
            and response.status == "success"
            and response.query_log_id is not None
        ):
            reply_markup = build_feedback_keyboard(response.query_log_id)

        if message_id is None:
            await self.send(conversation_ref, final_text, reply_markup=reply_markup)
        else:
            await self._edit_message_final(
                conversation_ref, message_id, final_text, reply_markup
            )
        return response

    async def _edit_message_safe(
        self, chat_id: object, message_id: int, text: str
    ) -> None:
        """Apply a mid-stream ``editMessageText``, swallowing any failure.

        A transient edit failure (rate limit, proxy hiccup) must not abort the
        stream — the next edit or the final render will catch the text up.

        Args:
            chat_id: The destination chat id.
            message_id: The message being progressively edited.
            text: The new (preview) message text.
        """
        try:
            await self._call_with_proxy_retries(
                "editMessageText",
                http_verb="POST",
                json={"chat_id": chat_id, "message_id": message_id, "text": text},
            )
        except Exception as exc:  # noqa: BLE001 - never abort the stream
            logger.debug("mid-stream edit failed (will catch up later): %s", exc)

    async def _edit_message_final(
        self,
        conversation_ref: dict,
        message_id: int,
        text: str,
        reply_markup: dict | None,
    ) -> None:
        """Apply the final ``editMessageText``; fall back to a fresh send.

        Args:
            conversation_ref: Reply address containing a ``"chat_id"`` key.
            message_id: The streamed message to finalize.
            text: The fully formatted final message text.
            reply_markup: Optional feedback keyboard to attach.

        Raises:
            TelegramProxyError: If both the edit and the fallback send fail.
        """
        payload: dict = {
            "chat_id": conversation_ref["chat_id"],
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._call_with_proxy_retries(
                "editMessageText", http_verb="POST", json=payload
            )
        except TelegramProxyError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. 400 message-not-modified
            logger.warning("final edit failed; sending a fresh message: %s", exc)
            await self.send(conversation_ref, text, reply_markup=reply_markup)

    def format(self, response: GeneratedResponse) -> str:
        """Format a response as plain text + ``Sources:`` footer for Telegram.

        Delegates to :func:`format_telegram_message`; see that function for the
        length-cap and citation-preservation guarantees (Req 8.5/8.7).

        Args:
            response: The generated response to format.

        Returns:
            The formatted message, at most
            :data:`TELEGRAM_MAX_MESSAGE_CHARS` characters.
        """
        return format_telegram_message(response)

    async def check_connectivity(self) -> ConnectivityResult:
        """Run a ``getMe`` end-to-end connectivity check through the proxy.

        Never raises: any failure (missing token, proxy unreachable, HTTP
        error, malformed response) is captured as an unsuccessful
        :class:`~app.core.models.ConnectivityResult` (Req 3.1/3.2).

        Returns:
            A :class:`~app.core.models.ConnectivityResult` for ``"telegram"``.
        """
        now = datetime.now(timezone.utc)
        try:
            data = await self._call_with_proxy_retries("getMe", http_verb="GET")
        except Exception as exc:  # noqa: BLE001 - surfaced via the result object
            return ConnectivityResult(
                tool=self.tool_name,
                ok=False,
                detail=str(exc),
                checked_at=now,
            )
        ok = bool(data.get("ok"))
        if ok:
            username = (data.get("result") or {}).get("username", "")
            detail = f"getMe ok (@{username})" if username else "getMe ok"
        else:
            detail = f"getMe returned ok=false: {data.get('description', '')}".strip()
        return ConnectivityResult(
            tool=self.tool_name, ok=ok, detail=detail, checked_at=now
        )

    # --- long-polling loop ---------------------------------------------

    async def _get_updates(self, offset: int | None, poll_timeout: int) -> list[dict]:
        """Fetch a batch of updates via long-poll ``getUpdates``.

        Args:
            offset: The ``update_id`` to acknowledge from; ``None`` on the first
                call.
            poll_timeout: Long-poll ``timeout`` value in seconds.

        Returns:
            The list of raw update objects (possibly empty).
        """
        params: dict = {"timeout": poll_timeout}
        if offset is not None:
            params["offset"] = offset
        data = await self._call_with_proxy_retries(
            "getUpdates", http_verb="GET", params=params
        )
        result = data.get("result")
        return result if isinstance(result, list) else []

    async def _dispatch_update(self, update: dict, handler: QueryHandler) -> None:
        """Run one update through the inbound gate and route it.

        Valid text messages are handed to ``handler`` as
        ``(conversation_ref, text)``; everything else receives the shared
        rejection reply and is not forwarded (Req 2.1/2.2).

        Args:
            update: A single raw Telegram update object.
            handler: Async callback invoked with the reply address and text.
        """
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_feedback_callback(callback)
            return

        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        conversation_ref = {"chat_id": chat_id}
        text = message.get("text")

        decision = evaluate_inbound(text if isinstance(text, str) else None)
        if decision.forward and decision.query_text is not None:
            await handler(conversation_ref, decision.query_text)
        else:
            # Rejection reply is best-effort; a delivery failure must not stop
            # the polling loop.
            try:
                await self.send(conversation_ref, decision.rejection_message or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to deliver rejection reply to chat %s: %s",
                    chat_id,
                    exc,
                )

    async def _handle_feedback_callback(self, callback: dict) -> None:
        """Record a 👍/👎 button press and acknowledge it (best-effort).

        Parses the ``callback_data`` (``fb:<query_log_id>:<verdict>``),
        records the verdict via the injected recorder, answers the callback so
        the End_User sees a confirmation toast, and removes the buttons so a
        query is voted on at most once. Every step is guarded — feedback must
        never break the polling loop.

        Args:
            callback: The raw ``callback_query`` object from an update.
        """
        parsed = parse_feedback_callback(callback.get("data"))
        recorded = False
        if parsed is not None and self._feedback_recorder is not None:
            query_log_id, verdict = parsed
            try:
                recorded = bool(self._feedback_recorder(query_log_id, verdict))
            except Exception:  # noqa: BLE001 - feedback is strictly best-effort
                logger.exception("feedback recorder failed; continuing")

        # Acknowledge the tap (dismisses Telegram's loading spinner).
        callback_id = callback.get("id")
        if callback_id is not None:
            toast = (
                "Thanks for your feedback! 🙏"
                if recorded
                else "Feedback could not be recorded."
            )
            try:
                await self._call_with_proxy_retries(
                    "answerCallbackQuery",
                    http_verb="POST",
                    json={"callback_query_id": callback_id, "text": toast},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("answerCallbackQuery failed: %s", exc)

        # Remove the buttons after a successful vote (one vote per answer).
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if recorded and chat_id is not None and message_id is not None:
            try:
                await self._call_with_proxy_retries(
                    "editMessageReplyMarkup",
                    http_verb="POST",
                    json={"chat_id": chat_id, "message_id": message_id},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("removing feedback keyboard failed: %s", exc)

    async def run_polling(
        self,
        handler: QueryHandler,
        *,
        stop_event: asyncio.Event | None = None,
        poll_timeout: int | None = None,
    ) -> None:
        """Run the ``getUpdates`` long-polling loop until stopped.

        Each received update is passed through the shared inbound gate; valid
        text queries are handed to ``handler`` as ``(conversation_ref, text)``,
        and invalid messages get the shared rejection reply (Req 2.1/2.2). The
        loop continues across transient proxy failures (surfaced as
        :class:`TelegramProxyError`), which are logged and retried on the next
        iteration. Already-processed updates are skipped via offset tracking so
        no update is handled twice.

        Args:
            handler: Async callback invoked once per forwarded query.
            stop_event: Optional event; when set, the loop exits before the next
                iteration. When ``None`` the loop runs until cancelled.
            poll_timeout: Long-poll timeout in seconds; defaults to the
                adapter's ``long_poll_timeout``.
        """
        timeout = self._long_poll_timeout if poll_timeout is None else poll_timeout
        offset: int | None = None
        while stop_event is None or not stop_event.is_set():
            try:
                updates = await self._get_updates(offset, timeout)
            except asyncio.CancelledError:
                raise
            except TelegramProxyError as exc:
                logger.warning("getUpdates poll failed, will retry: %s", exc)
                await asyncio.sleep(1)
                continue
            except httpx.TransportError as exc:
                # A long-poll read timeout (or other transient transport error)
                # is expected when no updates arrive within the poll window; it
                # must not kill the loop. Log and retry on the next iteration.
                logger.warning(
                    "getUpdates transport error, will retry: %s", exc
                )
                await asyncio.sleep(1)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    if offset is not None and update_id < offset:
                        # Already processed in a previous iteration - skip.
                        continue
                    offset = update_id + 1
                await self._dispatch_update(update, handler)
            # Yield so a concurrent stop signal can be observed promptly.
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        """Close the underlying httpx client if this adapter created/owns one."""
        if self._client is not None:
            await self._client.aclose()


# Static protocol conformance check.
_: type[FrontendAdapter] = TelegramAdapter
