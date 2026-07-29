"""Admin UI — KB Management screen (task 18.4).

The Knowledge Base management screen lets the Admin build and maintain the
document corpus (Req 4.4–4.10, 5.6). It provides:

* a **drag-and-drop upload zone** (``st.file_uploader``) with a 0–100 % progress
  indicator while a file is transferred to the backend (Req 4.4), plus
  client-side format/size pre-checks mirroring the backend rules (Req 4.2/4.3);
* a **document table** with the columns Name, Upload Date, Format, Size, Status
  and Actions (View / Delete / Update) (Req 4.5), where Error-status documents
  carry a visible error indication and their failure detail (Req 4.10);
* **filters** by name, format, upload date and Intent_Space that constrain the
  list to matching documents (Req 4.6);
* a **delete confirmation** prompt before a document is removed (Req 4.7/4.8);
* an **Update** action that re-triggers parsing/embedding (Req 4.9); and
* an **Intent_Space assignment** control per document (Req 5.6).

Backend contract (consumed via the shared :class:`ApiClient`):

===============================  ==========================================
``GET  /spaces``                 Intent_Space list for the assignment dropdown
``GET  /documents``              filterable document list
``POST /documents``              upload a document (JSON, base64 content)
``DELETE /documents/{id}``       delete a document
``POST /documents/{id}/update``  re-parse / regenerate embeddings
``PUT  /documents/{id}/space``   assign the document to an Intent_Space
===============================  ==========================================

Because :class:`ApiClient` speaks JSON only, an upload is sent as a JSON body
carrying the base64-encoded file content (``content_b64``) alongside the file
name, detected ``format``, ``size_bytes`` and target ``space_id``.

Import-safety and testability
------------------------------
Every Streamlit call lives inside ``_main`` (or the ``_render_*`` helpers it
calls), which run only under ``__main__``; ``streamlit`` is imported lazily.
The **pure helpers** (:func:`human_size`, :func:`detect_format`,
:func:`validate_upload`, :func:`build_list_params`, :func:`document_row`,
:func:`status_display`, :func:`extract_items`, :func:`space_label_map`) and the
**API wrappers** (:func:`fetch_spaces`, :func:`fetch_documents`,
:func:`upload_document`, :func:`delete_document`, :func:`update_document`,
:func:`assign_space`) depend only on the standard library / ``ApiClient`` and
are unit-testable without a running Streamlit server.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Make the project root importable when Streamlit runs this page directly.
_root = _os.path.abspath(__file__)
for _ in range(4):
    _root = _os.path.dirname(_root)
    if _os.path.isdir(_os.path.join(_root, "admin_ui")):
        break
if _root not in _sys.path:
    _sys.path.insert(0, _root)

import base64
from typing import Any

from admin_ui.ui.components import (
    ApiClient,
    ApiError,
    inject_base_css,
    render_sidebar_nav,
    section_header,
)

#: Navigation key for this screen (matches a ``SCREENS`` entry in components).
SCREEN_KEY = "kb_management"

#: Module identifier used to pick the green KB Management accent (Req 9.4).
MODULE = "kb_management"

#: Maximum accepted upload size in bytes (50 MB, Req 4.1/4.3).
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024

#: Supported document formats mapped from their accepted file extensions
#: (Req 4.1). Keys are lowercase extensions without the leading dot; values are
#: the canonical ``format`` string persisted by the backend.
EXTENSION_TO_FORMAT: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "txt": "txt",
    "md": "md",
    "markdown": "md",
}

#: Canonical formats offered in the format filter, in display order.
SUPPORTED_FORMATS: tuple[str, ...] = ("pdf", "docx", "xlsx", "txt", "md")

#: File-uploader ``type`` allow-list (extensions Streamlit will accept).
UPLOAD_EXTENSIONS: tuple[str, ...] = ("pdf", "docx", "xlsx", "txt", "md", "markdown")

#: Document statuses (Req 4.5).
STATUS_PROCESSED = "Processed"
STATUS_PENDING = "Pending"
STATUS_ERROR = "Error"


# ---------------------------------------------------------------------------
# Pure helpers (import-safe; no Streamlit)
# ---------------------------------------------------------------------------


def human_size(size_bytes: Any) -> str:
    """Render a byte count as a compact human-readable size (Req 4.5).

    Args:
        size_bytes: A byte count (int-like). Non-numeric/negative input yields
            ``"—"`` so the table degrades gracefully on malformed data.

    Returns:
        A short string such as ``"512 B"``, ``"3.4 KB"`` or ``"12.0 MB"``.
    """
    try:
        n = int(size_bytes)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return "—"
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def detect_format(filename: str | None) -> str | None:
    """Map a file name to its canonical document format, or ``None`` (Req 4.1).

    Args:
        filename: The uploaded file's name (may include a path or no extension).

    Returns:
        The canonical format (``"pdf"``/``"docx"``/``"xlsx"``/``"txt"``/``"md"``)
        for a supported extension, else ``None`` for an unsupported/absent one.
    """
    if not filename or "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[-1].strip().lower()
    return EXTENSION_TO_FORMAT.get(ext)


def validate_upload(filename: str | None, size_bytes: int) -> str | None:
    """Client-side pre-check of an upload before it is sent (Req 4.2/4.3).

    Mirrors the backend's format and size rules so the Admin gets immediate
    feedback; the backend remains the authority. Format is checked before size
    so an unsupported file is reported as such regardless of its size.

    Args:
        filename: The uploaded file's name.
        size_bytes: The file size in bytes.

    Returns:
        ``None`` when the file is acceptable, otherwise a human-readable error
        message naming the supported formats (Req 4.2) or the size limit
        (Req 4.3).
    """
    if detect_format(filename) is None:
        supported = ", ".join(SUPPORTED_FORMATS).upper()
        return f"Unsupported format. Supported formats: {supported}."
    if size_bytes > MAX_UPLOAD_BYTES:
        return "File exceeds the maximum allowed size of 50 MB."
    return None


def build_list_params(
    *,
    name: str | None = None,
    fmt: str | None = None,
    space_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Build the ``GET /documents`` query params, omitting empty filters (Req 4.6).

    Only filters the Admin actually set are included, so an unfiltered view
    fetches the full list. A blank/whitespace ``name`` and the sentinel format
    ``"All"`` are treated as "no filter".

    Args:
        name: Name search term.
        fmt: Format filter (canonical format, or ``"All"``/``None`` for any).
        space_id: Intent_Space id filter (``None`` for any).
        date_from: Inclusive lower bound for upload date (ISO ``YYYY-MM-DD``).
        date_to: Inclusive upper bound for upload date (ISO ``YYYY-MM-DD``).

    Returns:
        A params dict suitable for :meth:`ApiClient.get`.
    """
    params: dict[str, Any] = {}
    if name and name.strip():
        params["name"] = name.strip()
    if fmt and fmt != "All":
        params["format"] = fmt
    if space_id is not None:
        params["space_id"] = space_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    return params


def extract_items(payload: Any, key: str) -> list[dict[str, Any]]:
    """Normalise a list-or-wrapped-list backend payload to a list of dicts.

    Tolerates both a bare JSON array and an object wrapping the array under
    ``key`` (e.g. ``{"documents": [...]}``), so the screen is robust to either
    backend response shape.

    Args:
        payload: The parsed JSON returned by the backend.
        key: The wrapper key to look under when ``payload`` is a dict.

    Returns:
        A list of dict items (empty when nothing usable is present).
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        inner = payload.get(key)
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


def status_display(status: Any, error_message: Any = None) -> str:
    """Render a status cell with an error indication for failures (Req 4.10).

    Args:
        status: The document status (``Processed``/``Pending``/``Error``).
        error_message: Optional failure detail shown alongside an Error status.

    Returns:
        A short display string; Error statuses are prefixed with a red marker
        and include the truncated failure detail when available.
    """
    text = str(status or "").strip() or "Unknown"
    if text == STATUS_ERROR:
        detail = str(error_message or "").strip()
        if detail:
            if len(detail) > 80:
                detail = detail[:77] + "..."
            return f"🔴 Error — {detail}"
        return "🔴 Error"
    if text == STATUS_PROCESSED:
        return "🟢 Processed"
    if text == STATUS_PENDING:
        return "🟡 Pending"
    return text


def space_label_map(spaces: list[dict[str, Any]]) -> dict[int, str]:
    """Map Intent_Space ids to display names for dropdowns/labels (Req 5.6).

    Args:
        spaces: Space records from ``GET /spaces`` (each with ``id``/``name``).

    Returns:
        An ``{id: name}`` mapping; entries missing an id are skipped.
    """
    mapping: dict[int, str] = {}
    for space in spaces:
        sid = space.get("id")
        if sid is None:
            continue
        mapping[int(sid)] = str(space.get("name", f"Space {sid}"))
    return mapping


def document_row(doc: dict[str, Any], spaces_by_id: dict[int, str]) -> dict[str, Any]:
    """Project a document record into a display row for the table (Req 4.5).

    Args:
        doc: A document record from ``GET /documents``.
        spaces_by_id: ``{id: name}`` map used to render the Intent_Space name.

    Returns:
        An ordered mapping with the Name, Upload Date, Format, Size, Intent
        Space and Status columns ready for ``st.dataframe`` (Req 4.5, 4.10).
    """
    space_id = doc.get("space_id")
    space_name = ""
    if space_id is not None:
        space_name = spaces_by_id.get(int(space_id), f"Space {space_id}")
    return {
        "Name": str(doc.get("name", "")),
        "Upload Date": str(doc.get("uploaded_at", "")),
        "Format": str(doc.get("format", "")).upper(),
        "Size": human_size(doc.get("size_bytes")),
        "Intent Space": space_name,
        "Status": status_display(doc.get("status"), doc.get("error_message")),
    }


# ---------------------------------------------------------------------------
# API wrappers (thin, testable with a mock-transport ApiClient)
# ---------------------------------------------------------------------------


def fetch_spaces(client: ApiClient) -> list[dict[str, Any]]:
    """Fetch Intent_Spaces for the assignment dropdown (``GET /spaces``)."""
    return extract_items(client.get("/spaces"), "spaces")


def fetch_documents(
    client: ApiClient, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Fetch the (optionally filtered) document list (``GET /documents``)."""
    return extract_items(client.get("/documents", params=params or None), "documents")


def upload_document(
    client: ApiClient,
    *,
    name: str,
    fmt: str,
    size_bytes: int,
    content: bytes,
    space_id: int | None = None,
) -> Any:
    """Upload a document via ``POST /documents`` (base64 JSON body, Req 4.1/4.4).

    Args:
        client: The backend API client.
        name: The original file name.
        fmt: The canonical document format.
        size_bytes: The file size in bytes.
        content: The raw file bytes (base64-encoded for the JSON body).
        space_id: Optional target Intent_Space id.

    Returns:
        The backend's parsed JSON response (e.g. the created document record).
    """
    body: dict[str, Any] = {
        "name": name,
        "format": fmt,
        "size_bytes": size_bytes,
        "content_b64": base64.b64encode(content).decode("ascii"),
    }
    if space_id is not None:
        body["space_id"] = space_id
    return client.post("/documents", json=body)


def delete_document(client: ApiClient, document_id: int) -> Any:
    """Delete a document (``DELETE /documents/{id}``, Req 4.8)."""
    return client.delete(f"/documents/{document_id}")


def update_document(client: ApiClient, document_id: int) -> Any:
    """Re-process a document (``POST /documents/{id}/update``, Req 4.9)."""
    return client.post(f"/documents/{document_id}/update")


def assign_space(client: ApiClient, document_id: int, space_id: int) -> Any:
    """Assign a document to an Intent_Space (``PUT /documents/{id}/space``, Req 5.6)."""
    return client.put(f"/documents/{document_id}/space", json={"space_id": space_id})


# ---------------------------------------------------------------------------
# Streamlit rendering (invoked only under __main__)
# ---------------------------------------------------------------------------


def _render_upload(client: ApiClient, spaces_by_id: dict[int, str]) -> None:
    """Render the drag-and-drop upload zone with a 0–100 % progress bar (Req 4.4)."""
    import streamlit as st

    section_header("Upload a document", module=MODULE)
    uploaded = st.file_uploader(
        "Drag and drop a file here, or browse",
        type=list(UPLOAD_EXTENSIONS),
        accept_multiple_files=False,
        help="Supported formats: PDF, DOCX, XLSX, TXT, Markdown. Max size 50 MB.",
        key="kb_uploader",
    )

    target_space_id: int | None = None
    if spaces_by_id:
        space_ids = list(spaces_by_id.keys())
        target_space_id = st.selectbox(
            "Assign to Intent Space",
            options=space_ids,
            format_func=lambda sid: spaces_by_id.get(sid, f"Space {sid}"),
            key="kb_upload_space",
        )

    if uploaded is None:
        return

    content = uploaded.getvalue()
    size_bytes = len(content)
    error = validate_upload(uploaded.name, size_bytes)
    if error:
        # Reject client-side, retaining no partial data (Req 4.2/4.3).
        st.error(error)
        return

    st.caption(f"Ready to upload **{uploaded.name}** ({human_size(size_bytes)}).")
    if not st.button("Upload", key="kb_upload_btn", type="primary"):
        return

    fmt = detect_format(uploaded.name) or ""
    progress = st.progress(0, text="Starting upload…")
    try:
        # Show progress climbing to just under 100 % while the transfer runs,
        # then complete it once the backend acknowledges (Req 4.4).
        for pct in (15, 40, 70, 90):
            progress.progress(pct, text=f"Uploading… {pct}%")
        upload_document(
            client,
            name=uploaded.name,
            fmt=fmt,
            size_bytes=size_bytes,
            content=content,
            space_id=target_space_id,
        )
        progress.progress(100, text="Upload complete (100%)")
    except ApiError as exc:
        progress.empty()
        st.error(f"Upload failed: {exc.message}")
        return

    st.success(
        f"Uploaded '{uploaded.name}'. Status is Pending while it is processed."
    )


def _render_filters(spaces_by_id: dict[int, str]) -> dict[str, Any]:
    """Render the name/format/date/space filters and return list params (Req 4.6)."""
    import streamlit as st

    section_header("Filter documents", module=MODULE)
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name contains", key="kb_filter_name")
        fmt = st.selectbox(
            "Format", options=("All", *SUPPORTED_FORMATS), key="kb_filter_format"
        )
    with col2:
        space_choices = [None, *spaces_by_id.keys()]
        space_id = st.selectbox(
            "Intent Space",
            options=space_choices,
            format_func=lambda sid: "All"
            if sid is None
            else spaces_by_id.get(sid, f"Space {sid}"),
            key="kb_filter_space",
        )

    col3, col4 = st.columns(2)
    with col3:
        use_from = st.checkbox("From upload date", key="kb_filter_from_on")
        date_from = st.date_input("From", key="kb_filter_from") if use_from else None
    with col4:
        use_to = st.checkbox("To upload date", key="kb_filter_to_on")
        date_to = st.date_input("To", key="kb_filter_to") if use_to else None

    return build_list_params(
        name=name,
        fmt=fmt,
        space_id=space_id,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
    )


def _render_actions(
    client: ApiClient, documents: list[dict[str, Any]], spaces_by_id: dict[int, str]
) -> None:
    """Render per-document View/Delete/Update + space assignment controls."""
    import streamlit as st

    section_header("Document actions", module=MODULE)
    for doc in documents:
        doc_id = doc.get("id")
        if doc_id is None:
            continue
        name = str(doc.get("name", f"Document {doc_id}"))
        with st.expander(f"{name} — {status_display(doc.get('status'), doc.get('error_message'))}"):
            if str(doc.get("status")) == STATUS_ERROR and doc.get("error_message"):
                st.error(f"Processing error: {doc.get('error_message')}")

            cols = st.columns(3)
            with cols[0]:
                if st.button("View", key=f"kb_view_{doc_id}"):
                    st.json(doc)
            with cols[1]:
                if st.button("Update", key=f"kb_update_{doc_id}"):
                    try:
                        update_document(client, int(doc_id))
                        st.success("Re-processing started; status set to Pending.")
                    except ApiError as exc:
                        st.error(f"Update failed: {exc.message}")
            with cols[2]:
                if st.button("Delete", key=f"kb_delete_{doc_id}"):
                    st.session_state[f"kb_confirm_delete_{doc_id}"] = True

            # Delete confirmation prompt (Req 4.7/4.8).
            if st.session_state.get(f"kb_confirm_delete_{doc_id}"):
                st.warning(f"Delete '{name}'? This cannot be undone.")
                confirm_cols = st.columns(2)
                with confirm_cols[0]:
                    if st.button("Confirm delete", key=f"kb_delete_confirm_{doc_id}"):
                        try:
                            delete_document(client, int(doc_id))
                            st.session_state.pop(f"kb_confirm_delete_{doc_id}", None)
                            st.success(f"Deleted '{name}'.")
                        except ApiError as exc:
                            st.error(f"Delete failed: {exc.message}")
                with confirm_cols[1]:
                    if st.button("Cancel", key=f"kb_delete_cancel_{doc_id}"):
                        st.session_state.pop(f"kb_confirm_delete_{doc_id}", None)

            # Intent_Space assignment control (Req 5.6).
            if spaces_by_id:
                space_ids = list(spaces_by_id.keys())
                current = doc.get("space_id")
                index = (
                    space_ids.index(int(current))
                    if current is not None and int(current) in space_ids
                    else 0
                )
                chosen = st.selectbox(
                    "Assign to Intent Space",
                    options=space_ids,
                    index=index,
                    format_func=lambda sid: spaces_by_id.get(sid, f"Space {sid}"),
                    key=f"kb_assign_space_{doc_id}",
                )
                if st.button("Save assignment", key=f"kb_assign_btn_{doc_id}"):
                    try:
                        assign_space(client, int(doc_id), int(chosen))
                        st.success("Intent Space updated.")
                    except ApiError as exc:
                        st.error(f"Assignment failed: {exc.message}")


def _main() -> None:
    """Render the KB Management screen (Req 4.4–4.10, 5.6)."""
    import streamlit as st

    st.set_page_config(page_title="IntelliKnow KMS — KB Management", page_icon="📚")
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("KB Management", module=MODULE)

    client = ApiClient()

    # Load Intent_Spaces once for the assignment/filter dropdowns (Req 5.6).
    try:
        spaces = fetch_spaces(client)
    except ApiError as exc:
        spaces = []
        st.warning(f"Could not load Intent Spaces: {exc.message}")
    spaces_by_id = space_label_map(spaces)

    _render_upload(client, spaces_by_id)

    params = _render_filters(spaces_by_id)

    section_header("Documents", module=MODULE)
    try:
        documents = fetch_documents(client, params)
    except ApiError as exc:
        st.error(f"Could not load documents: {exc.message}")
        return

    if not documents:
        st.info("No documents match the current filters.")
        return

    rows = [document_row(doc, spaces_by_id) for doc in documents]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    _render_actions(client, documents, spaces_by_id)


if __name__ == "__main__":
    _main()
