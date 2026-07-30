"""Minimal Aliyun DNS (Alidns) API client for managing autobuy.top records.

Signs RPC-style requests with HMAC-SHA1 (Signature Version 1.0) using the
ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET env vars.

Usage:
    python scripts/alidns.py list <domain>
    python scripts/alidns.py add <domain> <rr> <type> <value>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

ENDPOINT = "https://alidns.aliyuncs.com/"


def _encode(value: str) -> str:
    """Percent-encode per Aliyun RPC signature rules."""
    return quote(str(value), safe="~")


def request(action: str, params: dict[str, str]) -> dict:
    """Send a signed Alidns RPC request and return the parsed JSON body."""
    key_id = os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"]
    secret = os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"]

    all_params = {
        "Action": action,
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **params,
    }
    canonical = "&".join(
        f"{_encode(k)}={_encode(v)}" for k, v in sorted(all_params.items())
    )
    string_to_sign = "GET&%2F&" + _encode(canonical)
    signature = base64.b64encode(
        hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    all_params["Signature"] = signature

    resp = httpx.get(ENDPOINT, params=all_params, timeout=15)
    body = resp.json()
    if resp.status_code != 200:
        raise SystemExit(f"API error {resp.status_code}: {json.dumps(body)}")
    return body


def main() -> None:
    """Dispatch the list/add subcommands."""
    cmd = sys.argv[1]
    if cmd == "list":
        body = request(
            "DescribeDomainRecords",
            {"DomainName": sys.argv[2], "PageSize": "100"},
        )
        for record in body["DomainRecords"]["Record"]:
            print(
                f"{record['RR']:<12} {record['Type']:<6} {record['Value']:<40} "
                f"TTL={record['TTL']} status={record['Status']}"
            )
    elif cmd == "add":
        domain, rr, rtype, value = sys.argv[2:6]
        body = request(
            "AddDomainRecord",
            {"DomainName": domain, "RR": rr, "Type": rtype, "Value": value},
        )
        print("Created RecordId:", body.get("RecordId"))
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
