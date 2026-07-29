"""Unit tests for the DashScope client (task 5.1).

All DashScope HTTP is mocked with ``respx`` — no real network calls are made.
The tests cover: a chat completion parsing a response, a batched embeddings
call returning ``float32`` vectors, a timeout path surfacing an error, and the
Credential_Store-first API-key resolution seam (Req 11.4).
"""

from __future__ import annotations

import httpx
import numpy as np
import pytest
import respx

from app.ai.dashscope_client import (
    CHAT_MODEL,
    DEFAULT_BASE_URL,
    EMBEDDING_MODEL,
    DashScopeClient,
    DashScopeTimeoutError,
)
from app.config import load_settings

CHAT_URL = f"{DEFAULT_BASE_URL}/chat/completions"
EMBEDDINGS_URL = f"{DEFAULT_BASE_URL}/embeddings"


def _settings(api_key: str = "sk-testfallbackkey0001"):
    """Build a Settings snapshot with a fallback DashScope key for tests."""
    return load_settings({"DASHSCOPE_API_KEY": api_key})


class _FakeCredentialStore:
    """Minimal Credential_Store stand-in returning fixed fields."""

    def __init__(self, fields: dict[str, str] | None) -> None:
        self._fields = fields

    def load(self, integration: str) -> dict[str, str] | None:
        return self._fields if integration == "dashscope" else None


@respx.mock
async def test_chat_completion_parses_response_content():
    """A chat completion returns the assistant message content and uses auth."""
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "HR space"}}
                ]
            },
        )
    )

    client = DashScopeClient(settings=_settings(), max_retries=0, backoff_base_s=0)
    try:
        content = await client.chat_completion(
            [{"role": "user", "content": "Where is the salary policy?"}],
            json_mode=True,
        )
    finally:
        await client.aclose()

    assert content == "HR space"
    assert route.called
    request = route.calls.last.request
    # Correct model + JSON mode are sent; Bearer auth is present.
    body = request.content.decode()
    assert CHAT_MODEL in body
    assert "json_object" in body
    assert request.headers["Authorization"] == "Bearer sk-testfallbackkey0001"


@respx.mock
async def test_embed_returns_float32_vectors_in_order():
    """Batched embeddings return one float32 numpy vector per input, in order."""
    route = respx.post(EMBEDDINGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [1.0, 2.0, 2.0]},
                ]
            },
        )
    )

    client = DashScopeClient(settings=_settings(), max_retries=0, backoff_base_s=0)
    try:
        vectors = await client.embed(["first text", "second text"])
    finally:
        await client.aclose()

    assert route.called
    assert EMBEDDING_MODEL in route.calls.last.request.content.decode()
    assert len(vectors) == 2
    # Returned in ascending input index order (index 0 first).
    assert vectors[0].dtype == np.float32
    np.testing.assert_allclose(vectors[0], np.array([1.0, 2.0, 2.0], dtype=np.float32))
    np.testing.assert_allclose(vectors[1], np.array([0.3, 0.4], dtype=np.float32))
    # Vectors are returned un-normalized so callers can L2-normalize.
    assert np.linalg.norm(vectors[0]) > 1.0


@respx.mock
async def test_timeout_surfaces_dashscope_timeout_error():
    """A request timeout surfaces as DashScopeTimeoutError after retries."""
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    client = DashScopeClient(settings=_settings(), max_retries=0, backoff_base_s=0)
    try:
        with pytest.raises(DashScopeTimeoutError):
            await client.generate(
                [{"role": "user", "content": "hello"}], timeout=0.01
            )
    finally:
        await client.aclose()


@respx.mock
async def test_credential_store_key_takes_precedence_over_settings():
    """The Credential_Store API key is used ahead of the settings fallback."""
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )
    )
    store = _FakeCredentialStore({"api_key": "sk-fromcredentialstore99"})

    client = DashScopeClient(
        settings=_settings("sk-testfallbackkey0001"),
        credential_store=store,
        max_retries=0,
        backoff_base_s=0,
    )
    try:
        await client.chat_completion([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    assert (
        route.calls.last.request.headers["Authorization"]
        == "Bearer sk-fromcredentialstore99"
    )
