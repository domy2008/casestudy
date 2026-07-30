"""WhatsApp Frontend_Tool adapter (Meta WhatsApp Cloud API via Outbound_Proxy).

Like Telegram, the WhatsApp Cloud API (``graph.facebook.com``) is not reachable
directly from AWS China, so every Graph API call in this module is routed
through the Outbound_Proxy configured on the httpx client
(``proxy=settings.whatsapp_proxy_url``). Unlike Telegram, WhatsApp offers no
long-polling mode — inbound messages arrive as Meta webhook notifications — so
this adapter combines a Telegram-style proxied sender/formatter/connectivity
check with a Teams-style inbound webhook handler (see the design's "Key Design
Decisions" and "Frontend Integration Module").

The adapter implements the :class:`app.bots.base.FrontendAdapter` protocol:

* :meth:`WhatsAppAdapter.send` posts a text message to
  ``/{phone_number_id}/messages`` through the proxy, retrying up to
  :data:`PROXY_MAX_RETRIES` times on proxy connectivity errors and logging each
  proxy failure to the optional integration error log (Req 12.2, 12.6).
* :meth:`WhatsAppAdapter.format` renders plain text plus a ``Sources:`` footer,
  hard-capped at :data:`WHATSAPP_MAX_MESSAGE_CHARS` with body-only truncation so
  citations always survive (Req 8.5/8.7). The Telegram formatter is reused since
  both platforms take plain-text bodies with the same footer style.
* :meth:`WhatsAppAdapter.check_connectivity` performs a ``GET
  /{phone_number_id}`` end-to-end check through the proxy (Req 3.1/3.2).
* :meth:`WhatsAppAdapter.verify_webhook` answers Meta's ``GET`` subscription
  challenge, and :meth:`WhatsAppAdapter.handle_notification` parses a ``POST``
  notification, applies the shared inbound gate
  (:func:`app.bots.base.evaluate_inbound`), forwards valid text to a dispatcher
  callback, and replies with the shared rejection message otherwise
  (Req 2.1/2.2).

Testability seam: the httpx client and the credential resolver are injected
through the constructor, so tests can drive the adapter with ``respx`` and fake
credentials and never make a real network call. By default credentials are read
from the :class:`~app.security.credentials.CredentialStore` (``whatsapp``) and
the client is created with the proxy from :class:`~app.config.Settings`. No
secrets are hardcoded and none are ever placed in a log line (Req 11.1).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx

from app.bots.base import (
    REJECTION_MESSAGE,
    FrontendAdapter,
    evaluate_inbound,
)
from app.bots.telegram import format_telegram_message
from app.config import Settings, get_settings
from app.core.models import ConnectivityResult, GeneratedResponse
from app.security.credentials import CredentialStore

__all__ = [
    "WHATSAPP_API_BASE",
    "WHATSAPP_API_VERSION",
    "WHATSAPP_MAX_MESSAGE_CHARS",
    "PROXY_MAX_RETRIES",
    "WhatsAppConfigError",
    "WhatsAppProxyError",
    "ErrorLog",
    "WhatsAppDispatcher",
    "extract_inbound_message",
    "format_whatsapp_message",
    "WhatsAppAdapter",
]

logger = logging.getLogger(__name__)

# Base URL for the Meta Graph API. Per-call URLs are
# ``{WHATSAPP_API_BASE}/{WHATSAPP_API_VERSION}/{phone_number_id}/{path}``.
WHATSAPP_API_BASE = "https://graph.facebook.com"

# Graph API version pinned for the Cloud API calls used here.
WHATSAPP_API_VERSION = "v20.0"

# WhatsApp's hard maximum text-message body length in characters (Req 8.5).
WHATSAPP_MAX_MESSAGE_CHARS = 4096

# Number of times a Graph API request is retried after a proxy connectivity
# failure, in addition to the initial attempt (Req 12.6; "up to 3 retries").
PROXY_MAX_RETRIES = 3

# Default seconds between proxy retries (multiplied by the retry index for a
# simple linear backoff). Tests inject ``0.0`` to run without delay.
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

# Timeout budget (seconds) for a single Graph API call.
REQUEST_TIMEOUT_S = 15.0

# httpx exceptions that indicate the Outbound_Proxy (or the connection through
# it) is unreachable, and therefore warrant a retry + proxy-failure log entry
# (Req 12.6). HTTP status errors are NOT in this set — they are not connectivity
# failures and must not be retried here.
_PROXY_ERRORS: tuple[type[Exception], ...] = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)


class WhatsAppConfigError(RuntimeError):
    """Raised when WhatsApp credentials are missing from the Credential_Store."""


class WhatsAppProxyError(RuntimeError):
    """Raised when a WhatsApp request fails after exhausting proxy retries.

    Indicates the Outbound_Proxy was unreachable for every attempt (Req 12.6).
    The originating httpx exception is attached via ``__cause__``.
    """


@runtime_checkable
class ErrorLog(Protocol):
    """Minimal sink for integration error-log entries (Req 2.7, 3.3, 12.6)."""

    def record(self, tool: str, operation: str, error_detail: str) -> None:
        """Record one integration error entry."""
        ...


#: An async callback that handles a validated inbound query. It receives the
#: originating conversation reference and the validated text and returns the
#: :class:`GeneratedResponse` to reply with.
WhatsAppDispatcher = Callable[[dict, str], Awaitable[GeneratedResponse]]


def format_whatsapp_message(response: GeneratedResponse) -> str:
    """Format a generated response for WhatsApp within the length limit.

    WhatsApp text bodies are plain text with the same 4,096-character cap as
    Telegram and the same ``Sources:`` footer style, so this delegates to
    :func:`app.bots.telegram.format_telegram_message`, which truncates the body
    first so citations always survive (Req 8.5/8.7).

    Args:
        response: The generated response to format.

    Returns:
        The formatted message, at most :data:`WHATSAPP_MAX_MESSAGE_CHARS`
        characters long.
    """
    return format_telegram_message(response)


def extract_inbound_message(notification: dict) -> tuple[str | None, dict | None]:
    """Extract the first inbound text message from a Cloud API notification.

    Parses the nested ``entry[].changes[].value.messages[]`` structure of a
    WhatsApp Cloud API webhook notification and returns the text of the first
    message together with a reply address. Non-text messages (image, audio,
    location, etc.), status-only callbacks, and malformed payloads yield a
    ``None`` text so the shared inbound gate rejects them without forwarding
    (Req 2.2).

    Args:
        notification: The parsed webhook notification body.

    Returns:
        A ``(text, conversation_ref)`` pair. ``text`` is the message body, or
        ``None`` when the notification carries no usable inbound text.
        ``conversation_ref`` carries the ``to`` (sender wa_id) and originating
        ``phone_number_id`` needed to reply, or ``None`` when absent.
    """
    entries = notification.get("entry")
    if not isinstance(entries, list):
        return None, None
    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list) or not messages:
                continue
            message = messages[0]
            if not isinstance(message, dict):
                continue
            metadata = value.get("metadata")
            phone_number_id = (
                metadata.get("phone_number_id")
                if isinstance(metadata, dict)
                else None
            )
            conversation_ref = {
                "to": message.get("from", ""),
                "phone_number_id": phone_number_id or "",
            }
            if message.get("type") != "text":
                return None, conversation_ref
            text_obj = message.get("text")
            body = text_obj.get("body") if isinstance(text_obj, dict) else None
            return (body if isinstance(body, str) else None), conversation_ref
    return None, None


class WhatsAppAdapter:
    """WhatsApp implementation of :class:`app.bots.base.FrontendAdapter`.

    All Graph API traffic flows through an injected httpx client configured with
    the Outbound_Proxy. Credentials are resolved lazily per call (default: from
    the Credential_Store) so the adapter can be constructed before credentials
    are loaded and so Admin credential updates take effect immediately.

    Attributes:
        tool_name: Always ``"whatsapp"``.
    """

    tool_name: str = "whatsapp"

    def __init__(
        self,
        *,
        credential_store: CredentialStore | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        credentials_resolver: Callable[[], dict[str, str] | None] | None = None,
        error_log: ErrorLog | None = None,
        api_base: str = WHATSAPP_API_BASE,
        api_version: str = WHATSAPP_API_VERSION,
        proxy_max_retries: int = PROXY_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        """Create a WhatsApp adapter.

        Args:
            credential_store: Source of the ``whatsapp`` credentials when no
                explicit ``credentials_resolver`` is supplied. Defaults to a new
                :class:`~app.security.credentials.CredentialStore`.
            settings: Settings snapshot providing ``whatsapp_proxy_url`` for the
                default client. Defaults to :func:`app.config.get_settings`.
            client: A pre-built httpx client to use. Primarily a test seam (e.g.
                combined with ``respx``). Takes precedence over
                ``client_factory``.
            http_client: Alias for ``client`` accepted for caller convenience;
                ``client`` wins if both are given.
            client_factory: Factory building the proxied httpx client on first
                use. Defaults to a client configured with the WhatsApp proxy.
            credentials_resolver: Callable returning the current credential
                mapping (or ``None``). Defaults to reading from the
                Credential_Store.
            error_log: Optional sink recording each proxy failure with tool,
                operation, and reason (Req 12.6).
            api_base: Base URL of the Graph API (override in tests).
            api_version: Graph API version segment.
            proxy_max_retries: Retries after a proxy connectivity failure
                (Req 12.6).
            backoff_base_s: Base delay between proxy retries (linear backoff).
        """
        self._settings = settings or get_settings()
        self._credential_store = credential_store
        self._client = client or http_client
        self._client_factory = client_factory
        self._credentials_resolver = credentials_resolver
        self._error_log = error_log
        self._api_base = api_base.rstrip("/")
        self._api_version = api_version
        self._proxy_max_retries = proxy_max_retries
        self._backoff_base_s = backoff_base_s

    # --- seams ----------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the httpx client, building the proxied default on first use."""
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                proxy = self._settings.whatsapp_proxy_url or None
                timeout = httpx.Timeout(REQUEST_TIMEOUT_S)
                self._client = httpx.AsyncClient(proxy=proxy, timeout=timeout)
        return self._client

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve WhatsApp credentials from the resolver or Credential_Store.

        Returns:
            The credential mapping (``access_token``, ``phone_number_id``,
            ``verify_token``).

        Raises:
            WhatsAppConfigError: If credentials are missing or incomplete. The
                message never includes any secret.
        """
        if self._credentials_resolver is not None:
            creds = self._credentials_resolver()
        else:
            creds = self._load_from_store()
        if not creds or not creds.get("access_token") or not creds.get(
            "phone_number_id"
        ):
            raise WhatsAppConfigError(
                "whatsapp credentials are not configured in the Credential_Store"
            )
        return creds

    # --- Graph API plumbing --------------------------------------------

    async def _call(
        self,
        *,
        http_verb: str,
        path: str,
        token: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Issue a single Graph API call through the proxy and return its JSON.

        Args:
            http_verb: ``"GET"`` or ``"POST"``.
            path: The path after the API version (e.g. ``"123/messages"``).
            token: The bearer access token (never logged).
            json: JSON request body for POST calls.
            params: Query parameters for GET calls.

        Returns:
            The decoded JSON response body.

        Raises:
            httpx.HTTPError: On transport or HTTP-status failures.
        """
        url = f"{self._api_base}/{self._api_version}/{path}"
        headers = {"Authorization": f"Bearer {token}"}
        client = self._get_client()
        if http_verb == "GET":
            resp = await client.get(url, params=params, headers=headers)
        else:
            resp = await client.post(url, json=json or {}, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _call_with_proxy_retries(
        self,
        *,
        http_verb: str,
        path: str,
        token: str,
        json: dict | None = None,
        params: dict | None = None,
        operation: str,
    ) -> dict:
        """Call the Graph API, retrying proxy connectivity failures (Req 12.6).

        Retries up to :attr:`_proxy_max_retries` additional times when the
        Outbound_Proxy is unreachable, logging every proxy failure (both to the
        module logger and, when configured, the integration error log). Non
        connectivity errors (e.g. HTTP status errors) propagate immediately
        without retry.

        Args:
            http_verb: ``"GET"`` or ``"POST"``.
            path: The path after the API version.
            token: The bearer access token (never logged).
            json: JSON request body for POST calls.
            params: Query parameters for GET calls.
            operation: Label recorded in the error log (e.g. ``"sendMessage"``).

        Returns:
            The decoded JSON response body from the first successful attempt.

        Raises:
            WhatsAppProxyError: If every attempt failed with a proxy
                connectivity error.
        """
        last_exc: Exception | None = None
        max_attempts = self._proxy_max_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._call(
                    http_verb=http_verb,
                    path=path,
                    token=token,
                    json=json,
                    params=params,
                )
            except _PROXY_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    "WhatsApp proxy connectivity failure on %s "
                    "(attempt %d/%d): %s",
                    operation,
                    attempt,
                    max_attempts,
                    exc,
                )
                if self._error_log is not None:
                    # Logging must never break query processing (Req 2.8).
                    try:
                        self._error_log.record(self.tool_name, operation, str(exc))
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to record WhatsApp proxy error for %s",
                            operation,
                        )
                if attempt < max_attempts and self._backoff_base_s > 0:
                    await asyncio.sleep(self._backoff_base_s * attempt)
        logger.error(
            "WhatsApp request %s failed after %d attempts: "
            "proxy connectivity failure",
            operation,
            max_attempts,
        )
        raise WhatsAppProxyError(
            f"WhatsApp {operation} failed after {max_attempts} attempts: "
            "Outbound_Proxy unreachable"
        ) from last_exc

    # --- FrontendAdapter interface -------------------------------------

    async def send(self, conversation_ref: dict, text: str) -> None:
        """Deliver a text message to a WhatsApp user through the proxy.

        Args:
            conversation_ref: Reply address containing a ``"to"`` key (the
                recipient wa_id). An optional ``"phone_number_id"`` overrides the
                configured sender number.
            text: The message body (already formatted by :meth:`format`).

        Raises:
            WhatsAppConfigError: If credentials are not configured.
            WhatsAppProxyError: If delivery fails after exhausting proxy
                retries (Req 12.6).
        """
        creds = self._resolve_credentials()
        phone_number_id = (
            conversation_ref.get("phone_number_id")
            or creds["phone_number_id"]
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": conversation_ref["to"],
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        await self._call_with_proxy_retries(
            http_verb="POST",
            path=f"{phone_number_id}/messages",
            token=creds["access_token"],
            json=payload,
            operation="sendMessage",
        )

    def format(self, response: GeneratedResponse) -> str:
        """Format a response as plain text + ``Sources:`` footer for WhatsApp.

        Delegates to :func:`format_whatsapp_message`; see that function for the
        length-cap and citation-preservation guarantees (Req 8.5/8.7).

        Args:
            response: The generated response to format.

        Returns:
            The formatted message, at most
            :data:`WHATSAPP_MAX_MESSAGE_CHARS` characters.
        """
        return format_whatsapp_message(response)

    async def check_connectivity(self) -> ConnectivityResult:
        """Run a ``GET /{phone_number_id}`` connectivity check through the proxy.

        Never raises: any failure (missing credentials, proxy unreachable, HTTP
        error, malformed response) is captured as an unsuccessful
        :class:`~app.core.models.ConnectivityResult` (Req 3.1/3.2).

        Returns:
            A :class:`~app.core.models.ConnectivityResult` for ``"whatsapp"``.
        """
        now = datetime.now(timezone.utc)
        try:
            creds = self._resolve_credentials()
            data = await self._call_with_proxy_retries(
                http_verb="GET",
                path=str(creds["phone_number_id"]),
                token=creds["access_token"],
                params={"fields": "display_phone_number,verified_name"},
                operation="getPhoneNumber",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via the result object
            return ConnectivityResult(
                tool=self.tool_name,
                ok=False,
                detail=str(exc),
                checked_at=now,
            )
        number = data.get("display_phone_number", "")
        detail = f"phone number ok ({number})" if number else "phone number ok"
        return ConnectivityResult(
            tool=self.tool_name, ok=True, detail=detail, checked_at=now
        )

    # --- inbound webhook ------------------------------------------------

    def verify_webhook(
        self, mode: str | None, token: str | None, challenge: str | None
    ) -> str | None:
        """Answer Meta's ``GET`` webhook subscription challenge.

        Meta verifies a webhook subscription by calling the endpoint with
        ``hub.mode=subscribe``, ``hub.verify_token``, and ``hub.challenge``. The
        subscription is confirmed only when the mode is ``subscribe`` and the
        supplied token matches the configured ``verify_token``; in that case the
        challenge string must be echoed back verbatim.

        Args:
            mode: The ``hub.mode`` query value.
            token: The ``hub.verify_token`` query value.
            challenge: The ``hub.challenge`` query value to echo on success.

        Returns:
            The challenge string when verification succeeds, otherwise ``None``.
        """
        if mode != "subscribe" or not challenge:
            return None
        try:
            creds = self._resolve_credentials_full()
        except WhatsAppConfigError:
            return None
        expected = creds.get("verify_token")
        if expected and token == expected:
            return challenge
        return None

    def _resolve_credentials_full(self) -> dict[str, str]:
        """Resolve credentials including ``verify_token`` (webhook verification).

        Unlike :meth:`_resolve_credentials`, this does not require the send
        fields; it is used only for webhook verification where the
        ``verify_token`` is the relevant value.

        Returns:
            The stored credential mapping.

        Raises:
            WhatsAppConfigError: When no credentials are stored.
        """
        if self._credentials_resolver is not None:
            creds = self._credentials_resolver()
        else:
            creds = self._load_from_store()
        if not creds:
            raise WhatsAppConfigError("whatsapp credentials are not configured")
        return creds

    def _load_from_store(self) -> dict[str, str] | None:
        """Load the ``whatsapp`` credentials from the Credential_Store.

        Returns:
            The stored credential mapping, or ``None`` when nothing is stored.

        Raises:
            WhatsAppConfigError: When the Credential_Store itself cannot be
                opened (e.g. ``CREDENTIAL_MASTER_KEY`` is not set), so callers
                see the same config error as missing credentials instead of a
                raw ``ValueError`` crashing the webhook.
        """
        if self._credential_store is None:
            try:
                self._credential_store = CredentialStore(self._settings)
            except Exception as exc:  # noqa: BLE001 - store unavailable => config error
                raise WhatsAppConfigError(
                    "credential store is unavailable; whatsapp credentials "
                    "cannot be loaded"
                ) from exc
        return self._credential_store.load("whatsapp")

    async def handle_notification(
        self,
        notification: dict,
        dispatcher: WhatsAppDispatcher,
        *,
        send_reply: bool = True,
    ) -> GeneratedResponse | None:
        """Handle one inbound Cloud API webhook notification end to end.

        Extracts the first inbound message, applies the shared inbound gate
        (:func:`app.bots.base.evaluate_inbound`), forwards valid text (1-4,000
        chars) to ``dispatcher``, and otherwise replies with the shared
        rejection message. Status-only callbacks and non-text messages are never
        forwarded (Req 2.1/2.2).

        Args:
            notification: The parsed webhook notification body.
            dispatcher: Async callback invoked as ``dispatcher(conversation_ref,
                text)`` for valid inbound queries, returning the response to
                reply with.
            send_reply: When ``True`` (default), the formatted reply is
                delivered via :meth:`send`. Set ``False`` to let the caller
                handle delivery.

        Returns:
            The :class:`GeneratedResponse` used for the reply, or ``None`` when
            the notification carries no inbound message to answer (e.g. a
            delivery-status callback).
        """
        text, conversation_ref = extract_inbound_message(notification)
        if conversation_ref is None or not conversation_ref.get("to"):
            # No message to reply to (e.g. a status-only callback).
            return None

        decision = evaluate_inbound(text)
        if decision.forward and decision.query_text is not None:
            response = await dispatcher(conversation_ref, decision.query_text)
        else:
            response = GeneratedResponse(
                text=REJECTION_MESSAGE, status="rejected"
            )

        if send_reply:
            # Reply delivery is best-effort; a failure must not break the
            # webhook acknowledgement.
            try:
                await self.send(conversation_ref, self.format(response))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to deliver WhatsApp reply to %s: %s",
                    conversation_ref.get("to"),
                    exc,
                )
        return response

    async def aclose(self) -> None:
        """Close the underlying httpx client if this adapter created/owns one."""
        if self._client is not None:
            await self._client.aclose()


# Static protocol conformance check.
_: type[FrontendAdapter] = WhatsAppAdapter
