"""
execution/daemons/apis/a2a_executor.py — A2A → Neotoma task bridge for Apis.

This is the core of the A2A inbound gateway, deliberately kept free of any
`a2a-sdk` import so it is fully unit-testable without the SDK installed and is
insulated from SDK version churn. The thin SDK adapter lives in
a2a_gateway.py and calls into ``ApisTaskBridge`` here.

Flow:

    inbound A2A message (text parts)
        → ApisTaskBridge.submit(text, caller=...)
        → infer domain tags (routing.py)
        → POST /api/store creating a Neotoma `task` entity, signed with Apis's
          AAuth keypair so the task is attributed to apis@ateles-swarm
        → return A2AInboundResult(a2a_task_id, neotoma_entity_id, tags, skill)

The created Neotoma `task` entity is then picked up by the existing Apis SSE
path (apis.py: handle_event → dispatch_task) and routed to the right T4 worker.
A2A is purely an additional ingestion mouth feeding the same Neotoma queue.

Authorization (caller → gateway) is enforced in a2a_gateway.py via
lib/daemon_runtime grant_checker before submit() is reached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Path bootstrap (standalone-script imports) ──────────────────────────────────
import sys

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_DAEMON_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from routing import infer_tags_from_text, resolve_skill  # noqa: E402

log = logging.getLogger("apis.a2a")

# ── Config ──────────────────────────────────────────────────────────────────
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Default visibility for tasks created from inbound A2A calls. Private by
# default — external callers cannot make swarm work public without an explicit
# scope grant (see a2a_gateway grant checks).
A2A_DEFAULT_TASK_VISIBILITY = os.environ.get("APIS_A2A_TASK_VISIBILITY", "private")

# AAuth sub used to attribute inbound-A2A task creation.
AAUTH_SUB = "apis@ateles-swarm"

_HTTP_TIMEOUT_SECONDS = 15


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class A2AInboundResult:
    """Outcome of bridging one inbound A2A message into a Neotoma task."""

    a2a_task_id: str
    neotoma_entity_id: Optional[str]
    title: str
    tags: list[str] = field(default_factory=list)
    skill: Optional[str] = None
    status: str = "submitted"  # submitted | failed
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "submitted" and self.neotoma_entity_id is not None


# ── Message parsing ─────────────────────────────────────────────────────────


def parse_message_text(parts: list[dict[str, Any]] | str) -> str:
    """
    Flatten an A2A message's parts into a single text body.

    Accepts either a raw string or a list of A2A part dicts of the shape
    ``{"kind": "text", "text": "..."}`` (also tolerates legacy ``{"type":
    "text"}``). Non-text parts are ignored. The SDK adapter normalizes parts
    to dicts before calling here so this stays SDK-agnostic.
    """
    if isinstance(parts, str):
        return parts.strip()

    chunks: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text" and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def split_title_body(text: str) -> tuple[str, str]:
    """
    Derive a task (title, body) from a free-text message.

    Title = first non-empty line (truncated to 200 chars); body = remainder.
    """
    lines = [ln for ln in text.splitlines()]
    title = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            title = ln.strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return title[:200] or "(untitled A2A task)", body


# ── Neotoma write ───────────────────────────────────────────────────────────


def _aauth_headers(method: str, path: str) -> dict[str, str]:
    """
    Build AAuth headers for the Neotoma write so the task is attributed to
    apis@ateles-swarm. Falls back to an empty dict (bearer-only attribution)
    when the keypair is not yet minted — mirrors AAuthSigner stub behaviour.
    """
    try:
        from lib.daemon_runtime import AAuthSigner  # local import; optional dep

        signer = AAuthSigner.from_key_file("apis")
        return signer.headers(method=method, path=path)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("AAuth header build skipped: %s", exc)
        return {}


def create_neotoma_task(
    title: str,
    body: str,
    tags: list[str],
    *,
    caller: str = "",
    idempotency_key: Optional[str] = None,
    base_url: Optional[str] = None,
    bearer_token: Optional[str] = None,
    transport: Any = None,
) -> Optional[str]:
    """
    Create a Neotoma `task` entity via POST /api/store and return its entity_id.

    Mirrors the proven cotinga.create_neotoma_task pattern (urllib + Bearer)
    and adds Apis AAuth attribution headers. ``transport`` is an injection seam
    for tests: a callable ``(url, data, headers) -> dict`` that replaces the
    real HTTP call. When None, a real urllib request is made.

    Returns the new entity_id, or None on failure (logged, never raises).
    """
    base_url = (base_url or NEOTOMA_BASE_URL).rstrip("/")
    bearer_token = bearer_token if bearer_token is not None else NEOTOMA_BEARER_TOKEN

    if not bearer_token or not base_url:
        log.warning("Neotoma not configured (base_url/bearer) — task not created")
        return None

    if idempotency_key is None:
        digest = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]
        idempotency_key = f"apis-a2a-task-{digest}"

    description = body
    if caller:
        # Record provenance of the external requester in the task body so the
        # operator can see who delegated the work. Caller identity is verified
        # upstream in a2a_gateway before this is reached.
        description = (f"{body}\n\n— delegated via A2A by: {caller}").strip()

    payload = {
        "entities": [
            {
                "entity_type": "task",
                "name": title,
                "title": title,
                "description": description,
                "status": "open",
                "priority": "p2",
                "tags": tags,
                "source": "a2a",
                "visibility": A2A_DEFAULT_TASK_VISIBILITY,
            }
        ],
        "idempotency_key": idempotency_key,
    }
    data = json.dumps(payload).encode()

    path = "/api/store"
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    headers.update(_aauth_headers("POST", path))

    try:
        if transport is not None:
            resp_json = transport(url, data, headers)
        else:
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                resp_json = json.loads(resp.read())
    except Exception as exc:
        log.warning("Neotoma task creation failed for %r: %s", title, exc)
        return None

    entities = (resp_json or {}).get("entities") or []
    if entities:
        eid = entities[0].get("entity_id")
        log.info("Created Neotoma task via A2A: %r → %s", title, eid)
        return eid
    log.warning("Neotoma store returned no entities for %r", title)
    return None


# ── Bridge ──────────────────────────────────────────────────────────────────


class ApisTaskBridge:
    """
    Translates inbound A2A submissions into Neotoma tasks and remembers the
    A2A-task-id ↔ Neotoma-entity-id mapping so the gateway's get/status calls
    can report progress.

    SDK-agnostic: the gateway feeds it already-parsed text. ``store_fn`` is an
    injection seam for tests (defaults to create_neotoma_task).
    """

    def __init__(self, store_fn: Any = None) -> None:
        self._store_fn = store_fn or create_neotoma_task
        # a2a_task_id -> A2AInboundResult
        self._tasks: dict[str, A2AInboundResult] = {}

    @staticmethod
    def _task_id_for(title: str, body: str) -> str:
        digest = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:24]
        return f"apis-a2a-{digest}"

    def submit(
        self,
        message: list[dict[str, Any]] | str,
        *,
        caller: str = "",
        a2a_task_id: Optional[str] = None,
    ) -> A2AInboundResult:
        """
        Bridge one inbound A2A message to a Neotoma task.

        Idempotent on (title, body): the same content reuses the same A2A task
        id and the same Neotoma idempotency key, so retries do not duplicate.
        """
        text = parse_message_text(message)
        title, body = split_title_body(text)
        tags = infer_tags_from_text(title, body)
        skill = resolve_skill(tags)
        tid = a2a_task_id or self._task_id_for(title, body)

        # Return cached result on idempotent re-submit.
        cached = self._tasks.get(tid)
        if cached and cached.ok:
            return cached

        entity_id = self._store_fn(
            title,
            body,
            tags,
            caller=caller,
            idempotency_key=f"apis-a2a-task-{tid}",
        )

        result = A2AInboundResult(
            a2a_task_id=tid,
            neotoma_entity_id=entity_id,
            title=title,
            tags=tags,
            skill=skill,
            status="submitted" if entity_id else "failed",
            error=None if entity_id else "neotoma_store_failed",
        )
        self._tasks[tid] = result
        return result

    def get(self, a2a_task_id: str) -> Optional[A2AInboundResult]:
        """Return the recorded result for an A2A task id, or None if unknown."""
        return self._tasks.get(a2a_task_id)
