#!/usr/bin/env python3
"""
One-off migration toward the canonical dual agent_message model (user + assistant rows,
PART_OF message -> conversation).

What it does
------------
1) Merged turns: agent_message snapshots that still have both ``role_user`` and ``role_agent``
   (legacy store-neotoma / conversation_tracking shape) are split into two new agent_message
   entities, each linked with PART_OF to the same conversation as the original message, then
   the legacy message entity is soft-deleted.

2) Inverted PART_OF: relationships where source is a ``conversation`` and target is an
   ``agent_message`` with type PART_OF are flipped to PART_OF(agent_message, conversation).

Transport
---------
- **HTTP (default):** ``NEOTOMA_BEARER_TOKEN`` + ``NEOTOMA_API_URL``. Uses a browser-like
  User-Agent (Cloudflare may block urllib defaults).

- **CLI (recommended when bearer is invalid for prod):** ``--cli`` uses the ``neotoma`` binary
  on PATH (OAuth/session auth as configured for the CLI). No bearer token required.

Usage
-----
  python3 execution/scripts/migrate_neotoma_chat_dual_messages.py --cli
  python3 execution/scripts/migrate_neotoma_chat_dual_messages.py --cli --execute

Safe to re-run: idempotency keys for new rows are deterministic per legacy entity id.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Protocol


class Backend(Protocol):
    def relationships_part_of_page(self, offset: int, limit: int) -> dict[str, Any]:
        ...

    def agent_messages_page(self, offset: int, limit: int) -> dict[str, Any]:
        ...

    def find_conversation_for_message(self, message_id: str) -> str | None:
        ...

    def store_agent_message(
        self,
        *,
        role: str,
        content: str,
        turn_key: str,
        idempotency_key: str,
    ) -> str:
        ...

    def create_part_of(self, message_id: str, conversation_id: str) -> None:
        ...

    def delete_part_of(self, source: str, target: str) -> None:
        ...

    def delete_agent_message(self, entity_id: str) -> None:
        ...


def _http_json_request(
    base: str,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> Any:
    url = f"{base.rstrip('/')}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {err_body}") from e


class HttpBackend:
    def __init__(self, base: str, token: str) -> None:
        self._base = base.rstrip("/")
        self._token = token

    def relationships_part_of_page(self, offset: int, limit: int) -> dict[str, Any]:
        out = _http_json_request(
            self._base,
            "GET",
            f"/relationships?relationship_type=PART_OF&limit={limit}&offset={offset}",
            self._token,
        )
        return out if isinstance(out, dict) else {}

    def agent_messages_page(self, offset: int, limit: int) -> dict[str, Any]:
        body = {
            "entity_type": "agent_message",
            "include_snapshots": True,
            "include_merged": False,
            "limit": limit,
            "offset": offset,
        }
        out = _http_json_request(
            self._base, "POST", "/entities/query", self._token, body
        )
        return out if isinstance(out, dict) else {}

    def find_conversation_for_message(self, message_id: str) -> str | None:
        type_cache: dict[str, str] = {}

        def etype(eid: str) -> str | None:
            if eid in type_cache:
                return type_cache[eid]
            detail = _http_json_request(
                self._base, "GET", f"/entities/{eid}", self._token
            )
            if not isinstance(detail, dict):
                return None
            t = detail.get("entity_type")
            if isinstance(t, str):
                type_cache[eid] = t
                return t
            return None

        rels = _http_json_request(
            self._base, "GET", f"/entities/{message_id}/relationships", self._token
        )
        if not isinstance(rels, dict):
            return None
        for rel in rels.get("outgoing") or []:
            if rel.get("relationship_type") != "PART_OF":
                continue
            tid = rel.get("target_entity_id")
            if isinstance(tid, str) and etype(tid) == "conversation":
                return tid
        for rel in rels.get("incoming") or []:
            if rel.get("relationship_type") != "PART_OF":
                continue
            sid = rel.get("source_entity_id")
            if isinstance(sid, str) and etype(sid) == "conversation":
                return sid
        return None

    def store_agent_message(
        self,
        *,
        role: str,
        content: str,
        turn_key: str,
        idempotency_key: str,
    ) -> str:
        body = {
            "idempotency_key": idempotency_key,
            "entities": [
                {
                    "entity_type": "agent_message",
                    "schema_version": "1.3.0",
                    "role": role,
                    "content": content,
                    "turn_key": turn_key,
                }
            ],
        }
        out = _http_json_request(self._base, "POST", "/store", self._token, body)
        if not isinstance(out, dict):
            raise RuntimeError(f"Unexpected store response: {out!r}")
        ents = out.get("entities")
        if not ents or not isinstance(ents, list):
            raise RuntimeError(f"Store missing entities: {out}")
        eid = ents[0].get("entity_id")
        if not isinstance(eid, str):
            raise RuntimeError(f"Store missing entity_id: {out}")
        return eid

    def create_part_of(self, message_id: str, conversation_id: str) -> None:
        _http_json_request(
            self._base,
            "POST",
            "/create_relationship",
            self._token,
            {
                "relationship_type": "PART_OF",
                "source_entity_id": message_id,
                "target_entity_id": conversation_id,
            },
        )

    def delete_part_of(self, source: str, target: str) -> None:
        _http_json_request(
            self._base,
            "POST",
            "/delete_relationship",
            self._token,
            {
                "relationship_type": "PART_OF",
                "source_entity_id": source,
                "target_entity_id": target,
            },
        )

    def delete_agent_message(self, entity_id: str) -> None:
        _http_json_request(
            self._base,
            "POST",
            "/delete_entity",
            self._token,
            {
                "entity_id": entity_id,
                "entity_type": "agent_message",
                "reason": "migrate_neotoma_chat_dual_messages: split merged role_user/role_agent row",
            },
        )


def _neotoma_cli(argv: list[str], *, bin_name: str = "neotoma") -> dict[str, Any]:
    r = subprocess.run(
        [bin_name, *argv],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"neotoma {' '.join(argv)} failed ({r.returncode}): {r.stderr or r.stdout}"
        )
    out = r.stdout.strip()
    if not out:
        return {}
    return json.loads(out)


class CliBackend:
    def __init__(self, bin_name: str = "neotoma") -> None:
        self._bin = bin_name

    def relationships_part_of_page(self, offset: int, limit: int) -> dict[str, Any]:
        return _neotoma_cli(
            [
                "relationships",
                "list",
                "--relationship-type",
                "PART_OF",
                "--limit",
                str(limit),
                "--offset",
                str(offset),
            ],
            bin_name=self._bin,
        )

    def agent_messages_page(self, offset: int, limit: int) -> dict[str, Any]:
        return _neotoma_cli(
            [
                "entities",
                "list",
                "--type",
                "agent_message",
                "--limit",
                str(limit),
                "--offset",
                str(offset),
            ],
            bin_name=self._bin,
        )

    def find_conversation_for_message(self, message_id: str) -> str | None:
        out = _neotoma_cli(
            [
                "relationships",
                "list",
                "--source-entity-id",
                message_id,
                "--relationship-type",
                "PART_OF",
            ],
            bin_name=self._bin,
        )
        for rel in out.get("relationships") or []:
            if rel.get("target_entity_type") == "conversation":
                tid = rel.get("target_entity_id")
                if isinstance(tid, str):
                    return tid
        out2 = _neotoma_cli(
            [
                "relationships",
                "list",
                "--target-entity-id",
                message_id,
                "--relationship-type",
                "PART_OF",
            ],
            bin_name=self._bin,
        )
        for rel in out2.get("relationships") or []:
            if rel.get("source_entity_type") == "conversation":
                sid = rel.get("source_entity_id")
                if isinstance(sid, str):
                    return sid
        return None

    def store_agent_message(
        self,
        *,
        role: str,
        content: str,
        turn_key: str,
        idempotency_key: str,
    ) -> str:
        entity = {
            "entity_type": "agent_message",
            "schema_version": "1.3.0",
            "role": role,
            "content": content,
            "turn_key": turn_key,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump([entity], f)
            path = f.name
        try:
            out = _neotoma_cli(
                ["store", "--file", path, "--idempotency-key", idempotency_key],
                bin_name=self._bin,
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        ents = out.get("entities")
        if not ents or not isinstance(ents, list):
            raise RuntimeError(f"CLI store missing entities: {out}")
        eid = ents[0].get("entity_id")
        if not isinstance(eid, str):
            raise RuntimeError(f"CLI store missing entity_id: {out}")
        return eid

    def create_part_of(self, message_id: str, conversation_id: str) -> None:
        _neotoma_cli(
            [
                "relationships",
                "create",
                "--source-entity-id",
                message_id,
                "--target-entity-id",
                conversation_id,
                "--relationship-type",
                "PART_OF",
            ],
            bin_name=self._bin,
        )

    def delete_part_of(self, source: str, target: str) -> None:
        try:
            _neotoma_cli(
                [
                    "relationships",
                    "delete",
                    "--relationship-type",
                    "PART_OF",
                    "--source-entity-id",
                    source,
                    "--target-entity-id",
                    target,
                ],
                bin_name=self._bin,
            )
        except RuntimeError:
            pass

    def delete_agent_message(self, entity_id: str) -> None:
        _neotoma_cli(
            [
                "entities",
                "delete",
                entity_id,
                "agent_message",
                "--reason",
                "migrate_neotoma_chat_dual_messages: split merged role_user/role_agent row",
            ],
            bin_name=self._bin,
        )


def _inner_snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    snap = entity.get("snapshot")
    if not isinstance(snap, dict):
        return {}
    inner = snap.get("snapshot")
    if isinstance(inner, dict):
        return inner
    return snap


def _is_merged_turn(inner: dict[str, Any]) -> bool:
    ru = inner.get("role_user")
    ra = inner.get("role_agent")
    if not isinstance(ru, str) or not isinstance(ra, str):
        return False
    if not ru.strip() or not ra.strip():
        return False
    return True


def _unlink_message_from_conversation(
    b: Backend, message_id: str, conversation_id: str
) -> None:
    b.delete_part_of(message_id, conversation_id)
    b.delete_part_of(conversation_id, message_id)


def _paginate_agent_messages(b: Backend, *, page_size: int, max_pages: int | None):
    offset = 0
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        out = b.agent_messages_page(offset, page_size)
        entities = out.get("entities") or []
        total = int(out.get("total") or 0)
        yield from entities
        offset += len(entities)
        pages += 1
        if not entities or offset >= total:
            break


def _fix_inverted_part_of(
    b: Backend, *, execute: bool, rel_limit: int
) -> tuple[int, int]:
    fixed = 0
    scanned = 0
    offset = 0
    while offset < rel_limit:
        out = b.relationships_part_of_page(offset, 100)
        rels = out.get("relationships") or []
        if not rels:
            break
        for rel in rels:
            scanned += 1
            st = rel.get("source_entity_type")
            tt = rel.get("target_entity_type")
            sid = rel.get("source_entity_id")
            tid = rel.get("target_entity_id")
            if not isinstance(sid, str) or not isinstance(tid, str):
                continue
            if st == "conversation" and tt == "agent_message":
                if execute:
                    b.delete_part_of(sid, tid)
                    b.create_part_of(tid, sid)
                fixed += 1
        offset += len(rels)
        if len(rels) < 100:
            break
    return scanned, fixed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform writes; default is dry-run",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use `neotoma` CLI (recommended if HTTP bearer returns 401)",
    )
    parser.add_argument(
        "--neotoma-bin",
        default="neotoma",
        help="Neotoma CLI binary name or path (with --cli)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Max agent_message list pages",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size for agent_message list",
    )
    args = parser.parse_args()

    execute = bool(args.execute)

    if args.cli:
        backend: Backend = CliBackend(args.neotoma_bin)
        print(
            f"transport=CLI ({args.neotoma_bin})  mode={'EXECUTE' if execute else 'DRY-RUN'}"
        )
    else:
        base = os.environ.get(
            "NEOTOMA_API_URL", "https://neotoma.markmhendrickson.com"
        ).rstrip("/")
        token = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
        if not token:
            print(
                "NEOTOMA_BEARER_TOKEN is required for HTTP mode, or pass --cli.",
                file=sys.stderr,
            )
            return 1
        backend = HttpBackend(base, token)
        print(f"transport=HTTP {base}  mode={'EXECUTE' if execute else 'DRY-RUN'}")

    inv_scanned, inv_fixed = _fix_inverted_part_of(
        backend, execute=execute, rel_limit=50_000
    )
    print(f"Inverted PART_OF: scanned={inv_scanned} to_fix={inv_fixed}")

    merged_candidates = 0
    split_done = 0
    skipped = 0

    for ent in _paginate_agent_messages(
        backend, page_size=args.page_size, max_pages=args.max_pages
    ):
        if not isinstance(ent, dict):
            continue
        eid = ent.get("entity_id")
        if not isinstance(eid, str):
            continue
        inner = _inner_snapshot(ent)
        if not _is_merged_turn(inner):
            continue
        merged_candidates += 1
        ru = str(inner.get("role_user", "")).strip()
        ra = str(inner.get("role_agent", "")).strip()
        conv = backend.find_conversation_for_message(eid)
        if not conv:
            print(f"SKIP merged {eid}: no conversation link found")
            skipped += 1
            continue

        print(
            f"MERGED {eid} -> conv {conv} (user {len(ru)} chars, assistant {len(ra)} chars)"
        )
        if not execute:
            split_done += 1
            continue

        u_key = f"migrate-dual-{eid}-user"
        a_key = f"migrate-dual-{eid}-assistant"
        uid = backend.store_agent_message(
            role="user",
            content=ru,
            turn_key=f"{eid}:migrated:user",
            idempotency_key=u_key,
        )
        aid = backend.store_agent_message(
            role="assistant",
            content=ra,
            turn_key=f"{eid}:migrated:assistant",
            idempotency_key=a_key,
        )
        backend.create_part_of(uid, conv)
        backend.create_part_of(aid, conv)
        _unlink_message_from_conversation(backend, eid, conv)
        backend.delete_agent_message(eid)
        split_done += 1

    print(
        f"Merged turns: candidates={merged_candidates} "
        f"processed_or_would_process={split_done} skipped_no_conv={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
