# Feature: intelliknow-kms, Property 25: Loaded credential values never appear in log output
"""Property 25: Loaded credential values never appear in log output.

For any set of loaded credential values and any log message — including
messages that embed those values in the format string and/or in positional
arguments — the output produced through a :class:`RedactingFilter` contains
none of the credential values.

Validates: Requirements 11.3
"""

from __future__ import annotations

import io
import logging

from hypothesis import given, settings
from hypothesis import strategies as st

from app.security.logfilter import RedactingFilter

# Credential values are tokens/keys: printable, non-space characters. We
# exclude the mask character '*' from the secret space because the redaction
# placeholder is composed of '*' — a literal '*' secret is not a realistic
# credential value and would be trivially "reintroduced" by the placeholder.
_secret_chars = st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="*")
secret_value = st.text(alphabet=_secret_chars, min_size=1, max_size=40)

# Free-form surrounding text may contain spaces and any printable characters.
filler_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=30,
)


def _capture_logger(name: str, filt: RedactingFilter) -> tuple[logging.Logger, io.StringIO]:
    """Build an isolated logger with the redacting filter and a buffer sink."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(filt)
    logger.addHandler(handler)
    return logger, buffer


@settings(max_examples=100)
@given(
    secrets=st.lists(secret_value, min_size=1, max_size=5, unique=True),
    prefix=filler_text,
    middle=filler_text,
    suffix=filler_text,
)
def test_loaded_credentials_never_appear_in_log_output(secrets, prefix, middle, suffix):
    """No loaded credential value survives to the emitted log output."""
    filt = RedactingFilter(secrets)
    logger, buffer = _capture_logger("test_prop25_redaction", filt)

    # Message that embeds every secret directly in the format string.
    embedded = prefix + secrets[0] + middle + secrets[-1].join(secrets) + suffix
    logger.info(embedded)

    # Message that embeds the secrets via positional %-args instead.
    template = "context=%s token=%s tail=%s"
    logger.warning(template, prefix + secrets[0], middle, secrets[-1] + suffix)

    output = buffer.getvalue()
    for secret in secrets:
        assert secret not in output, (
            f"secret {secret!r} leaked into log output {output!r}"
        )
