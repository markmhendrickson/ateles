#!/usr/bin/env python3
"""
render_plan_docs.py — render the plan-mirrored architecture docs from Neotoma.

The Ateles plan entity is canonical for three repo docs:

    plan field              →  repo file
    ----------------------     -------------------------
    taxonomy_markdown       →  docs/taxonomy.md
    phases_markdown         →  docs/phases.md
    architecture_markdown   →  docs/architecture.md

Direct edits to those files are reverted on the next render. The supported
flow is: correct the plan field in Neotoma, then run this script.

Usage:
    render_plan_docs.py             # Neotoma → disk (overwrite local files)
    render_plan_docs.py --check     # exit 1 if disk differs from Neotoma
    render_plan_docs.py --push      # disk → Neotoma corrections (write-back
                                    # for an operator-reviewed local edit)

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env), ATELES_PLAN_ENTITY_ID (defaults to the Ateles plan).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN_ID = "ent_99ace4dd6673aa36ed08b1fe"

FIELD_TO_PATH = {
    "taxonomy_markdown": "docs/taxonomy.md",
    "phases_markdown": "docs/phases.md",
    "architecture_markdown": "docs/architecture.md",
}


def _load_env() -> tuple[str, str]:
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key == "NEOTOMA_BASE_URL" and not base_url:
                base_url = value
            elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                token = value
    if not base_url:
        sys.exit("NEOTOMA_BASE_URL not set (env or ~/.config/neotoma/.env)")
    return base_url.rstrip("/"), token


def _request(url: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _fetch_fields(base_url: str, token: str, plan_id: str) -> dict[str, str]:
    entity = _request(f"{base_url}/entities/{plan_id}", token)
    # Unwrap the nested snapshot shape returned by /entities/{id}.
    snapshot = entity.get("snapshot", entity)
    if isinstance(snapshot.get("snapshot"), dict):
        snapshot = snapshot["snapshot"]
    return {f: snapshot.get(f) for f in FIELD_TO_PATH}


def render(fields: dict[str, str]) -> int:
    missing = [f for f, body in fields.items() if not body]
    for field, body in fields.items():
        if not body:
            continue
        path = REPO_ROOT / FIELD_TO_PATH[field]
        path.write_text(body if body.endswith("\n") else body + "\n")
        print(f"wrote {FIELD_TO_PATH[field]} ({len(body)} chars) from {field}")
    if missing:
        print(f"WARNING: plan fields empty, files left untouched: {missing}")
        return 1
    return 0


def check(fields: dict[str, str]) -> int:
    failures = []
    for field, body in fields.items():
        path = REPO_ROOT / FIELD_TO_PATH[field]
        if not body:
            failures.append(f"{field}: empty in Neotoma")
            continue
        canonical = body if body.endswith("\n") else body + "\n"
        on_disk = path.read_text() if path.exists() else ""
        if on_disk != canonical:
            diff = list(
                difflib.unified_diff(
                    canonical.splitlines(), on_disk.splitlines(),
                    fromfile=f"neotoma:{field}", tofile=str(FIELD_TO_PATH[field]),
                    lineterm="", n=1,
                )
            )
            failures.append(f"{FIELD_TO_PATH[field]} differs ({len(diff)} diff lines)")
            print("\n".join(diff[:40]))
    if failures:
        print("MIRROR CHECK FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("mirror check OK — disk matches Neotoma for all three docs")
    return 0


def push(base_url: str, token: str, plan_id: str) -> int:
    for field, rel_path in FIELD_TO_PATH.items():
        body = (REPO_ROOT / rel_path).read_text()
        digest = hashlib.sha256(body.encode()).hexdigest()[:12]
        result = _request(
            f"{base_url}/correct",
            token,
            {
                "entity_id": plan_id,
                "entity_type": "plan",
                "field": field,
                "value": body,
                "idempotency_key": f"update-plan-{field}-{digest}",
            },
        )
        status = result.get("status") or result.get("action") or "ok"
        print(f"corrected {field} from {rel_path} ({digest}) — {status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify disk matches Neotoma")
    mode.add_argument("--push", action="store_true", help="write local files back as plan corrections")
    args = parser.parse_args()

    base_url, token = _load_env()
    plan_id = os.environ.get("ATELES_PLAN_ENTITY_ID", DEFAULT_PLAN_ID)

    if args.push:
        return push(base_url, token, plan_id)
    fields = _fetch_fields(base_url, token, plan_id)
    return check(fields) if args.check else render(fields)


if __name__ == "__main__":
    sys.exit(main())
