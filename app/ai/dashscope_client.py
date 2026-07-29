"""Async client for the DashScope (Aliyun Tongyi Qwen) AI vendor.

IntelliKnow KMS uses a single AI vendor for every AI task (Req 7.1, 8.1):
Qwen-Max for intent classification and RAG generation, and
``text-embedding-v3`` for query/document embeddings. DashScope exposes an
OpenAI-compatible surface, so this client speaks that dialect over
``httpx.AsyncClient``:

* Chat completions: ``POST {base_url}/chat/completions`` against ``qwen-max``.
  JSON-mode / structured output is available to callers via ``json_mode`` so
  the classifier can request a strict JSON object (Req 7.1).
* Embeddings: ``POST {base_url}/embeddings`` against ``text-embedding-v3``,
  accepting a batch of strings and returning one ``float32`` ``numpy`` vector
  per input. Vectors are returned un-normalized so callers can L2-normalize
  them for cosine search (the FAISS layer expects normalized vectors).

Design seams and guarantees:

* **Single mocking seam** — the only outbound I/O goes through one injectable
  ``httpx.AsyncClient``. Tests inject a client (or use ``respx`` to patch the
  transport) and never make real network calls.
* **Explicit per-call timeouts** — classification 5s, embedding 5s, RAG 10s
  (Req 8.8), exposed as module constants and as defaults on the named methods.
  Every method also accepts an explicit ``timeout`` override.
* **Bounded retries** — transient failures (connection errors, timeouts, and
  5xx/429 responses) are retried a bounded number of times with exponential
  backoff; non-transient failures surface immediately.
* **API key resolution** — the Credential_Store is the primary source
  (``dashscope`` -> ``api_key``, Req 11.4); if it is unavailable the client
  falls back to ``settings.dashscope_api_key``. The key is resolved per call
  so Admin credential updates take effect immediately, and it is **never**
  logged or placed in an exception message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

import httpx
import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# --- Endpoint / model constants -----------------------------------------

#: OpenAI-compatible base URL for DashScope.
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

#: Path suffixes appended to the base URL.
CHAT_PATH = "/chat/completions"
EMBEDDINGS_PATH = "/embeddings"

#: Qwen-Max is used for both classification and RAG generation.
CHAT_MODEL = "qwen-max"
#: DashScope embedding model paired with the same credentials.
EMBEDDING_MODEL = "text-embedding-v3"

# --- Per-call timeout budgets (seconds), see Req 8.8 --------------------

CLASSIFICATION_TIMEOUT_S = 5.0
EMBEDDING_TIMEOUT_S = 5.0
RAG_TIMEOUT_S = 10.0

# --- Retry policy --------------------------------------------------------

#: Number of *additional* attempts after the first for transient failures.
DEFAULT_MAX_RETRIES = 2
#: Base seconds for exponential backoff between retries (0 disables sleeping).
DEFAULT_BACKOFF_BASE_S = 0.5
#: HTTP status codes treated as transient and therefore retryable.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class DashScopeError(RuntimeError):
    """Raised when a DashScope call fails and cannot be recovered.

    The message never contains the API key or the ``Authorization`` header so
    that credentials are not leaked into logs or error reports (Req 11.4).
    """


class DashScopeTimeoutError(DashScopeError):
    """Raised when a DashScope call exhausts its per-call timeout budget."""


@runtime_checkable
class CredentialProvider(Protocol):
    """Minimal seam for the Credential_Store as consumed by this client.

    The Credential_Store (``app/security/credentials.py``) exposes a ``load``
    method returning the stored fields for an integration, or ``None`` when the
    integration has never been configured. Declaring the dependency as this
    protocol lets the real store be injected without a rewrite and lets tests
    supply a trivial fake.
    """

    def load(self, integration: str) -> dict[str, str] | None:
        """Return stored credential fields for ``integration`` or ``None``."""
        ...


class DashScopeClient:
    """Async DashScope client for chat completion and embeddings.

    This is the single seam through which the application talks to the AI
    vendor. All network I/O flows through one :class:`httpx.AsyncClient`, which
    may be injected for testing.

    Args:
        settings: Settings snapshot used for the fallback API key. Defaults to
            the process-wide cached settings.
        credential_store: Optional primary API-key source (the Credential_Store
            or any object with a compatible ``load`` method). When present and
            it yields a ``dashscope``/``api_key`` value, that key wins over the
            settings fallback (Req 11.4).
        http_client: Optional pre-built async HTTP client. When provided the
            caller owns its lifecycle (this class will not close it); this is
            the primary injection point for tests. When omitted, the client
            builds and owns its own :class:`httpx.AsyncClient`.
        base_url: DashScope OpenAI-compatible base URL.
        max_retries: Additional attempts after the first on transient errors.
        backoff_base_s: Base seconds for exponential backoff between retries.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        credential_store: CredentialProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._credential_store = credential_store
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, max_retries)
        self._backoff_base_s = max(0.0, backoff_base_s)

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient()
            self._owns_client = True

    # --- Lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance owns it.

        No-op for an injected client, whose lifecycle the caller controls.
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "DashScopeClient":
        """Enter an async context manager, returning ``self``."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit the async context manager, closing an owned client."""
        await self.aclose()

    # --- Public API ------------------------------------------------------

    async def chat_completion(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str = CHAT_MODEL,
        timeout: float = RAG_TIMEOUT_S,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **extra: Any,
    ) -> str:
        """Run a chat completion and return the assistant message content.

        Args:
            messages: OpenAI-style chat messages (``{"role", "content"}``).
            model: Chat model name; defaults to ``qwen-max``.
            timeout: Per-call timeout in seconds.
            json_mode: When ``True`` request a strict JSON object response
                (``response_format={"type": "json_object"}``) so callers can
                parse structured output reliably.
            max_tokens: Optional cap on generated tokens.
            temperature: Optional sampling temperature.
            **extra: Additional payload fields passed through unchanged.

        Returns:
            The text content of the first choice's assistant message.

        Raises:
            DashScopeTimeoutError: If the call exhausts its timeout budget.
            DashScopeError: On any other unrecoverable failure (including a
                malformed response).
        """
        payload: dict[str, Any] = {"model": model, "messages": list(messages)}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        payload.update(extra)

        data = await self._post(CHAT_PATH, payload, timeout)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DashScopeError(
                "Malformed chat completion response from DashScope"
            ) from exc

    async def classify(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        timeout: float = CLASSIFICATION_TIMEOUT_S,
        json_mode: bool = True,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> str:
        """Chat completion tuned for intent classification.

        Uses the classification timeout budget (5s) and JSON mode by default so
        the classifier receives a parseable JSON object (Req 7.1, 8.8).

        Args:
            messages: Classification prompt messages.
            timeout: Per-call timeout; defaults to
                :data:`CLASSIFICATION_TIMEOUT_S`.
            json_mode: Request a strict JSON object; defaults to ``True``.
            max_tokens: Optional generated-token cap.
            **extra: Additional payload fields passed through unchanged.

        Returns:
            The assistant message content (expected to be JSON text).
        """
        return await self.chat_completion(
            messages,
            timeout=timeout,
            json_mode=json_mode,
            max_tokens=max_tokens,
            **extra,
        )

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        timeout: float = RAG_TIMEOUT_S,
        json_mode: bool = False,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> str:
        """Chat completion tuned for RAG answer generation.

        Uses the RAG timeout budget (10s, Req 8.8).

        Args:
            messages: RAG prompt messages (grounding passages + user query).
            timeout: Per-call timeout; defaults to :data:`RAG_TIMEOUT_S`.
            json_mode: Whether to request a strict JSON object; defaults to
                ``False`` for free-form answers.
            max_tokens: Optional generated-token cap.
            **extra: Additional payload fields passed through unchanged.

        Returns:
            The generated answer text.
        """
        return await self.chat_completion(
            messages,
            timeout=timeout,
            json_mode=json_mode,
            max_tokens=max_tokens,
            **extra,
        )

    async def embed(
        self,
        texts: str | Iterable[str],
        *,
        model: str = EMBEDDING_MODEL,
        timeout: float = EMBEDDING_TIMEOUT_S,
    ) -> list[np.ndarray]:
        """Embed one or more texts, returning ``float32`` vectors.

        Args:
            texts: A single string or an iterable of strings to embed in one
                batched request.
            model: Embedding model name; defaults to ``text-embedding-v3``.
            timeout: Per-call timeout; defaults to :data:`EMBEDDING_TIMEOUT_S`.

        Returns:
            One ``numpy`` ``float32`` array per input text, in input order. The
            vectors are returned un-normalized; callers L2-normalize as needed
            for cosine search.

        Raises:
            DashScopeTimeoutError: If the call exhausts its timeout budget.
            DashScopeError: On any other unrecoverable failure (including a
                malformed response).
        """
        if isinstance(texts, str):
            inputs = [texts]
        else:
            inputs = list(texts)

        payload = {"model": model, "input": inputs}
        data = await self._post(EMBEDDINGS_PATH, payload, timeout)
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            return [
                np.asarray(item["embedding"], dtype=np.float32) for item in items
            ]
        except (KeyError, TypeError) as exc:
            raise DashScopeError(
                "Malformed embeddings response from DashScope"
            ) from exc

    # --- Internals -------------------------------------------------------

    def _resolve_api_key(self) -> str:
        """Resolve the DashScope API key, Credential_Store first (Req 11.4).

        Returns:
            The resolved API key string.

        Raises:
            DashScopeError: If no key is available from either source. The
                error message never includes any key material.
        """
        if self._credential_store is not None:
            try:
                creds = self._credential_store.load("dashscope")
            except Exception:  # noqa: BLE001 - store failure must not leak/crash
                logger.warning(
                    "Credential_Store lookup for dashscope failed; "
                    "falling back to settings"
                )
                creds = None
            if creds and creds.get("api_key"):
                return creds["api_key"]

        if self._settings.dashscope_api_key:
            return self._settings.dashscope_api_key

        raise DashScopeError(
            "No DashScope API key available from Credential_Store or settings"
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """POST ``payload`` to ``path`` with bounded retries; return JSON.

        Retries transient failures (connection errors, timeouts, and
        429/5xx responses) up to ``max_retries`` times with exponential
        backoff. Never logs the API key or the ``Authorization`` header.

        Args:
            path: Endpoint path (e.g. ``/chat/completions``).
            payload: JSON request body.
            timeout: Per-call timeout in seconds.

        Returns:
            The decoded JSON response body.

        Raises:
            DashScopeTimeoutError: On timeout after exhausting retries.
            DashScopeError: On other transport failures, error status codes,
                or an undecodable body.
        """
        api_key = self._resolve_api_key()
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    attempt += 1
                    continue
                raise DashScopeTimeoutError(
                    f"DashScope request to {path} timed out after {timeout}s"
                ) from exc
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    attempt += 1
                    continue
                raise DashScopeError(
                    f"DashScope request to {path} failed: transport error"
                ) from exc

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < self._max_retries
            ):
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code >= 400:
                raise DashScopeError(
                    f"DashScope request to {path} failed with status "
                    f"{response.status_code}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise DashScopeError(
                    f"DashScope response from {path} was not valid JSON"
                ) from exc

    async def _backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff before the next retry.

        Args:
            attempt: Zero-based index of the attempt that just failed.
        """
        if self._backoff_base_s <= 0:
            return
        await asyncio.sleep(self._backoff_base_s * (2**attempt))
