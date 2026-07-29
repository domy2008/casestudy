"""Credential-redacting logging filter for the IntelliKnow KMS.

Requirement 11.3 mandates that credential values never appear in application
logs, error messages, or stack traces. This module provides
:class:`RedactingFilter`, a :class:`logging.Filter` intended to be installed on
the root logger (see :mod:`app.main`). It maintains a live set of secret
strings — the currently-loaded credential values — and replaces any occurrence
of one of those secrets in a log record with the :data:`REDACTION` placeholder
before the record is emitted.

Design goals:

* **Refreshable secret set.** Credentials change at runtime (an Admin saves a
  new token, startup loads them from the Credential_Store). The filter exposes
  :meth:`RedactingFilter.set_secrets`, :meth:`RedactingFilter.add_secrets`, and
  :meth:`RedactingFilter.refresh` so the redaction set can be updated whenever
  credentials change, without recreating the filter or touching the logging
  configuration.
* **Never raises.** A logging filter runs on the hot path of every log call;
  an exception here would break logging (and potentially the request that was
  being logged). Every code path in :meth:`RedactingFilter.filter` is guarded
  so the filter always returns ``True`` and never propagates an exception —
  worst case a record passes through unmodified rather than crashing.
* **Placeholder cannot reintroduce a secret.** The redaction placeholder
  contains only ``*`` characters, so substituting it can never splice together
  a new occurrence of an alphanumeric credential value.

The filter can source its secrets directly from a
:class:`~app.security.credentials.CredentialStore` via
:func:`secrets_from_store`, and a provider callable can be supplied so
:meth:`RedactingFilter.refresh` re-pulls the latest values on demand.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

__all__ = ["REDACTION", "RedactingFilter", "secrets_from_store"]

# Placeholder substituted for any detected secret value. Composed solely of
# non-alphanumeric characters so a replacement can never create a new
# occurrence of a token/key by joining neighboring text.
REDACTION = "***"

# Integrations whose stored field values should be treated as secrets. Kept in
# sync with app.security.credentials.CREDENTIAL_SCHEMAS.
_DEFAULT_INTEGRATIONS: tuple[str, ...] = ("telegram", "teams", "dashscope")


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs currently-loaded credential values.

    Install one instance on the root logger; every emitted record has its
    fully-rendered message scanned and any known secret replaced with
    :data:`REDACTION`. The set of secrets is mutable and thread-safe so it can
    be refreshed whenever credentials are loaded or changed (Req 11.3).

    The filter is deliberately total: it always returns ``True`` (it redacts,
    it never drops records) and never raises, so it is safe on the logging hot
    path.
    """

    def __init__(
        self,
        secrets: Iterable[str] | None = None,
        *,
        provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        """Create a redacting filter.

        Args:
            secrets: Initial secret values to redact. Empty/blank and
                non-string entries are ignored.
            provider: Optional callable returning the current secret values.
                When supplied, :meth:`refresh` calls it and replaces the
                redaction set with its result — useful for pulling live values
                from the Credential_Store after credentials change.
        """
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._provider = provider
        if secrets is not None:
            self.set_secrets(secrets)

    # --- secret-set management -----------------------------------------

    @staticmethod
    def _clean(secrets: Iterable[str]) -> set[str]:
        """Return the usable secrets from an iterable (non-empty strings)."""
        cleaned: set[str] = set()
        for value in secrets:
            if isinstance(value, str) and value != "":
                cleaned.add(value)
        return cleaned

    def set_secrets(self, secrets: Iterable[str]) -> None:
        """Replace the entire redaction set with ``secrets``.

        Args:
            secrets: The new secret values. Non-string and empty entries are
                dropped.
        """
        cleaned = self._clean(secrets)
        with self._lock:
            self._secrets = cleaned

    def add_secrets(self, secrets: Iterable[str]) -> None:
        """Add ``secrets`` to the existing redaction set.

        Args:
            secrets: Additional secret values to redact. Non-string and empty
                entries are dropped.
        """
        cleaned = self._clean(secrets)
        with self._lock:
            self._secrets |= cleaned

    def clear(self) -> None:
        """Remove all secrets from the redaction set."""
        with self._lock:
            self._secrets = set()

    def refresh(self) -> None:
        """Re-pull secrets from the configured provider, if any.

        Replaces the redaction set with the provider's current result. Does
        nothing when no provider was supplied. Never raises: a failing provider
        leaves the existing secret set intact.
        """
        if self._provider is None:
            return
        try:
            values = self._provider()
        except Exception:  # pragma: no cover - defensive; provider misbehaves
            return
        self.set_secrets(values)

    def _snapshot(self) -> list[str]:
        """Return the current secrets ordered longest-first for safe replace.

        Replacing longer secrets before shorter ones prevents a shorter secret
        that is a substring of a longer one from partially masking it.
        """
        with self._lock:
            return sorted(self._secrets, key=len, reverse=True)

    # --- redaction ------------------------------------------------------

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret replaced by the placeholder.

        Args:
            text: The text to scrub.

        Returns:
            The redacted text. When there are no secrets, ``text`` is returned
            unchanged.
        """
        for secret in self._snapshot():
            if secret in text:
                text = text.replace(secret, REDACTION)
        return text

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """Redact secrets from ``record`` in place; always allow the record.

        The record's arguments are merged into its message (via
        :meth:`logging.LogRecord.getMessage`), the merged text is scrubbed, and
        the result is stored back as the record message with the arguments
        cleared. This guarantees no secret survives regardless of whether it
        appeared in the format string or in a positional argument.

        This method never raises and always returns ``True`` (Req 11.3): on any
        internal error the record is passed through unmodified rather than
        interrupting logging.

        Args:
            record: The log record to scrub.

        Returns:
            ``True`` always, so the record continues to the handlers.
        """
        try:
            # Fast path: nothing to redact.
            with self._lock:
                has_secrets = bool(self._secrets)
            if not has_secrets:
                return True

            try:
                message = record.getMessage()
            except Exception:
                # Malformed args/format string — fall back to the raw msg.
                message = str(record.msg)

            redacted = self.redact(message)
            record.msg = redacted
            record.args = ()
        except Exception:
            # A redacting filter must never break logging. Swallow everything.
            pass
        return True


def secrets_from_store(
    store: "object",
    integrations: Iterable[str] = _DEFAULT_INTEGRATIONS,
) -> list[str]:
    """Collect every stored credential value from a Credential_Store.

    Loads each integration's fields and returns all of their values, suitable
    for seeding or refreshing a :class:`RedactingFilter`. Missing integrations
    and any load error are ignored so this can be used as a filter provider on
    the logging hot path without risk of raising.

    Args:
        store: A :class:`~app.security.credentials.CredentialStore` (or any
            object exposing a compatible ``load(integration)`` method).
        integrations: Integration keys to gather values for. Defaults to all
            known integrations (telegram, teams, dashscope).

    Returns:
        A list of secret string values (may contain duplicates removed;
        empty when nothing is stored).
    """
    values: set[str] = set()
    for integration in integrations:
        try:
            fields = store.load(integration)  # type: ignore[attr-defined]
        except Exception:
            fields = None
        if not fields:
            continue
        for value in fields.values():
            if isinstance(value, str) and value:
                values.add(value)
    return list(values)
