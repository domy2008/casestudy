"""Microsoft Teams Frontend_Tool adapter (Bot Framework inbound webhook).

Unlike Telegram, Microsoft's Bot Connector service is reachable directly from
AWS China, so Teams uses the standard Bot Framework inbound webhook: Teams POSTs
an *activity* to our endpoint and we reply by POSTing an activity back to the
Bot Connector ``serviceUrl`` carried on that activity (see the design's "Key
Design Decisions" and "Frontend Integration Module").

This module implements :class:`TeamsAdapter`, one concrete
:class:`app.bots.base.FrontendAdapter`:

* **Inbound** — :meth:`TeamsAdapter.handle_activity` is a FastAPI-agnostic
  handler: it takes an already-parsed activity ``dict``, runs the caller-supplied
  JWT/auth validation seam, extracts the message text, applies the shared
  inbound gate (:func:`app.bots.base.evaluate_inbound`), and forwards valid text
  to a provided dispatcher callback. Invalid or non-text activities get the
  shared rejection message and are never forwarded (Req 2.1/2.2). The handler
  returns the reply so the webhook route (``app/main.py``, task 15.1) can decide
  what to do with it.
* **Outbound** — :meth:`TeamsAdapter.send` posts a reply activity to the Bot
  Connector ``serviceUrl`` from the conversation reference, authorized with a
  Bot Framework bearer token. The HTTP client and the token provider are both
  injectable seams so tests never touch the network.
* **Formatting** — :meth:`TeamsAdapter.format` renders Teams-friendly markdown
  (a body plus a bold ``**Sources:**`` bullet list of citations), hard-capped at
  :data:`TEAMS_MAX_MESSAGE_CHARS` with body-only truncation so citations always
  survive, adding a truncation indicator when truncation occurs (Req 8.6/8.7).
* **Connectivity** — :meth:`TeamsAdapter.check_connectivity` acquires a Bot
  Framework token to verify the configured credentials (Req 3.1/3.2).

Teams app credentials (``app_id``/``app_password``) are resolved from the
Credential_Store per call, so Admin credential updates take effect immediately,
and neither value is ever placed in a log line or exception message. No secrets
are hardcoded in this module (Req 11.1).

Security note: JWT validation of inbound Bot Framework activities is delegated
to an injected seam (:class:`JwtValidator`). Production callers MUST inject a
real validator; the built-in default is a permissive no-op intended only for
tests and local development, and it emits a warning so an unauthenticated
deployment is never silent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol, runtime_checkable

import httpx

from app.bots.base import REJECTION_MESSAGE, FrontendAdapter, evaluate_inbound
from app.config import Settings, get_settings
from app.core.models import ConnectivityResult, GeneratedResponse

logger = logging.getLogger(__name__)

__all__ = [
    "TEAMS_MAX_MESSAGE_CHARS",
    "TEAMS_TRUNCATION_INDICATOR",
    "TEAMS_SOURCES_HEADER",
    "BOT_FRAMEWORK_LOGIN_URL",
    "BOT_FRAMEWORK_TENANT_LOGIN_URL_TEMPLATE",
    "BOT_FRAMEWORK_SCOPE",
    "render_teams_sources_footer",
    "format_teams_message",
    "extract_conversation_reference",
    "extract_activity_text",
    "TeamsError",
    "TeamsAuthError",
    "JwtValidator",
    "NoopJwtValidator",
    "IntegrationErrorLog",
    "TeamsAdapter",
]


# --- Teams / Bot Framework constants ------------------------------------

#: Practical upper bound for a single Teams message body. Teams accepts large
#: activity payloads (~28 KB); we cap formatted output here so a reply is never
#: rejected for length while leaving generous room for real answers (Req 8.6).
TEAMS_MAX_MESSAGE_CHARS = 28000

#: Appended to a truncated body so the End_User knows the answer was shortened
#: (Req 8.7). Rendered in Teams markdown (italic).
TEAMS_TRUNCATION_INDICATOR = "\n\n_Response truncated_"

#: Markdown header that introduces the citation footer. Bold per Teams styling.
TEAMS_SOURCES_HEADER = "\n\n**Sources:**\n"

#: OAuth2 token endpoint for acquiring a Bot Framework bearer token via the
#: client-credentials grant (app_id/app_password). Used for Multi Tenant app
#: registrations (the default when ``TEAMS_TENANT_ID`` is unset).
BOT_FRAMEWORK_LOGIN_URL = (
    "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
)

#: Tenant-specific OAuth2 token endpoint template for Single Tenant app
#: registrations. Selected when the ``TEAMS_TENANT_ID`` env var is set.
BOT_FRAMEWORK_TENANT_LOGIN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
)

#: OAuth2 scope requested for Bot Connector calls.
BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"

#: Timeout budget (seconds) for a single Bot Connector reply POST.
SEND_TIMEOUT_S = 10.0

#: Timeout budget (seconds) for a token-acquisition request.
TOKEN_TIMEOUT_S = 10.0


# --- Pure formatting helpers (mirrors Telegram, but Teams markdown) ------


def render_teams_sources_footer(citations: list[str]) -> str:
    """Render the ``**Sources:**`` markdown footer for a list of citations.

    Each citation is emitted as a markdown bullet with the document name in
    bold, matching Teams-native styling (Req 8.6).

    Args:
        citations: Unique source document names to list. May be empty.

    Returns:
        The markdown footer string (blank lines + bold ``Sources:`` header +
        one bold bullet per citation), or an empty string when there are no
        citations so a response with no sources gets no footer.
    """
    if not citations:
        return ""
    lines = "\n".join(f"- **{name}**" for name in citations)
    return f"{TEAMS_SOURCES_HEADER}{lines}"


def format_teams_message(text: str, citations: list[str]) -> str:
    """Format a Teams markdown message body + citations within the length cap.

    The delivered message is ``body`` followed by a bold ``**Sources:**``
    bullet list of every citation. When the combined length exceeds
    :data:`TEAMS_MAX_MESSAGE_CHARS`, the **body is truncated first** and a
    :data:`TEAMS_TRUNCATION_INDICATOR` is inserted so citations always survive
    (Req 8.6/8.7).

    Last-resort behavior: if the citation footer alone exceeds the cap
    (pathologically many/long citations), the body is dropped and the
    ``indicator + footer`` string is hard-capped to the limit. This trades away
    complete citations only in that degenerate case, but keeps the
    non-negotiable invariant — the returned message is **never** longer than
    :data:`TEAMS_MAX_MESSAGE_CHARS` — always true.

    Args:
        text: The response body text.
        citations: Source document names to list in the footer.

    Returns:
        The formatted Teams markdown message, guaranteed to be at most
        :data:`TEAMS_MAX_MESSAGE_CHARS` characters.
    """
    body = text or ""
    footer = render_teams_sources_footer(citations)
    full = body + footer

    if len(full) <= TEAMS_MAX_MESSAGE_CHARS:
        return full

    # Truncation required. Preserve the whole footer by shrinking the body.
    body_budget = (
        TEAMS_MAX_MESSAGE_CHARS - len(footer) - len(TEAMS_TRUNCATION_INDICATOR)
    )
    if body_budget >= 0:
        return body[:body_budget] + TEAMS_TRUNCATION_INDICATOR + footer

    # Degenerate case: citations alone exceed the cap. Drop the body and
    # hard-cap indicator + footer so the message never exceeds the limit.
    return (TEAMS_TRUNCATION_INDICATOR + footer)[:TEAMS_MAX_MESSAGE_CHARS]


# --- Activity parsing helpers -------------------------------------------


def extract_activity_text(activity: dict) -> str | None:
    """Extract the plain message text from a Bot Framework activity.

    Returns the ``text`` field only for message-type activities that carry a
    non-empty string; any other activity (e.g. a ``conversationUpdate`` event
    or an attachment-only message) yields ``None`` so the shared inbound gate
    treats it as non-text and rejects it without forwarding (Req 2.2).

    Args:
        activity: The parsed Bot Framework activity object.

    Returns:
        The message text, or ``None`` when the activity carries no usable text.
    """
    if activity.get("type") != "message":
        return None
    text = activity.get("text")
    return text if isinstance(text, str) else None


def extract_conversation_reference(activity: dict) -> dict:
    """Build a reply address from an inbound Bot Framework activity.

    The returned reference carries everything :meth:`TeamsAdapter.send` needs to
    post a reply back to the Bot Connector: the ``serviceUrl`` base, the
    conversation id, the inbound activity id (used to thread the reply), the
    channel id, and the ``from``/``recipient`` identities (swapped on reply so
    the bot answers the originating user).

    Args:
        activity: The parsed Bot Framework activity object.

    Returns:
        A conversation-reference ``dict`` with keys ``service_url``,
        ``conversation_id``, ``activity_id``, ``channel_id``, ``bot`` (our
        recipient identity), and ``user`` (the sender identity). Missing pieces
        are represented as empty strings / dicts rather than raising, so a
        malformed activity degrades gracefully.
    """
    conversation = activity.get("conversation")
    conversation_id = (
        conversation.get("id", "") if isinstance(conversation, dict) else ""
    )
    return {
        "service_url": (activity.get("serviceUrl") or "").rstrip("/"),
        "conversation_id": conversation_id,
        "activity_id": activity.get("id", ""),
        "channel_id": activity.get("channelId", "msteams"),
        # On reply the bot is the sender: it was the *recipient* of the inbound
        # activity, and it answers the inbound *from* identity.
        "bot": activity.get("recipient") if isinstance(activity.get("recipient"), dict) else {},
        "user": activity.get("from") if isinstance(activity.get("from"), dict) else {},
    }


# --- Errors --------------------------------------------------------------


class TeamsError(RuntimeError):
    """Raised when a Teams / Bot Framework call fails unrecoverably.

    The message never contains the app password or bearer token so credentials
    are not leaked into logs or error reports.
    """


class TeamsAuthError(TeamsError):
    """Raised when inbound JWT validation fails or a token cannot be acquired.

    Signals that the request is unauthenticated/unauthorized or that Bot
    Framework credentials are missing or invalid.
    """


# --- JWT validation seam -------------------------------------------------


@runtime_checkable
class JwtValidator(Protocol):
    """Pluggable inbound-authentication seam for Bot Framework activities.

    Implementations verify the ``Authorization`` header of an inbound request
    (a Bot Framework JWT) against the configured app credentials and the
    activity contents, raising :class:`TeamsAuthError` when validation fails.
    Declaring it as a protocol lets production inject a real validator while
    tests supply a trivial no-op — no secrets are hardcoded here.
    """

    async def validate(self, auth_header: str | None, activity: dict) -> None:
        """Validate the inbound request's auth header, raising on failure."""
        ...


class NoopJwtValidator:
    """Permissive no-op :class:`JwtValidator` for tests and local development.

    Accepts every request and logs a single warning per instance so an
    unauthenticated deployment is never silent. Production callers MUST inject a
    real validator instead (Req 11.1 — do not ship unauthenticated webhooks).
    """

    def __init__(self) -> None:
        self._warned = False

    async def validate(self, auth_header: str | None, activity: dict) -> None:
        """Accept any request without checking it, warning once.

        Args:
            auth_header: The inbound ``Authorization`` header value (ignored).
            activity: The parsed activity (ignored).
        """
        if not self._warned:
            logger.warning(
                "TeamsAdapter is using a no-op JWT validator; inbound Bot "
                "Framework activities are NOT authenticated. Inject a real "
                "JwtValidator before deploying."
            )
            self._warned = True


# --- Optional error-log sink --------------------------------------------


@runtime_checkable
class IntegrationErrorLog(Protocol):
    """Minimal seam for recording integration error-log entries.

    Mirrors the ``integration_error_log`` repository (Req 2.7, 3.3): each entry
    carries the Frontend_Tool identifier, the operation, and the error detail.
    """

    def record(self, tool: str, operation: str, error_detail: str) -> None:
        """Persist one integration error-log entry."""
        ...


#: An async callback that handles a validated inbound query. It receives the
#: originating conversation reference and the validated text and returns the
#: :class:`GeneratedResponse` to reply with.
TeamsDispatcher = Callable[[dict, str], Awaitable[GeneratedResponse]]

#: An async callable that returns a Bot Framework bearer token. Injected in
#: tests to avoid the network; the default acquires one via client credentials.
TokenProvider = Callable[[], Awaitable[str]]


class TeamsAdapter(FrontendAdapter):
    """Teams adapter: inbound webhook handling, sending, formatting, connectivity.

    App credentials (``app_id``/``app_password``) are resolved from the
    Credential_Store per call. The HTTP client, token provider, and JWT
    validator are all injectable seams so tests never touch the network and no
    secrets are hardcoded.

    Args:
        credential_store: Object exposing
            ``load("teams") -> {"app_id": ..., "app_password": ...}`` (the real
            :class:`app.security.credentials.CredentialStore` or a compatible
            fake).
        settings: Settings snapshot. Defaults to the process-wide cached
            settings.
        http_client: Optional pre-built async HTTP client. When provided the
            caller owns its lifecycle (this class will not close it); this is
            the primary injection point for tests. When omitted, the adapter
            builds and owns an :class:`httpx.AsyncClient`.
        jwt_validator: Inbound-auth seam. Defaults to :class:`NoopJwtValidator`
            (tests/local only); production MUST inject a real validator.
        token_provider: Optional async callable returning a Bot Framework
            bearer token. When omitted, tokens are acquired via the
            client-credentials grant using the stored app credentials.
        error_log: Optional sink for integration error-log entries.
    """

    tool_name = "teams"

    def __init__(
        self,
        *,
        credential_store: "_CredentialLoader",
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        jwt_validator: JwtValidator | None = None,
        token_provider: TokenProvider | None = None,
        error_log: IntegrationErrorLog | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._settings = settings if settings is not None else get_settings()
        self._jwt_validator: JwtValidator = jwt_validator or NoopJwtValidator()
        self._token_provider = token_provider
        self._error_log = error_log

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient()
            self._owns_client = True

    # --- Lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TeamsAdapter":
        """Enter an async context manager, returning ``self``."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit the async context manager, closing an owned client."""
        await self.aclose()

    # --- FrontendAdapter: formatting ------------------------------------

    def format(self, response: GeneratedResponse) -> str:
        """Render a generated response as a Teams markdown message.

        Delegates to :func:`format_teams_message`: body text plus a bold
        ``**Sources:**`` bullet list, capped at :data:`TEAMS_MAX_MESSAGE_CHARS`
        with body-only truncation preserving citations (Req 8.6/8.7).

        Args:
            response: The generated response to format.

        Returns:
            The formatted Teams markdown message string, at most
            :data:`TEAMS_MAX_MESSAGE_CHARS` characters.
        """
        return format_teams_message(response.text, list(response.citations or []))

    # --- Inbound webhook handler (FastAPI-agnostic) ---------------------

    async def handle_activity(
        self,
        activity: dict,
        dispatcher: TeamsDispatcher,
        *,
        auth_header: str | None = None,
        send_reply: bool = True,
    ) -> GeneratedResponse | None:
        """Handle one inbound Bot Framework activity end to end.

        This is intentionally FastAPI-agnostic so the webhook route in
        ``app/main.py`` (task 15.1) can call it with a plain parsed ``dict``.
        The steps are:

        1. Validate the inbound request via the injected JWT seam (Req 11.1);
           a failure raises :class:`TeamsAuthError` and nothing is forwarded.
        2. Extract the message text and apply the shared inbound gate
           (:func:`app.bots.base.evaluate_inbound`). Valid text (1-4,000 chars)
           is forwarded to ``dispatcher``; anything else yields the shared
           rejection message and is never forwarded (Req 2.1/2.2).
        3. Optionally deliver the reply to the Bot Connector (``send_reply``),
           and return the :class:`GeneratedResponse` so the caller can act on
           it (e.g. include it in the HTTP response).

        Args:
            activity: The parsed Bot Framework activity object.
            dispatcher: Async callback invoked as ``dispatcher(conversation_ref,
                text)`` for valid inbound queries, returning the response to
                reply with.
            auth_header: The inbound request's ``Authorization`` header value,
                passed to the JWT validation seam.
            send_reply: When ``True`` (default), the formatted reply is
                delivered to the Bot Connector via :meth:`send`. Set ``False``
                to let the caller handle delivery.

        Returns:
            The :class:`GeneratedResponse` used for the reply, or ``None`` for
            an activity that carries no reply target (e.g. a non-message system
            event with no conversation).

        Raises:
            TeamsAuthError: If inbound JWT validation fails.
        """
        await self._jwt_validator.validate(auth_header, activity)

        if activity.get("type") != "message":
            # System events (conversationUpdate, typing, ...) carry no user
            # query; ignore them silently instead of replying with a rejection.
            return None

        conversation_ref = extract_conversation_reference(activity)
        if not conversation_ref.get("conversation_id"):
            # No conversation to reply to (e.g. a bare system event).
            return None

        text = extract_activity_text(activity)
        decision = evaluate_inbound(text)
        if decision.forward:
            response = await dispatcher(conversation_ref, decision.query_text)
        else:
            response = GeneratedResponse(text=REJECTION_MESSAGE, status="rejected")

        if send_reply:
            await self.send(conversation_ref, self.format(response))
        return response

    # --- FrontendAdapter: sending ---------------------------------------

    async def send(self, conversation_ref: dict, text: str) -> None:
        """Deliver a reply activity to the Bot Connector ``serviceUrl``.

        Acquires a Bot Framework bearer token (via the injected token provider
        or the client-credentials grant) and POSTs a ``message`` activity to
        ``{service_url}/v3/conversations/{conversation_id}/activities``.

        Args:
            conversation_ref: Reply address produced by
                :func:`extract_conversation_reference`.
            text: The message body to deliver (already formatted as markdown).

        Raises:
            TeamsError: On a missing ``serviceUrl``/conversation id or an HTTP
                error status from the Bot Connector.
            TeamsAuthError: If a bearer token cannot be acquired.
        """
        service_url = conversation_ref.get("service_url")
        conversation_id = conversation_ref.get("conversation_id")
        if not service_url or not conversation_id:
            raise TeamsError("Teams reply missing serviceUrl or conversation id")

        token = await self._get_token()

        url = f"{service_url}/v3/conversations/{conversation_id}/activities"
        activity_id = conversation_ref.get("activity_id")
        if activity_id:
            url = f"{url}/{activity_id}"

        payload: dict = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }
        # Swap identities so the bot answers the originating user.
        if conversation_ref.get("bot"):
            payload["from"] = conversation_ref["bot"]
        if conversation_ref.get("user"):
            payload["recipient"] = conversation_ref["user"]

        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=SEND_TIMEOUT_S,
            )
        except httpx.TransportError as exc:
            self._log_failure("send", exc)
            raise TeamsError("Teams reply delivery failed: connector unreachable") from exc

        if response.status_code >= 400:
            detail = f"Bot Connector reply returned HTTP {response.status_code}"
            self._log_failure("send", detail)
            raise TeamsError(detail)

    # --- FrontendAdapter: connectivity ----------------------------------

    async def check_connectivity(self) -> ConnectivityResult:
        """Verify Teams credentials by acquiring a Bot Framework token.

        Returns a :class:`ConnectivityResult` describing the outcome rather than
        raising, so the status monitor and Admin test function can render it
        uniformly (Req 3.1/3.2). A successful token acquisition confirms the
        configured ``app_id``/``app_password`` are valid.

        Returns:
            A :class:`ConnectivityResult` for the ``teams`` tool.
        """
        checked_at = datetime.now(timezone.utc)
        try:
            await self._get_token()
        except TeamsAuthError as exc:
            return ConnectivityResult(
                tool=self.tool_name,
                ok=False,
                detail=str(exc),
                checked_at=checked_at,
            )
        except httpx.TransportError:
            self._log_failure("check_connectivity", "token endpoint unreachable")
            return ConnectivityResult(
                tool=self.tool_name,
                ok=False,
                detail="Bot Framework token endpoint unreachable",
                checked_at=checked_at,
            )
        return ConnectivityResult(
            tool=self.tool_name,
            ok=True,
            detail="Bot Framework token acquired",
            checked_at=checked_at,
        )

    # --- Internals -------------------------------------------------------

    async def _get_token(self) -> str:
        """Return a Bot Framework bearer token via the seam or credentials.

        Uses the injected :data:`TokenProvider` when supplied (the test seam);
        otherwise performs a client-credentials OAuth2 request with the stored
        app credentials.

        Returns:
            The bearer token string.

        Raises:
            TeamsAuthError: If credentials are missing or the token endpoint
                does not return a token. The message never includes any secret.
        """
        if self._token_provider is not None:
            return await self._token_provider()
        return await self._acquire_token()

    def _login_url(self) -> str:
        """Return the OAuth2 token endpoint for the configured app type.

        Returns:
            The tenant-specific endpoint when ``teams_tenant_id`` is set in
            settings (Single Tenant app registration); otherwise the
            multi-tenant ``botframework.com`` endpoint.
        """
        tenant_id = self._settings.teams_tenant_id
        if tenant_id:
            return BOT_FRAMEWORK_TENANT_LOGIN_URL_TEMPLATE.format(
                tenant_id=tenant_id
            )
        return BOT_FRAMEWORK_LOGIN_URL

    async def _acquire_token(self) -> str:
        """Acquire a Bot Framework token via the client-credentials grant.

        Returns:
            The bearer token string.

        Raises:
            TeamsAuthError: If app credentials are unavailable, the endpoint is
                unreachable, or no token is returned. No secret is included in
                the message.
        """
        app_id, app_password = self._resolve_credentials()
        data = {
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": app_password,
            "scope": BOT_FRAMEWORK_SCOPE,
        }
        try:
            response = await self._client.post(
                self._login_url(), data=data, timeout=TOKEN_TIMEOUT_S
            )
        except httpx.TransportError as exc:
            self._log_failure("acquire_token", exc)
            raise  # surfaced by callers as unreachable/transport error

        if response.status_code >= 400:
            self._log_failure(
                "acquire_token", f"token endpoint HTTP {response.status_code}"
            )
            raise TeamsAuthError(
                f"Bot Framework token request failed with status "
                f"{response.status_code}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TeamsAuthError("Bot Framework token response was not JSON") from exc

        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise TeamsAuthError("Bot Framework token response contained no token")
        return token

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve the Teams app credentials from the Credential_Store.

        Returns:
            A ``(app_id, app_password)`` tuple.

        Raises:
            TeamsAuthError: If either credential is unavailable. The message
                never includes any credential material.
        """
        try:
            creds = self._credential_store.load("teams")
        except Exception as exc:  # noqa: BLE001 - store failure must not leak
            raise TeamsAuthError("Credential_Store lookup for teams failed") from exc
        if not creds or not creds.get("app_id") or not creds.get("app_password"):
            raise TeamsAuthError("Teams app credentials are not configured")
        return creds["app_id"], creds["app_password"]

    def _log_failure(self, operation: str, detail: object) -> None:
        """Log a Teams integration failure to the logger and error-log sink.

        Args:
            operation: The operation that failed (e.g. ``"send"``).
            detail: The error detail (an exception or a message string).
        """
        message = f"Teams {operation} failure: {detail!r}"
        logger.error(message)
        if self._error_log is not None:
            try:
                self._error_log.record(self.tool_name, operation, message)
            except Exception:  # noqa: BLE001 - logging must never break delivery
                logger.exception("Failed to record integration error-log entry")


@runtime_checkable
class _CredentialLoader(Protocol):
    """Minimal seam for the Credential_Store as consumed by this adapter."""

    def load(self, integration: str) -> dict[str, str] | None:
        """Return stored credential fields for ``integration`` or ``None``."""
        ...
