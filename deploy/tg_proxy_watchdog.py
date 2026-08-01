#!/usr/bin/env python3
"""Telegram proxy watchdog: probe, self-heal, reboot relay, and alert.

The Telegram bot reaches Telegram only through an stunnel TLS tunnel
(``127.0.0.1:8888``) to a Hong Kong proxy VM. When that relay hangs (accepts
TLS but returns nothing), every ``getUpdates``/``sendMessage`` times out and
the bot goes silent with no application-level error. This watchdog detects
that end to end and heals it:

1. Probe ``api.telegram.org`` through the local proxy with a short timeout.
2. On failure, restart the local ``stunnel-tgclient`` (fixes local staleness)
   and re-probe.
3. If it is still failing, reboot the HK relay ECS instance via the Alibaba
   OpenAPI (rate-limited by a cooldown), provided a credential is configured.
4. Always publish a ``TelegramProxyHealthy`` CloudWatch metric (1/0) using the
   instance role, so an alarm can notify a human regardless of auto-reboot.

Auto-reboot is optional: without ``ALIBABA_CLOUD_ACCESS_KEY_ID`` /
``ALIBABA_CLOUD_ACCESS_KEY_SECRET`` in the environment the watchdog still
probes, self-heals stunnel, and alerts — it just does not reboot the VM.

Environment (see ``/etc/intelliknow/tg-watchdog.env`` on the host):
* ``TG_PROXY_URL`` — local proxy (default ``http://127.0.0.1:8888``).
* ``TG_PROXY_INSTANCE_ID`` — HK relay ECS instance id to reboot.
* ``TG_PROXY_REGION`` — ECS region id (default ``cn-hongkong``).
* ``AWS_DEFAULT_REGION`` — CloudWatch region (default ``cn-north-1``).
* ``TG_REBOOT_COOLDOWN_S`` — min seconds between reboots (default ``900``).
* ``ALIBABA_CLOUD_ACCESS_KEY_ID`` / ``..._SECRET`` — optional reboot creds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s tg-watchdog %(levelname)s %(message)s"
)
log = logging.getLogger("tg-watchdog")

PROXY_URL = os.environ.get("TG_PROXY_URL", "http://127.0.0.1:8888")
PROBE_URL = "https://api.telegram.org/"
PROBE_TIMEOUT_S = float(os.environ.get("TG_PROBE_TIMEOUT_S", "15"))
INSTANCE_ID = os.environ.get("TG_PROXY_INSTANCE_ID", "")
ECS_REGION = os.environ.get("TG_PROXY_REGION", "cn-hongkong")
CW_REGION = os.environ.get("AWS_DEFAULT_REGION", "cn-north-1")
CW_NAMESPACE = os.environ.get("TG_METRIC_NAMESPACE", "IntelliKnow")
CW_METRIC = "TelegramProxyHealthy"
COOLDOWN_S = int(os.environ.get("TG_REBOOT_COOLDOWN_S", "900"))
STUNNEL_UNIT = os.environ.get("TG_STUNNEL_UNIT", "stunnel-tgclient")
LAST_REBOOT_FILE = "/run/tg-watchdog-last-reboot"

AK = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
SK = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")


def probe() -> bool:
    """Return True when Telegram is reachable through the local proxy."""
    try:
        with httpx.Client(proxy=PROXY_URL, timeout=PROBE_TIMEOUT_S) as client:
            resp = client.get(PROBE_URL)
        # Any HTTP response (even a redirect/4xx) proves the tunnel carries
        # traffic; only a hang/transport error or 5xx counts as unhealthy.
        return resp.status_code < 500
    except Exception as exc:  # noqa: BLE001 - any failure is "unhealthy"
        log.warning("probe failed: %s", exc)
        return False


def restart_stunnel() -> None:
    """Restart the local stunnel client (best-effort)."""
    try:
        subprocess.run(
            ["systemctl", "restart", STUNNEL_UNIT], check=True, timeout=30
        )
        log.info("restarted %s", STUNNEL_UNIT)
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        log.error("failed to restart %s: %s", STUNNEL_UNIT, exc)


def _reboot_cooldown_ok() -> bool:
    """Return True when the last reboot is older than the cooldown."""
    try:
        last = os.path.getmtime(LAST_REBOOT_FILE)
    except OSError:
        return True
    return (time.time() - last) >= COOLDOWN_S


def _mark_reboot() -> None:
    """Record the time of a reboot for cooldown tracking."""
    try:
        with open(LAST_REBOOT_FILE, "w") as handle:
            handle.write(str(time.time()))
    except OSError as exc:
        log.warning("could not write reboot marker: %s", exc)


def _sign(params: dict[str, str]) -> str:
    """Compute the Alibaba RPC v1.0 HMAC-SHA1 signature for ``params``."""
    ordered = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
    )
    to_sign = "GET&%2F&" + quote(ordered, safe="~")
    digest = hmac.new(
        (SK + "&").encode(), to_sign.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def reboot_relay() -> bool:
    """Reboot the HK relay ECS instance via the Alibaba OpenAPI.

    Returns True when the reboot request was accepted. No-op (returns False)
    when credentials or the instance id are not configured.
    """
    if not (AK and SK and INSTANCE_ID):
        log.warning(
            "reboot skipped: credentials or instance id not configured "
            "(alert-only mode)"
        )
        return False
    params = {
        "Action": "RebootInstance",
        "InstanceId": INSTANCE_ID,
        "ForceStop": "false",
        "Format": "JSON",
        "Version": "2014-05-26",
        "AccessKeyId": AK,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "RegionId": ECS_REGION,
    }
    params["Signature"] = _sign(params)
    endpoint = f"https://ecs.{ECS_REGION}.aliyuncs.com/"
    try:
        resp = httpx.get(endpoint, params=params, timeout=20)
        if resp.status_code < 300:
            log.info("reboot requested for %s", INSTANCE_ID)
            _mark_reboot()
            return True
        log.error("reboot failed: HTTP %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.error("reboot request error: %s", exc)
    return False


def publish_metric(healthy: bool) -> None:
    """Publish the health metric to CloudWatch via the instance role."""
    try:
        subprocess.run(
            [
                "aws", "cloudwatch", "put-metric-data",
                "--region", CW_REGION,
                "--namespace", CW_NAMESPACE,
                "--metric-name", CW_METRIC,
                "--value", "1" if healthy else "0",
            ],
            check=True,
            timeout=20,
            stdout=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - metric is best-effort
        log.warning("failed to publish metric: %s", exc)


def main() -> int:
    """Run one watchdog cycle. Always exits 0 so the timer keeps scheduling."""
    if probe():
        log.info("proxy healthy")
        publish_metric(True)
        return 0

    log.warning("proxy unhealthy; attempting local stunnel restart")
    restart_stunnel()
    if probe():
        log.info("proxy recovered after stunnel restart")
        publish_metric(True)
        return 0

    log.error("proxy still unhealthy after stunnel restart")
    if _reboot_cooldown_ok():
        reboot_relay()
    else:
        log.warning("within reboot cooldown; not rebooting (alert-only)")
    publish_metric(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
