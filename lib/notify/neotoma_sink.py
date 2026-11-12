"""
lib/notify/neotoma_sink.py — also store each daemon alert in Neotoma.

The operator wants every daemon alert to reach him through his harness
session, which watches Neotoma. ``Notifier.send`` hands each alert here, at
every level, in addition to (never instead of) its existing delivery. The
alert becomes an ``escalation`` record. This module never changes what
``send`` returns and never raises into the caller.

Mode (env ``ATELES_NOTIFY_NEOTOMA_SINK``, read on every call):
    off      nothing is recorded
    shadow   (default) the would-store record is appended to
             ``shadow-YYYYMMDD.jsonl``; no network call is made
    on       the record is stored in Neotoma by a bounded background worker

Mapping onto the existing ``escalation`` type (no new type, no undeclared
fields; ``canonical_name_fields`` is ``["title"]`` so the title is the
identity and the same title is the same entity):
    title         "[<daemon>] <dedup key>", one line, length-capped
    body          the message text, stored as data
    severity      critical->critical, blocker->error,
                  operator_decision->warning, warn->warning, info->info
    status        "open"
    source_agent  "<daemon>@ateles-swarm"
    tags          "notify", "level:<level>", "daemon:<name>"
The inbox filter is: entity_type escalation, tag ``notify``, status ``open``,
plus the ``level:`` tag. Tags, level, daemon and entity fields are always
derived from code-controlled values, never from message text: message text
can embed attacker-influenced strings (see the security note in held_view.py).

Load bounds. A key is written at most once per process per rolling window
(``ATELES_NOTIFY_NEOTOMA_SINK_WINDOW_SECONDS``, default 6h). When the caller
passes no ``dedupe_key`` one is derived from handler plus the message with
digits and hex runs normalized, so a repeat with different numbers collapses.
One worker thread keeps at most one write in flight, with a short timeout.

Failure: no credential, HTTP error, timeout or a full queue appends the
record to ``spool-YYYYMMDD.jsonl`` (0600) and logs one warning per condition.
The credential is only the NEOTOMA_BASE_URL + NEOTOMA_BEARER_TOKEN pair the
notifier already uses for the priority_rubric; there is no other.

Preview volume before switching on:
    python3 -m lib.notify.neotoma_sink --summary [--days N]
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODE_ENV = "ATELES_NOTIFY_NEOTOMA_SINK"
WINDOW_ENV = "ATELES_NOTIFY_NEOTOMA_SINK_WINDOW_SECONDS"
DIR_ENV = "ATELES_NOTIFY_SINK_DIR"
DEFAULT_WINDOW_SECONDS = 6 * 3600
HTTP_TIMEOUT_SECONDS = 5.0
QUEUE_MAX = 50
EXIT_FLUSH_SECONDS = 3.0
TITLE_MAX = 200
BODY_MAX = 8000
KEY_MAX = 120

SEVERITY = {
    "critical": "critical",
    "blocker": "error",
    "operator_decision": "warning",
    "warn": "warning",
    "info": "info",
}

_DIGITS_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b")
_SPACE_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f  \x85]+")
_DAEMON_RE = re.compile(r"[^a-z0-9_.-]+")

_lock = threading.Lock()
_last_write: dict[str, float] = {}  # key -> monotonic time of the last accepted write
_stored: dict[str, dict[str, Any]] = {}  # key -> {"entity_id", "title", "daemon"}
_warned: set[str] = set()
_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
_worker: threading.Thread | None = None
_atexit_registered = False


# ── helpers ──────────────────────────────────────────────────────────────────


def mode() -> str:
    raw = os.environ.get(MODE_ENV, "shadow").strip().lower()
    return raw if raw in ("off", "shadow", "on") else "shadow"


def _window_seconds() -> float:
    try:
        value = float(os.environ.get(WINDOW_ENV, "").strip() or DEFAULT_WINDOW_SECONDS)
        return value if value > 0 else DEFAULT_WINDOW_SECONDS
    except ValueError:
        return DEFAULT_WINDOW_SECONDS


def sink_dir() -> Path:
    raw = os.environ.get(DIR_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".ateles-work" / "notify-sink"


def _one_line(text: str, limit: int) -> str:
    cleaned = _SPACE_RE.sub(" ", _CTRL_RE.sub(" ", str(text))).strip()
    return cleaned[:limit].rstrip()


def sanitize_daemon(handler: str) -> str:
    name = _DAEMON_RE.sub("-", str(handler or "").strip().lower()).strip("-.")[:40]
    return name or "unknown"


def derive_key(handler: str, message: str) -> str:
    """Stable key for the *shape* of an alert: digits and hex runs normalized."""
    norm = _HEX_RE.sub("#", str(message).lower())
    norm = _DIGITS_RE.sub("#", norm)
    norm = _SPACE_RE.sub(" ", norm).strip()
    digest = hashlib.sha256(
        f"{sanitize_daemon(handler)}\0{norm}".encode("utf-8", "replace")
    ).hexdigest()[:8]
    return f"{_one_line(norm, 90)} ~{digest}"


def build_title(daemon: str, key: str) -> str:
    return _one_line(f"[{daemon}] {_one_line(key, KEY_MAX)}", TITLE_MAX)


def build_record(
    *, message: str, level: str, handler: str, dedupe_key: str | None
) -> dict[str, Any]:
    daemon = sanitize_daemon(handler)
    key = _one_line(dedupe_key, KEY_MAX) if dedupe_key else derive_key(handler, message)
    title = build_title(daemon, key)
    return {
        "kind": "store",
        "ts": datetime.now(timezone.utc).isoformat(),
        "key": key,
        "daemon": daemon,
        "level": level,
        "entity": {
            "entity_type": "escalation",
            "title": title,
            "body": str(message)[:BODY_MAX],
            "severity": SEVERITY[level],
            "source_agent": f"{daemon}@ateles-swarm",
            "status": "open",
            "tags": ["notify", f"level:{level}", f"daemon:{daemon}"],
        },
    }


def _append(prefix: str, record: dict[str, Any]) -> None:
    d = sink_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{prefix}-{datetime.now().strftime('%Y%m%d')}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _warn_once(condition: str, detail: str) -> None:
    with _lock:
        if condition in _warned:
            return
        _warned.add(condition)
    log.warning("[notify-sink] %s: %s (further occurrences spooled silently)", condition, detail)


def _spool(record: dict[str, Any], condition: str, detail: str) -> None:
    _warn_once(condition, detail)
    try:
        _append("spool", {**record, "spool_reason": condition})
    except Exception as exc:  # noqa: BLE001 — never raise into the caller
        _warn_once("spool-unwritable", str(exc))


def _credentials() -> tuple[str, str]:
    """The pair the notifier already uses to load the priority_rubric."""
    from . import notifier  # noqa: PLC0415 — avoids an import cycle

    return (notifier.NEOTOMA_BASE_URL or "").rstrip("/"), notifier.NEOTOMA_BEARER_TOKEN or ""


def _http_post(url: str, token: str, payload: dict[str, Any]) -> Any:
    import httpx  # noqa: PLC0415

    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {}


# ── public API ───────────────────────────────────────────────────────────────


def record(*, message: str, level: str, handler: str = "", dedupe_key: str | None = None) -> None:
    """Hand one alert to the sink. Never raises, never blocks on the network."""
    try:
        m = mode()
        if m == "off" or level not in SEVERITY:
            return
        rec = build_record(
            message=message, level=level, handler=handler, dedupe_key=dedupe_key
        )
        now = time.monotonic()
        with _lock:
            last = _last_write.get(rec["key"])
            if last is not None and now - last < _window_seconds():
                return
            _last_write[rec["key"]] = now
        if m == "shadow":
            _append("shadow", rec)
            return
        _enqueue(rec)
    except Exception as exc:  # noqa: BLE001
        _warn_once("record-failed", f"{type(exc).__name__}: {exc}")


def resolve(key: str) -> None:
    """Mark the entity this process stored for ``key`` resolved. Never raises."""
    try:
        m = mode()
        if m == "off" or not key:
            return
        k = _one_line(key, KEY_MAX)
        with _lock:
            _last_write.pop(k, None)  # a recurrence after resolution is stored again
            known = _stored.get(k)
        if m == "shadow":
            _append("shadow", {"kind": "resolve", "ts": datetime.now(timezone.utc).isoformat(), "key": k})
            return
        if known is None and not _pending_for(k):
            return
        _enqueue({"kind": "resolve", "ts": datetime.now(timezone.utc).isoformat(), "key": k})
    except Exception as exc:  # noqa: BLE001
        _warn_once("resolve-failed", f"{type(exc).__name__}: {exc}")


def flush(timeout: float = EXIT_FLUSH_SECONDS) -> bool:
    """Wait (bounded) for the worker to drain; True when drained."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


# ── worker ───────────────────────────────────────────────────────────────────

_pending_keys: Counter = Counter()


def _pending_for(key: str) -> bool:
    with _lock:
        return _pending_keys[key] > 0


def _enqueue(rec: dict[str, Any]) -> None:
    global _worker, _atexit_registered
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="notify-neotoma-sink", daemon=True)
            _worker.start()
        if not _atexit_registered:
            atexit.register(_drain_at_exit)
            _atexit_registered = True
        if rec["kind"] == "store":
            _pending_keys[rec["key"]] += 1
    try:
        _queue.put_nowait(rec)
    except queue.Full:
        if rec["kind"] == "store":
            with _lock:
                _pending_keys[rec["key"]] -= 1
        _spool(rec, "queue-full", f"more than {QUEUE_MAX} writes waiting")


def _drain_at_exit() -> None:
    flush(EXIT_FLUSH_SECONDS)
    while True:  # whatever the worker did not reach is spooled, not lost
        try:
            rec = _queue.get_nowait()
        except queue.Empty:
            return
        _spool(rec, "exit-before-write", "process exiting with writes still queued")
        _queue.task_done()


def _run() -> None:
    while True:
        rec = _queue.get()
        try:
            _process(rec)
        except Exception as exc:  # noqa: BLE001
            try:
                _spool(rec, "worker-error", f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            if rec.get("kind") == "store":
                with _lock:
                    _pending_keys[rec["key"]] -= 1
            _queue.task_done()


def _process(rec: dict[str, Any]) -> None:
    base, token = _credentials()
    if not base or not token:
        _spool(rec, "no-credential", "NEOTOMA_BASE_URL/NEOTOMA_BEARER_TOKEN pair not available")
        return
    try:
        if rec["kind"] == "store":
            _do_store(base, token, rec)
        else:
            _do_resolve(base, token, rec)
    except Exception as exc:  # noqa: BLE001
        _spool(rec, "write-failed", f"{type(exc).__name__}: {str(exc)[:160]}")


def _idem(title: str, rec: dict[str, Any], suffix: str) -> str:
    bucket = int(time.time() // _window_seconds())
    h = hashlib.sha256(title.encode("utf-8", "replace")).hexdigest()[:16]
    return f"notify-sink-{suffix}-{h}-{bucket}"


def _do_store(base: str, token: str, rec: dict[str, Any]) -> None:
    ent = rec["entity"]
    payload = {
        "idempotency_key": _idem(ent["title"], rec, "open"),
        "observation_source": "workflow_state",
        "entities": [ent],
    }
    data = _http_post(f"{base}/store", token, payload)
    entity_id = None
    if isinstance(data, dict):
        for e in data.get("entities") or []:
            if isinstance(e, dict) and e.get("entity_id"):
                entity_id = e["entity_id"]
                break
    with _lock:
        _stored[rec["key"]] = {"entity_id": entity_id, "title": ent["title"], "daemon": rec["daemon"]}
    try:
        _append("stored", {**{k: v for k, v in rec.items() if k != "entity"}, "title": ent["title"], "entity_id": entity_id, "level": rec["level"]})
    except Exception:  # noqa: BLE001
        pass


def _do_resolve(base: str, token: str, rec: dict[str, Any]) -> None:
    with _lock:
        known = _stored.get(rec["key"])
    if not known:
        return
    resolved_at = rec["ts"]
    if known.get("entity_id"):
        for field, value in (("status", "resolved"), ("resolved_at", resolved_at)):
            _http_post(
                f"{base}/correct",
                token,
                {
                    "entity_id": known["entity_id"],
                    "entity_type": "escalation",
                    "field": field,
                    "value": value,
                    "idempotency_key": _idem(known["title"], rec, f"resolve-{field}-{resolved_at}"),
                },
            )
    else:
        # The store response carried no entity id; the title is the identity,
        # so a same-title store updates the same entity.
        _http_post(
            f"{base}/store",
            token,
            {
                "idempotency_key": _idem(known["title"], rec, f"resolve-{resolved_at}"),
                "observation_source": "workflow_state",
                "entities": [
                    {
                        "entity_type": "escalation",
                        "title": known["title"],
                        "status": "resolved",
                        "resolved_at": resolved_at,
                    }
                ],
            },
        )
    with _lock:
        _stored.pop(rec["key"], None)


# ── read-only summary CLI ────────────────────────────────────────────────────


def summarize(days: int = 7, directory: Path | None = None) -> dict[str, Any]:
    d = directory or sink_dir()
    cutoff = (datetime.now() - timedelta(days=max(days, 1) - 1)).strftime("%Y%m%d")
    out: dict[str, Any] = {"files": 0, "rows": []}
    for prefix in ("shadow", "spool", "stored"):
        for path in sorted(d.glob(f"{prefix}-*.jsonl")):
            day = path.stem.split("-", 1)[1]
            if day < cutoff:
                continue
            out["files"] += 1
            for line in path.read_text(errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("kind", "store") != "store":
                    continue
                out["rows"].append((day, prefix, r.get("level", "?"), r.get("daemon", "?")))
    return out


def _format_summary(s: dict[str, Any]) -> str:
    rows = s["rows"]
    lines = [f"notify sink records ({len(rows)} rows from {s['files']} files)"]
    if not rows:
        return lines[0] + "\nnothing recorded yet"
    for title, idx in (("per day and source", (0, 1)), ("per level", (2,)), ("per daemon", (3,))):
        lines.append(f"\n{title}:")
        for key, n in sorted(Counter(tuple(r[i] for i in idx) for r in rows).items()):
            lines.append(f"  {' '.join(key):<40} {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m lib.notify.neotoma_sink", description=__doc__.split("\n\n")[0])
    ap.add_argument("--summary", action="store_true", help="print counts per day, level and daemon")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dir", type=Path, default=None)
    args = ap.parse_args(argv)
    if not args.summary:
        ap.print_help()
        return 2
    print(_format_summary(summarize(args.days, args.dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
