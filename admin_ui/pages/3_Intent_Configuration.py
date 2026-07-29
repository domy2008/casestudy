"""Admin UI — Intent Configuration screen (task 18.5).

This is the third screen of Streamlit's built-in multipage app. It lets the
Admin manage Intent_Spaces and the global Confidence_Threshold that governs
query routing:

* **Intent_Space cards** — each space renders as a card showing its name,
  description, associated document count, and classification accuracy rate
  (or ``N/A`` when no queries have been classified into it) (Req 6.4).
* **Create / edit / delete** — with per-field validation errors surfaced
  inline: a name of 1–50 characters, a description of at most 500 characters
  (Req 6.2), case-insensitive name-uniqueness rejection reported by the backend
  (Req 6.6), and General_Space deletion rejection (Req 6.7).
* **Keyword management** — up to 50 keywords, each 1–50 characters, used to
  guide classification (Req 6.5).
* **Deletion confirmation** — deleting a space with associated documents
  prompts for confirmation and notes that its documents will be reassigned to
  the General_Space (Req 6.3); cancelling leaves everything unchanged (Req 6.8).
* **Confidence_Threshold control** — a 0–100 configuration with range
  validation and a default of 70; out-of-range submissions are rejected with a
  validation error and the prior value retained (Req 7.4, 7.9).

The screen talks to the backend admin REST API through the shared
:class:`~admin_ui.ui.components.ApiClient`:
``GET/POST/PUT/DELETE /spaces`` (with keywords) and
``GET/PUT /settings/confidence-threshold``.

Import-safety and testability
-----------------------------
All decision logic lives in **pure helpers** (:func:`normalize_keywords`,
:func:`validate_space_form`, :func:`validate_confidence_threshold`,
:func:`format_accuracy`, :func:`format_document_count`, :func:`space_payload`,
and the resilient ``space_*``/``extract_threshold`` extractors) that depend only
on the standard library, so they import and unit-test **without a running
Streamlit server**. Every ``streamlit`` call is confined to ``_main`` and the
``render_*`` helpers, which import ``streamlit`` lazily and only run under
``__main__``. Importing this module therefore never requires a Streamlit
runtime.
"""

from __future__ import annotations

from collections.abc import Iterable
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
SCREEN_KEY = "intent_configuration"

#: Module identifier used to pick the purple accent colour (Req 9.4).
MODULE = "intent_configuration"

# --- Validation bounds (Req 6.2, 6.5, 7.4, 7.9) ---------------------------

#: Minimum / maximum Intent_Space name length in characters (Req 6.2).
SPACE_NAME_MIN = 1
SPACE_NAME_MAX = 50

#: Maximum Intent_Space description length in characters (Req 6.2).
DESCRIPTION_MAX = 500

#: Maximum number of keywords per Intent_Space (Req 6.5).
KEYWORDS_MAX = 50

#: Minimum / maximum length of a single keyword in characters (Req 6.5).
KEYWORD_MIN = 1
KEYWORD_MAX = 50

#: Confidence_Threshold bounds and default (Req 7.4, 7.9).
THRESHOLD_MIN = 0
THRESHOLD_MAX = 100
THRESHOLD_DEFAULT = 70


# ---------------------------------------------------------------------------
# Pure helpers (server-free, unit-testable)
# ---------------------------------------------------------------------------


def normalize_keywords(raw: str | Iterable[str] | None) -> list[str]:
    """Parse free-form keyword input into a clean, de-duplicated list.

    Accepts either the raw multi-line/comma text from a text area or an
    iterable of strings. Splits on newlines and commas, strips surrounding
    whitespace from each token, drops empty tokens, and removes case-insensitive
    duplicates while preserving first-seen order.

    Args:
        raw: The keyword text (newline- and/or comma-separated) or an iterable
            of keyword strings, or ``None``.

    Returns:
        The cleaned keyword list (order preserved, duplicates removed).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens: list[str] = []
        for line in raw.replace(",", "\n").splitlines():
            tokens.append(line)
    else:
        tokens = []
        for item in raw:
            tokens.extend(str(item).replace(",", "\n").splitlines())

    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        kw = token.strip()
        if not kw:
            continue
        lowered = kw.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(kw)
    return cleaned


def validate_space_form(
    name: str,
    description: str,
    keywords: Iterable[str] | None,
) -> list[str]:
    """Validate an Intent_Space create/edit submission (Req 6.2, 6.5).

    Applies the client-side rules that mirror the backend contract so the Admin
    gets immediate, specific feedback before a request is sent:

    * name: 1–50 characters after stripping surrounding whitespace,
    * description: at most 500 characters,
    * keywords: at most 50, each 1–50 characters.

    Case-insensitive name-uniqueness (Req 6.6) is enforced by the backend and
    surfaced separately via :class:`~admin_ui.ui.components.ApiError`; it is not
    checked here because the UI does not own the authoritative space list.

    Args:
        name: The proposed space name.
        description: The proposed description.
        keywords: The already-normalized keyword list (see
            :func:`normalize_keywords`), or ``None``.

    Returns:
        A list of human-readable error messages; empty when the form is valid.
    """
    errors: list[str] = []

    stripped_name = (name or "").strip()
    if len(stripped_name) < SPACE_NAME_MIN:
        errors.append("Name is required.")
    elif len(stripped_name) > SPACE_NAME_MAX:
        errors.append(
            f"Name must be at most {SPACE_NAME_MAX} characters "
            f"(got {len(stripped_name)})."
        )

    if len(description or "") > DESCRIPTION_MAX:
        errors.append(
            f"Description must be at most {DESCRIPTION_MAX} characters "
            f"(got {len(description or '')})."
        )

    kw_list = list(keywords or [])
    if len(kw_list) > KEYWORDS_MAX:
        errors.append(
            f"At most {KEYWORDS_MAX} keywords are allowed (got {len(kw_list)})."
        )
    for kw in kw_list:
        length = len(kw)
        if length < KEYWORD_MIN or length > KEYWORD_MAX:
            errors.append(
                f"Keyword {kw!r} must be between {KEYWORD_MIN} and "
                f"{KEYWORD_MAX} characters (got {length})."
            )

    return errors


def validate_confidence_threshold(raw: Any) -> tuple[int | None, str | None]:
    """Validate a Confidence_Threshold submission (Req 7.4, 7.9).

    Accepts only integers in the inclusive range 0–100. Non-numeric or
    out-of-range input is rejected with a range error so the caller can retain
    the previously configured value and display the message (Req 7.9).

    Args:
        raw: The submitted threshold value (int, float with no fractional part,
            or a numeric string).

    Returns:
        A ``(value, error)`` pair. On success ``value`` is the accepted integer
        and ``error`` is ``None``; on failure ``value`` is ``None`` and
        ``error`` is a human-readable range message.
    """
    range_error = (
        f"Confidence threshold must be a whole number between "
        f"{THRESHOLD_MIN} and {THRESHOLD_MAX}."
    )
    if isinstance(raw, bool):  # bool is an int subclass; reject explicitly.
        return None, range_error
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None, range_error
        value = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = int(text)
        except ValueError:
            return None, range_error
    else:
        return None, range_error

    if value < THRESHOLD_MIN or value > THRESHOLD_MAX:
        return None, range_error
    return value, None


def format_accuracy(value: Any) -> str:
    """Format a classification accuracy rate for display (Req 6.4).

    A ``None`` value (no queries classified into the space) renders as ``N/A``.
    A numeric value is rendered as a whole-percent string (e.g. ``72%``); the
    value is treated as an already-computed percentage in the range 0–100 and
    rounded to the nearest whole percent.

    Args:
        value: The accuracy rate as a percentage (0–100), or ``None``.

    Returns:
        ``"N/A"`` or a ``"<n>%"`` string.
    """
    if value is None:
        return "N/A"
    try:
        return f"{round(float(value))}%"
    except (TypeError, ValueError):
        return "N/A"


def format_document_count(value: Any) -> int:
    """Coerce an associated-document count to a non-negative integer (Req 6.4).

    Tolerates missing or malformed values from the backend by falling back to
    zero, so a single odd field never breaks the card layout.

    Args:
        value: The raw document-count value from the space payload.

    Returns:
        The count as a non-negative ``int`` (``0`` on missing/invalid input).
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count >= 0 else 0


def space_payload(
    name: str,
    description: str,
    keywords: Iterable[str] | None,
) -> dict[str, Any]:
    """Build the JSON body for a ``POST``/``PUT`` ``/spaces`` request.

    Trims the name, passes the description through unchanged, and includes the
    normalized keyword list so the backend stores keywords alongside the space
    (Req 6.5).

    Args:
        name: The space name.
        description: The space description.
        keywords: The normalized keyword list, or ``None``.

    Returns:
        A dict with ``name``, ``description``, and ``keywords`` keys.
    """
    return {
        "name": (name or "").strip(),
        "description": description or "",
        "keywords": list(keywords or []),
    }


def _first_present(space: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-``None`` value among ``keys``."""
    for key in keys:
        if key in space and space[key] is not None:
            return space[key]
    return None


def space_document_count(space: dict[str, Any]) -> int:
    """Extract a space's associated-document count, resilient to field naming.

    Args:
        space: A space payload from the backend.

    Returns:
        The document count as a non-negative integer (``0`` when absent).
    """
    return format_document_count(
        _first_present(space, "document_count", "documents", "doc_count")
    )


def space_accuracy(space: dict[str, Any]) -> Any:
    """Extract a space's classification accuracy rate (Req 6.4).

    Args:
        space: A space payload from the backend.

    Returns:
        The accuracy percentage (0–100) or ``None`` when the backend reports no
        classified queries.
    """
    return _first_present(space, "accuracy", "accuracy_rate", "classification_accuracy")


def space_keywords(space: dict[str, Any]) -> list[str]:
    """Extract a space's keyword list, tolerant of shape variations.

    Args:
        space: A space payload from the backend.

    Returns:
        The keyword list (empty when absent or malformed).
    """
    raw = _first_present(space, "keywords")
    if isinstance(raw, list):
        return [str(kw) for kw in raw]
    if isinstance(raw, str):
        return normalize_keywords(raw)
    return []


def is_general_space(space: dict[str, Any]) -> bool:
    """Return whether a space is the undeletable General_Space (Req 6.7).

    Args:
        space: A space payload from the backend.

    Returns:
        ``True`` when the space is flagged as the General_Space.
    """
    flag = _first_present(space, "is_general")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, (int, float)):
        return bool(flag)
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes"}
    name = _first_present(space, "name")
    return isinstance(name, str) and name.strip().lower() == "general"


def extract_threshold(payload: Any) -> int:
    """Read the Confidence_Threshold value from a settings response (Req 7.4).

    Understands a few plausible response shapes — a bare number, or an object
    keyed by ``value``/``confidence_threshold``/``threshold`` — and falls back
    to the default of 70 when the value is missing or unparseable.

    Args:
        payload: The parsed ``GET /settings/confidence-threshold`` response.

    Returns:
        The threshold as an integer in 0–100, or :data:`THRESHOLD_DEFAULT`.
    """
    raw: Any
    if isinstance(payload, dict):
        raw = _first_present(payload, "value", "confidence_threshold", "threshold")
    else:
        raw = payload
    value, error = validate_confidence_threshold(raw)
    if error is not None or value is None:
        return THRESHOLD_DEFAULT
    return value


# ---------------------------------------------------------------------------
# Data access (thin wrappers over the shared ApiClient)
# ---------------------------------------------------------------------------


def fetch_spaces(client: ApiClient) -> list[dict[str, Any]]:
    """Fetch the Intent_Spaces from ``GET /spaces``.

    Args:
        client: The shared backend API client.

    Returns:
        The list of space payloads (an empty list when the backend returns an
        empty or unexpected body).
    """
    data = client.get("/spaces")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        spaces = data.get("spaces")
        if isinstance(spaces, list):
            return spaces
    return []


def fetch_threshold(client: ApiClient) -> int:
    """Fetch the current Confidence_Threshold (Req 7.4).

    Args:
        client: The shared backend API client.

    Returns:
        The configured threshold, or the default of 70 on an empty response.
    """
    return extract_threshold(client.get("/settings/confidence-threshold"))


# ---------------------------------------------------------------------------
# Streamlit rendering (only invoked under __main__)
# ---------------------------------------------------------------------------


def _render_space_card(space: dict[str, Any]) -> None:
    """Render a single Intent_Space as a metric card (Req 6.4)."""
    name = str(_first_present(space, "name") or "(unnamed)")
    description = str(_first_present(space, "description") or "")
    count = space_document_count(space)
    accuracy = format_accuracy(space_accuracy(space))
    keywords = space_keywords(space)

    help_bits = [description] if description else []
    help_bits.append(f"Keywords: {', '.join(keywords) if keywords else 'none'}")
    render_metric_card(
        f"{name}  •  {count} docs  •  {accuracy} accuracy",
        name,
        module=MODULE,
        help_text=" — ".join(help_bits),
    )


def _render_space_editor(client: ApiClient, spaces: list[dict[str, Any]]) -> None:
    """Render the create/edit/delete controls for Intent_Spaces."""
    import streamlit as st

    section_header("Create or edit an Intent Space", module=MODULE)

    labels = ["➕ New space"] + [
        str(_first_present(s, "name") or f"Space {i}") for i, s in enumerate(spaces)
    ]
    choice = st.selectbox("Select a space", options=range(len(labels)),
                          format_func=lambda i: labels[i])
    editing = choice > 0
    current = spaces[choice - 1] if editing else {}

    with st.form("space_form"):
        name = st.text_input(
            "Name", value=str(_first_present(current, "name") or ""),
            max_chars=SPACE_NAME_MAX,
            help=f"{SPACE_NAME_MIN}–{SPACE_NAME_MAX} characters.",
        )
        description = st.text_area(
            "Description",
            value=str(_first_present(current, "description") or ""),
            max_chars=DESCRIPTION_MAX,
            help=f"At most {DESCRIPTION_MAX} characters.",
        )
        keywords_raw = st.text_area(
            "Keywords (one per line or comma-separated)",
            value="\n".join(space_keywords(current)),
            help=f"Up to {KEYWORDS_MAX} keywords, each {KEYWORD_MIN}–"
                 f"{KEYWORD_MAX} characters.",
        )
        submitted = st.form_submit_button("Save space")

    if submitted:
        keywords = normalize_keywords(keywords_raw)
        errors = validate_space_form(name, description, keywords)
        if errors:
            for message in errors:
                st.error(message)
            return
        payload = space_payload(name, description, keywords)
        try:
            if editing:
                client.put(f"/spaces/{_first_present(current, 'id')}", json=payload)
                st.success(f"Updated “{payload['name']}”.")
            else:
                client.post("/spaces", json=payload)
                st.success(f"Created “{payload['name']}”.")
        except ApiError as exc:
            # Case-insensitive name-uniqueness (Req 6.6) and any other backend
            # rejection is surfaced here without changing local state.
            st.error(exc.message)

    if editing:
        _render_delete_control(client, current)


def _render_delete_control(client: ApiClient, space: dict[str, Any]) -> None:
    """Render the deletion control with confirmation + reassignment notice."""
    import streamlit as st

    section_header("Delete this space", module=MODULE)
    name = str(_first_present(space, "name") or "")

    if is_general_space(space):
        # General_Space is undeletable (Req 6.7).
        st.info("The General space cannot be deleted.")
        return

    count = space_document_count(space)
    if count:
        st.warning(
            f"Deleting “{name}” will reassign its {count} document(s) to the "
            f"General space before the space is removed."
        )
    confirm = st.checkbox(
        f"I understand — delete “{name}”"
        + (" and reassign its documents to General" if count else ""),
        key=f"confirm_delete_{_first_present(space, 'id')}",
    )
    if st.button("Delete space", disabled=not confirm):
        # Confirmation gate satisfies Req 6.3; leaving the box unchecked (or not
        # pressing the button) leaves everything unchanged (Req 6.8).
        try:
            client.delete(f"/spaces/{_first_present(space, 'id')}")
            st.success(f"Deleted “{name}”.")
        except ApiError as exc:
            st.error(exc.message)


def _render_threshold_control(client: ApiClient) -> None:
    """Render the Confidence_Threshold configuration control (Req 7.4, 7.9)."""
    import streamlit as st

    section_header("Confidence threshold", module=MODULE)
    try:
        current = fetch_threshold(client)
    except ApiError as exc:
        st.error(f"Could not load the confidence threshold: {exc.message}")
        current = THRESHOLD_DEFAULT

    render_metric_card(
        "Current confidence threshold",
        f"{current}%",
        module=MODULE,
        help_text="Applies to all subsequently received queries.",
    )

    with st.form("threshold_form"):
        proposed = st.number_input(
            "Confidence threshold (%)",
            min_value=THRESHOLD_MIN,
            max_value=THRESHOLD_MAX,
            value=current,
            step=1,
            help=f"A whole number between {THRESHOLD_MIN} and {THRESHOLD_MAX}; "
                 f"default {THRESHOLD_DEFAULT}.",
        )
        submitted = st.form_submit_button("Save threshold")

    if submitted:
        value, error = validate_confidence_threshold(proposed)
        if error is not None:
            # Reject and retain the previous value (Req 7.9).
            st.error(error)
            return
        try:
            client.put(
                "/settings/confidence-threshold", json={"value": value}
            )
            st.success(f"Confidence threshold set to {value}%.")
        except ApiError as exc:
            st.error(exc.message)


def _main() -> None:
    """Render the Intent Configuration screen."""
    import streamlit as st

    st.set_page_config(
        page_title="IntelliKnow KMS — Intent Configuration", page_icon="🎯"
    )
    inject_base_css()
    render_sidebar_nav(SCREEN_KEY)
    section_header("Intent Configuration", module=MODULE)

    client = ApiClient()

    section_header("Intent Spaces", module=MODULE)
    try:
        spaces = fetch_spaces(client)
    except ApiError as exc:
        st.error(f"Could not load intent spaces: {exc.message}")
        spaces = []

    if spaces:
        for space in spaces:
            _render_space_card(space)
    else:
        st.caption("No intent spaces to display.")

    _render_space_editor(client, spaces)
    _render_threshold_control(client)


if __name__ == "__main__":
    _main()
