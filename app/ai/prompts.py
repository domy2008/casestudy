"""Prompt templates for every DashScope (Qwen-Max) AI task.

This module is the single place where the raw prompt text for IntelliKnow KMS'
three AI tasks lives, keeping wording, guardrails, and formatting rules out of
the orchestration/generation code and easy to review and test:

* **Intent classification** (:func:`build_classification_messages`) — a
  JSON-mode prompt that lists *every* Intent_Space with its description and
  Admin-defined keywords and asks Qwen-Max to pick the single best space and
  return a strict JSON object with the chosen space and a calibrated
  confidence score 0-100 (Req 7.1, 7.5).
* **Document structuring** (:func:`build_structuring_messages`) — instructs
  Qwen-Max to clean and normalize extracted text and render every extracted
  table as a GitHub-flavored markdown table so row/column structure (e.g.
  salary grids) survives chunking and retrieval (Req 5.2).
* **RAG answer generation** (:func:`build_rag_messages`) — grounds the answer
  strictly in the supplied passages ("answer ONLY from the passages below; if
  they don't contain the answer, say so") and asks for a concise, cited answer
  (Req 8.1).

Every builder returns an OpenAI-style message list — ``list[dict]`` with
``role``/``content`` keys — which is exactly what
:meth:`app.ai.dashscope_client.DashScopeClient.chat_completion` (and its
``classify``/``generate`` wrappers) consume. The classifier prompt is designed
so the set of Intent_Spaces and each space's keywords are injected dynamically
(needed for Property 16): spaces are supplied as
:class:`IntentSpaceSpec` values (or ``(name, description, keywords)`` tuples).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.core.models import ExtractedContent, Passage

__all__ = [
    "IntentSpaceSpec",
    "Message",
    "build_classification_messages",
    "build_structuring_messages",
    "build_rag_messages",
    "classification_prompt_text",
    "rewrite_passage_references",
    "NO_MATCH_MESSAGE",
]

#: An OpenAI-style chat message: ``{"role": ..., "content": ...}``.
Message = dict[str, str]

#: The user-facing message returned when retrieval yields nothing usable. The
#: generator uses this on the no-match path; it is defined here so the "if the
#: passages do not contain the answer, say so" wording has one home (Req 8.3).
NO_MATCH_MESSAGE = (
    "I couldn't find any relevant information in the knowledge base to "
    "answer your question.\n"
    "知识库中没有找到能回答您问题的相关信息。"
)


@dataclass(frozen=True)
class IntentSpaceSpec:
    """A single Intent_Space as presented to the classification model.

    This is the dynamic unit injected into the classification prompt so that
    the full set of spaces and their Admin-defined keywords appear in the
    model's context (Req 7.5, Property 16).

    Attributes:
        space_id: The Intent_Space database id, echoed back by the model as its
            chosen ``space_id`` so routing needs no name lookup.
        name: The Intent_Space name (e.g. ``"HR"``).
        description: Admin-authored description of what belongs in this space.
            May be empty.
        keywords: Admin-defined hint keywords for this space. May be empty.
    """

    space_id: int
    name: str
    description: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)


def _coerce_space(space: IntentSpaceSpec | Sequence) -> IntentSpaceSpec:
    """Normalize a space input into an :class:`IntentSpaceSpec`.

    Accepts an :class:`IntentSpaceSpec` as-is, or a
    ``(space_id, name, description, keywords)`` /
    ``(name, description, keywords)`` sequence for convenience.

    Args:
        space: The Intent_Space specification or tuple to normalize.

    Returns:
        An :class:`IntentSpaceSpec` with keywords coerced to a tuple.

    Raises:
        TypeError: If ``space`` is neither a spec nor a supported sequence.
    """
    if isinstance(space, IntentSpaceSpec):
        return IntentSpaceSpec(
            space_id=space.space_id,
            name=space.name,
            description=space.description,
            keywords=tuple(space.keywords),
        )
    if isinstance(space, Sequence) and not isinstance(space, (str, bytes)):
        items = list(space)
        if len(items) == 4:
            space_id, name, description, keywords = items
        elif len(items) == 3:
            space_id = 0
            name, description, keywords = items
        else:
            raise TypeError(
                "Intent_Space tuple must be (space_id, name, description, "
                "keywords) or (name, description, keywords)"
            )
        return IntentSpaceSpec(
            space_id=int(space_id),
            name=str(name),
            description=str(description or ""),
            keywords=tuple(keywords or ()),
        )
    raise TypeError(f"Unsupported Intent_Space input: {type(space)!r}")


def _format_space_block(space: IntentSpaceSpec) -> str:
    """Render one Intent_Space as a text block for the classification prompt.

    Every Admin-defined keyword for the space is emitted verbatim so the
    classifier can use them as hints and so Property 16 (every defined keyword
    appears in the classification context) holds.

    Args:
        space: The normalized Intent_Space specification.

    Returns:
        A multi-line string describing the space, its description, and its
        keywords.
    """
    description = space.description.strip() or "(no description provided)"
    lines = [
        f"- space_id: {space.space_id}",
        f"  name: {space.name}",
        f"  description: {description}",
    ]
    if space.keywords:
        keyword_list = ", ".join(space.keywords)
        lines.append(f"  keywords (hints): {keyword_list}")
    else:
        lines.append("  keywords (hints): (none)")
    return "\n".join(lines)


_CLASSIFICATION_SYSTEM = (
    "You are an intent classifier for an enterprise knowledge management "
    "system. Your only job is to decide which knowledge domain "
    "(Intent_Space) a user's question most likely belongs to.\n\n"
    "Follow these rules:\n"
    "1. Choose exactly ONE Intent_Space from the provided list — the single "
    "best fit for the question.\n"
    "2. Base your decision on the meaning of the question and each space's "
    "description. Treat the listed keywords as helpful hints, not strict "
    "requirements.\n"
    "3. Keep the domains distinct: pick the space whose description and "
    "keywords most specifically match the question's topic.\n"
    "4. If the question does not clearly fit any space, or it is ambiguous, "
    "small talk, or spans multiple domains equally, choose the General space "
    "and assign a LOW confidence.\n"
    "5. Calibrate confidence honestly on a 0-100 scale: 90-100 only when the "
    "match is unmistakable, 70-89 for a clear match, 40-69 when plausible but "
    "uncertain, and below 40 when you are guessing or nothing fits well.\n"
    "6. Respond with ONLY a strict JSON object and nothing else, in exactly "
    'this shape: {"space_id": <integer>, "space_name": <string>, '
    '"confidence": <number 0-100>}. Do not add explanations or extra keys.'
)


def build_classification_messages(
    spaces: Iterable[IntentSpaceSpec | Sequence],
    query: str,
    *,
    general_space_name: str = "General",
) -> list[Message]:
    """Build the JSON-mode classification prompt messages.

    Lists every Intent_Space with its description and Admin-defined keywords
    and instructs Qwen-Max to pick the single best space and return a strict
    JSON object with the chosen space id/name and a calibrated confidence
    score 0-100 (Req 7.1, 7.5). The set of spaces and their keywords are
    injected dynamically, so any keyword defined for any space appears in the
    resulting prompt (Property 16).

    Args:
        spaces: The Intent_Spaces to choose among, as :class:`IntentSpaceSpec`
            values or ``(space_id, name, description, keywords)`` /
            ``(name, description, keywords)`` tuples. The General space should
            be included so the model can fall back to it.
        query: The End_User query text to classify.
        general_space_name: Name of the fallback General space, referenced in
            the guidance so the model knows where to route poor fits.

    Returns:
        An OpenAI-style message list suitable for
        :meth:`DashScopeClient.classify` with ``json_mode=True``.
    """
    normalized = [_coerce_space(space) for space in spaces]
    space_blocks = "\n".join(_format_space_block(s) for s in normalized)

    user_content = (
        "Available Intent_Spaces (choose exactly one):\n"
        f"{space_blocks}\n\n"
        f'If nothing fits well, choose the "{general_space_name}" space with a '
        "low confidence score.\n\n"
        "Classify the following user question:\n"
        f'"""\n{query}\n"""\n\n'
        'Return ONLY the JSON object: {"space_id": <integer>, '
        '"space_name": <string>, "confidence": <number 0-100>}.'
    )

    return [
        {"role": "system", "content": _CLASSIFICATION_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def classification_prompt_text(
    spaces: Iterable[IntentSpaceSpec | Sequence],
    query: str,
    *,
    general_space_name: str = "General",
) -> str:
    """Return the full classification prompt as a single string.

    Convenience for callers (and tests) that want the concatenated context the
    model actually sees rather than the structured message list. Concatenates
    the content of every message produced by
    :func:`build_classification_messages`.

    Args:
        spaces: The Intent_Spaces to choose among (see
            :func:`build_classification_messages`).
        query: The End_User query text to classify.
        general_space_name: Name of the fallback General space.

    Returns:
        The joined text of all classification prompt messages.
    """
    messages = build_classification_messages(
        spaces, query, general_space_name=general_space_name
    )
    return "\n".join(m["content"] for m in messages)


_STRUCTURING_SYSTEM = (
    "You normalize raw text extracted from enterprise documents into clean, "
    "well-structured Markdown for downstream search and retrieval.\n\n"
    "Follow these rules:\n"
    "1. Clean and normalize the text: fix broken line breaks and reading "
    "order, join hyphenated words split across lines, remove extraction "
    "artifacts, and preserve headings and logical section structure.\n"
    "2. Do NOT summarize, add, remove, or invent any facts. Preserve all "
    "original information exactly; only improve its structure and formatting.\n"
    "3. Render EVERY provided table as a GitHub-flavored Markdown table, "
    "preserving the exact row and column structure (for example, salary "
    "grids). Keep each table intact as a single block and place it where it "
    "belongs in the text flow.\n"
    "4. Output only the cleaned Markdown document — no commentary, no code "
    "fences around the whole document."
)


def _format_table_block(index: int, table: list[list[str]]) -> str:
    """Render one extracted table as a labeled pipe-delimited block.

    The raw cell grid is passed to the model (as pipe-delimited rows) with an
    instruction to emit a proper GitHub-flavored Markdown table, so the model
    both sees the structure and knows the target format.

    Args:
        index: 1-based table number, used only for labeling in the prompt.
        table: The table as a list of rows, each a list of cell strings.

    Returns:
        A labeled text block representing the table's rows.
    """
    if not table:
        return f"Table {index}: (empty)"
    rows = [" | ".join(str(cell) for cell in row) for row in table]
    return f"Table {index} ({len(table)} rows):\n" + "\n".join(rows)


def build_structuring_messages(content: ExtractedContent) -> list[Message]:
    """Build the document-structuring prompt messages.

    Instructs Qwen-Max to clean/normalize the extracted body text and render
    each extracted table as a GitHub-flavored Markdown table, preserving
    row/column structure (Req 5.2). The input is an :class:`ExtractedContent`
    (text plus tables) as produced by the format loaders.

    Args:
        content: The extracted document content (text + tables) to structure.

    Returns:
        An OpenAI-style message list suitable for
        :meth:`DashScopeClient.chat_completion`.
    """
    text = content.text.strip() or "(no body text extracted)"

    parts = ["Extracted document body text:", '"""', text, '"""']

    if content.tables:
        parts.append("")
        parts.append(
            f"Extracted tables ({len(content.tables)} total). Render each one "
            "as a GitHub-flavored Markdown table, preserving its rows and "
            "columns exactly:"
        )
        for i, table in enumerate(content.tables, start=1):
            parts.append("")
            parts.append(_format_table_block(i, table))
    else:
        parts.append("")
        parts.append("There are no extracted tables for this document.")

    parts.append("")
    parts.append(
        "Produce the cleaned, well-structured Markdown version of this "
        "document now."
    )

    return [
        {"role": "system", "content": _STRUCTURING_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


_RAG_SYSTEM = (
    "You are a knowledge assistant that answers questions using ONLY the "
    "reference passages provided by the user.\n\n"
    "Follow these rules strictly:\n"
    "1. Answer ONLY from the passages below. Do not use any outside knowledge "
    "or make assumptions beyond what the passages state.\n"
    "2. If the passages do not contain the answer, say so clearly and do not "
    "guess.\n"
    "3. Be concise and direct. Prefer a short, well-organized answer over a "
    "long one.\n"
    "4. ALWAYS reply in the same language as the user's question: answer in "
    "Chinese for a Chinese question, in English for an English question, "
    "even when the passages are written in a different language.\n"
    "5. Ground every claim in the passages. Do not fabricate facts, numbers, "
    "names, or citations.\n"
    "6. When citing sources, refer to them by their source document name "
    "(e.g. \u201caccording to hr_employee_handbook.docx\u201d). NEVER mention "
    "passage numbers such as \u201c[Passage 1]\u201d \u2014 the reader cannot "
    "see the numbered passages, only the final answer."
)


def _format_passage_block(index: int, passage: Passage) -> str:
    """Render one retrieved passage as a labeled block for the RAG prompt.

    Args:
        index: 1-based passage number for labeling.
        passage: The retrieved :class:`Passage` to render.

    Returns:
        A labeled text block including the source document name and text.
    """
    return (
        f"[Passage {index}] (source document: {passage.document_name})\n"
        f"{passage.text}"
    )


def build_rag_messages(query: str, passages: Sequence[Passage]) -> list[Message]:
    """Build the RAG answer-generation prompt messages.

    Grounds the answer exclusively in the supplied passages ("answer ONLY from
    the passages below; if they do not contain the answer, say so") and asks
    for a concise answer citing the source documents (Req 8.1).

    Args:
        query: The End_User query to answer.
        passages: The retrieved passages to ground the answer in. Each carries
            its source document name for citation.

    Returns:
        An OpenAI-style message list suitable for
        :meth:`DashScopeClient.generate`.
    """
    if passages:
        passage_blocks = "\n\n".join(
            _format_passage_block(i, p) for i, p in enumerate(passages, start=1)
        )
    else:
        passage_blocks = "(no passages provided)"

    user_content = (
        "Reference passages:\n"
        f"{passage_blocks}\n\n"
        "----\n"
        f"Question: {query}\n\n"
        "Answer the question using ONLY the passages above. If the passages "
        "do not contain the answer, say that you could not find the "
        "information in the knowledge base. Keep the answer concise and cite "
        "the source document name(s) you used. Never reference passages by "
        "number (e.g. \u201c[Passage 1]\u201d); the reader cannot see them."
    )

    return [
        {"role": "system", "content": _RAG_SYSTEM},
        {"role": "user", "content": user_content},
    ]


#: Matches passage-number references the model may echo from the prompt's
#: internal ``[Passage N]`` labels, e.g. ``[Passage 1]`` or ``[passage 12]``.
_PASSAGE_REF_PATTERN = re.compile(r"\[\s*Passage\s+(\d+)\s*\]", re.IGNORECASE)


def rewrite_passage_references(text: str, passages: Sequence[Passage]) -> str:
    """Replace ``[Passage N]`` references in ``text`` with document names.

    The RAG prompt labels each retrieved chunk as ``[Passage N]`` so the model
    can ground its answer, but End_Users never see those numbered blocks — a
    citation like "依据：[Passage 1]" is meaningless to them. The prompt asks
    the model to cite document names instead; this is the deterministic safety
    net for when it echoes the internal labels anyway.

    Args:
        text: The generated answer, possibly containing ``[Passage N]``.
        passages: The passages sent to the model, in prompt order (1-based).

    Returns:
        ``text`` with each resolvable reference replaced by the corresponding
        source document name (e.g. ``[hr_employee_handbook.docx]``).
        Out-of-range references are left unchanged.
    """

    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 1 <= index <= len(passages):
            return f"[{passages[index - 1].document_name}]"
        return match.group(0)

    return _PASSAGE_REF_PATTERN.sub(_replace, text)
