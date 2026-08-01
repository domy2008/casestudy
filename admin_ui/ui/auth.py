"""Branded in-app login gate for the Admin UI.

Replaces the nginx HTTP basic-auth browser popup with a proper AIA-branded
login screen rendered inside the Streamlit app. A user types an account and
passcode into a form on a welcome page; on success an authenticated flag is
stored in the per-session ``st.session_state`` and the real app renders.

Design
------
* **Credentials** come from the ``PORTAL_USER`` / ``PORTAL_PASSWORD``
  environment variables, defaulting to ``aia`` / ``hireme`` for the demo, so
  they can be rotated without a code change and without an nginx htpasswd file.
* **Gate** — :func:`require_login` is called at the top of every screen's
  ``_main`` (right after ``set_page_config`` + base CSS). When the session is
  not yet authenticated it renders the login page and calls ``st.stop()`` so no
  screen content is exposed. Once authenticated it returns immediately.
* **Session scope** — auth lives in ``st.session_state`` (per browser session,
  shared across the multipage app), so navigating between pages keeps the user
  logged in; a full browser refresh requires signing in again (acceptable for a
  demo, and it means no long-lived credential is cached in the browser).
* **Logout** — visiting ``/?logout=1`` (the sidebar Logout link) clears the
  flag and returns the user to the login page.

Import-safety and testability
------------------------------
The credential helpers (:func:`expected_credentials`, :func:`verify_credentials`)
are pure and unit-testable without Streamlit. Everything that touches
``streamlit`` imports it lazily inside the function body, so importing this
module never requires a Streamlit runtime.
"""

from __future__ import annotations

import hmac
import hashlib
import hmac
import os

from admin_ui.ui.components import AIA_RED, AIA_RED_DARK, AIA_TAGLINE, NEUTRAL

#: Environment variable naming the portal account.
PORTAL_USER_ENV: str = "PORTAL_USER"

#: Environment variable naming the portal passcode.
PORTAL_PASSWORD_ENV: str = "PORTAL_PASSWORD"

#: Demo defaults used when the environment variables are unset.
DEFAULT_USER: str = "aia"
DEFAULT_PASSWORD: str = "hireme"

#: ``st.session_state`` key holding the authenticated flag for the session.
AUTH_SESSION_KEY: str = "ik_authenticated"

#: Transient ``st.session_state`` key signalling that the next authenticated
#: run should (re)issue or clear the "remember me" cookie. Value is ``True`` to
#: set the cookie, ``False`` to clear it; absent means "leave it alone".
REMEMBER_PENDING_KEY: str = "ik_remember_pending"

#: Query-string parameter that triggers a logout when present and truthy.
LOGOUT_PARAM: str = "logout"

#: Name of the first-party cookie that persists a "remember me" login across
#: browser sessions. It holds only an HMAC token (never the passcode).
REMEMBER_COOKIE: str = "ik_remember"

#: How long a remembered login stays valid, in days.
REMEMBER_MAX_AGE_DAYS: int = 30


def remember_token(environ: dict[str, str] | None = None) -> str:
    """Return the opaque "remember me" token for the current credentials.

    The token is ``HMAC-SHA256(key=passcode, msg=account)`` hex-encoded, so it
    proves a prior successful login without storing the passcode in the cookie,
    and it self-invalidates whenever the account or passcode is rotated.

    Args:
        environ: Optional environment mapping (defaults to ``os.environ``).

    Returns:
        The hex-encoded HMAC token.
    """
    user, password = expected_credentials(environ)
    return hmac.new(
        password.encode("utf-8"), user.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def expected_credentials(environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Return the configured ``(account, passcode)`` pair.

    Args:
        environ: Optional environment mapping (defaults to ``os.environ``),
            supplied by tests for deterministic values.

    Returns:
        The expected account and passcode, falling back to the demo defaults.
    """
    env = os.environ if environ is None else environ
    user = env.get(PORTAL_USER_ENV) or DEFAULT_USER
    password = env.get(PORTAL_PASSWORD_ENV) or DEFAULT_PASSWORD
    return user, password


def verify_credentials(
    account: str | None,
    passcode: str | None,
    environ: dict[str, str] | None = None,
) -> bool:
    """Check submitted credentials against the configured pair.

    Uses constant-time comparison to avoid leaking length/prefix information
    through timing.

    Args:
        account: The submitted account (may be ``None``/empty).
        passcode: The submitted passcode (may be ``None``/empty).
        environ: Optional environment mapping for the expected values.

    Returns:
        ``True`` only when both account and passcode match exactly.
    """
    exp_user, exp_pw = expected_credentials(environ)
    account_ok = hmac.compare_digest(account or "", exp_user)
    passcode_ok = hmac.compare_digest(passcode or "", exp_pw)
    return account_ok and passcode_ok


# ---------------------------------------------------------------------------
# Streamlit rendering (import-safe: only touched inside these functions)
# ---------------------------------------------------------------------------


def _welcome_hero_html() -> str:
    """Build the AIA welcome banner shown above the login form."""
    return (
        f'<div style="background:linear-gradient(135deg,{AIA_RED} 0%,'
        f'{AIA_RED_DARK} 100%);border-radius:16px;padding:32px 28px;'
        f'color:#fff;text-align:center;box-shadow:0 8px 24px rgba(166,9,61,.28)">'
        '<div style="font-family:Arial Black,Arial,sans-serif;font-style:italic;'
        'font-weight:900;font-size:52px;line-height:1;letter-spacing:1px">AIA</div>'
        '<div style="height:3px;width:120px;background:#fff;opacity:.85;'
        'margin:12px auto;border-radius:2px"></div>'
        f'<div style="font-size:.72rem;letter-spacing:.14em;opacity:.9">'
        f'{AIA_TAGLINE}</div>'
        '<div style="font-size:1.5rem;font-weight:700;margin-top:18px">'
        'Welcome</div>'
        '<div style="font-size:.95rem;opacity:.92;margin-top:4px">'
        'IntelliKnow Knowledge Management Portal</div>'
        '</div>'
    )


def _inject_login_css() -> None:
    """Hide app chrome and centre the login card on the login screen."""
    import streamlit as st

    st.markdown(
        f"""
        <style>
          [data-testid="stSidebar"], [data-testid="stSidebarNav"],
          [data-testid="stHeader"] {{ display: none !important; }}
          .stApp {{ background: {NEUTRAL['canvas']}; }}
          .block-container {{ max-width: 460px; padding-top: 4rem; }}
          .stButton > button {{
            width: 100%;
            background: {AIA_RED};
            color: #fff;
            border: none;
            font-weight: 600;
            border-radius: 8px;
            padding: 8px 0;
          }}
          .stButton > button:hover {{ background: {AIA_RED_DARK}; color: #fff; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _write_remember_cookie(value: str, max_age_seconds: int) -> None:
    """Set (or, with ``max_age_seconds=0``, clear) the remember-me cookie.

    Streamlit has no server-side cookie API, so the cookie is written on the
    parent document from a zero-height helper component. The component's
    ``srcdoc`` iframe is same-origin with the app, so ``window.parent.document``
    is reachable and the cookie lands as a first-party cookie on the portal
    domain (``Secure``; HTTPS-only).

    Args:
        value: The cookie value (an HMAC token), or ``""`` when clearing.
        max_age_seconds: Cookie lifetime in seconds; ``0`` expires it now.
    """
    import streamlit.components.v1 as components

    components.html(
        "<script>try{window.parent.document.cookie="
        f'"{REMEMBER_COOKIE}={value}; Max-Age={max_age_seconds}; '
        'Path=/; SameSite=Lax; Secure";}catch(e){}</script>',
        height=0,
    )


def _remembered(environ: dict[str, str] | None = None) -> bool:
    """Return ``True`` when a valid remember-me cookie is present.

    Reads the cookie sent with the page request via ``st.context.cookies`` and
    compares it in constant time to the expected :func:`remember_token`, so a
    tampered or stale token (e.g. after a passcode rotation) is rejected.
    """
    import streamlit as st

    try:
        cookies = st.context.cookies
    except Exception:  # pragma: no cover — older/headless runtimes
        return False
    token = cookies.get(REMEMBER_COOKIE) if cookies else None
    if not token:
        return False
    return hmac.compare_digest(token, remember_token(environ))


def _apply_pending_remember() -> None:
    """Issue or clear the remember-me cookie requested at the last sign-in.

    Runs on the authenticated rerun (not the interrupted submit run) so the
    cookie-writing component actually flushes to the browser.
    """
    import streamlit as st

    if REMEMBER_PENDING_KEY not in st.session_state:
        return
    remember = st.session_state.pop(REMEMBER_PENDING_KEY)
    if remember:
        _write_remember_cookie(remember_token(), REMEMBER_MAX_AGE_DAYS * 86400)
    else:
        _write_remember_cookie("", 0)


def _render_login() -> None:
    """Render the branded login page with the account/passcode form."""
    import streamlit as st

    _inject_login_css()
    st.markdown(_welcome_hero_html(), unsafe_allow_html=True)
    st.write("")

    with st.form("aia_login", clear_on_submit=False):
        account = st.text_input("Account", placeholder="Enter your account")
        passcode = st.text_input(
            "Passcode", type="password", placeholder="Enter your passcode"
        )
        remember = st.checkbox("Remember my password", value=False)
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if verify_credentials(account, passcode):
            st.session_state[AUTH_SESSION_KEY] = True
            # Defer the cookie write to the next (authenticated) run so the
            # helper component flushes instead of being cut off by st.rerun().
            st.session_state[REMEMBER_PENDING_KEY] = bool(remember)
            st.rerun()
        else:
            st.error("Incorrect account or passcode. Please try again.")

    st.markdown(
        f'<div style="text-align:center;color:{NEUTRAL["muted"]};'
        f'font-size:.75rem;margin-top:18px">Authorized access only · '
        f'AIA IntelliKnow KMS</div>',
        unsafe_allow_html=True,
    )


def _handle_logout() -> bool:
    """Clear auth when the logout query param is present.

    Clears both the per-session flag and the persistent remember-me cookie so
    a remembered browser is fully signed out.

    Returns:
        ``True`` if a logout was processed (session flag cleared).
    """
    import streamlit as st

    if st.query_params.get(LOGOUT_PARAM):
        st.session_state.pop(AUTH_SESSION_KEY, None)
        st.session_state.pop(REMEMBER_PENDING_KEY, None)
        _write_remember_cookie("", 0)
        st.query_params.clear()
        return True
    return False


def require_login() -> None:
    """Gate the current screen behind the branded login page.

    Call this at the top of every screen's ``_main`` after ``set_page_config``.
    When the session is authenticated it returns immediately; otherwise it
    renders the login page and stops the script so no content is exposed.
    """
    import streamlit as st

    if _handle_logout():
        _render_login()
        st.stop()
    if st.session_state.get(AUTH_SESSION_KEY):
        _apply_pending_remember()
        return
    if _remembered():
        st.session_state[AUTH_SESSION_KEY] = True
        return
    _render_login()
    st.stop()
