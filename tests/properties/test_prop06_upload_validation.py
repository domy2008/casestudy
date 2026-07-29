# Feature: intelliknow-kms, Property 6: Upload validation accepts exactly the supported envelope
"""Property 6: Upload validation accepts exactly the supported envelope.

For *any* upload filename extension (drawn from the supported set ∪ an
unsupported set) and *any* declared size (weighted around the 50 MB boundary),
:meth:`DocumentLifecycleService.accept_upload` accepts the upload — creating a
single ``Pending`` document row and storing exactly one original file — **if
and only if** the extension is one of
:data:`~app.kb.loaders.SUPPORTED_EXTENSIONS` and the size is at most
:data:`~app.kb.service.MAX_UPLOAD_BYTES` (50 MB). A rejected upload leaves no
document row and no stored file.

Sizes are simulated via the API's ``declared_size`` parameter so the test never
writes 50 MB files and stays fast.

**Validates: Requirements 4.1, 4.2, 4.3**
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from app.config import load_settings
from app.db import bootstrap
from app.kb.loaders import SUPPORTED_EXTENSIONS
from app.kb.service import (
    MAX_UPLOAD_BYTES,
    DocumentLifecycleService,
    UploadRejected,
)

# Extensions with no registered loader — the "outside the envelope" formats,
# including the no-extension case ("").
UNSUPPORTED_EXTENSIONS: tuple[str, ...] = (
    "",
    ".exe",
    ".zip",
    ".png",
    ".csv",
    ".json",
    ".html",
    ".pptx",
    ".doc",
    ".xls",
)

# Draw from supported ∪ unsupported so both accept and reject paths are covered.
_extension_strategy = st.one_of(
    st.sampled_from(sorted(SUPPORTED_EXTENSIONS)),
    st.sampled_from(UNSUPPORTED_EXTENSIONS),
)

# Sizes weighted around the 50 MB boundary, plus a broad 0..100 MB spread so
# both "within" and "over" the limit are well represented.
_size_strategy = st.one_of(
    st.integers(min_value=MAX_UPLOAD_BYTES - 2048, max_value=MAX_UPLOAD_BYTES + 2048),
    st.integers(min_value=0, max_value=MAX_UPLOAD_BYTES * 2),
)


@settings(max_examples=200, deadline=None)
@given(ext=_extension_strategy, size=_size_strategy)
def test_upload_validation_accepts_exactly_the_supported_envelope(
    ext: str, size: int
) -> None:
    """Acceptance holds iff format supported AND size ≤ 50 MB; else nothing persists."""
    with tempfile.TemporaryDirectory() as tmp:
        db_settings = load_settings({"DATA_DIR": tmp})
        conn = bootstrap(db_settings)
        try:
            service = DocumentLifecycleService(conn, settings=db_settings)
            filename = f"document{ext}"
            uploads_dir = Path(tmp) / "uploads"

            expected_ok = ext in SUPPORTED_EXTENSIONS and size <= MAX_UPLOAD_BYTES

            if expected_ok:
                doc_id = service.accept_upload(
                    filename, content=b"payload", declared_size=size
                )

                row = conn.execute(
                    "SELECT * FROM documents WHERE id = ?", (doc_id,)
                ).fetchone()
                # A single Pending row exists with the declared size recorded.
                assert row is not None
                assert row["status"] == "Pending"
                assert row["size_bytes"] == size
                assert row["name"] == filename
                assert (
                    conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
                )
                # Exactly one original file was stored.
                stored = sorted(uploads_dir.glob("*"))
                assert len(stored) == 1
                assert str(stored[0]) == row["file_path"]
                assert stored[0].exists()
            else:
                with pytest.raises(UploadRejected) as excinfo:
                    service.accept_upload(
                        filename, content=b"payload", declared_size=size
                    )

                # No document row was created.
                assert (
                    conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
                )
                # No file was stored.
                stored = list(uploads_dir.glob("*")) if uploads_dir.exists() else []
                assert stored == []
                # The error names the supported formats and/or the max size.
                message = str(excinfo.value).lower()
                assert "supported" in message or "size" in message or "50 mb" in message
        finally:
            conn.close()
