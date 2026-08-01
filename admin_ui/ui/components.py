"""Shared Streamlit helpers implementing the Admin_UI visual system.

This module is the single source of truth for how every admin screen looks and
how it talks to the backend. It provides four things (Req 9.1–9.4):

1. **Visual system** — a neutral base colour scheme with AIA-brand accent
   colours (graded AIA reds across Frontend Integration, KB Management and
   Intent Configuration), injected as CSS through ``st.markdown``, and
   a card layout with 12 px rounded corners and 16 px padding (Req 9.3, 9.4).
2. **Reusable widgets** — a labelled metric/status card and an accent-coloured
   section header (Req 9.3, 9.4).
3. **Navigation** — a sidebar helper that lists all five screens of the
   built-in Streamlit multipage app and highlights the active one
   (Req 9.1, 9.2).
4. **API client** — :class:`ApiClient`, a thin wrapper over ``httpx`` that
   calls the backend FastAPI admin REST API (base URL from the ``KMS_API_BASE``
   environment variable, defaulting to ``http://localhost:8000``) with
   ``GET``/``POST``/``PUT``/``DELETE`` verbs, JSON handling, and error surfacing
   via :class:`ApiError`.

Import-safety and testability
-----------------------------
The **pure helpers** — :func:`accent_color`, :func:`normalize_module`,
:func:`card_html`, :func:`build_url`, :func:`handle_response`, and
:func:`api_base_url` — depend only on the standard library and ``httpx`` data
types, so they are importable and unit-testable **without a running Streamlit
server**. The ``render_*`` / ``inject_base_css`` functions are the only ones
that touch ``streamlit``; they import it lazily inside the function body so this
module imports cleanly in headless contexts (including the test suite).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "ACCENT_COLORS",
    "DEFAULT_ACCENT",
    "DEFAULT_API_BASE",
    "NEUTRAL",
    "SCREENS",
    "ApiClient",
    "ApiError",
    "LOGOUT_PATH",
    "Screen",
    "accent_color",
    "api_base_url",
    "build_url",
    "card_html",
    "handle_response",
    "inject_base_css",
    "normalize_module",
    "render_metric_card",
    "render_sidebar_nav",
    "section_header",
]


# ---------------------------------------------------------------------------
# Visual system: neutral base scheme + per-module accent colours (Req 9.4)
# ---------------------------------------------------------------------------

#: Neutral base palette shared by every screen. Cards sit on a light neutral
#: surface with subtle borders and dark slate text, so module accent colours
#: are the only strong hues on the page (Req 9.4).
NEUTRAL: dict[str, str] = {
    "surface": "#ffffff",  # card background
    "canvas": "#f8fafc",  # page background behind cards
    "border": "#e2e8f0",  # hairline card border
    "text": "#0f172a",  # primary text
    "muted": "#64748b",  # secondary/label text
}

#: AIA brand palette — demo skin for the AIA presentation. ``AIA_RED`` is the
#: primary brand red; the darker variants differentiate the three functional
#: modules while keeping every accent on-brand (supersedes the original
#: blue/green/purple scheme of Req 9.4 for this customer demo).
AIA_RED: str = "#D31145"
AIA_RED_DARK: str = "#A6093D"
AIA_BURGUNDY: str = "#7A0930"

#: AIA brand tagline shown under the sidebar wordmark.
AIA_TAGLINE: str = "HEALTHIER, LONGER, BETTER LIVES"

#: Inline SVG wordmark evoking the AIA logo (bold italic red letters over a
#: red arc). Rendered as markup so no binary asset needs shipping.
AIA_LOGO_SVG: str = (
    '<svg width="110" height="46" viewBox="0 0 110 46" role="img" '
    'aria-label="AIA">'
    '<text x="2" y="30" font-family="Arial Black, Arial, sans-serif" '
    f'font-size="30" font-style="italic" font-weight="900" fill="{AIA_RED}">'
    "AIA</text>"
    f'<path d="M4 37 Q 55 47 106 33" stroke="{AIA_RED}" stroke-width="3.5" '
    'fill="none" stroke-linecap="round"/></svg>'
)

#: Brand-coloured inline SVG glyphs for each Frontend_Tool channel, keyed by the
#: canonical tool identifier (see :func:`normalize_module`). Rendered as inline
#: markup so the portal ships no binary icon assets. Each glyph is sized to sit
#: inline beside a card label (18 px).
CHANNEL_ICONS: dict[str, str] = {
    "telegram": (
        '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">'
        '<circle cx="12" cy="12" r="12" fill="#29A9EB"/>'
        '<path d="M5.5 11.8 16.4 7.3c.6-.2 1.1.2.9.9l-1.9 8.6c-.1.6-.5.7-1 .4'
        'l-2.8-2-1.3 1.3c-.2.2-.4.3-.7.1l.3-3 5.2-4.7c.2-.2-.1-.3-.3-.2'
        'l-6.4 4-2.7-.9c-.6-.2-.6-.6.1-.9z" fill="#fff"/></svg>'
    ),
    "whatsapp": (
        '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">'
        '<circle cx="12" cy="12" r="12" fill="#25D366"/>'
        '<path d="M12 5.6a6.3 6.3 0 0 0-5.4 9.5L5.6 18.4l3.4-.9A6.3 6.3 0 1 0 '
        '12 5.6zm3.7 8.9c-.2.4-.9.8-1.2.8-.3.1-.7.1-1.1 0-.3-.1-.7-.2-1.2-.5'
        '-2.1-.9-3.5-3-3.6-3.2-.1-.1-.9-1.1-.9-2.2s.5-1.5.7-1.7c.2-.2.4-.3'
        '.6-.3h.4c.1 0 .3 0 .5.4l.6 1.5c0 .1.1.2 0 .4l-.3.4-.3.3c-.1.1-.2.2'
        '-.1.4l.6 1c.4.6.9.9 1.3 1.1.2.1.3.1.4 0l.6-.7c.2-.2.3-.2.5-.1l1.4.7'
        'c.2.1.4.2.4.3.1.1.1.5 0 .8z" fill="#fff"/></svg>'
    ),
    "teams": (
        '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">'
        '<circle cx="17" cy="6.2" r="2.6" fill="#5059C9"/>'
        '<rect x="11.5" y="8.6" width="11" height="8.4" rx="1.6" '
        'fill="#5059C9"/>'
        '<circle cx="9.2" cy="5.4" r="3" fill="#7B83EB"/>'
        '<rect x="2.5" y="8.2" width="12" height="9.6" rx="1.8" '
        'fill="#7B83EB"/>'
        '<text x="8.5" y="15.4" font-family="Arial, sans-serif" '
        'font-size="8" font-weight="700" fill="#fff" '
        'text-anchor="middle">T</text></svg>'
    ),
}


def channel_icon(tool: str | None) -> str:
    """Return the inline brand SVG for a Frontend_Tool channel, or ``""``.

    The lookup is case- and separator-insensitive via :func:`normalize_module`,
    so ``"WhatsApp"``, ``"whatsapp"`` and ``"whats_app"`` all resolve. Unknown
    channels return the empty string so callers can fall back gracefully.

    Args:
        tool: The channel/tool label in any casing, or ``None``.

    Returns:
        The inline SVG markup, or ``""`` when the channel has no icon.
    """
    return CHANNEL_ICONS.get(normalize_module(tool), "")


#: Per-module accent colours. Keys are canonical module identifiers produced by
#: :func:`normalize_module`. The three functional modules carry graded AIA-red
#: accents; Dashboard and Analytics fall back to the primary brand red.
ACCENT_COLORS: dict[str, str] = {
    "frontend_integration": AIA_RED,
    "kb_management": AIA_RED_DARK,
    "intent_configuration": AIA_BURGUNDY,
}

#: Accent used for modules without a dedicated colour (Dashboard, Analytics,
#: or any unknown module): the primary AIA brand red.
DEFAULT_ACCENT: str = AIA_RED


def normalize_module(module: str | None) -> str:
    """Reduce a free-form module label to its canonical identifier.

    Accepts any of the ways a module might be referred to — display titles
    ("Frontend Integration"), keys ("frontend_integration"), or hyphenated /
    mixed-case variants — and folds them to the snake_case identifier used as
    the key in :data:`ACCENT_COLORS`.

    Args:
        module: A module label in any casing/spacing, or ``None``.

    Returns:
        The canonical snake_case identifier (e.g. ``"kb_management"``). Empty
        or ``None`` input yields the empty string.
    """
    if not module:
        return ""
    cleaned = module.strip().lower()
    for separator in (" ", "-", "/"):
        cleaned = cleaned.replace(separator, "_")
    # Collapse any accidental repeated underscores.
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def accent_color(module: str | None) -> str:
    """Return the accent colour for a module (Req 9.4).

    Blue for Frontend Integration, green for KB Management, purple for Intent
    Configuration; any other or unrecognised module gets the neutral
    :data:`DEFAULT_ACCENT`. The lookup is case- and separator-insensitive via
    :func:`normalize_module`.

    Args:
        module: The module label in any casing/spacing, or ``None``.

    Returns:
        A hex colour string (e.g. ``"#2563eb"``).
    """
    return ACCENT_COLORS.get(normalize_module(module), DEFAULT_ACCENT)


# ---------------------------------------------------------------------------
# Card layout: 12 px rounded corners, 16 px padding (Req 9.3)
# ---------------------------------------------------------------------------

#: Card corner radius in pixels (Req 9.3).
CARD_RADIUS_PX: int = 12

#: Card inner padding in pixels (Req 9.3).
CARD_PADDING_PX: int = 16

#: Viewport width (px) below which the layout switches to its mobile form:
#: columns stack vertically, padding tightens, and tables scroll horizontally.
MOBILE_BREAKPOINT_PX: int = 640


def card_html(
    body: str,
    *,
    accent: str | None = None,
    radius_px: int = CARD_RADIUS_PX,
    padding_px: int = CARD_PADDING_PX,
) -> str:
    """Build the HTML for a single card following the visual system (Req 9.3).

    The returned markup is a ``<div>`` styled with the neutral surface colour,
    a hairline border, 12 px rounded corners and 16 px padding by default. When
    an ``accent`` colour is supplied it is applied as a coloured left edge so a
    card can be tinted to its owning module without changing its body.

    This is a **pure** function (string in, string out) so it is unit-testable
    without Streamlit.

    Args:
        body: Inner HTML for the card (caller-controlled markup/text).
        accent: Optional accent colour for the left edge; ``None`` for a plain
            neutral card.
        radius_px: Corner radius in pixels (defaults to 12).
        padding_px: Inner padding in pixels (defaults to 16).

    Returns:
        A single ``<div class="ik-card">…</div>`` HTML string.
    """
    styles = [
        f"background:{NEUTRAL['surface']}",
        f"border:1px solid {NEUTRAL['border']}",
        f"border-radius:{radius_px}px",
        f"padding:{padding_px}px",
        f"color:{NEUTRAL['text']}",
    ]
    if accent:
        # A 4 px accent strip on the leading edge ties the card to its module.
        styles.append(f"border-left:4px solid {accent}")
    style_attr = ";".join(styles)
    return f'<div class="ik-card" style="{style_attr}">{body}</div>'


def inject_base_css() -> None:
    """Inject the shared base CSS into the current Streamlit page (Req 9.3/9.4).

    Applies the neutral canvas background and registers the ``.ik-card`` class
    (12 px radius, 16 px padding) plus small typographic helpers used by
    :func:`render_metric_card` and :func:`section_header`. Call this once near
    the top of every screen. Streamlit is imported lazily so importing this
    module never requires a Streamlit runtime.
    """
    import streamlit as st

    css = f"""
    <style>
      .stApp {{ background: {NEUTRAL['canvas']}; }}
      .ik-card {{
        background: {NEUTRAL['surface']};
        border: 1px solid {NEUTRAL['border']};
        border-radius: {CARD_RADIUS_PX}px;
        padding: {CARD_PADDING_PX}px;
        color: {NEUTRAL['text']};
        margin-bottom: 12px;
      }}
      .ik-card .ik-card-label {{
        color: {NEUTRAL['muted']};
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin: 0 0 4px 0;
      }}
      .ik-card .ik-card-label--icon {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}
      .ik-card .ik-card-icon {{
        display: inline-flex;
        align-items: center;
        line-height: 0;
      }}
      .ik-card .ik-card-value {{
        color: {NEUTRAL['text']};
        font-size: 1.6rem;
        font-weight: 600;
        margin: 0;
      }}
      .ik-card .ik-card-help {{
        color: {NEUTRAL['muted']};
        font-size: 0.8rem;
        margin: 4px 0 0 0;
      }}
      .ik-section-header {{
        border-left: 4px solid {DEFAULT_ACCENT};
        padding-left: 10px;
        margin: 8px 0 12px 0;
        font-size: 1.15rem;
        font-weight: 600;
        color: {NEUTRAL['text']};
      }}
      /* Mobile responsiveness: on narrow viewports, stack side-by-side
         columns vertically, tighten page padding, scale down card values,
         and let wide tables scroll horizontally instead of clipping. */
      @media (max-width: {MOBILE_BREAKPOINT_PX}px) {{
        .block-container {{
          padding-left: 1rem;
          padding-right: 1rem;
          padding-top: 2.5rem;
        }}
        div[data-testid="stHorizontalBlock"] {{
          flex-direction: column;
        }}
        div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
          width: 100% !important;
          flex: 1 1 100% !important;
          min-width: 100% !important;
        }}
        .ik-card-value {{ font-size: 1.3rem; }}
        div[data-testid="stDataFrame"],
        div[data-testid="stTable"] {{
          overflow-x: auto;
        }}
      }}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def render_metric_card(
    label: str,
    value: Any,
    *,
    module: str | None = None,
    help_text: str | None = None,
    icon: str | None = None,
) -> None:
    """Render a labelled metric/status card on the current page (Req 9.3/9.4).

    Produces a neutral card (12 px radius, 16 px padding) showing an uppercase
    label, a prominent value, and optional helper text, tinted with the owning
    module's accent edge when ``module`` is given.

    Args:
        label: Short caption shown above the value (e.g. ``"Documents"``).
        value: The metric/status value; rendered via ``str()``.
        module: Optional module label used to pick the accent colour.
        help_text: Optional secondary line beneath the value.
        icon: Optional inline SVG/emoji markup shown beside the label (e.g. a
            channel brand glyph from :func:`channel_icon`).
    """
    import streamlit as st

    if icon:
        label_html = (
            f'<p class="ik-card-label ik-card-label--icon">'
            f'<span class="ik-card-icon">{icon}</span>{label}</p>'
        )
    else:
        label_html = f'<p class="ik-card-label">{label}</p>'
    body_parts = [
        label_html,
        f'<p class="ik-card-value">{value}</p>',
    ]
    if help_text:
        body_parts.append(f'<p class="ik-card-help">{help_text}</p>')
    accent = accent_color(module) if module else None
    st.markdown(
        card_html("".join(body_parts), accent=accent),
        unsafe_allow_html=True,
    )


def section_header(
    title: str, module: str | None = None, *, icon: str | None = None
) -> None:
    """Render an accent-coloured section header on the current page (Req 9.4).

    The header shows ``title`` with a left border tinted to the module's accent
    colour, giving each module a consistent visual signature. An optional
    ``icon`` (inline SVG/emoji) is shown just before the title.

    Args:
        title: The header text.
        module: Optional module label used to pick the accent colour.
        icon: Optional inline SVG/emoji markup rendered before the title.
    """
    import streamlit as st

    accent = accent_color(module)
    icon_html = (
        f'<span class="ik-card-icon" style="margin-right:8px;'
        f'vertical-align:-3px">{icon}</span>'
        if icon
        else ""
    )
    st.markdown(
        f'<div class="ik-section-header" style="border-left-color:{accent}">'
        f"{icon_html}{title}</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Navigation: five-screen sidebar with active highlight (Req 9.1, 9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    """One entry in the admin app's navigation.

    Attributes:
        key: Stable identifier used to mark the active screen.
        title: Human-readable label shown in the sidebar.
        module: Module identifier used to pick the accent colour.
        icon: A small emoji/icon shown beside the title.
    """

    key: str
    title: str
    module: str
    icon: str


#: The screens of the admin app, in sidebar order (Req 9.1). ``Dashboard``
#: is the ``Home.py`` entry point; the remaining screens live under ``pages/``.
SCREENS: tuple[Screen, ...] = (
    Screen("dashboard", "Dashboard", "dashboard", "🏠"),
    Screen("frontend_integration", "Frontend Integration", "frontend_integration", "🔌"),
    Screen("kb_management", "KB Management", "kb_management", "📚"),
    Screen("intent_configuration", "Intent Configuration", "intent_configuration", "🎯"),
    Screen("analytics", "Analytics", "analytics", "📊"),
    Screen("test_chat", "Test Chat", "test_chat", "💬"),
)


#: Logout target. The in-app login gate (:mod:`admin_ui.ui.auth`) clears the
#: session when this query parameter is present, returning the user to the
#: branded login page.
LOGOUT_PATH: str = "/?logout=1"


def render_sidebar_nav(active_key: str) -> None:
    """Render the sidebar extras beneath the native page navigation.

    Streamlit's built-in multipage navigation (auto-generated from the files
    in ``pages/``) is the single functional menu — an earlier custom
    per-screen list produced a duplicate menu, so no page list is rendered
    here. The only addition is a **Logout** link targeting the reverse
    proxy's :data:`LOGOUT_PATH`, which invalidates the browser's cached
    basic-auth credentials so the next visit prompts for login again.

    Args:
        active_key: The :attr:`Screen.key` of the currently displayed screen
            (unused; kept for the pages' existing call signature).
    """
    del active_key
    import streamlit as st

    logo_path = os.path.join(os.path.dirname(__file__), "aia_logo.svg")
    try:
        st.logo(logo_path, size="large")
    except Exception:  # pragma: no cover — fallback if st.logo is unavailable
        st.sidebar.markdown(AIA_LOGO_SVG, unsafe_allow_html=True)
    st.sidebar.markdown(
        f'<div style="color:{NEUTRAL["muted"]};font-size:0.62rem;'
        f'letter-spacing:0.08em;margin-top:4px">{AIA_TAGLINE}</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<div style="margin-top:8px"><a href="{LOGOUT_PATH}" target="_self" '
        f'style="color:{NEUTRAL["muted"]};text-decoration:none;'
        f'font-size:0.9rem">🚪 Logout</a></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# API client: talk to the backend admin REST API (Req 9.x consumer)
# ---------------------------------------------------------------------------

#: Default backend base URL when ``KMS_API_BASE`` is unset.
DEFAULT_API_BASE: str = "http://localhost:8000"

#: Environment variable naming the backend base URL.
API_BASE_ENV: str = "KMS_API_BASE"

#: Fallback environment variable set by docker-compose for the admin-ui
#: container (``API_BASE_URL=http://app:8000``).
API_BASE_ENV_FALLBACK: str = "API_BASE_URL"


def api_base_url(environ: dict[str, str] | None = None) -> str:
    """Resolve the backend admin API base URL (Req 9 consumer).

    Reads the ``KMS_API_BASE`` environment variable, then the compose-provided
    ``API_BASE_URL``, falling back to ``http://localhost:8000``. Any trailing
    slash is stripped so callers can always join paths with a single leading
    slash.

    Args:
        environ: Optional environment mapping (defaults to ``os.environ``),
            supplied by tests to control the value deterministically.

    Returns:
        The normalised base URL with no trailing slash.
    """
    env = os.environ if environ is None else environ
    base = env.get(API_BASE_ENV) or env.get(API_BASE_ENV_FALLBACK) or DEFAULT_API_BASE
    return base.rstrip("/")


def build_url(base: str, path: str) -> str:
    """Join a base URL and a request path into a full URL.

    Pure helper (no I/O): normalises slashes so exactly one separates the base
    from the path, regardless of whether the caller included leading/trailing
    slashes.

    Args:
        base: The base URL (e.g. ``"http://localhost:8000"``).
        path: The request path (e.g. ``"/integrations/telegram/credentials"``
            or ``"integrations/telegram/credentials"``).

    Returns:
        The combined URL (e.g.
        ``"http://localhost:8000/integrations/telegram/credentials"``).
    """
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


class ApiError(Exception):
    """Raised when the backend admin API returns an error or is unreachable.

    Carries enough structured detail for a screen to surface a helpful message
    to the Admin (Req error surfacing). ``status_code`` is ``None`` for
    transport-level failures (connection refused, timeout).

    Attributes:
        message: Human-readable error summary suitable for display.
        status_code: HTTP status code, or ``None`` for transport failures.
        payload: The parsed error body when available (e.g. a validation error
            listing per-field messages), otherwise ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


def _extract_error_message(status_code: int, body: Any) -> str:
    """Derive a display message from an error response body.

    Understands the backend's error shapes: a FastAPI ``{"detail": ...}`` where
    ``detail`` may be a plain string or a structured object (e.g. the
    validation-error body carrying a ``message`` and per-field ``errors``).

    Args:
        status_code: The HTTP status code of the response.
        body: The parsed JSON body, or a raw string when parsing failed.

    Returns:
        A concise, human-readable error message.
    """
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message:
                errors = detail.get("errors")
                if isinstance(errors, list) and errors:
                    fields = ", ".join(
                        str(e.get("field"))
                        for e in errors
                        if isinstance(e, dict) and e.get("field")
                    )
                    if fields:
                        return f"{message} (fields: {fields})"
                return message
            return f"Request failed with status {status_code}."
        if isinstance(detail, str) and detail:
            return detail
    if isinstance(body, str) and body.strip():
        return body.strip()
    return f"Request failed with status {status_code}."


def handle_response(response: httpx.Response) -> Any:
    """Turn an ``httpx.Response`` into parsed JSON or raise :class:`ApiError`.

    Pure with respect to the network (it only inspects an already-received
    response), so it is unit-testable by passing a hand-built
    ``httpx.Response``. On a 2xx status the JSON body is returned (``None`` for
    an empty body, e.g. ``204 No Content``). On any other status an
    :class:`ApiError` is raised carrying a display message and, when available,
    the parsed error payload.

    Args:
        response: The response returned by an ``httpx`` request.

    Returns:
        The parsed JSON body, or ``None`` when the body is empty.

    Raises:
        ApiError: When the response status is not 2xx.
    """
    # Parse the body once; tolerate non-JSON bodies by falling back to text.
    # json.JSONDecodeError subclasses ValueError, so one except clause covers
    # both malformed JSON and any other decode error.
    body: Any
    if response.content:
        try:
            body = response.json()
        except ValueError:
            body = response.text
    else:
        body = None

    if response.is_success:
        return body

    message = _extract_error_message(response.status_code, body)
    payload = body if isinstance(body, (dict, list)) else None
    raise ApiError(message, status_code=response.status_code, payload=payload)


class ApiClient:
    """Thin client over the backend FastAPI admin REST API.

    Wraps an ``httpx.Client`` with the resolved base URL and provides
    ``GET``/``POST``/``PUT``/``DELETE`` verbs that send/receive JSON and surface
    failures as :class:`ApiError`. The base URL comes from ``KMS_API_BASE``
    (default ``http://localhost:8000``); tests can inject both the base URL and
    a mock transport-backed ``httpx.Client`` so no real server is required.

    Example:
        >>> client = ApiClient()                     # doctest: +SKIP
        >>> client.get("/dashboard/summary")         # doctest: +SKIP
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        environ: dict[str, str] | None = None,
    ) -> None:
        """Create a client bound to the backend base URL.

        Args:
            base_url: Explicit base URL; when ``None`` it is resolved from the
                environment via :func:`api_base_url`.
            client: An optional pre-built ``httpx.Client`` (used by tests to
                inject a mock transport). When ``None`` one is created lazily.
            timeout: Default per-request timeout in seconds.
            environ: Optional environment mapping for base-URL resolution.
        """
        self.base_url = (base_url or api_base_url(environ)).rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        """Return the underlying client, creating it on first use."""
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- request plumbing ----------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        """Send a request and return parsed JSON, surfacing errors.

        Args:
            method: HTTP verb (``"GET"``/``"POST"``/``"PUT"``/``"DELETE"``).
            path: Request path joined onto the base URL.
            params: Optional query-string parameters.
            json: Optional JSON body.

        Returns:
            The parsed JSON body (``None`` for an empty response).

        Raises:
            ApiError: On a non-2xx response or a transport-level failure.
        """
        url = build_url(self.base_url, path)
        try:
            response = self._get_client().request(
                method, url, params=params, json=json
            )
        except httpx.HTTPError as exc:
            raise ApiError(
                f"Could not reach the backend at {url}: {exc}",
                status_code=None,
            ) from exc
        return handle_response(response)

    # -- verbs ----------------------------------------------------------

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Issue a ``GET`` request. See :meth:`_request`."""
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a ``POST`` request. See :meth:`_request`."""
        return self._request("POST", path, params=params, json=json)

    def put(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a ``PUT`` request. See :meth:`_request`."""
        return self._request("PUT", path, params=params, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Issue a ``DELETE`` request. See :meth:`_request`."""
        return self._request("DELETE", path, params=params)
