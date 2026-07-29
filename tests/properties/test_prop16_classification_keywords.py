# Feature: intelliknow-kms, Property 16: Keywords appear in the classification context
"""Property 16: Keywords appear in the classification context.

For any set of Intent_Spaces with any defined keywords, the built
classification prompt contains every defined keyword. This exercises the
dynamic-injection design of the classification prompt: Admin-defined keywords
are always surfaced to the AI_Model as hints (Req 7.5).

Validates: Requirements 7.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ai.prompts import (
    IntentSpaceSpec,
    build_classification_messages,
    classification_prompt_text,
)

# Keywords are 1-50 chars (Req 6.5). Exclude characters that carry structural
# meaning in the prompt layout (newlines, commas, pipes) so the assertion tests
# keyword *presence* rather than the formatter's delimiter choices; the space
# name is likewise kept free of newlines. All remaining printable text — the
# realistic keyword input space — must appear verbatim in the prompt.
_keyword_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters="\n\r,|",
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip() == s and len(s) >= 1)

_name_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=50,
).filter(lambda s: "\n" not in s and "\r" not in s)

_description_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    max_size=200,
).filter(lambda s: "\n" not in s and "\r" not in s)


@st.composite
def _intent_space(draw) -> IntentSpaceSpec:
    """Generate a single Intent_Space with 0-50 keywords."""
    space_id = draw(st.integers(min_value=1, max_value=10_000))
    name = draw(_name_text)
    description = draw(_description_text)
    keywords = tuple(
        draw(st.lists(_keyword_text, min_size=0, max_size=50))
    )
    return IntentSpaceSpec(
        space_id=space_id,
        name=name,
        description=description,
        keywords=keywords,
    )


@settings(max_examples=200)
@given(
    spaces=st.lists(_intent_space(), min_size=1, max_size=8),
    query=st.text(min_size=1, max_size=200),
)
def test_every_defined_keyword_appears_in_classification_prompt(spaces, query):
    """Every keyword defined for any space appears in the built prompt."""
    prompt = classification_prompt_text(spaces, query)

    for space in spaces:
        for keyword in space.keywords:
            assert keyword in prompt, (
                f"keyword {keyword!r} of space {space.name!r} missing "
                "from classification prompt"
            )


@settings(max_examples=200)
@given(
    spaces=st.lists(_intent_space(), min_size=1, max_size=8),
    query=st.text(min_size=1, max_size=200),
)
def test_keywords_present_in_message_contents(spaces, query):
    """Keywords appear in the concatenated message contents the model sees."""
    messages = build_classification_messages(spaces, query)
    combined = "\n".join(m["content"] for m in messages)

    for space in spaces:
        for keyword in space.keywords:
            assert keyword in combined
