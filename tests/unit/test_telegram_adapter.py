"""Unit tests for the Telegram adapter's networked paths (mocked via respx).

These cover the sender's proxy-retry behavior (Req 12.6), the ``getMe``
connectivity check (Req 3.1/3.2), and the long-polling gate that forwards valid
text and rejects invalid/non-text messages (Req 2.1/2.2). All Telegram HTTP is
mocked with respx so no real network calls are made.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.bots.base import REJECTION_MESSAGE
from app.bots.telegram import (
    TELEGRAM_API_BASE,
    TelegramAdapter,
    TelegramProxyError,
)
from app.core.models import GeneratedResponse

_TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_BASE = f"{TELEGRAM_API_BASE}/bot{_TOKEN}"


class _FakeStore:
    """Trivial Credential_Store stand-in returning a fixed telegram token."""

    def __init__(self, token: str | None = _TOKEN) -> None:
        self._token = token

    def load(self, integration: str) -> dict[str, str] | None:
        if integration == "telegram" and self._token is not None:
            return {"bot_token": self._token}
        return None


class _RecordingErrorLog:
    """Captures integration error-log entries for assertions."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def record(self, tool: str, operation: str, error_detail: str) -> None:
        self.entries.append((tool, operation, error_detail))


def _adapter(**kwargs) -> TelegramAdapter:
    """Build an adapter with an injected client and no backoff delay."""
    return TelegramAdapter(
        credential_store=_FakeStore(),
        http_client=httpx.AsyncClient(),
        backoff_base_s=0.0,
        **kwargs,
    )


@respx.mock
async def test_send_success_posts_to_send_message() -> None:
    """A successful send POSTs chat_id + text to sendMessage."""
    route = respx.post(f"{_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    adapter = _adapter()
    await adapter.send({"chat_id": 42}, "hello")
    await adapter.aclose()

    assert route.called
    assert route.calls.last.request.content == b'{"chat_id": 42, "text": "hello"}'


@respx.mock
async def test_send_retries_on_proxy_error_then_succeeds() -> None:
    """A transient proxy connectivity error is retried, then delivery succeeds."""
    error_log = _RecordingErrorLog()
    route = respx.post(f"{_BASE}/sendMessage").mock(
        side_effect=[
            httpx.ConnectError("proxy down"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    adapter = _adapter(error_log=error_log)
    await adapter.send({"chat_id": 7}, "hi")
    await adapter.aclose()

    assert route.call_count == 2
    # The single failed attempt was logged as a proxy failure.
    assert len(error_log.entries) == 1
    assert error_log.entries[0][0:2] == ("telegram", "sendMessage")


@respx.mock
async def test_send_raises_after_exhausting_proxy_retries() -> None:
    """Persistent proxy failure exhausts retries and raises TelegramProxyError."""
    error_log = _RecordingErrorLog()
    respx.post(f"{_BASE}/sendMessage").mock(
        side_effect=httpx.ConnectError("proxy down")
    )
    adapter = _adapter(error_log=error_log, proxy_max_retries=3)

    with pytest.raises(TelegramProxyError):
        await adapter.send({"chat_id": 1}, "hi")
    await adapter.aclose()

    # Initial attempt + 3 retries = 4 logged proxy failures.
    assert len(error_log.entries) == 4


@respx.mock
async def test_check_connectivity_ok_via_get_me() -> None:
    """check_connectivity reports ok when getMe returns ok=True."""
    respx.get(f"{_BASE}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"id": 1}})
    )
    adapter = _adapter()
    result = await adapter.check_connectivity()
    await adapter.aclose()

    assert result.tool == "telegram"
    assert result.ok is True


@respx.mock
async def test_check_connectivity_reports_proxy_failure() -> None:
    """A transport error during getMe yields an unhealthy result, not an exception."""
    respx.get(f"{_BASE}/getMe").mock(side_effect=httpx.ConnectError("proxy down"))
    adapter = _adapter()
    result = await adapter.check_connectivity()
    await adapter.aclose()

    assert result.ok is False
    assert "Outbound_Proxy" in result.detail


@respx.mock
async def test_polling_forwards_valid_text_and_rejects_non_text() -> None:
    """The gate forwards valid text to the handler and rejects non-text messages."""
    updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 100}, "text": "what is the leave policy?"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 200}, "sticker": {"file_id": "abc"}},
        },
    ]
    # First getUpdates returns the batch; the poll loop is stopped afterward.
    respx.get(f"{_BASE}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": updates})
    )
    send_route = respx.post(f"{_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    forwarded: list[tuple[dict, str]] = []

    async def handler(conversation_ref: dict, text: str) -> None:
        forwarded.append((conversation_ref, text))

    import asyncio

    stop = asyncio.Event()
    adapter = _adapter()

    async def stop_after_first() -> None:
        # Let one polling iteration run, then request shutdown.
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        adapter.run_polling(handler, stop_event=stop, poll_timeout=0),
        stop_after_first(),
    )
    await adapter.aclose()

    # Valid text forwarded to the orchestrator handler.
    assert forwarded == [({"chat_id": 100}, "what is the leave policy?")]
    # Non-text message rejected with the shared rejection message, not forwarded.
    assert send_route.called
    assert REJECTION_MESSAGE in send_route.calls.last.request.content.decode()


@respx.mock
async def test_polling_survives_http_error_from_proxy(monkeypatch) -> None:
    """A 502 from the proxy during getUpdates is retried, not fatal.

    Regression: a transient ``502 Bad Gateway`` (proxy relay briefly
    unhealthy) raised ``httpx.HTTPStatusError``, which previously escaped the
    poll loop and silently killed the bot until a restart. The loop must
    absorb it, back off, and keep polling.
    """
    import asyncio

    import app.bots.telegram as tg

    async def _no_sleep(*_args, **_kwargs) -> None:
        return None

    # Keep the test fast: skip the real back-off delay.
    monkeypatch.setattr(tg.asyncio, "sleep", _no_sleep)

    valid = [{"update_id": 1, "message": {"chat": {"id": 100}, "text": "hi?"}}]
    respx.get(f"{_BASE}/getUpdates").mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),  # transient proxy failure
            httpx.Response(200, json={"ok": True, "result": valid}),
            httpx.Response(200, json={"ok": True, "result": []}),
        ]
    )
    respx.post(f"{_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    forwarded: list[tuple[dict, str]] = []
    stop = asyncio.Event()

    async def handler(conversation_ref: dict, text: str) -> None:
        forwarded.append((conversation_ref, text))
        stop.set()  # recovery proven — end the loop

    adapter = _adapter()
    await asyncio.wait_for(
        adapter.run_polling(handler, stop_event=stop, poll_timeout=0), timeout=5
    )
    await adapter.aclose()

    # The loop survived the 502 and processed the update from the next poll.
    assert forwarded == [({"chat_id": 100}, "hi?")]


@respx.mock
async def test_polling_survives_handler_error(monkeypatch) -> None:
    """A handler error on one update is contained; the loop keeps polling.

    Regression: the query handler runs outside the fetch try/except, so a
    single failing update (e.g. an orchestrator/AI error) used to propagate
    out and kill the poll loop, taking the whole bot offline.
    """
    import asyncio

    import app.bots.telegram as tg

    async def _no_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(tg.asyncio, "sleep", _no_sleep)

    batch1 = [{"update_id": 1, "message": {"chat": {"id": 100}, "text": "boom?"}}]
    batch2 = [{"update_id": 2, "message": {"chat": {"id": 200}, "text": "ok?"}}]
    respx.get(f"{_BASE}/getUpdates").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": batch1}),
            httpx.Response(200, json={"ok": True, "result": batch2}),
            httpx.Response(200, json={"ok": True, "result": []}),
        ]
    )
    respx.post(f"{_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    seen: list[str] = []
    stop = asyncio.Event()

    async def handler(conversation_ref: dict, text: str) -> None:
        seen.append(text)
        if text == "boom?":
            raise RuntimeError("orchestrator blew up")
        stop.set()  # second update processed → loop survived the first error

    adapter = _adapter()
    await asyncio.wait_for(
        adapter.run_polling(handler, stop_event=stop, poll_timeout=0), timeout=5
    )
    await adapter.aclose()

    # Both updates reached the handler; the first one's error did not stop it.
    assert seen == ["boom?", "ok?"]


@respx.mock
async def test_polling_activates_when_token_configured_later(monkeypatch) -> None:
    """The poller idles without a token and starts polling once one is saved.

    Regression: the poll loop must survive a missing bot token (raising
    TelegramConfigError) by idling, so an Admin who configures Telegram for the
    first time via the UI gets a working bot without an app restart. Once the
    token resolves, the very next poll uses it.
    """
    import asyncio

    import app.bots.telegram as tg

    async def _no_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(tg.asyncio, "sleep", _no_sleep)

    # Simulate a token being saved after a few polls: None until the 3rd call.
    resolve_calls = {"n": 0}

    def resolver() -> str | None:
        resolve_calls["n"] += 1
        return None if resolve_calls["n"] < 3 else _TOKEN

    valid = [{"update_id": 1, "message": {"chat": {"id": 100}, "text": "ping?"}}]
    respx.get(f"{_BASE}/getUpdates").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": valid}),
            httpx.Response(200, json={"ok": True, "result": []}),
        ]
    )
    respx.post(f"{_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    seen: list[str] = []
    stop = asyncio.Event()

    async def handler(conversation_ref: dict, text: str) -> None:
        seen.append(text)
        stop.set()

    adapter = TelegramAdapter(
        credential_store=_FakeStore(token=None),
        http_client=httpx.AsyncClient(),
        backoff_base_s=0.0,
        token_resolver=resolver,
    )
    await asyncio.wait_for(
        adapter.run_polling(handler, stop_event=stop, poll_timeout=0), timeout=5
    )
    await adapter.aclose()

    # It idled while unconfigured (>=2 token-less resolves), then polled.
    assert resolve_calls["n"] >= 3
    assert seen == ["ping?"]


def test_format_delegates_to_message_formatter() -> None:
    """format() produces plain text plus a Sources footer within the limit."""
    adapter = TelegramAdapter(credential_store=_FakeStore())
    response = GeneratedResponse(
        text="The policy allows 20 days.",
        citations=["hr_handbook.pdf"],
        status="success",
    )
    message = adapter.format(response)
    assert "The policy allows 20 days." in message
    assert "Sources:" in message
    assert "hr_handbook.pdf" in message
    assert len(message) <= 4096


def test_format_strips_markdown_markers() -> None:
    """Markdown the model emits never reaches the plain-text message (Req 8.5)."""
    adapter = TelegramAdapter(credential_store=_FakeStore())
    response = GeneratedResponse(
        text="## 休假制度\n- **年假**: 工龄0-2年 10天\n- __病假__: 每年`10`天\n- 2*3=6 stays",
        citations=["hr_考勤与休假制度.md"],
        status="success",
    )
    message = adapter.format(response)
    assert "**" not in message and "__" not in message and "`" not in message
    assert "##" not in message
    assert "- 年假: 工龄0-2年 10天" in message
    assert "病假: 每年10天" in message
    # A lone asterisk is literal content and must survive untouched.
    assert "2*3=6 stays" in message
