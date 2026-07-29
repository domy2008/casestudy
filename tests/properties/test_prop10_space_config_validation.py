# Feature: intelliknow-kms, Property 10: Intent_Space configuration validation
"""Property-based test for Intent_Space configuration validation.

**Property 10: Intent_Space configuration validation**

For any submitted Intent_Space configuration, :func:`validate_space_config`
returns no errors if and only if the name is 1–50 characters (after trimming),
the description is at most 500 characters, and there are at most 50 keywords
each 1–50 characters long. When any constraint is violated at least one error
is returned, and only violated constraints produce errors.

**Validates: Requirements 6.2, 6.5**

The validator is a pure function, so the test compares its verdict against an
independent Python reference computation over a wide range of generated inputs.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.api.admin import (
    SPACE_DESCRIPTION_MAX,
    SPACE_KEYWORD_MAX,
    SPACE_KEYWORD_MIN,
    SPACE_KEYWORDS_MAX,
    SPACE_NAME_MAX,
    SPACE_NAME_MIN,
    validate_space_config,
)

# Names span empty → over-limit, including surrounding whitespace so trimming
# is exercised. Keep the alphabet small but include spaces.
_names = st.text(alphabet="ab cd", min_size=0, max_size=60)
_descriptions = st.text(alphabet="xy ", min_size=0, max_size=520)
_keyword = st.text(alphabet="kw", min_size=0, max_size=55)
_keywords = st.lists(_keyword, min_size=0, max_size=55)


def _reference_valid(name: str, description: str, keywords: list[str]) -> bool:
    """Independently decide whether a configuration is valid (Req 6.2, 6.5)."""
    trimmed = name.strip()
    if not (SPACE_NAME_MIN <= len(trimmed) <= SPACE_NAME_MAX):
        return False
    if len(description) > SPACE_DESCRIPTION_MAX:
        return False
    if len(keywords) > SPACE_KEYWORDS_MAX:
        return False
    for kw in keywords:
        if not (SPACE_KEYWORD_MIN <= len(kw) <= SPACE_KEYWORD_MAX):
            return False
    return True


@settings(max_examples=100, deadline=None)
@given(name=_names, description=_descriptions, keywords=_keywords)
def test_space_config_valid_iff_within_bounds(
    name: str, description: str, keywords: list[str]
) -> None:
    """No errors iff every field is within its documented bounds."""
    errors = validate_space_config(name, description, keywords)
    expected_valid = _reference_valid(name, description, keywords)
    assert (errors == []) is expected_valid
    # When invalid, at least one field error is reported.
    if not expected_valid:
        assert len(errors) >= 1
        assert all(e.field in {"name", "description", "keywords"} for e in errors)
