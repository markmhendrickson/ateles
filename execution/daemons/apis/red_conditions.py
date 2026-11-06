"""Record Apis's red alerts once per condition, durably, for the delivery check.

"Red" is not a field: it is a leading 🔴 in the text of messages
``swarm_dispatch.py`` sends with ``Priority.BLOCKER``. Apis's own "already
told" memory is in-process, so every restart re-sends the same alerts (about
500 a day in the log). This module gives the operator one durable record per
condition instead:

    ApisNotifier.send  -->  state file (this module, first-seen only)
    codex_delivery_check.py (hourly)  -->  codex-delivery-findings.json
    an existing Codex heartbeat  -->  tells the operator once per finding

``ApisNotifier`` changes nothing about delivery or logging: it records, then
always calls the base ``send`` with the same arguments and returns its result.
Recording never raises into a send.

Only a kind and a subject (a PR ref, a ref plus lens name, or a release
version) are stored. No free text from a message (PR titles, stderr tails,
script paths, error text) is ever written, because the delivery check publishes
the file's contents to a session that treats them as data.

The state file is ``~/.local/state/ateles/apis-red-conditions.json`` unless
``APIS_RED_CONDITIONS_PATH`` is set:

    {"schema_version": 1, "created_at": iso,
     "conditions": {key: {"kind", "subject", "raised_at", "backlog"}}}

Backlog: the approved-PR conditions already open when this was introduced are
recorded with ``backlog: true`` (first seen within ``BACKLOG_WINDOW_SECONDS``
of the file's ``created_at``) so the reader does not announce them all at once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.notify import Notifier, Priority

log = logging.getLogger("apis.red_conditions")

RED_MARKER = "🔴"
EXCLUDED_PHRASE = "NO FORMAL REVIEW"
SCHEMA_VERSION = 1
BACKLOG_WINDOW_SECONDS = 7200
KEY_PREFIX = "apis-red"
STATE_ENV = "APIS_RED_CONDITIONS_PATH"
UNRECOGNISED = "unrecognised"

APPROVED_KINDS = frozenset(
    {"approved-conflicting", "approved-blocked", "approved-mergeable"}
)

# (fixed phrase in the alert text, kind). Order matters only for readability:
# the phrases are mutually exclusive in swarm_dispatch.py.
_PHRASES: tuple[tuple[str, str], ...] = (
    ("rotted to CONFLICTING", "approved-conflicting"),
    ("is blocked by a required check", "approved-blocked"),
    ("has been APPROVED and mergeable", "approved-mergeable"),
    ("has no formal review after", "review-escalation"),
    ("the author pushed a revision", "revision-stuck"),
    ("lens has produced no verdict", "lens-no-verdict"),
)
KNOWN_KINDS = frozenset(kind for _, kind in _PHRASES) | {"release-failed"}

_REF_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+")
_LENS_RE = re.compile(r"the '([A-Za-z0-9_.-]{1,40})' lens")
_VERSION_RE = re.compile(r"🔴 Release ([0-9A-Za-z][0-9A-Za-z._+-]{0,39})(?=\s|$)")
_KEY_BAD_RE = re.compile(r"[^A-Za-z0-9_.:@-]")


def is_surfaced_red(message: str) -> bool:
    """A red alert the operator should hear about once."""
    text = str(message or "").strip()
    return text.startswith(RED_MARKER) and EXCLUDED_PHRASE not in text


def classify(message: str) -> tuple[str, str]:
    """Return ``(kind, subject)``; subject is a ref, ref + lens, or version."""
    text = str(message or "").strip()
    ref_match = _REF_RE.search(text)
    ref = ref_match.group(0) if ref_match else ""
    for phrase, kind in _PHRASES:
        if phrase not in text:
            continue
        if not ref:
            break
        if kind == "lens-no-verdict":
            lens = _LENS_RE.search(text)
            if not lens:
                break
            return kind, f"{ref} {lens.group(1)}"
        return kind, ref
    version = _VERSION_RE.match(text)
    if version:
        return "release-failed", version.group(1)
    return UNRECOGNISED, ""


def condition_key(message: str) -> tuple[str, str, str]:
    """Return ``(key, kind, subject)``. Age and mergeable-state text do not
    enter the identity: kind plus subject does."""
    kind, subject = classify(message)
    if kind == UNRECOGNISED:
        text = re.sub(r"\d+", "#", str(message or "").strip())
        ident = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    else:
        ident = subject
    key = _KEY_BAD_RE.sub("_", f"{KEY_PREFIX}:{kind}:{ident}")[:200]
    return key, kind, subject


def default_state_path() -> Path:
    override = os.environ.get(STATE_ENV, "").strip()
    return (
        Path(override)
        if override
        else Path.home() / ".local/state/ateles/apis-red-conditions.json"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


class StateUnreadable(Exception):
    """The state file exists but is not a valid conditions document."""


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StateUnreadable(type(exc).__name__) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StateUnreadable("not JSON") from exc
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != SCHEMA_VERSION
        or not isinstance(doc.get("conditions"), dict)
    ):
        raise StateUnreadable("wrong shape")
    return doc


def _write_atomic(
    path: Path, doc: dict[str, Any], *, create_only: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        if create_only:
            try:
                os.link(tmp_name, path)  # fails if the file already exists
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        else:
            os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class ApisNotifier(Notifier):
    """``Notifier`` that also records surfaced red alerts, once per condition."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            self._ensure_state_file()
        except Exception as exc:  # noqa: BLE001 — never stop Apis over this
            log.error(
                "red-conditions: could not create state file: %s", type(exc).__name__
            )

    def _ensure_state_file(self) -> None:
        path = default_state_path()
        if path.exists():
            return
        doc = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _now_iso(),
            "conditions": {},
        }
        try:
            _write_atomic(path, doc, create_only=True)
        except FileExistsError:
            pass

    def send(
        self,
        message: str,
        priority: Priority | str = Priority.INFO,
        handler: str = "",
        bypass_silence: bool = False,
        dedupe_key: str | None = None,
        email_eligible: bool = True,
    ) -> bool:
        try:
            if is_surfaced_red(message):
                self._record_red(message)
        except Exception as exc:  # noqa: BLE001 — recording must never touch the send
            log.error("red-conditions: could not record alert: %s", type(exc).__name__)
        return super().send(
            message,
            priority,
            handler,
            bypass_silence,
            dedupe_key,
            email_eligible,
        )

    def _record_red(self, message: str) -> None:
        key, kind, subject = condition_key(message)
        path = default_state_path()
        with self._notification_lock:
            try:
                doc = _read_state(path)
            except FileNotFoundError:
                # The constructor creates the file, so a miss here is real.
                # Do not recreate it: the delivery check reports "missing" as
                # a failed check, which is the visible signal we want.
                log.error("red-conditions: state file missing; alert not recorded")
                return
            except StateUnreadable as exc:
                log.error(
                    "red-conditions: state file unreadable (%s); not overwriting", exc
                )
                return
            conditions = doc["conditions"]
            if key in conditions:
                return
            now = datetime.now(timezone.utc)
            created = _parse_iso(doc.get("created_at"))
            backlog = bool(
                kind in APPROVED_KINDS
                and created is not None
                and 0 <= (now - created).total_seconds() <= BACKLOG_WINDOW_SECONDS
            )
            conditions[key] = {
                "kind": kind,
                "subject": subject,
                "raised_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "backlog": backlog,
            }
            _write_atomic(path, doc)
