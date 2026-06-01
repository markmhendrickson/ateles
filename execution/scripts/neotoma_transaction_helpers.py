"""Store and list transaction entities via Neotoma CLI (no parquet)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from shutil import which
from typing import Any


def neotoma_store_transaction_entity(
    entity: dict[str, Any],
    idempotency_key: str,
    *,
    timeout: int = 120,
    source_file_path: str | None = None,
) -> tuple[bool, str]:
    """
    Persist one transaction-shaped entity. Returns (ok, stderr_or_empty).
    When *source_file_path* is given, the raw source file is attached to the
    Neotoma sources row via --file-path for provenance.
    """
    if not which("neotoma"):
        return False, "neotoma CLI not found on PATH"

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as f:
        json.dump([entity], f, ensure_ascii=False)
        tmp_path = f.name

    try:
        cmd = [
            "neotoma",
            "store",
            "--file",
            tmp_path,
            "--idempotency-key",
            idempotency_key,
            "--api-only",
        ]
        if source_file_path:
            cmd.extend(["--file-path", source_file_path])
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "neotoma store failed").strip()
        return True, ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def neotoma_list_transactions_page(limit: int, offset: int) -> dict[str, Any]:
    """Single page of transaction entities (JSON)."""
    p = subprocess.run(
        [
            "neotoma",
            "entities",
            "list",
            "--type",
            "transaction",
            "--limit",
            str(limit),
            "--offset",
            str(offset),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if p.returncode != 0:
        return {"entities": [], "total": 0, "error": (p.stderr or "").strip()}
    try:
        return json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return {"entities": [], "total": 0, "error": "invalid JSON from neotoma"}


def neotoma_list_all_transactions(page_size: int = 200) -> list[dict[str, Any]]:
    """Paginate until no more entities."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = neotoma_list_transactions_page(page_size, offset)
        if data.get("error"):
            break
        batch = data.get("entities") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return out


def flatten_transaction_snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    """Normalize list/get snapshot shape to a flat dict of fields."""
    s = entity.get("snapshot")
    if not isinstance(s, dict):
        return {}
    inner = s.get("snapshot")
    if isinstance(inner, dict):
        merged = dict(inner)
        for k, v in s.items():
            if k != "snapshot" and k not in merged:
                merged[k] = v
        return merged
    return s
