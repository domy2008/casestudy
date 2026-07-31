"""Admin UI Dashboard (Home) — multipage entry point (task 18.2).

This is the landing page of Streamlit's built-in multipage app: ``Home.py`` at
the app root plus the screens under ``pages/`` give the sidebar its five
entries (Req 9.1). The Dashboard shows three groups of summary cards
(Req 9.5):

* **Integration status** — the connection state of each Frontend_Tool.
* **Document counts by status** — how many documents sit in each of
  ``Pending`` / ``Processed`` / ``Error``.
* **Query activity (last 24 hours)** — total / successful / failed queries in
  the most recent 24-hour window.

Each card group fetches its data **independently** through the shared
:class:`~admin_ui.ui.components.ApiClient` and renders an inline error state in
its own card when the fetch (or the backend's per-section data) is unavailable,
while the other card groups keep rendering whatever data they did get
(Req 9.7). Fetching per group — rather than sharing a single request — is what
gives each card true failure isolation.

Import-safety and testability
------------------------------
All the parsing logic (:func:`parse_integration_status`,
:func:`parse_document_counts`, :func:`parse_query_activity`) is pure: it takes
the decoded ``/dashboard/summary`` payload and returns display rows, with no
Streamlit dependency, so it is unit-testable headlessly. Every Streamlit call
lives inside ``_main`` (and the private ``_render_*`` helpers it invokes) which
runs only under ``__main__``; ``streamlit`` is imported lazily inside those
helpers. Importing this module therefore never requires a Streamlit runtime
(``python -c "import admin_ui.Home"`` works headless).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Ensure the project root is importable when Streamlit launches this script
# directly (`streamlit run admin_ui/Home.py`), which puts the script's own
# directory on sys.path rather than the project root, breaking `admin_ui.*`
# imports. Walk up until the directory containing the ``admin_ui`` package.
_root = _os.path.abspath(__file__)
for _ in range(4):
    _root = _os.path.dirname(_root)
    if _os.path.isdir(_os.path.join(_root, "admin_ui")):
        break
if _root not in _sys.path:
    _sys.path.insert(0, _root)

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
SCREEN_KEY = "dashboard"

#: Backend endpoint that aggregates the Dashboard metrics, tolerant of
#: per-section partial failure (Req 9.5/9.7).
DASHBOARD_SUMMARY_PATH = "/dashboard/summary"

#: The document statuses the Dashboard always reports a count for, so a status
#: with zero documents still shows up as ``0`` rather than vanishing (Req 9.5).
DOCUMENT_STATUSES: tuple[str, ...] = ("Pending", "Processed", "Error")

#: CloudWatch monitoring dashboard for this deployment (latency p95, error
#: rate, backend health, alarm states). Provisioned by
#: ``deploy/setup_cloudwatch_alarms.py``; override per environment via the
#: ``CLOUDWATCH_DASHBOARD_URL`` environment variable.
CLOUDWATCH_DASHBOARD_URL: str = _os.environ.get(
    "CLOUDWATCH_DASHBOARD_URL",
    "https://cn-north-1.console.amazonaws.cn/cloudwatch/home"
    "?region=cn-north-1#dashboards:name=IntelliKnow-KMS",
)


class DashboardDataError(ValueError):
    """Raised when a ``/dashboard/summary`` section is missing or malformed.

    Cards catch this alongside :class:`ApiError` so a data-shape problem (or a
    backend-signalled per-section error) surfaces as an inline card error
    without taking down the other cards (Req 9.7).
    """


# ---------------------------------------------------------------------------
# Pure parsing helpers (no Streamlit) — unit-tested headlessly
# ---------------------------------------------------------------------------


def _section(summary: dict[str, Any], *aliases: str) -> Any:
    """Return the first present, non-null section value among ``aliases``.

    The backend contract for ``/dashboard/summary`` is small but its exact key
    spelling is owned by the API layer; accepting a few reasonable aliases
    keeps this screen resilient to harmless naming differences.

    Args:
        summary: The decoded ``/dashboard/summary`` object.
        *aliases: Candidate keys, in priority order.

    Returns:
        The value of the first alias that is present and not ``None``.

    Raises:
        DashboardDataError: If none of the aliases are present, or the matched
            section is a backend-signalled error marker (a mapping carrying a
            non-empty ``"error"`` string).
    """
    for key in aliases:
        if key in summary and summary[key] is not None:
            value = summary[key]
            if isinstance(value, dict):
                error = value.get("error")
                if isinstance(error, str) and error.strip():
                    raise DashboardDataError(error.strip())
            return value
    raise DashboardDataError(f"No data for {aliases[0]!r}.")


def _as_int(value: Any) -> int:
    """Coerce a count to ``int``, defaulting to ``0`` for unusable values.

    Args:
        value: A count-like value (``int``, numeric string, ...).

    Returns:
        The integer value, or ``0`` when it cannot be interpreted as one.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _status_text(value: Any) -> str:
    """Render an integration status value as a display string.

    Args:
        value: Either a plain status string, or a mapping carrying a
            ``"status"`` key.

    Returns:
        A human-readable status string (``"Unknown"`` when absent).
    """
    if isinstance(value, dict):
        return str(value.get("status", "Unknown"))
    if value is None:
        return "Unknown"
    return str(value)


def parse_integration_status(summary: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract per-tool integration status rows from the summary (Req 9.5).

    Accepts either a mapping (``{"telegram": "Connected", ...}``) or a list of
    objects (``[{"tool": "telegram", "status": "Connected"}, ...]``).

    Args:
        summary: The decoded ``/dashboard/summary`` object.

    Returns:
        A list of ``(tool, status)`` pairs (possibly empty when no integrations
        are configured).

    Raises:
        DashboardDataError: If the integration section is absent or malformed.
    """
    section = _section(summary, "integrations", "integration_status", "integration")
    rows: list[tuple[str, str]] = []
    if isinstance(section, dict):
        for tool, status in section.items():
            rows.append((str(tool), _status_text(status)))
    elif isinstance(section, list):
        for item in section:
            if not isinstance(item, dict):
                raise DashboardDataError("Malformed integration status entry.")
            tool = item.get("tool") or item.get("name")
            if not tool:
                raise DashboardDataError("Integration status entry is missing a tool.")
            rows.append((str(tool), _status_text(item.get("status"))))
    else:
        raise DashboardDataError("Integration status has an unexpected shape.")
    return rows


def parse_document_counts(summary: dict[str, Any]) -> list[tuple[str, int]]:
    """Extract document counts for every status from the summary (Req 9.5).

    The result always covers :data:`DOCUMENT_STATUSES` in that order, defaulting
    any status the backend omitted to ``0`` so the Admin sees a complete
    picture.

    Args:
        summary: The decoded ``/dashboard/summary`` object.

    Returns:
        A list of ``(status, count)`` pairs, one per :data:`DOCUMENT_STATUSES`.

    Raises:
        DashboardDataError: If the document section is absent or not a mapping.
    """
    section = _section(
        summary, "documents", "documents_by_status", "document_counts"
    )
    if not isinstance(section, dict):
        raise DashboardDataError("Document counts have an unexpected shape.")
    lowered = {str(key).lower(): val for key, val in section.items()}
    return [(status, _as_int(lowered.get(status.lower(), 0))) for status in DOCUMENT_STATUSES]


def parse_query_activity(summary: dict[str, Any]) -> list[tuple[str, int]]:
    """Extract the last-24h query activity rows from the summary (Req 9.5).

    Accepts a bare integer (treated as the 24h total) or a mapping with any of
    ``total`` / ``success`` (``successful``) / ``failed`` (``failure``).

    Args:
        summary: The decoded ``/dashboard/summary`` object.

    Returns:
        A non-empty list of ``(label, value)`` pairs.

    Raises:
        DashboardDataError: If the section is absent, malformed, or carries no
            recognizable fields.
    """
    section = _section(
        summary, "queries_24h", "query_activity", "queries", "recent_queries"
    )
    if isinstance(section, (int, float)) and not isinstance(section, bool):
        return [("Total (24h)", _as_int(section))]
    if not isinstance(section, dict):
        raise DashboardDataError("Query activity has an unexpected shape.")

    lowered = {str(key).lower(): val for key, val in section.items()}
    rows: list[tuple[str, int]] = []
    if "total" in lowered:
        rows.append(("Total (24h)", _as_int(lowered["total"])))
    if "success" in lowered or "successful" in lowered:
        raw = lowered.get("success", lowered.get("successful"))
        rows.append(("Successful", _as_int(raw)))
    if "failed" in lowered or "failure" in lowered:
        raw = lowered.get("failed", lowered.get("failure"))
        rows.append(("Failed", _as_int(raw)))
    if not rows:
        raise DashboardDataError("Query activity has no recognizable fields.")
    return rows


# ---------------------------------------------------------------------------
# Streamlit rendering (import-safe: only touched inside _main)
# ---------------------------------------------------------------------------


def _fetch_summary(client: ApiClient) -> dict[str, Any]:
    """Fetch and validate the ``/dashboard/summary`` payload.

    Args:
        client: The shared backend API client.

    Returns:
        The decoded summary object.

    Raises:
        ApiError: On a non-2xx response or a transport-level failure.
        DashboardDataError: If the payload is not a JSON object.
    """
    data = client.get(DASHBOARD_SUMMARY_PATH)
    if not isinstance(data, dict):
        raise DashboardDataError("Dashboard summary response was not an object.")
    return data


def _render_error_card(title: str, exc: Exception, *, module: str | None) -> None:
    """Render an inline error state for one card group (Req 9.7).

    Args:
        title: The card-group title.
        exc: The failure to summarize for the Admin.
        module: Accent module for the card, if any.
    """
    render_metric_card(
        title,
        "Unavailable",
        module=module,
        help_text=f"\u26a0\ufe0f {exc}",
    )


def _render_integration_status(client: ApiClient) -> None:
    """Render the integration-status card group, isolating its failures."""
    import streamlit as st

    section_header("Integration Status", module="frontend_integration")
    try:
        rows = parse_integration_status(_fetch_summary(client))
    except (ApiError, DashboardDataError) as exc:
        _render_error_card("Integration Status", exc, module="frontend_integration")
        return
    if not rows:
        render_metric_card(
            "Integrations",
            "None configured",
            module="frontend_integration",
            help_text="No Frontend_Tool integrations are configured yet.",
        )
        return
    columns = st.columns(len(rows))
    for column, (tool, status) in zip(columns, rows):
        with column:
            render_metric_card(
                tool.replace("_", " ").title(),
                status,
                module="frontend_integration",
                help_text="Current connection status",
            )


def _render_document_counts(client: ApiClient) -> None:
    """Render the document-counts-by-status card group, isolating failures."""
    import streamlit as st

    section_header("Documents by Status", module="kb_management")
    try:
        rows = parse_document_counts(_fetch_summary(client))
    except (ApiError, DashboardDataError) as exc:
        _render_error_card("Documents by Status", exc, module="kb_management")
        return
    columns = st.columns(len(rows))
    for column, (status, count) in zip(columns, rows):
        with column:
            render_metric_card(
                status,
                count,
                module="kb_management",
                help_text="Documents in this status",
            )


def _render_query_activity(client: ApiClient) -> None:
    """Render the last-24h query-activity card group, isolating failures."""
    import streamlit as st

    section_header("Query Activity (last 24h)", module="dashboard")
    try:
        rows = parse_query_activity(_fetch_summary(client))
    except (ApiError, DashboardDataError) as exc:
        _render_error_card("Query Activity (last 24h)", exc, module="dashboard")
        return
    columns = st.columns(len(rows))
    for column, (label, value) in zip(columns, rows):
        with column:
            render_metric_card(
                label,
                value,
                module="dashboard",
                help_text="Most recent 24-hour period",
            )


def _main() -> None:
    """Render the Dashboard screen with its three independent card groups."""
    import streamlit as st

    st.set_page_config(page_title="IntelliKnow KMS — Dashboard", page_icon="🏠")
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Dashboard", module="dashboard")
    st.caption(
        "Live summary of integrations, the knowledge base, and recent query "
        "activity. Each card loads independently."
    )

    client = ApiClient()
    try:
        # Each group fetches independently so one failing group still leaves the
        # others rendering their own data or their own inline error (Req 9.7).
        _render_integration_status(client)
        _render_document_counts(client)
        _render_query_activity(client)
    finally:
        client.close()

    section_header("Monitoring", module="dashboard")
    st.markdown(
        "📈 Infrastructure metrics and alarms (query latency p95, error "
        "rate, backend health) live in the "
        f"[CloudWatch dashboard]({CLOUDWATCH_DASHBOARD_URL}) "
        "(AWS console sign-in required)."
    )


if __name__ == "__main__":
    _main()
