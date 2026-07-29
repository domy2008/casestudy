"""Unit tests for the Intent Configuration screen's pure helpers (task 18.5).

These exercise the server-free decision logic of
``admin_ui/pages/3_Intent_Configuration.py``:

* keyword parsing/normalization (Req 6.5),
* Intent_Space form validation — name 1–50 chars, description ≤500 chars,
  ≤50 keywords of 1–50 chars each (Req 6.2, 6.5),
* Confidence_Threshold range validation (Req 7.4, 7.9),
* accuracy / document-count formatting for the space cards (Req 6.4),
* resilient extraction of space fields and the current threshold, and
* General_Space detection for the deletion guard (Req 6.7).

The page module is imported by file path because its filename begins with a
digit (Streamlit's page-ordering convention), which is not a valid Python
identifier for a normal import. Importing it here also asserts the module is
**headless-safe** — it must import without a running Streamlit server.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Path to the page module (filename starts with a digit → import by path).
_PAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "admin_ui"
    / "pages"
    / "3_Intent_Configuration.py"
)


def _load_page_module() -> ModuleType:
    """Import the Intent Configuration page module by file path (headless)."""
    spec = importlib.util.spec_from_file_location(
        "intent_configuration_page", _PAGE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page = _load_page_module()


# ---------------------------------------------------------------------------
# normalize_keywords (Req 6.5)
# ---------------------------------------------------------------------------


def test_normalize_keywords_splits_newlines_and_commas() -> None:
    """Newlines and commas both separate keywords; whitespace is stripped."""
    assert page.normalize_keywords("hr, payroll\n benefits ") == [
        "hr",
        "payroll",
        "benefits",
    ]


def test_normalize_keywords_drops_empties_and_dupes() -> None:
    """Empty tokens are dropped and case-insensitive duplicates removed."""
    assert page.normalize_keywords("HR\n\nhr,,Payroll") == ["HR", "Payroll"]


def test_normalize_keywords_accepts_iterable_and_none() -> None:
    """An iterable input is normalized; ``None`` yields an empty list."""
    assert page.normalize_keywords(["a", " b ", "a"]) == ["a", "b"]
    assert page.normalize_keywords(None) == []


# ---------------------------------------------------------------------------
# validate_space_form (Req 6.2, 6.5)
# ---------------------------------------------------------------------------


def test_valid_form_has_no_errors() -> None:
    """A well-formed submission validates cleanly."""
    assert page.validate_space_form("HR", "People ops", ["hr", "payroll"]) == []


def test_empty_name_is_rejected() -> None:
    """A blank name is required (Req 6.2)."""
    errors = page.validate_space_form("   ", "", [])
    assert any("required" in e.lower() for e in errors)


def test_name_over_50_chars_is_rejected() -> None:
    """A name longer than 50 characters is rejected (Req 6.2)."""
    errors = page.validate_space_form("x" * 51, "", [])
    assert any("50" in e for e in errors)


def test_description_over_500_chars_is_rejected() -> None:
    """A description longer than 500 characters is rejected (Req 6.2)."""
    errors = page.validate_space_form("HR", "d" * 501, [])
    assert any("500" in e for e in errors)


def test_more_than_50_keywords_is_rejected() -> None:
    """At most 50 keywords are allowed (Req 6.5)."""
    errors = page.validate_space_form("HR", "", [f"k{i}" for i in range(51)])
    assert any("50 keyword" in e for e in errors)


def test_keyword_length_bounds_are_enforced() -> None:
    """Each keyword must be 1–50 characters (Req 6.5)."""
    errors = page.validate_space_form("HR", "", ["x" * 51])
    assert any("between 1 and 50" in e for e in errors)


def test_exactly_50_keywords_of_max_length_is_valid() -> None:
    """The inclusive upper bounds (50 keywords, 50 chars) are accepted."""
    keywords = ["x" * 50 for _ in range(50)]
    assert page.validate_space_form("HR", "d" * 500, keywords) == []


# ---------------------------------------------------------------------------
# validate_confidence_threshold (Req 7.4, 7.9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [0, 70, 100, "0", "100", 55.0, " 42 "])
def test_threshold_accepts_valid_values(raw) -> None:
    """Whole numbers 0–100 (int, numeric string, integral float) are accepted."""
    value, error = page.validate_confidence_threshold(raw)
    assert error is None
    assert isinstance(value, int)
    assert page.THRESHOLD_MIN <= value <= page.THRESHOLD_MAX


@pytest.mark.parametrize("raw", [-1, 101, 55.5, "abc", None, True, [70]])
def test_threshold_rejects_invalid_values(raw) -> None:
    """Out-of-range, non-integer, or non-numeric input is rejected (Req 7.9)."""
    value, error = page.validate_confidence_threshold(raw)
    assert value is None
    assert error and str(page.THRESHOLD_MAX) in error


def test_threshold_default_is_seventy() -> None:
    """The documented default Confidence_Threshold is 70 (Req 7.4)."""
    assert page.THRESHOLD_DEFAULT == 70


# ---------------------------------------------------------------------------
# format_accuracy / format_document_count (Req 6.4)
# ---------------------------------------------------------------------------


def test_accuracy_none_renders_na() -> None:
    """No classified queries → 'N/A' (Req 6.4)."""
    assert page.format_accuracy(None) == "N/A"


@pytest.mark.parametrize("value, expected", [(72, "72%"), (72.4, "72%"), (72.6, "73%")])
def test_accuracy_rounds_to_whole_percent(value, expected) -> None:
    """A numeric accuracy is rendered as a rounded whole percent (Req 6.4)."""
    assert page.format_accuracy(value) == expected


def test_document_count_coerces_and_floors_at_zero() -> None:
    """Missing/negative counts fall back to zero; valid counts pass through."""
    assert page.format_document_count(None) == 0
    assert page.format_document_count(-5) == 0
    assert page.format_document_count("7") == 7


# ---------------------------------------------------------------------------
# space field extraction + threshold extraction (resilience)
# ---------------------------------------------------------------------------


def test_space_payload_trims_name_and_includes_keywords() -> None:
    """The request body trims the name and carries the keyword list (Req 6.5)."""
    body = page.space_payload("  HR  ", "desc", ["a", "b"])
    assert body == {"name": "HR", "description": "desc", "keywords": ["a", "b"]}


def test_space_extractors_tolerate_field_naming() -> None:
    """Document count / accuracy / keywords are read across field-name variants."""
    space = {
        "id": 3,
        "name": "Finance",
        "documents": 4,
        "accuracy_rate": 88,
        "keywords": ["invoice", "budget"],
    }
    assert page.space_document_count(space) == 4
    assert page.space_accuracy(space) == 88
    assert page.space_keywords(space) == ["invoice", "budget"]


@pytest.mark.parametrize(
    "space, expected",
    [
        ({"is_general": True}, True),
        ({"is_general": 1}, True),
        ({"is_general": 0}, False),
        ({"name": "General"}, True),
        ({"name": "HR"}, False),
    ],
)
def test_is_general_space_detection(space, expected) -> None:
    """The General_Space is detected via flag or name for the delete guard."""
    assert page.is_general_space(space) is expected


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"value": 65}, 65),
        ({"confidence_threshold": 80}, 80),
        (55, 55),
        ({}, 70),
        ({"value": 999}, 70),
        (None, 70),
    ],
)
def test_extract_threshold_reads_shapes_and_defaults(payload, expected) -> None:
    """The threshold is read from several shapes, defaulting to 70 (Req 7.4)."""
    assert page.extract_threshold(payload) == expected


# ---------------------------------------------------------------------------
# fetch_spaces / fetch_threshold over a mock ApiClient
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal stand-in returning canned bodies for the two GET endpoints."""

    def __init__(self, spaces_body, threshold_body):
        self._spaces = spaces_body
        self._threshold = threshold_body

    def get(self, path, *, params=None):
        if path == "/spaces":
            return self._spaces
        if path == "/settings/confidence-threshold":
            return self._threshold
        raise AssertionError(f"unexpected GET {path}")


def test_fetch_spaces_handles_list_and_wrapped_shapes() -> None:
    """A bare list and a ``{"spaces": [...]}`` wrapper both yield the list."""
    listed = page.fetch_spaces(_FakeClient([{"id": 1}], None))
    assert listed == [{"id": 1}]
    wrapped = page.fetch_spaces(_FakeClient({"spaces": [{"id": 2}]}, None))
    assert wrapped == [{"id": 2}]
    empty = page.fetch_spaces(_FakeClient({"unexpected": True}, None))
    assert empty == []


def test_fetch_threshold_extracts_value() -> None:
    """The current threshold is read from the settings response."""
    assert page.fetch_threshold(_FakeClient(None, {"value": 42})) == 42


# ---------------------------------------------------------------------------
# Screen wiring sanity
# ---------------------------------------------------------------------------


def test_screen_key_and_bounds() -> None:
    """The screen uses the intent-configuration key and the designed bounds."""
    assert page.SCREEN_KEY == "intent_configuration"
    assert page.MODULE == "intent_configuration"
    assert (page.SPACE_NAME_MIN, page.SPACE_NAME_MAX) == (1, 50)
    assert page.DESCRIPTION_MAX == 500
    assert page.KEYWORDS_MAX == 50
    assert (page.KEYWORD_MIN, page.KEYWORD_MAX) == (1, 50)
    assert (page.THRESHOLD_MIN, page.THRESHOLD_MAX) == (0, 100)
