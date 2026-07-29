"""Unit tests for the KB Management screen helpers (task 18.4).

The screen file lives at ``admin_ui/pages/2_KB_Management.py``; its name starts
with a digit so it is loaded via :mod:`importlib` rather than a plain import.
These tests exercise only the **import-safe** pieces — the pure helpers and the
thin API wrappers driven by an ``httpx.MockTransport``-backed :class:`ApiClient`
— so no Streamlit runtime is required (verifying the module imports headless).

Coverage maps to the acceptance criteria:

* :func:`validate_upload` / :func:`detect_format` — format & size gating
  (Req 4.1/4.2/4.3) that fronts the drag-and-drop upload (Req 4.4).
* :func:`document_row` / :func:`human_size` / :func:`status_display` — the
  Name/Upload Date/Format/Size/Status columns and the Error indication
  (Req 4.5, 4.10).
* :func:`build_list_params` — name/format/date/Intent_Space filters (Req 4.6).
* :func:`upload_document` / :func:`delete_document` / :func:`update_document` /
  :func:`assign_space` — the backend endpoints for upload, delete, update
  (Req 4.8/4.9) and Intent_Space assignment (Req 5.6).
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from admin_ui.ui.components import ApiClient, ApiError

_PAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "admin_ui"
    / "pages"
    / "2_KB_Management.py"
)


def _load_page() -> ModuleType:
    """Import the digit-prefixed page module headless (no Streamlit runtime)."""
    spec = importlib.util.spec_from_file_location("kb_management_page", _PAGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


page = _load_page()


def _client_with_handler(handler) -> ApiClient:
    """Build an ApiClient backed by an httpx.MockTransport using ``handler``."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return ApiClient(base_url="http://backend:8000", client=http)


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------


def test_module_imports_headless_and_exposes_screen_key() -> None:
    """The page imports without a Streamlit runtime and declares its nav key."""
    assert page.SCREEN_KEY == "kb_management"
    assert page.MAX_UPLOAD_BYTES == 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Upload validation (Req 4.1/4.2/4.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("policy.pdf", "pdf"),
        ("report.DOCX", "docx"),
        ("sheet.xlsx", "xlsx"),
        ("notes.txt", "txt"),
        ("readme.md", "md"),
        ("readme.markdown", "md"),
        ("archive.zip", None),
        ("noext", None),
        (None, None),
    ],
)
def test_detect_format(filename, expected) -> None:
    """Supported extensions map to canonical formats; others yield None."""
    assert page.detect_format(filename) == expected


def test_validate_upload_accepts_supported_within_limit() -> None:
    """A supported format under 50 MB passes validation."""
    assert page.validate_upload("policy.pdf", 1024) is None


def test_validate_upload_rejects_unsupported_format_first() -> None:
    """An unsupported format is rejected with the supported-format list (Req 4.2)."""
    msg = page.validate_upload("virus.exe", page.MAX_UPLOAD_BYTES + 1)
    assert msg is not None
    assert "Unsupported format" in msg
    assert "PDF" in msg


def test_validate_upload_rejects_oversize(monkeypatch) -> None:
    """A supported file above 50 MB is rejected citing the size limit (Req 4.3)."""
    msg = page.validate_upload("big.pdf", page.MAX_UPLOAD_BYTES + 1)
    assert msg is not None
    assert "50 MB" in msg


# ---------------------------------------------------------------------------
# Table projection (Req 4.5, 4.10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1024 * 1024, "1.0 MB"),
        (-1, "—"),
        ("bad", "—"),
    ],
)
def test_human_size(size, expected) -> None:
    """Byte counts render as compact human-readable sizes."""
    assert page.human_size(size) == expected


def test_status_display_marks_errors_with_detail() -> None:
    """Error status carries a visible marker and the failure detail (Req 4.10)."""
    out = page.status_display("Error", "parse timeout")
    assert out.startswith("🔴 Error")
    assert "parse timeout" in out


def test_status_display_plain_statuses() -> None:
    """Processed/Pending render with their own markers."""
    assert "Processed" in page.status_display("Processed")
    assert "Pending" in page.status_display("Pending")


def test_document_row_has_required_columns() -> None:
    """A document projects to the exact table columns, including the space name."""
    doc = {
        "id": 7,
        "name": "handbook.pdf",
        "uploaded_at": "2024-05-01T10:00:00",
        "format": "pdf",
        "size_bytes": 2048,
        "status": "Processed",
        "space_id": 3,
    }
    row = page.document_row(doc, {3: "HR"})
    assert row["Name"] == "handbook.pdf"
    assert row["Upload Date"] == "2024-05-01T10:00:00"
    assert row["Format"] == "PDF"
    assert row["Size"] == "2.0 KB"
    assert row["Intent Space"] == "HR"
    assert "Processed" in row["Status"]


# ---------------------------------------------------------------------------
# Filters (Req 4.6)
# ---------------------------------------------------------------------------


def test_build_list_params_omits_empty_and_all() -> None:
    """Blank name and the 'All' format sentinel produce no filter params."""
    assert page.build_list_params(name="  ", fmt="All", space_id=None) == {}


def test_build_list_params_includes_set_filters() -> None:
    """Every explicitly set filter is forwarded to the backend query."""
    params = page.build_list_params(
        name="pay",
        fmt="pdf",
        space_id=2,
        date_from="2024-01-01",
        date_to="2024-02-01",
    )
    assert params == {
        "name": "pay",
        "format": "pdf",
        "space_id": 2,
        "date_from": "2024-01-01",
        "date_to": "2024-02-01",
    }


def test_extract_items_handles_list_and_wrapped() -> None:
    """Both a bare array and a wrapped array normalise to a list of dicts."""
    assert page.extract_items([{"id": 1}], "documents") == [{"id": 1}]
    assert page.extract_items({"documents": [{"id": 2}]}, "documents") == [{"id": 2}]
    assert page.extract_items({"other": 1}, "documents") == []


def test_space_label_map() -> None:
    """Spaces map id -> name, skipping records without an id."""
    spaces = [{"id": 1, "name": "General"}, {"name": "orphan"}]
    assert page.space_label_map(spaces) == {1: "General"}


# ---------------------------------------------------------------------------
# API wrappers over a mock transport (no real server)
# ---------------------------------------------------------------------------


def test_upload_document_posts_base64_json() -> None:
    """Upload sends a base64 JSON body to POST /documents (Req 4.1/4.4)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 5, "status": "Pending"})

    client = _client_with_handler(handler)
    result = page.upload_document(
        client,
        name="policy.pdf",
        fmt="pdf",
        size_bytes=3,
        content=b"abc",
        space_id=2,
    )
    assert result == {"id": 5, "status": "Pending"}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://backend:8000/documents"
    body = captured["body"]
    assert body["name"] == "policy.pdf"
    assert body["format"] == "pdf"
    assert body["space_id"] == 2
    assert base64.b64decode(body["content_b64"]) == b"abc"


def test_delete_document_calls_delete_endpoint() -> None:
    """Delete issues DELETE /documents/{id} (Req 4.8)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(204)

    client = _client_with_handler(handler)
    assert page.delete_document(client, 9) is None
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/documents/9")


def test_update_document_calls_update_endpoint() -> None:
    """Update issues POST /documents/{id}/update (Req 4.9)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": "Pending"})

    client = _client_with_handler(handler)
    page.update_document(client, 4)
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/documents/4/update")


def test_assign_space_puts_space_body() -> None:
    """Assignment issues PUT /documents/{id}/space with the space id (Req 5.6)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"space_id": 6})

    client = _client_with_handler(handler)
    page.assign_space(client, 3, 6)
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/documents/3/space")
    assert seen["body"] == {"space_id": 6}


def test_fetch_documents_forwards_filters_and_normalises() -> None:
    """fetch_documents forwards filter params and returns a list of dicts."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"documents": [{"id": 1, "name": "a.pdf"}]})

    client = _client_with_handler(handler)
    docs = page.fetch_documents(client, {"format": "pdf"})
    assert docs == [{"id": 1, "name": "a.pdf"}]
    assert "format=pdf" in seen["url"]


def test_fetch_documents_surfaces_api_error() -> None:
    """A backend failure surfaces as ApiError for the screen to display."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = _client_with_handler(handler)
    with pytest.raises(ApiError):
        page.fetch_documents(client)
