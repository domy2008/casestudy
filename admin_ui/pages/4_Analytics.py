"""Admin UI — Analytics screen (task 18.6).

The Analytics screen is the Admin's window onto query activity and knowledge
coverage. It renders (all against the shared backend admin REST API via
:class:`~admin_ui.ui.components.ApiClient`):

* **Query history** — the 50 most recent Query_Log entries, ordered by
  timestamp descending, filtered by an Admin-chosen time range and one or more
  Intent_Spaces, showing query text, detected intent, confidence score, and
  response status (Success/Failed) (Req 7.7, 10.4). When no entries match the
  applied filters, a clear "no matching entries" message is shown (Req 10.7).
* **Usage metrics** — the 10 most accessed documents and the 10 most common
  Intent_Spaces for the selected range (Req 10.2).
* **Per-space accuracy** — the classification accuracy rate per Intent_Space,
  ``N/A`` where a space has no Admin-verified queries (Req 10.3).
* **Query verification** — the history table's *Verified* column is directly
  editable: picking an Intent_Space in a row's dropdown records the Admin's
  verdict immediately (pick the detected space to confirm it, or a different
  one to correct it), feeding the accuracy computation (Req 10.3).
* **CSV export** — a download button producing the filtered Query_Log plus
  metrics; an export failure surfaces an error message and changes nothing
  (Req 10.5, 10.8).

Backend endpoints consumed (see ``app/api/admin.py``): ``GET /queries`` for the
filtered history, ``GET /analytics/top-documents`` / ``/analytics/top-spaces``
/ ``/analytics/accuracy`` for metrics, ``GET /analytics/export`` for the CSV,
and ``POST /queries/{id}/verify`` for verification. Every call degrades
gracefully: a missing endpoint (404) or any transport/HTTP error is caught and
surfaced inline so the rest of the screen keeps working (mirrors the
Dashboard's per-card resilience, Req 9.7).

Import-safety
-------------
As with every screen, all Streamlit calls live inside :func:`_main`, which runs
only under ``__main__``. Streamlit is imported lazily so this module imports
cleanly in headless contexts (including the test suite). The parsing/parameter
helpers below are pure (no Streamlit, no I/O) and unit-tested directly.
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

from datetime import date, datetime, timedelta
from typing import Any

from admin_ui.ui.components import (
    ApiClient,
    ApiError,
    inject_base_css,
    render_metric_card,
    render_sidebar_nav,
    section_header,
)

#: Navigation key for this screen (matches a :data:`SCREENS` entry).
SCREEN_KEY = "analytics"

#: Number of most-recent query-log entries shown / requested (Req 7.7, 10.4).
HISTORY_LIMIT = 50

#: Size of the "top N" usage-metric listings (Req 10.2).
TOP_N = 10

# Backend endpoint paths consumed by this screen.
QUERIES_PATH = "/queries"
TOP_DOCUMENTS_PATH = "/analytics/top-documents"
TOP_SPACES_PATH = "/analytics/top-spaces"
GAPS_PATH = "/analytics/gaps"
FEEDBACK_PATH = "/analytics/feedback"
ACCURACY_PATH = "/analytics/accuracy"
EXPORT_PATH = "/analytics/export"
SPACES_PATH = "/spaces"

#: Shown when the applied filters match no Query_Log entries (Req 10.7).
NO_MATCHING_ENTRIES_MESSAGE = "No query-log entries match the applied filters."

#: Shown when a specific backend endpoint is not (yet) available.
ENDPOINT_UNAVAILABLE_MESSAGE = "This data is not available from the backend yet."


# ---------------------------------------------------------------------------
# Pure helpers (no Streamlit, no I/O) — unit-tested directly
# ---------------------------------------------------------------------------


def iso_start_of_day(day: date) -> str:
    """Render ``day`` as the inclusive start-of-day timestamp.

    The backend stores timestamps in ``"YYYY-MM-DD HH:MM:SS"`` form (a
    space-separated ISO string); a start filter therefore uses midnight.

    Args:
        day: The calendar date chosen by the Admin.

    Returns:
        The start-of-day timestamp string, e.g. ``"2024-05-01 00:00:00"``.
    """
    return f"{day.isoformat()} 00:00:00"


def iso_end_of_day(day: date) -> str:
    """Render ``day`` as the inclusive end-of-day timestamp.

    Args:
        day: The calendar date chosen by the Admin.

    Returns:
        The end-of-day timestamp string, e.g. ``"2024-05-01 23:59:59"``.
    """
    return f"{day.isoformat()} 23:59:59"


def build_history_params(
    *,
    start: str | None = None,
    end: str | None = None,
    space_ids: list[int] | None = None,
    tool: str | None = None,
    limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    """Build the query-string params for the ``GET /queries`` history call.

    Only the filters the Admin actually set are included, so an unset filter is
    simply omitted rather than sent as an empty value. ``limit`` is always sent
    so the backend caps the result set (Req 7.7, 10.4).

    Args:
        start: Inclusive start timestamp, or ``None`` for no lower bound.
        end: Inclusive end timestamp, or ``None`` for no upper bound.
        space_ids: Selected Intent_Space ids to filter by, or ``None``/empty
            for all spaces.
        tool: Restrict to one Frontend_Tool, or ``None`` for all.
        limit: Maximum number of rows to request (default 50).

    Returns:
        A params dict suitable for :meth:`ApiClient.get`.
    """
    params: dict[str, Any] = {"limit": limit}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if space_ids:
        params["space_ids"] = list(space_ids)
    if tool:
        params["tool"] = tool
    return params


def build_range_params(
    *, start: str | None = None, end: str | None = None, n: int = TOP_N
) -> dict[str, Any]:
    """Build the params for a range-scoped metric call (top docs/spaces).

    Args:
        start: Inclusive start timestamp, or ``None``.
        end: Inclusive end timestamp, or ``None``.
        n: Maximum number of ranked entries to request (default 10).

    Returns:
        A params dict suitable for :meth:`ApiClient.get`.
    """
    params: dict[str, Any] = {"n": n}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return params


def verify_path(query_id: int) -> str:
    """Return the verify endpoint path for a query-log entry.

    Args:
        query_id: The Query_Log entry id to annotate.

    Returns:
        The path, e.g. ``"/queries/42/verify"``.
    """
    return f"{QUERIES_PATH}/{int(query_id)}/verify"


def format_confidence(value: Any) -> str:
    """Format a 0–100 confidence score as a whole-percent string.

    Args:
        value: The stored confidence (a number 0..100), or ``None``.

    Returns:
        A string like ``"84%"``; ``"—"`` when the value is missing or
        non-numeric.
    """
    if value is None:
        return "—"
    try:
        return f"{round(float(value))}%"
    except (TypeError, ValueError):
        return "—"


def extract_history_entries(data: Any) -> list[dict[str, Any]]:
    """Normalize a ``GET /queries`` response into a list of row dicts.

    Tolerates the shapes the backend might return — a bare list, or a wrapper
    object keyed by ``queries``/``entries``/``items``/``results`` — so the
    screen keeps working regardless of the exact envelope.

    Args:
        data: The parsed JSON returned by the history endpoint.

    Returns:
        A list of entry dicts (empty when the payload carries no entries).
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("queries", "entries", "items", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
    return []


def history_row_to_display(
    entry: dict[str, Any], space_names: dict[int, str]
) -> dict[str, str]:
    """Map one Query_Log entry to its display columns (Req 7.7).

    Shows query text, detected intent (resolved to a space name), confidence
    score, and response status. The detected space id is resolved via
    ``space_names`` and falls back to ``#<id>`` when unknown.

    Args:
        entry: A Query_Log row dict from the history endpoint.
        space_names: Mapping of Intent_Space id → display name.

    Returns:
        An ordered dict of display column → string value.
    """
    detected_id = entry.get("detected_space_id")
    intent = resolve_space_label(detected_id, space_names)
    verified_id = entry.get("verified_space_id")
    verified: str | None = None
    if verified_id is not None:
        verified = resolve_space_label(verified_id, space_names)
    # Trim to whole seconds: stored timestamps may carry microseconds
    # ("2026-07-31 03:44:52.398574"), which is noise in the history table.
    ts = str(entry.get("ts", "") or "").replace("T", " ").split(".")[0]
    return {
        "Time": ts,
        "Query": str(entry.get("query_text", "") or ""),
        "Detected Intent": intent,
        "Confidence": format_confidence(entry.get("confidence")),
        "Status": str(entry.get("response_status", "") or ""),
        "Verified": verified,
    }


def pending_verifications(
    widget_state: Any,
    snapshot_entries: list[dict[str, Any]],
    name_to_id: dict[str, int],
) -> list[tuple[int, int]]:
    """Extract the verifications implied by a data editor's edit state.

    ``st.data_editor`` keeps its uncommitted cell edits in session state as
    ``{"edited_rows": {<row_index>: {<column>: <value>}}}``. This maps each
    edited *Verified* cell back to the Query_Log entry that was **on screen
    when the Admin edited it** (``snapshot_entries``, the exact rows the
    editor was rendered from) — not a re-fetched row set, which may have
    shifted if new queries arrived meanwhile.

    Rows with an unknown index, no query id, an empty/cleared cell, an
    unknown space name, or an unchanged verdict are skipped, so re-runs never
    produce duplicate submissions.

    Args:
        widget_state: The editor's session-state value (mapping-like or
            ``None``).
        snapshot_entries: The raw Query_Log entries the editor rendered, in
            row order.
        name_to_id: Mapping of Intent_Space display name → space id.

    Returns:
        A list of ``(query_id, verified_space_id)`` pairs to submit.
    """
    if widget_state is None:
        return []
    edited = (
        widget_state.get("edited_rows")
        if hasattr(widget_state, "get")
        else getattr(widget_state, "edited_rows", None)
    )
    if not isinstance(edited, dict):
        return []

    changes: list[tuple[int, int]] = []
    for raw_index, cells in edited.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if not isinstance(cells, dict) or not (0 <= index < len(snapshot_entries)):
            continue
        chosen = cells.get("Verified")
        if not chosen:
            continue
        new_id = name_to_id.get(str(chosen))
        if new_id is None:
            continue
        entry = snapshot_entries[index]
        query_id = entry.get("id")
        if query_id is None:
            continue
        stored = entry.get("verified_space_id")
        try:
            unchanged = stored is not None and int(stored) == int(new_id)
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            changes.append((int(query_id), int(new_id)))
    return sorted(changes)


def resolve_space_label(space_id: Any, space_names: dict[int, str]) -> str:
    """Resolve an Intent_Space id to a display label.

    Args:
        space_id: The space id (may be ``None`` or non-int).
        space_names: Mapping of id → name.

    Returns:
        The space name, ``"—"`` when the id is missing, or ``#<id>`` when the
        id is unknown.
    """
    if space_id is None:
        return "—"
    try:
        sid = int(space_id)
    except (TypeError, ValueError):
        return str(space_id)
    return space_names.get(sid, f"#{sid}")


def normalize_spaces(data: Any) -> dict[int, str]:
    """Normalize a ``GET /spaces`` response into an id → name mapping.

    Args:
        data: The parsed JSON from the spaces endpoint (a list of space dicts,
            or a wrapper object keyed by ``spaces``).

    Returns:
        A mapping of Intent_Space id → name (empty when none are present).
    """
    rows: list[Any]
    if isinstance(data, dict):
        inner = data.get("spaces")
        rows = inner if isinstance(inner, list) else []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    mapping: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        name = row.get("name")
        if sid is None or name is None:
            continue
        try:
            mapping[int(sid)] = str(name)
        except (TypeError, ValueError):
            continue
    return mapping


def normalize_pairs(data: Any) -> list[tuple[str, int]]:
    """Normalize a top-documents/top-spaces response into (label, count) pairs.

    Accepts a list of ``[label, count]`` pairs, a list of dicts (with any of
    the usual name/count key spellings), or a wrapper object keyed by
    ``documents``/``spaces``/``items``. Order is preserved (the backend already
    ranks by count descending, Req 10.2).

    Args:
        data: The parsed JSON from a usage-metric endpoint.

    Returns:
        A list of ``(label, count)`` tuples.
    """
    rows: list[Any]
    if isinstance(data, dict):
        inner: Any = None
        for key in ("documents", "spaces", "items", "results", "data"):
            if isinstance(data.get(key), list):
                inner = data[key]
                break
        rows = inner if isinstance(inner, list) else []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    pairs: list[tuple[str, int]] = []
    for row in rows:
        label: Any = None
        count: Any = None
        if isinstance(row, dict):
            for name_key in (
                "name",
                "document_name",
                "space_name",
                "label",
            ):
                if name_key in row:
                    label = row[name_key]
                    break
            for count_key in ("count", "access_count", "query_count", "value"):
                if count_key in row:
                    count = row[count_key]
                    break
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            label, count = row[0], row[1]
        if label is None:
            continue
        try:
            pairs.append((str(label), int(count)))
        except (TypeError, ValueError):
            pairs.append((str(label), 0))
    return pairs


def normalize_accuracy(data: Any) -> dict[int, float | None]:
    """Normalize an accuracy response into an id → percent-or-None mapping.

    Accepts a mapping of ``{space_id: percent}`` or a list of dicts carrying a
    space id and an accuracy value; ``"N/A"``/``None`` accuracy maps to
    ``None`` (a space with no verified queries, Req 10.3).

    Args:
        data: The parsed JSON from the accuracy endpoint.

    Returns:
        A mapping of Intent_Space id → accuracy percent, or ``None`` for N/A.
    """
    result: dict[int, float | None] = {}

    def _coerce_pct(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and value.strip().upper() in {"N/A", "NA", ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if isinstance(data, dict):
        # Could be {space_id: pct} or {"accuracy": {...}} / {"items": [...]}.
        inner: Any = data
        for key in ("accuracy", "items", "results", "data"):
            if key in data:
                inner = data[key]
                break
        if isinstance(inner, dict):
            for sid, pct in inner.items():
                try:
                    result[int(sid)] = _coerce_pct(pct)
                except (TypeError, ValueError):
                    continue
            return result
        data = inner  # fall through to list handling

    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            sid = row.get("space_id", row.get("id"))
            pct = row.get("accuracy_percent", row.get("accuracy"))
            if sid is None:
                continue
            try:
                result[int(sid)] = _coerce_pct(pct)
            except (TypeError, ValueError):
                continue
    return result


def format_accuracy(value: float | None) -> str:
    """Render an accuracy value as ``"NN%"`` or ``"N/A"`` (Req 10.3).

    Args:
        value: An accuracy percent, or ``None`` for no verified queries.

    Returns:
        ``"N/A"`` when ``value`` is ``None``; otherwise a whole-percent string.
    """
    if value is None:
        return "N/A"
    return f"{round(value)}%"


# ---------------------------------------------------------------------------
# Streamlit rendering (invoked only from _main under __main__)
# ---------------------------------------------------------------------------


def _fetch(client: ApiClient, path: str, *, params: dict[str, Any] | None = None):
    """Fetch a backend resource, returning ``(data, error_message)``.

    Never raises: a 404 (endpoint absent) or any other HTTP/transport failure
    is turned into a human-readable message so callers can degrade gracefully
    and keep the rest of the screen alive.

    Args:
        client: The shared API client.
        path: The endpoint path to GET.
        params: Optional query-string params.

    Returns:
        A ``(data, error)`` tuple: on success ``error`` is ``None``; on failure
        ``data`` is ``None`` and ``error`` is a display message.
    """
    try:
        return client.get(path, params=params), None
    except ApiError as exc:
        if exc.status_code == 404:
            return None, ENDPOINT_UNAVAILABLE_MESSAGE
        return None, exc.message


def _render_filters(space_names: dict[int, str]):
    """Render the time-range and Intent_Space filter controls.

    Returns:
        A tuple ``(start_iso, end_iso, selected_space_ids)`` where the ISO
        strings are ``None`` when the range filter is disabled.
    """
    import streamlit as st

    section_header("Filters", module="analytics")
    col1, col2 = st.columns(2)
    today = date.today()
    with col1:
        start_day = st.date_input(
            "Start date", value=today - timedelta(days=7), key="analytics_start"
        )
    with col2:
        end_day = st.date_input("End date", value=today, key="analytics_end")

    # date_input can return a tuple in range mode; normalize to a single date.
    if isinstance(start_day, (list, tuple)):
        start_day = start_day[0] if start_day else today
    if isinstance(end_day, (list, tuple)):
        end_day = end_day[-1] if end_day else today

    start_iso = iso_start_of_day(start_day)
    end_iso = iso_end_of_day(end_day)

    options = sorted(space_names, key=lambda sid: space_names[sid].lower())
    selected = st.multiselect(
        "Intent Spaces",
        options=options,
        format_func=lambda sid: space_names.get(sid, f"#{sid}"),
        key="analytics_spaces",
        help="Leave empty to include every Intent_Space.",
    )
    return start_iso, end_iso, list(selected)


def _render_history(
    client: ApiClient,
    space_names: dict[int, str],
    start_iso: str,
    end_iso: str,
    space_ids: list[int],
) -> None:
    """Render the filtered query-history table (Req 7.7, 10.4, 10.7)."""
    import streamlit as st

    section_header("Query History", module="analytics")

    # Submit any Verified-cell edits made on the PREVIOUS render before
    # fetching, so the refreshed table (and the accuracy section rendered
    # later in this same run) already reflect the new verdicts. The edits are
    # read from the editor's widget state and resolved against a session
    # snapshot of the rows the Admin was actually looking at, so new queries
    # arriving between reruns can never misalign a verdict onto a different
    # query.
    editor_key = "analytics_history_editor"
    snapshot_key = "analytics_history_snapshot"
    _submit_verifications(st, client, editor_key, snapshot_key, space_names)

    params = build_history_params(
        start=start_iso, end=end_iso, space_ids=space_ids, limit=HISTORY_LIMIT
    )
    data, error = _fetch(client, QUERIES_PATH, params=params)
    if error is not None:
        st.error(f"Could not load query history: {error}")
        return

    entries = extract_history_entries(data)
    if not entries:
        st.info(NO_MATCHING_ENTRIES_MESSAGE)
        return

    rows = [history_row_to_display(entry, space_names) for entry in entries]
    st.caption(
        f"Showing {len(rows)} most recent entries (newest first). "
        "To verify a classification, pick the correct space in the row's "
        "**Verified** cell — choose the detected space to confirm it."
    )
    space_options = sorted(space_names.values(), key=str.lower)
    st.session_state[snapshot_key] = entries
    st.data_editor(
        rows,
        use_container_width=True,
        hide_index=True,
        disabled=["Time", "Query", "Detected Intent", "Confidence", "Status"],
        column_config={
            "Verified": st.column_config.SelectboxColumn(
                "Verified",
                options=space_options,
                required=False,
                help="The Admin-verified correct Intent_Space for this query. "
                "Editing this cell saves immediately.",
            )
        },
        key=editor_key,
    )


def _submit_verifications(
    st: Any,
    client: ApiClient,
    editor_key: str,
    snapshot_key: str,
    space_names: dict[int, str],
) -> None:
    """Persist Verified-cell edits via ``POST /queries/{id}/verify``.

    Reads the editor's pending cell edits from session state, resolves them
    against the snapshot of entries the editor rendered, submits each changed
    verdict individually (a failure is surfaced but does not block the rest),
    and clears the editor state so the refreshed table shows the stored
    verdicts (Req 10.3).
    """
    widget_state = st.session_state.get(editor_key)
    snapshot = st.session_state.get(snapshot_key) or []
    name_to_id = {name: sid for sid, name in space_names.items()}
    changes = pending_verifications(widget_state, snapshot, name_to_id)
    if not changes:
        return

    saved = 0
    for query_id, space_id in changes:
        try:
            client.post(
                verify_path(query_id), json={"verified_space_id": space_id}
            )
            saved += 1
        except ApiError as exc:
            st.error(
                f"Could not verify query {query_id}: "
                f"{'query not found or endpoint absent.' if exc.status_code == 404 else exc.message}"
            )
    # Drop the editor's edit overlay so the re-rendered table reflects the
    # stored verdicts rather than replaying stale cell edits.
    del st.session_state[editor_key]
    if saved:
        st.toast(
            f"Saved {saved} verification{'s' if saved > 1 else ''}.", icon="✅"
        )


def _render_usage_metrics(
    client: ApiClient, start_iso: str, end_iso: str
) -> None:
    """Render top-10 documents and top-10 spaces for the range (Req 10.2)."""
    import streamlit as st

    section_header("Usage Metrics", module="analytics")
    params = build_range_params(start=start_iso, end=end_iso, n=TOP_N)
    col_docs, col_spaces = st.columns(2)

    with col_docs:
        st.markdown("**Top 10 Documents**")
        data, error = _fetch(client, TOP_DOCUMENTS_PATH, params=params)
        if error is not None:
            st.error(f"Top documents unavailable: {error}")
        else:
            pairs = normalize_pairs(data)
            if pairs:
                st.dataframe(
                    [{"Document": n, "Accesses": c} for n, c in pairs],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No document access recorded for this range.")

    with col_spaces:
        st.markdown("**Top 10 Intent Spaces**")
        data, error = _fetch(client, TOP_SPACES_PATH, params=params)
        if error is not None:
            st.error(f"Top spaces unavailable: {error}")
        else:
            pairs = normalize_pairs(data)
            if pairs:
                st.dataframe(
                    [{"Intent Space": n, "Queries": c} for n, c in pairs],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No queries recorded for this range.")


def _render_knowledge_gaps(
    client: ApiClient, start_iso: str, end_iso: str
) -> None:
    """Render the top unanswered questions so gaps drive the next uploads."""
    import streamlit as st

    section_header("Knowledge Gaps — Top Unanswered Questions", module="analytics")
    st.caption(
        "Questions the knowledge base could not answer (no-match). "
        "Upload documents covering these topics to close the gaps."
    )
    params = build_range_params(start=start_iso, end=end_iso, n=TOP_N)
    data, error = _fetch(client, GAPS_PATH, params=params)
    if error is not None:
        st.error(f"Knowledge gaps unavailable: {error}")
        return
    if not data:
        st.success("No unanswered questions in this range — no gaps detected.")
        return
    st.dataframe(
        [
            {
                "Question": row.get("query_text", ""),
                "Routed Space": row.get("space_name", ""),
                "Times Asked": row.get("count", 0),
                "Last Asked": row.get("last_ts", ""),
            }
            for row in data
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_feedback_summary(client: ApiClient) -> None:
    """Render the End_User 👍/👎 satisfaction card."""
    data, error = _fetch(client, FEEDBACK_PATH)
    if error is not None:
        render_metric_card(
            "User Satisfaction", "—", module="analytics", help_text=str(error)
        )
        return
    up = (data or {}).get("up", 0)
    down = (data or {}).get("down", 0)
    pct = (data or {}).get("satisfaction_pct")
    value = "N/A" if pct is None else f"{int(pct)}%"
    render_metric_card(
        "User Satisfaction",
        value,
        module="analytics",
        help_text=f"👍 {up} · 👎 {down} (from bot answer feedback buttons)",
    )


def _render_accuracy(client: ApiClient, space_names: dict[int, str]) -> None:
    """Render per-space classification accuracy (Req 10.3)."""
    import streamlit as st

    section_header("Classification Accuracy", module="analytics")
    data, error = _fetch(client, ACCURACY_PATH)
    if error is not None:
        st.error(f"Accuracy metrics unavailable: {error}")
        return

    accuracy = normalize_accuracy(data)
    if not accuracy:
        st.info("No verified queries yet — accuracy will appear once you verify.")
        return

    rows = [
        {
            "Intent Space": resolve_space_label(sid, space_names),
            "Accuracy": format_accuracy(pct),
        }
        for sid, pct in sorted(accuracy.items())
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_export(
    client: ApiClient, start_iso: str, end_iso: str, space_ids: list[int]
) -> None:
    """Render the CSV export control with a failure message (Req 10.5, 10.8)."""
    import streamlit as st

    section_header("Export", module="analytics")
    st.caption("Download the filtered query log and metrics as CSV.")
    params = build_history_params(
        start=start_iso, end=end_iso, space_ids=space_ids, limit=HISTORY_LIMIT
    )

    if st.button("Generate CSV export", key="analytics_export_btn"):
        try:
            payload = client.get(EXPORT_PATH, params=params)
        except ApiError as exc:
            # Export failed — surface the error; stored history is unchanged
            # because export is read-only (Req 10.8).
            if exc.status_code == 404:
                st.error("Export is not available from the backend yet.")
            else:
                st.error(f"Export failed: {exc.message}")
            return

        csv_bytes = _coerce_csv_bytes(payload)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name=f"intelliknow_analytics_{stamp}.csv",
            mime="text/csv",
            key="analytics_export_download",
        )


def _coerce_csv_bytes(payload: Any) -> bytes:
    """Coerce an export payload into CSV bytes for the download button.

    The export endpoint returns CSV text; :class:`ApiClient` hands it back as a
    string (JSON parse falls through to text). A dict/other payload is rendered
    via ``str`` as a last resort.

    Args:
        payload: The value returned by the export GET.

    Returns:
        UTF-8 encoded CSV bytes.
    """
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return str(payload).encode("utf-8")


def _main() -> None:
    """Render the Analytics screen (Req 7.7, 10.2–10.5, 10.7, 10.8)."""
    import streamlit as st

    st.set_page_config(page_title="IntelliKnow KMS — Analytics", page_icon="📊")
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Analytics", module="analytics")

    client = ApiClient()

    # Intent_Spaces power the filters, intent labels, accuracy rows, and the
    # verification control. Degrade gracefully if the endpoint is absent.
    spaces_data, spaces_error = _fetch(client, SPACES_PATH)
    space_names = normalize_spaces(spaces_data)
    if spaces_error is not None:
        st.warning(f"Intent_Spaces could not be loaded: {spaces_error}")

    start_iso, end_iso, space_ids = _render_filters(space_names)

    _render_history(client, space_names, start_iso, end_iso, space_ids)
    _render_usage_metrics(client, start_iso, end_iso)
    _render_knowledge_gaps(client, start_iso, end_iso)

    col_left, col_right = st.columns(2)
    with col_left:
        _render_accuracy(client, space_names)
    with col_right:
        _render_feedback_summary(client)
        render_metric_card(
            "Intent Spaces",
            len(space_names),
            module="analytics",
            help_text="Configured Intent_Spaces available for filtering.",
        )

    _render_export(client, start_iso, end_iso, space_ids)


if __name__ == "__main__":
    _main()
