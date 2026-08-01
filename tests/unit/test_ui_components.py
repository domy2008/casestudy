"""Unit tests for the shared admin-UI pure helpers (task 18.1).

These exercise only the import-safe, server-free helpers in
:mod:`admin_ui.ui.components`:

* per-module accent-colour mapping (Req 9.4),
* the card HTML builder embedding the 12 px radius / 16 px padding (Req 9.3),
* API base-URL resolution and URL construction, and
* :class:`ApiClient` request/JSON handling plus error surfacing, driven by an
  ``httpx.MockTransport`` so no real backend server is required.
"""

from __future__ import annotations

import httpx
import pytest

from admin_ui.ui.components import (
    ACCENT_COLORS,
    DEFAULT_ACCENT,
    DEFAULT_API_BASE,
    ApiClient,
    ApiError,
    accent_color,
    api_base_url,
    build_url,
    card_html,
    handle_response,
    normalize_module,
)


# ---------------------------------------------------------------------------
# Accent-colour mapping (Req 9.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, expected",
    [
        ("Frontend Integration", ACCENT_COLORS["frontend_integration"]),
        ("frontend_integration", ACCENT_COLORS["frontend_integration"]),
        ("KB Management", ACCENT_COLORS["kb_management"]),
        ("kb-management", ACCENT_COLORS["kb_management"]),
        ("Intent Configuration", ACCENT_COLORS["intent_configuration"]),
    ],
)
def test_accent_color_maps_each_module(module: str, expected: str) -> None:
    """Each named module maps to its designated accent colour, casing-agnostic."""
    assert accent_color(module) == expected


def test_module_accents_are_the_designed_hues() -> None:
    """AIA demo skin: graded AIA-red accents per functional module."""
    assert accent_color("Frontend Integration") == "#D31145"  # AIA red
    assert accent_color("KB Management") == "#A6093D"  # dark red
    assert accent_color("Intent Configuration") == "#7A0930"  # burgundy


@pytest.mark.parametrize("module", ["Dashboard", "Analytics", "unknown", "", None])
def test_accent_color_falls_back_to_neutral(module: str | None) -> None:
    """Modules without a dedicated colour use the neutral default accent."""
    assert accent_color(module) == DEFAULT_ACCENT


def test_normalize_module_folds_separators_and_case() -> None:
    """Free-form labels collapse to the canonical snake_case identifier."""
    assert normalize_module("  Frontend  Integration ") == "frontend_integration"
    assert normalize_module("KB-Management") == "kb_management"
    assert normalize_module(None) == ""


# ---------------------------------------------------------------------------
# Card HTML builder (Req 9.3)
# ---------------------------------------------------------------------------


def test_card_html_embeds_radius_and_padding() -> None:
    """The card carries the 12 px corner radius and 16 px padding (Req 9.3)."""
    html = card_html("hello")
    assert "border-radius:12px" in html
    assert "padding:16px" in html
    assert "hello" in html
    assert html.strip().startswith("<div")


def test_card_html_applies_accent_edge() -> None:
    """An accent colour is rendered as a coloured left edge."""
    accent = ACCENT_COLORS["kb_management"]
    html = card_html("body", accent=accent)
    assert f"border-left:4px solid {accent}" in html


def test_card_html_without_accent_has_no_left_edge() -> None:
    """A plain neutral card omits the accent edge entirely."""
    html = card_html("body")
    assert "border-left" not in html


# ---------------------------------------------------------------------------
# Base URL resolution + URL construction
# ---------------------------------------------------------------------------


def test_api_base_url_defaults_when_unset() -> None:
    """With no KMS_API_BASE, the default localhost backend URL is used."""
    assert api_base_url(environ={}) == DEFAULT_API_BASE


def test_api_base_url_reads_env_and_strips_trailing_slash() -> None:
    """KMS_API_BASE overrides the default and any trailing slash is removed."""
    env = {"KMS_API_BASE": "https://api.example.com/"}
    assert api_base_url(environ=env) == "https://api.example.com"


@pytest.mark.parametrize(
    "base, path, expected",
    [
        ("http://localhost:8000", "/documents", "http://localhost:8000/documents"),
        ("http://localhost:8000/", "documents", "http://localhost:8000/documents"),
        ("http://localhost:8000", "documents", "http://localhost:8000/documents"),
        (
            "http://h/",
            "/integrations/telegram/credentials",
            "http://h/integrations/telegram/credentials",
        ),
    ],
)
def test_build_url_normalises_slashes(base: str, path: str, expected: str) -> None:
    """Exactly one slash joins base and path regardless of input slashes."""
    assert build_url(base, path) == expected


# ---------------------------------------------------------------------------
# handle_response: JSON handling + error surfacing
# ---------------------------------------------------------------------------


def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    """Build an httpx.Response with a JSON or text body for handler tests."""
    request = httpx.Request("GET", "http://localhost:8000/x")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=request)
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, request=request)


def test_handle_response_returns_json_on_success() -> None:
    """A 2xx JSON body is parsed and returned."""
    resp = _response(200, json_body={"ok": True, "n": 3})
    assert handle_response(resp) == {"ok": True, "n": 3}


def test_handle_response_returns_none_on_empty_body() -> None:
    """A 204 with no content yields None rather than raising."""
    assert handle_response(_response(204)) is None


def test_handle_response_raises_on_plain_detail() -> None:
    """A FastAPI string ``detail`` becomes the ApiError message."""
    resp = _response(404, json_body={"detail": "Unknown integration 'foo'."})
    with pytest.raises(ApiError) as excinfo:
        handle_response(resp)
    assert excinfo.value.status_code == 404
    assert "Unknown integration" in excinfo.value.message


def test_handle_response_surfaces_validation_fields() -> None:
    """A structured validation error lists offending fields in the message."""
    body = {
        "detail": {
            "tool": "telegram",
            "saved": False,
            "message": "Credential submission was rejected; no changes were saved.",
            "errors": [
                {"field": "bot_token", "message": "missing"},
            ],
        }
    }
    resp = _response(400, json_body=body)
    with pytest.raises(ApiError) as excinfo:
        handle_response(resp)
    err = excinfo.value
    assert err.status_code == 400
    assert "rejected" in err.message
    assert "bot_token" in err.message
    # payload preserves the full parsed error body for the screen to inspect.
    assert err.payload == body


def test_handle_response_handles_non_json_error_body() -> None:
    """A non-JSON error body still produces a sensible message."""
    resp = _response(500, text="internal boom")
    with pytest.raises(ApiError) as excinfo:
        handle_response(resp)
    assert excinfo.value.status_code == 500
    assert "internal boom" in excinfo.value.message


# ---------------------------------------------------------------------------
# ApiClient over a mock transport (no real server)
# ---------------------------------------------------------------------------


def _client_with_handler(handler) -> ApiClient:
    """Build an ApiClient backed by an httpx.MockTransport using ``handler``."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return ApiClient(base_url="http://backend:8000", client=http)


def test_apiclient_get_builds_url_and_returns_json() -> None:
    """GET hits the joined URL and returns the parsed JSON payload."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"documents": []})

    client = _client_with_handler(handler)
    result = client.get("/documents")
    assert result == {"documents": []}
    assert seen["url"] == "http://backend:8000/documents"
    assert seen["method"] == "GET"


def test_apiclient_put_sends_json_body() -> None:
    """PUT forwards the JSON body and returns the parsed response."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["content"] = request.content
        return httpx.Response(200, json={"saved": True})

    client = _client_with_handler(handler)
    result = client.put(
        "/integrations/telegram/credentials", json={"bot_token": "x"}
    )
    assert result == {"saved": True}
    assert captured["method"] == "PUT"
    assert b"bot_token" in captured["content"]


def test_apiclient_surfaces_http_error() -> None:
    """A non-2xx response is raised as an ApiError carrying the detail."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _client_with_handler(handler)
    with pytest.raises(ApiError) as excinfo:
        client.get("/documents/999")
    assert excinfo.value.status_code == 404
    assert "not found" in excinfo.value.message


def test_apiclient_surfaces_transport_failure() -> None:
    """A transport-level failure becomes an ApiError with no status code."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with_handler(handler)
    with pytest.raises(ApiError) as excinfo:
        client.get("/dashboard/summary")
    assert excinfo.value.status_code is None
    assert "Could not reach the backend" in excinfo.value.message
