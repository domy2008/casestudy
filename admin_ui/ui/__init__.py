"""Shared UI toolkit for the Streamlit admin app.

Exposes the reusable visual system (cards, accent colors, section headers),
the sidebar navigation helper, and the REST :class:`~admin_ui.ui.components.ApiClient`
used by every screen to talk to the backend admin API.

The pure helpers (accent-color mapping, card HTML builder, URL construction,
and response handling) are importable and unit-testable without a running
Streamlit server; only the ``render_*`` functions touch ``streamlit`` and they
import it lazily so this package stays import-safe in headless contexts.
"""

from admin_ui.ui.components import (
    ACCENT_COLORS,
    DEFAULT_ACCENT,
    DEFAULT_API_BASE,
    NEUTRAL,
    SCREENS,
    ApiClient,
    ApiError,
    Screen,
    accent_color,
    api_base_url,
    build_url,
    card_html,
    handle_response,
    inject_base_css,
    normalize_module,
    render_metric_card,
    render_sidebar_nav,
    section_header,
)
from admin_ui.ui.auth import require_login, verify_credentials

__all__ = [
    "require_login",
    "verify_credentials",
    "ACCENT_COLORS",
    "DEFAULT_ACCENT",
    "DEFAULT_API_BASE",
    "NEUTRAL",
    "SCREENS",
    "ApiClient",
    "ApiError",
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
