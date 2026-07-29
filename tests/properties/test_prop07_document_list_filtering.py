# Feature: intelliknow-kms, Property 7: Document list filtering returns exactly the matching documents
"""Property-based test for document list filtering.

**Property 7: Document list filtering returns exactly the matching documents**

For any set of documents and any combination of name / format / upload-date /
Intent_Space filters, :meth:`DocumentRepository.list` returns exactly the
documents that match all applied filters — no more, no fewer.

**Validates: Requirements 4.6**

The test builds a throwaway SQLite database via :func:`app.db.bootstrap` on a
per-example temporary directory, inserts a generated set of documents, applies
a generated filter combination, and compares the repository's result against an
independent Python reference computation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import Settings
from app.db import bootstrap
from app.kb.store import DocumentRepository

# The four seeded Intent_Spaces (General=1, HR=2, Legal=3, Finance=4) are valid
# foreign-key targets for documents.space_id.
VALID_SPACE_IDS = [1, 2, 3, 4]
FORMATS = ["pdf", "docx", "xlsx", "txt", "md"]
# Kept small so filters realistically match generated documents.
DATES = ["2023-12-31", "2024-01-01", "2024-01-02", "2024-02-15"]

# A restricted alphabet (no LIKE wildcards) that mixes case so the test also
# exercises case-insensitive name matching.
_NAME_ALPHABET = "abABxY "

# One generated document: (name, format, space_id, upload_date).
_document = st.tuples(
    st.text(alphabet=_NAME_ALPHABET, min_size=0, max_size=6),
    st.sampled_from(FORMATS),
    st.sampled_from(VALID_SPACE_IDS),
    st.sampled_from(DATES),
)

# A generated filter combination; each field is optional (None = not applied).
_filters = st.fixed_dictionaries(
    {
        "name": st.one_of(
            st.none(), st.text(alphabet="abABxY", min_size=1, max_size=3)
        ),
        "format": st.one_of(st.none(), st.sampled_from(FORMATS)),
        "space_id": st.one_of(st.none(), st.sampled_from(VALID_SPACE_IDS)),
        "uploaded_on": st.one_of(st.none(), st.sampled_from(DATES)),
    }
)


def _reference_matches(
    docs: list[tuple[str, str, int, int, str]], flt: dict
) -> set[int]:
    """Compute the expected matching document ids independently of SQL.

    Args:
        docs: Inserted documents as ``(id, name, format, space_id, date)``.
        flt: The applied filter dict (``name``/``format``/``space_id``/
            ``uploaded_on``); ``None`` values are not applied.

    Returns:
        The set of document ids that satisfy every applied filter.
    """
    result: set[int] = set()
    for doc_id, name, fmt, space_id, date_str in docs:
        if flt["name"] is not None and flt["name"].lower() not in name.lower():
            continue
        if flt["format"] is not None and fmt != flt["format"]:
            continue
        if flt["space_id"] is not None and space_id != flt["space_id"]:
            continue
        if flt["uploaded_on"] is not None and date_str != flt["uploaded_on"]:
            continue
        result.add(doc_id)
    return result


@settings(max_examples=200)
@given(documents=st.lists(_document, min_size=0, max_size=12), flt=_filters)
def test_document_list_returns_exactly_matching_documents(
    documents: list[tuple[str, str, int, str]], flt: dict
) -> None:
    """Repository filtering equals the independent reference for all inputs."""
    with tempfile.TemporaryDirectory() as tmp:
        settings_obj = Settings(
            data_dir=Path(tmp),
            dashscope_api_key="",
            telegram_proxy_url="",
            credential_master_key="",
            aws_region="cn-north-1",
        )
        conn = bootstrap(settings_obj)
        try:
            repo = DocumentRepository(conn)

            inserted: list[tuple[int, str, str, int, str]] = []
            for name, fmt, space_id, date_str in documents:
                doc_id = repo.create(
                    name=name,
                    format=fmt,
                    size_bytes=1,
                    space_id=space_id,
                    file_path=f"/data/uploads/{name or 'x'}.{fmt}",
                    uploaded_at=date_str,
                )
                inserted.append((doc_id, name, fmt, space_id, date_str))

            expected = _reference_matches(inserted, flt)

            listed = repo.list(
                name=flt["name"],
                format=flt["format"],
                space_id=flt["space_id"],
                uploaded_on=flt["uploaded_on"],
            )
            actual = {row["id"] for row in listed}

            assert actual == expected
            # No duplicate rows and every returned row genuinely matches.
            assert len(listed) == len(actual)
        finally:
            conn.close()
