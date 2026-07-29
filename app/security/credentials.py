"""Credential storage, validation, and masking for the IntelliKnow KMS.

Integration credentials (Telegram bot token, Teams app id/password, DashScope
API key) are persisted as a single Fernet-encrypted JSON document on the
persistent volume (``settings.credentials_path``, i.e.
``/data/credentials/credentials.enc`` in production). The Fernet master key is
supplied via the ``CREDENTIAL_MASTER_KEY`` environment variable, surfaced
through :class:`app.config.Settings` and sourced from a host-only ``.env`` file
that is never committed or baked into an image (Req 1.3, 11.1).

This module exposes three cooperating pieces (see the design's "Credential
Store" interface):

* :data:`CREDENTIAL_SCHEMAS` — the required fields and format patterns for each
  integration.
* :func:`validate_credentials` — a pure function returning one
  :class:`~app.core.models.FieldError` per missing, empty, or format-invalid
  field. It performs no I/O, so validation can (and does) run BEFORE any write:
  an invalid submission stores nothing and prior credentials survive
  (Req 1.4, 1.6).
* :class:`CredentialStore` — encrypted load/save with atomic replacement
  (Req 1.5) and value masking that reveals at most the last four characters
  (Req 11.2, 11.6).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings, get_settings
from app.core.models import FieldError

__all__ = [
    "CREDENTIAL_SCHEMAS",
    "CredentialValidationError",
    "validate_credentials",
    "mask_value",
    "CredentialStore",
]


# Required fields per integration and the regex each field value must match.
# Patterns follow the design's "Credential Store" section exactly.
CREDENTIAL_SCHEMAS: dict[str, dict[str, str]] = {
    "telegram": {"bot_token": r"^\d+:[A-Za-z0-9_-]{30,}$"},
    "teams": {"app_id": r"^[0-9a-fA-F-]{36}$", "app_password": r"^\S{8,}$"},
    "dashscope": {"api_key": r"^sk-[A-Za-z0-9]{16,}$"},
}

# Number of trailing characters that may ever be revealed when masking a secret.
_MASK_REVEAL = 4

# Character used to obscure hidden portions of a secret when masking.
_MASK_CHAR = "*"


class CredentialValidationError(ValueError):
    """Raised when a credential submission fails validation.

    Carries the per-field errors so callers (e.g. the admin API) can surface
    them without re-running validation.

    Attributes:
        errors: The list of :class:`~app.core.models.FieldError` describing
            every offending field.
    """

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        joined = ", ".join(f"{e.field}: {e.message}" for e in errors)
        super().__init__(f"invalid credential submission ({joined})")


def validate_credentials(integration: str, fields: dict) -> list[FieldError]:
    """Validate a credential submission for an integration (pure function).

    Checks every required field for the integration in schema-declared order
    and returns exactly one :class:`~app.core.models.FieldError` per field that
    is missing, empty, or does not match its format pattern. An empty list
    means the submission is valid (Req 1.2, 1.4). Extra fields not defined for
    the integration are ignored.

    This function performs no I/O and never mutates the store, so it is safe to
    call before writing: an invalid submission is rejected without touching any
    previously stored credentials (Req 1.6).

    Args:
        integration: The integration key, one of the keys of
            :data:`CREDENTIAL_SCHEMAS` (``"telegram"``, ``"teams"``,
            ``"dashscope"``).
        fields: The submitted field values (typically ``dict[str, str]``).

    Returns:
        A list of :class:`~app.core.models.FieldError`, one per offending
        field; empty when every required field is present, non-empty, and
        format-valid.

    Raises:
        ValueError: If ``integration`` is not a known integration.
    """
    schema = CREDENTIAL_SCHEMAS.get(integration)
    if schema is None:
        raise ValueError(f"unknown integration: {integration!r}")

    errors: list[FieldError] = []
    for name, pattern in schema.items():
        if name not in fields or fields[name] is None:
            errors.append(FieldError(name, "is required"))
            continue
        value = fields[name]
        if not isinstance(value, str) or value == "":
            errors.append(FieldError(name, "must not be empty"))
            continue
        if re.fullmatch(pattern, value) is None:
            errors.append(FieldError(name, "has an invalid format"))
    return errors


def mask_value(value: str) -> str:
    """Mask a secret, revealing at most the last four characters.

    The returned string has the same length as ``value``; all but the trailing
    portion is replaced with ``*``. Secrets of four characters or fewer are
    fully masked so a short secret is never revealed in its entirety
    (Req 11.2, 11.6).

    Args:
        value: The secret string to mask.

    Returns:
        The masked representation of ``value``.
    """
    length = len(value)
    if length <= _MASK_REVEAL:
        return _MASK_CHAR * length
    return _MASK_CHAR * (length - _MASK_REVEAL) + value[-_MASK_REVEAL:]


class CredentialStore:
    """Fernet-encrypted credential storage with atomic writes and masking.

    Persists all integration credentials as one encrypted JSON document at
    ``settings.credentials_path``. Writes are atomic — the new ciphertext is
    written to a temporary file in the same directory and then moved into place
    with :func:`os.replace` — so an update either fully replaces the previous
    credentials or leaves them untouched; a partial/failed write never corrupts
    the store (Req 1.5).

    The master key is read from ``settings.credential_master_key``
    (``CREDENTIAL_MASTER_KEY``), keeping the key out of source and images
    (Req 1.3, 11.1).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Create a store bound to a settings snapshot.

        Args:
            settings: The settings providing ``credentials_path`` and
                ``credential_master_key``. Defaults to the process-wide
                :func:`app.config.get_settings` snapshot.
        """
        self._settings = settings or get_settings()
        self._path: Path = self._settings.credentials_path
        key = self._settings.credential_master_key
        if not key:
            raise ValueError(
                "CREDENTIAL_MASTER_KEY is not set; cannot open credential store"
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    # --- internal encrypted-document helpers ----------------------------

    def _load_all(self) -> dict[str, dict[str, str]]:
        """Decrypt and return the full credential document.

        Returns an empty mapping when the store file does not yet exist.
        """
        if not self._path.exists():
            return {}
        ciphertext = self._path.read_bytes()
        if not ciphertext:
            return {}
        plaintext = self._fernet.decrypt(ciphertext)
        data = json.loads(plaintext.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict[str, dict[str, str]]) -> None:
        """Encrypt and atomically persist the full credential document."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet.encrypt(
            json.dumps(data).encode("utf-8")
        )
        # Write to a temp file in the same directory, then atomically replace
        # so the store is never left half-written (Req 1.5).
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".credentials-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(ciphertext)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            # Clean up the temp file on any failure so no partial file lingers.
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise

    # --- public interface ----------------------------------------------

    def save(self, integration: str, fields: dict[str, str]) -> None:
        """Validate and atomically store credentials for an integration.

        Validation runs first; if the submission is invalid the store is left
        completely unchanged and a :class:`CredentialValidationError` is raised
        (Req 1.4, 1.6). On success the integration's entry is replaced with the
        submitted required fields and the whole document is re-encrypted and
        atomically written (Req 1.5).

        Args:
            integration: The integration key (see :data:`CREDENTIAL_SCHEMAS`).
            fields: The credential field values to store.

        Raises:
            CredentialValidationError: If any required field is missing, empty,
                or format-invalid.
            ValueError: If ``integration`` is unknown.
        """
        errors = validate_credentials(integration, fields)
        if errors:
            raise CredentialValidationError(errors)

        schema = CREDENTIAL_SCHEMAS[integration]
        # Persist only the schema-defined required fields, dropping extras.
        entry = {name: fields[name] for name in schema}

        data = self._load_all()
        data[integration] = entry
        self._write_all(data)

    def load(self, integration: str) -> dict[str, str] | None:
        """Return the stored credential fields for an integration.

        Args:
            integration: The integration key to look up.

        Returns:
            The stored field mapping, or ``None`` if nothing has been stored
            for the integration.
        """
        return self._load_all().get(integration)

    def masked(self, integration: str) -> dict[str, str]:
        """Return the integration's credentials with every value masked.

        Each stored value is passed through :func:`mask_value`, revealing at
        most the last four characters (Req 11.2, 11.6). Returns an empty
        mapping when nothing is stored for the integration.

        Args:
            integration: The integration key to look up.

        Returns:
            A mapping of field name to masked value; empty when absent.
        """
        entry = self.load(integration)
        if not entry:
            return {}
        return {name: mask_value(value) for name, value in entry.items()}
