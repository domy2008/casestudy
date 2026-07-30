"""Application settings and persistent-storage paths.

Settings are sourced from environment variables so that the same image runs
unchanged across hosts (Req 12) while tests can point every persistent path at
a throwaway temporary directory instead of the production ``/data`` volume.

The environment variables consumed here are:

``DASHSCOPE_API_KEY``
    Fallback API key for the DashScope AI vendor. The Credential_Store is the
    primary source at runtime (Req 11.4); this env var exists for local/dev use.
``TELEGRAM_PROXY_URL``
    HTTPS forward proxy used ONLY by the Telegram client, since Telegram is
    unreachable from AWS China (Req 12.2). Empty means "no proxy configured".
``WHATSAPP_PROXY_URL``
    HTTPS forward proxy used ONLY by the WhatsApp client. Like Telegram, the
    WhatsApp Cloud API (``graph.facebook.com``) is unreachable from AWS China,
    so all Graph API traffic is routed through this proxy (Req 12.2). Empty
    means "no proxy configured".
``TEAMS_TENANT_ID``
    Microsoft Entra tenant (directory) ID for a Single Tenant bot app
    registration. When set, Bot Framework tokens are acquired from the
    tenant-specific OAuth2 endpoint; when empty, the multi-tenant
    ``botframework.com`` endpoint is used.
``CREDENTIAL_MASTER_KEY``
    Fernet master key that encrypts ``credentials.enc`` (Req 1.3, 11.1). Comes
    from a host-only ``.env`` file that is never committed or baked into images.
``AWS_REGION`` / ``AWS_DEFAULT_REGION``
    AWS China region for CloudWatch metrics, logs, and alarms (Req 13).
``DATA_DIR``
    Root of the persistent volume. Defaults to ``/data`` in production but is
    overridable so tests use an isolated temporary directory (Req 12.4).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Default root of the persistent volume in production (a host bind mount on
# EBS). Overridable via the DATA_DIR env var so tests never touch it.
DEFAULT_DATA_DIR = "/data"

# Default AWS China region when neither AWS_REGION nor AWS_DEFAULT_REGION is set.
DEFAULT_AWS_REGION = "cn-north-1"


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of environment-derived configuration.

    Attributes:
        data_dir: Root directory of the persistent volume.
        dashscope_api_key: Fallback DashScope API key (may be empty).
        telegram_proxy_url: Proxy URL for Telegram traffic (may be empty).
        whatsapp_proxy_url: Proxy URL for WhatsApp Cloud API traffic (may be
            empty).
        teams_tenant_id: Entra tenant ID for a Single Tenant Teams bot app
            (empty means multi-tenant).
        credential_master_key: Fernet master key for the Credential_Store.
        aws_region: AWS region for CloudWatch integration.
    """

    data_dir: Path
    dashscope_api_key: str
    telegram_proxy_url: str
    whatsapp_proxy_url: str
    teams_tenant_id: str
    credential_master_key: str
    aws_region: str

    # --- Derived persistent-storage paths -------------------------------

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database file (``/data/app.db``)."""
        return self.data_dir / "app.db"

    @property
    def faiss_dir(self) -> Path:
        """Directory holding per-Intent_Space FAISS index files."""
        return self.data_dir / "faiss"

    @property
    def uploads_dir(self) -> Path:
        """Directory holding original uploaded documents."""
        return self.data_dir / "uploads"

    @property
    def credentials_path(self) -> Path:
        """Path to the Fernet-encrypted credential store file."""
        return self.data_dir / "credentials" / "credentials.enc"

    @property
    def logbuffer_dir(self) -> Path:
        """Directory buffering metrics/logs when CloudWatch is unreachable."""
        return self.data_dir / "logbuffer"

    def ensure_directories(self) -> None:
        """Create all persistent-storage directories if they are missing.

        Idempotent: existing directories are left untouched. Called during
        startup and by the database bootstrap so the SQLite file, FAISS
        indexes, uploads, credentials, and log buffer all have a home.
        """
        for directory in (
            self.data_dir,
            self.faiss_dir,
            self.uploads_dir,
            self.credentials_path.parent,
            self.logbuffer_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Build a :class:`Settings` snapshot from environment variables.

    Args:
        environ: Optional mapping to read from instead of ``os.environ``.
            Useful in tests to inject a fully controlled environment.

    Returns:
        A populated, immutable :class:`Settings` instance.
    """
    env = os.environ if environ is None else environ

    data_dir = Path(env.get("DATA_DIR") or DEFAULT_DATA_DIR)
    aws_region = (
        env.get("AWS_REGION")
        or env.get("AWS_DEFAULT_REGION")
        or DEFAULT_AWS_REGION
    )

    return Settings(
        data_dir=data_dir,
        dashscope_api_key=env.get("DASHSCOPE_API_KEY", ""),
        telegram_proxy_url=env.get("TELEGRAM_PROXY_URL", ""),
        whatsapp_proxy_url=env.get("WHATSAPP_PROXY_URL", ""),
        teams_tenant_id=env.get("TEAMS_TENANT_ID", ""),
        credential_master_key=env.get("CREDENTIAL_MASTER_KEY", ""),
        aws_region=aws_region,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings snapshot.

    Reads ``os.environ`` on first call and caches the result. Tests that need
    a different ``DATA_DIR`` should call :func:`load_settings` directly with an
    explicit environment rather than mutating the cache.

    Returns:
        The cached :class:`Settings` instance.
    """
    return load_settings()
