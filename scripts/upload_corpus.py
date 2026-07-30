#!/usr/bin/env python3
"""Batch-upload the sample corpus to a running IntelliKnow KMS instance.

Unlike ``seed_demo`` / ``emulate_usage`` (which write directly to a local
``DATA_DIR``), this script talks to the **deployed** admin REST API so the
running app ingests and indexes each document itself — the supported way to
populate the KB backing the Telegram/Teams bots.

For every sample document it:

1. Resolves the target Intent_Space id from the filename prefix
   (``hr_`` → HR, ``legal_`` → Legal, ``finance_`` → Finance, else General)
   against ``GET /spaces``.
2. Skips the upload when a Processed document of the same name already exists
   (idempotent), unless ``--force`` is given.
3. ``POST /documents`` with the base64-encoded bytes and the space id.

Usage::

    python scripts/upload_corpus.py --base-url http://<host>:8000
    python scripts/upload_corpus.py --base-url http://localhost:8000 --force

The base URL may also come from the ``KMS_BASE_URL`` environment variable.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = _PROJECT_ROOT / "samples"

#: Filename-prefix → Intent_Space name. Anything else falls back to General.
PREFIX_TO_SPACE: dict[str, str] = {
    "hr_": "HR",
    "legal_": "Legal",
    "finance_": "Finance",
    "general_": "General",
}

#: Supported upload extensions (matches app.kb.loaders.SUPPORTED_EXTENSIONS).
SUPPORTED_EXTS: frozenset[str] = frozenset({".pdf", ".docx", ".xlsx", ".txt", ".md"})

#: Filenames under ``samples/`` that are documentation, not knowledge content.
EXCLUDED_NAMES: frozenset[str] = frozenset({
    "README.md", "测试问答手册.md", "用户测试问答报告.md",
})


def space_for(filename: str) -> str:
    """Return the Intent_Space name for ``filename`` by its prefix."""
    for prefix, space in PREFIX_TO_SPACE.items():
        if filename.startswith(prefix):
            return space
    return "General"


def discover_documents() -> list[Path]:
    """Return every supported sample document under ``samples/`` (recursive)."""
    return sorted(
        p for p in SAMPLES_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        and p.name not in EXCLUDED_NAMES
    )


def upload(base_url: str, force: bool) -> int:
    """Upload every sample document to the instance at ``base_url``.

    Args:
        base_url: Root URL of the deployed admin API (e.g. ``http://host:8000``).
        force: When True, upload even if a Processed document of the same name
            already exists.

    Returns:
        Process exit code: 0 when nothing failed, 1 otherwise.
    """
    base = base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=60.0) as client:
        spaces = {s["name"]: s["id"] for s in client.get("/spaces").json()}
        existing = {
            d["name"]
            for d in client.get("/documents").json()
            if str(d.get("status")) == "Processed"
        }

        failures = 0
        for path in discover_documents():
            space_name = space_for(path.name)
            space_id = spaces.get(space_name)
            if space_id is None:
                print(f"[SKIP] {path.name}: space {space_name!r} not found")
                failures += 1
                continue
            if path.name in existing and not force:
                print(f"[SKIP] {path.name}: already Processed in {space_name}")
                continue

            payload = {
                "name": path.name,
                "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "space_id": space_id,
            }
            resp = client.post("/documents", json=payload)
            if resp.status_code >= 400:
                print(f"[FAIL] {path.name}: HTTP {resp.status_code} {resp.text[:200]}")
                failures += 1
                continue
            body = resp.json()
            print(f"[OK]   {path.name} -> {space_name} "
                  f"(doc #{body.get('id')}, status={body.get('status')})")

    print("\n上传完成。" if failures == 0 else f"\n{failures} 个文件未成功。")
    return 0 if failures == 0 else 1


def main() -> int:
    """CLI entry point: parse args and run the batch upload."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KMS_BASE_URL", "http://localhost:8000"),
        help="Root URL of the deployed admin API (or set KMS_BASE_URL).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Upload even if a Processed document of the same name exists.",
    )
    args = parser.parse_args()
    print(f"Target: {args.base_url}")
    return upload(args.base_url, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
