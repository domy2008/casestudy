"""Unit tests for the Dashboard (Home) pure parsing helpers (task 18.2).

These exercise only the import-safe, server-free helpers in
:mod:`admin_ui.Home`: the parsers that turn a decoded ``/dashboard/summary``
payload into per-card display rows, plus their error handling for missing or
malformed sections (Req 9.5, 9.7). No Streamlit runtime is required — importing
the module and calling the parsers is enough.
"""

from __future__ import annotations

import importlib

import pytest

from admin_ui.Home import (
    DOCUMENT_STATUSES,
    DashboardDataError,
    parse_document_counts,
    parse_integration_status,
    parse_query_activity,
)


# ---------------------------------------------------------------------------
# Import-safety: the module imports without a Streamlit runtime
# ---------------------------------------------------------------------------


def test_module_imports_headless() -> None:
    """Importing Home never pulls in / requires a running Streamlit server."""
    module = importlib.import_module("admin_ui.Home")
    assert module.SCREEN_KEY == "dashboard"
    assert module.DASHBOARD_SUMMARY_PATH == "/dashboard/summary"


# ---------------------------------------------------------------------------
# Integration status parsing (Req 9.5)
# ---------------------------------------------------------------------------


def test_parse_integration_status_from_mapping() -> None:
    """A ``{tool: status}`` mapping yields one row per tool."""
    summary = {"integrations": {"telegram": "Connected", "teams": "Disconnected"}}
    assert parse_integration_status(summary) == [
        ("telegram", "Connected"),
        ("teams", "Disconnected"),
    ]


def test_parse_integration_status_from_list_of_objects() -> None:
    """A list of ``{"tool", "status"}`` objects is supported too."""
    summary = {
        "integrations": [
            {"tool": "telegram", "status": "Connected"},
            {"tool": "teams", "status": "Error"},
        ]
    }
    assert parse_integration_status(summary) == [
        ("telegram", "Connected"),
        ("teams", "Error"),
    ]


def test_parse_integration_status_empty_is_allowed() -> None:
    """No configured integrations is a valid (empty) result, not an error."""
    assert parse_integration_status({"integrations": []}) == []


def test_parse_integration_status_missing_section_raises() -> None:
    """A wholly absent integration section surfaces as a data error (Req 9.7)."""
    with pytest.raises(DashboardDataError):
        parse_integration_status({})


def test_parse_integration_status_backend_error_marker_raises() -> None:
    """A backend per-section error marker becomes an inline card error."""
    summary = {"integrations": {"error": "status service unavailable"}}
    with pytest.raises(DashboardDataError) as excinfo:
        parse_integration_status(summary)
    assert "status service unavailable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Document counts parsing (Req 9.5)
# ---------------------------------------------------------------------------


def test_parse_document_counts_covers_all_statuses() -> None:
    """Every status is reported, defaulting omitted ones to zero, in order."""
    summary = {"documents": {"Processed": 5, "Pending": 2}}
    result = parse_document_counts(summary)
    assert [status for status, _ in result] == list(DOCUMENT_STATUSES)
    assert dict(result) == {"Pending": 2, "Processed": 5, "Error": 0}


def test_parse_document_counts_is_case_insensitive() -> None:
    """Backend key casing does not matter for matching document statuses."""
    summary = {"documents": {"pending": 1, "processed": 3, "error": 4}}
    assert dict(parse_document_counts(summary)) == {
        "Pending": 1,
        "Processed": 3,
        "Error": 4,
    }


def test_parse_document_counts_alias_key() -> None:
    """The ``documents_by_status`` alias is accepted."""
    summary = {"documents_by_status": {"Error": 7}}
    assert dict(parse_document_counts(summary)) == {
        "Pending": 0,
        "Processed": 0,
        "Error": 7,
    }


def test_parse_document_counts_missing_section_raises() -> None:
    """An absent document section surfaces as a data error (Req 9.7)."""
    with pytest.raises(DashboardDataError):
        parse_document_counts({"integrations": {}})


def test_parse_document_counts_bad_shape_raises() -> None:
    """A non-mapping document section is rejected."""
    with pytest.raises(DashboardDataError):
        parse_document_counts({"documents": [1, 2, 3]})


# ---------------------------------------------------------------------------
# Query activity parsing (Req 9.5)
# ---------------------------------------------------------------------------


def test_parse_query_activity_full_breakdown() -> None:
    """Total / successful / failed are all surfaced when present."""
    summary = {"queries_24h": {"total": 12, "success": 10, "failed": 2}}
    assert parse_query_activity(summary) == [
        ("Total (24h)", 12),
        ("Successful", 10),
        ("Failed", 2),
    ]


def test_parse_query_activity_bare_integer_is_total() -> None:
    """A bare integer is interpreted as the 24h total."""
    assert parse_query_activity({"queries_24h": 9}) == [("Total (24h)", 9)]


def test_parse_query_activity_alias_fields() -> None:
    """The ``successful`` / ``failure`` field aliases are accepted."""
    summary = {"query_activity": {"total": 4, "successful": 3, "failure": 1}}
    assert parse_query_activity(summary) == [
        ("Total (24h)", 4),
        ("Successful", 3),
        ("Failed", 1),
    ]


def test_parse_query_activity_missing_section_raises() -> None:
    """An absent query-activity section surfaces as a data error (Req 9.7)."""
    with pytest.raises(DashboardDataError):
        parse_query_activity({})


def test_parse_query_activity_unrecognized_fields_raise() -> None:
    """A mapping with no recognizable fields is rejected."""
    with pytest.raises(DashboardDataError):
        parse_query_activity({"queries_24h": {"unrelated": 1}})
