"""Unit tests for the Analytics screen helpers (task 18.6).

These exercise the pure, import-safe helpers in ``admin_ui/pages/4_Analytics.py``
plus its headless import-safety. The page filename starts with a digit (it is a
Streamlit multipage entry), so it is loaded from its file path via importlib
rather than a normal ``import``.

Covered behavior:

* history param building and response normalization (Req 7.7, 10.4),
* usage-metric pair normalization (Req 10.2),
* accuracy normalization/formatting incl. N/A (Req 10.3),
* the no-matching-entries path (Req 10.7), and
* that importing the module never requires a Streamlit runtime.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ``4_Analytics.py`` is not a valid module name, so load it from its path.
_PAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "admin_ui"
    / "pages"
    / "4_Analytics.py"
)


def _load_page() -> ModuleType:
    """Import the Analytics page module from its file path (headless-safe)."""
    spec = importlib.util.spec_from_file_location("analytics_page", _PAGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def page() -> ModuleType:
    return _load_page()


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------


def test_module_imports_without_streamlit_runtime(page: ModuleType) -> None:
    """The page imports cleanly and defers all Streamlit use to _main.

    Successful ``exec_module`` (via the ``page`` fixture) already proves the
    module imports without a Streamlit runtime. We additionally assert that
    ``import streamlit`` appears only inside function bodies, never at module
    top level.
    """
    assert page.SCREEN_KEY == "analytics"
    assert callable(page._main)
    for line in _PAGE_PATH.read_text().splitlines():
        # Any streamlit import must be indented (i.e. inside a function).
        if "import streamlit" in line:
            assert line.startswith(" "), f"top-level streamlit import: {line!r}"


# ---------------------------------------------------------------------------
# Timestamp / param helpers
# ---------------------------------------------------------------------------


def test_iso_day_bounds(page: ModuleType) -> None:
    from datetime import date

    assert page.iso_start_of_day(date(2024, 5, 1)) == "2024-05-01 00:00:00"
    assert page.iso_end_of_day(date(2024, 5, 1)) == "2024-05-01 23:59:59"


def test_build_history_params_omits_unset_filters(page: ModuleType) -> None:
    params = page.build_history_params()
    assert params == {"limit": page.HISTORY_LIMIT}


def test_build_history_params_includes_set_filters(page: ModuleType) -> None:
    params = page.build_history_params(
        start="2024-05-01 00:00:00",
        end="2024-05-02 23:59:59",
        space_ids=[1, 3],
        tool="telegram",
        limit=50,
    )
    assert params == {
        "limit": 50,
        "start": "2024-05-01 00:00:00",
        "end": "2024-05-02 23:59:59",
        "space_ids": [1, 3],
        "tool": "telegram",
    }


def test_build_range_params(page: ModuleType) -> None:
    assert page.build_range_params() == {"n": page.TOP_N}
    assert page.build_range_params(start="a", end="b", n=5) == {
        "n": 5,
        "start": "a",
        "end": "b",
    }


def test_verify_path(page: ModuleType) -> None:
    assert page.verify_path(42) == "/queries/42/verify"


# ---------------------------------------------------------------------------
# Confidence / accuracy formatting
# ---------------------------------------------------------------------------


def test_format_confidence(page: ModuleType) -> None:
    assert page.format_confidence(83.6) == "84%"
    assert page.format_confidence(0) == "0%"
    assert page.format_confidence(None) == "—"
    assert page.format_confidence("nope") == "—"


def test_format_accuracy_na_and_percent(page: ModuleType) -> None:
    assert page.format_accuracy(None) == "N/A"
    assert page.format_accuracy(100.0) == "100%"
    assert page.format_accuracy(66.4) == "66%"


# ---------------------------------------------------------------------------
# History normalization + display mapping (Req 7.7, 10.4)
# ---------------------------------------------------------------------------


def test_extract_history_entries_accepts_list_and_wrappers(page: ModuleType) -> None:
    rows = [{"id": 1}, {"id": 2}]
    assert page.extract_history_entries(rows) == rows
    assert page.extract_history_entries({"queries": rows}) == rows
    assert page.extract_history_entries({"items": rows}) == rows
    assert page.extract_history_entries(None) == []
    assert page.extract_history_entries({"unrelated": 1}) == []


def test_history_row_to_display_maps_all_columns(page: ModuleType) -> None:
    space_names = {1: "HR", 2: "Finance"}
    entry = {
        "id": 7,
        "ts": "2024-05-01 09:00:00",
        "query_text": "How many vacation days?",
        "detected_space_id": 1,
        "confidence": 91.2,
        "response_status": "Success",
    }
    row = page.history_row_to_display(entry, space_names)
    assert row == {
        "Time": "2024-05-01 09:00:00",
        "Query": "How many vacation days?",
        "Detected Intent": "HR",
        "Confidence": "91%",
        "Status": "Success",
        "Verified": None,
    }
    # A verified entry resolves its verified space name.
    entry["verified_space_id"] = 2
    assert page.history_row_to_display(entry, space_names)["Verified"] == "Finance"


def test_pending_verifications_detects_new_and_changed_verdicts(page: ModuleType) -> None:
    name_to_id = {"HR": 1, "Finance": 2}
    snapshot = [
        {"id": 10, "verified_space_id": None},  # newly verified -> submit
        {"id": 11, "verified_space_id": 1},  # unchanged -> skip
        {"id": 12, "verified_space_id": 1},  # corrected -> submit
        {"id": 13, "verified_space_id": None},  # cleared cell -> skip
        {"verified_space_id": None},  # no id -> skip
    ]
    state = {
        "edited_rows": {
            0: {"Verified": "HR"},
            1: {"Verified": "HR"},
            2: {"Verified": "Finance"},
            3: {"Verified": None},
            4: {"Verified": "HR"},
        }
    }
    assert page.pending_verifications(state, snapshot, name_to_id) == [(10, 1), (12, 2)]


def test_pending_verifications_ignores_malformed_state(page: ModuleType) -> None:
    name_to_id = {"HR": 1}
    snapshot = [{"id": 1, "verified_space_id": None}]
    # No state / no edits / malformed shapes -> nothing to submit.
    assert page.pending_verifications(None, snapshot, name_to_id) == []
    assert page.pending_verifications({}, snapshot, name_to_id) == []
    assert page.pending_verifications({"edited_rows": "junk"}, snapshot, name_to_id) == []
    # Unknown space name, out-of-range index, non-dict cells -> skipped.
    state = {"edited_rows": {0: {"Verified": "Ghost"}, 9: {"Verified": "HR"}, 1: "junk"}}
    assert page.pending_verifications(state, snapshot, name_to_id) == []
    # String row indices (Streamlit serializes them as str) still resolve.
    state = {"edited_rows": {"0": {"Verified": "HR"}}}
    assert page.pending_verifications(state, snapshot, name_to_id) == [(1, 1)]


def test_resolve_space_label_unknown_and_missing(page: ModuleType) -> None:
    assert page.resolve_space_label(9, {1: "HR"}) == "#9"
    assert page.resolve_space_label(None, {1: "HR"}) == "—"
    assert page.resolve_space_label(1, {1: "HR"}) == "HR"


# ---------------------------------------------------------------------------
# Spaces / pairs / accuracy normalization
# ---------------------------------------------------------------------------


def test_normalize_spaces_from_list_and_wrapper(page: ModuleType) -> None:
    data = [{"id": 1, "name": "HR"}, {"id": 2, "name": "Finance"}]
    assert page.normalize_spaces(data) == {1: "HR", 2: "Finance"}
    assert page.normalize_spaces({"spaces": data}) == {1: "HR", 2: "Finance"}
    assert page.normalize_spaces(None) == {}


def test_normalize_pairs_from_tuples_and_dicts(page: ModuleType) -> None:
    assert page.normalize_pairs([["policy.pdf", 5], ["faq.md", 2]]) == [
        ("policy.pdf", 5),
        ("faq.md", 2),
    ]
    dicts = [
        {"document_name": "policy.pdf", "access_count": 5},
        {"space_name": "HR", "query_count": 9},
    ]
    assert page.normalize_pairs(dicts) == [("policy.pdf", 5), ("HR", 9)]
    assert page.normalize_pairs({"documents": [["a", 1]]}) == [("a", 1)]
    assert page.normalize_pairs(None) == []


def test_normalize_accuracy_mapping_and_list(page: ModuleType) -> None:
    assert page.normalize_accuracy({"1": 100, "2": "N/A"}) == {1: 100.0, 2: None}
    listed = [
        {"space_id": 1, "accuracy_percent": 80},
        {"space_id": 2, "accuracy_percent": None},
    ]
    assert page.normalize_accuracy(listed) == {1: 80.0, 2: None}
    assert page.normalize_accuracy(None) == {}


def test_coerce_csv_bytes(page: ModuleType) -> None:
    assert page._coerce_csv_bytes("a,b\n1,2") == b"a,b\n1,2"
    assert page._coerce_csv_bytes(b"x") == b"x"
