"""Capture Neotoma's state *before* anything restarts it.

The problem this exists to solve
--------------------------------

On 2026-09-01 a hosted Neotoma instance became unusable for ~30 minutes. Three
agents lost writes. `flyctl status` said `started` throughout, `flyctl machine
restart` printed "No health checks found", and the CHECKS column was empty, so
nothing anywhere reported a problem.

The instance was restarted to recover availability. That worked, and it
destroyed every piece of evidence: `flyctl logs` retains only a short rolling
window (measured at ~100 lines / ~9 seconds on this app), so the pre-restart
window was gone before anyone thought to look at it. The proximate cause of
that outage is now permanently unrecoverable.

Nothing forced that tradeoff to be deliberate. Availability and diagnosis were
in tension, and the person holding the lever had no reason to weigh them
because "capture first" was a thing to remember rather than a thing the code
did. Memory is not a control.

So: this module is the capture step, and it is designed to run *inside* the
recovery path rather than beside it. `recover_with_capture()` takes the
recovery action as a callable and will not invoke it until a snapshot has been
written to durable local disk. A caller cannot restart without capturing,
because the restart is only reachable through the capture.

Why local disk, and not Neotoma
-------------------------------

The obvious home for a diagnostic record is a `daemon_report` entity. That is
exactly wrong here: the condition being diagnosed is "Neotoma cannot serve
requests". Writing the evidence of a Neotoma outage into Neotoma is a
dependency loop that fails precisely when it is needed.

Snapshots therefore land on local disk first, always, unconditionally. Uploading
them to Neotoma later, once it is healthy again, is a separate and strictly
optional step (`pending_snapshots()`); a failure to upload never costs the
snapshot.

What it collects, and why each item earns its place
---------------------------------------------------

Everything here is read-only and chosen against a specific hypothesis that the
2026-09-01 evidence left open:

* `fly_logs` — the rolling window that the restart would erase. Highest value,
  collected first, because it is the only item with a hard deadline.
* `fly_machine_status` — the event log, including `exit_code` and `oom_killed`.
  Distinguishes a crash-and-restart from a hang.
* `process_table` + `proc_status` — RSS vs VSZ and CPU for the Node process.
  Separates real memory pressure from an event loop that is merely blocked;
  on 2026-09-01 RSS was 476MB against 8GB, which ruled out OOM entirely.
* `loadavg` / `meminfo` — whether the box is starved or idle. An idle box with
  an unresponsive server is a very different bug from a saturated one.
* `db_file_sizes` — WAL growth. A WAL that is not checkpointing is a specific,
  recognisable failure with a specific remedy. (Recorded as sizes over time
  rather than interpreted: which backend is actually in play has itself been
  a point of confusion on this deployment, so the snapshot captures the
  measurement and leaves the semantics to whoever reads it.)
* `db_backend` — which storage backend the process *actually* loaded, read
  from the running process rather than from config. `NEOTOMA_DB_BACKEND` was
  found set to a value the shipped build does not implement, so the env var
  cannot be trusted to say what is running.
* `event_loop_probe` — times `/health` *from inside the machine*. This is the
  single most diagnostic measurement available, and it is worth explaining
  why: `/health` reads one small file synchronously and returns. It touches no
  database. If it is slow from inside the VM, the network, the proxy, the
  volume, and memory are all excluded by construction, and the only remaining
  explanation is that the Node event loop is blocked. It converts a vague
  "the server is slow" into a specific claim about where the time goes.

Deliberately not collected: query text or response bodies. Those carry the
operator's personal data, and a diagnostic artifact that leaks PII is a worse
problem than the outage. Sizes and timings only.

Failure posture
---------------

Every collector is individually wrapped. A collector that fails records its
error into the snapshot and the capture continues, because a partial snapshot
is enormously more useful than an exception thrown in the middle of an
incident. The capture as a whole is bounded by `budget_seconds` (default 45s):
recovery is genuinely urgent, and a capture that delays it indefinitely would
be a worse failure than the missing evidence. When the budget expires the
snapshot is written with whatever has been collected, flagged `budget_expired`,
and recovery proceeds.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_APP = os.environ.get("NEOTOMA_FLY_APP", "neotoma-markmhendrickson")
DEFAULT_MACHINE = os.environ.get("NEOTOMA_FLY_MACHINE", "")
DEFAULT_BUDGET_SECONDS = float(os.environ.get("NEOTOMA_FORENSICS_BUDGET_SECONDS", "45"))
DEFAULT_HEALTH_PORT = os.environ.get("NEOTOMA_HTTP_PORT", "3180")

# Node's `/health` handler is a synchronous readFileSync plus a JSON response.
# Anything above this from *inside* the VM means the event loop is blocked,
# because no other component is in the path.
EVENT_LOOP_BLOCKED_MS = float(os.environ.get("NEOTOMA_EVENT_LOOP_BLOCKED_MS", "1000"))


def snapshot_dir() -> Path:
    """Durable, local, and deliberately not inside the repo or /tmp.

    /tmp is cleared on reboot, and a snapshot that does not survive the
    incident it documents is not a snapshot.
    """
    root = os.environ.get("NEOTOMA_FORENSICS_DIR")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".local" / "state" / "ateles" / "neotoma-forensics"


@dataclass
class Collector:
    """One named, individually-failable piece of evidence."""

    name: str
    run: Callable[[], Any]
    # Ordered by how fast the evidence decays, not by how interesting it is.
    priority: int = 50


@dataclass
class Snapshot:
    reason: str
    started_at: str
    items: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    budget_expired: bool = False
    duration_seconds: float = 0.0
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "budget_expired": self.budget_expired,
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
            },
            "items": self.items,
            "errors": self.errors,
            "analysis": self.analysis(),
        }

    def analysis(self) -> dict[str, Any]:
        """Turn the raw evidence into the one or two claims it supports.

        A snapshot nobody interprets is a file nobody opens. This does the
        narrow, mechanical part of the interpretation — the part that is a
        direct consequence of what /health does — and leaves the rest alone.
        """
        out: dict[str, Any] = {}
        probe = self.items.get("event_loop_probe") or {}
        ms = probe.get("internal_health_ms")
        if isinstance(ms, (int, float)):
            blocked = ms >= EVENT_LOOP_BLOCKED_MS
            out["event_loop_blocked"] = blocked
            out["event_loop_note"] = (
                f"/health took {ms:.0f}ms from inside the VM. It performs one small "
                "synchronous file read and no database access, so this time is the "
                "event loop being unavailable, not I/O, not the network, not the "
                "Fly proxy."
                if blocked
                else f"/health responded in {ms:.0f}ms from inside the VM; the event "
                "loop was responsive at capture time."
            )
        mem = self.items.get("proc_status") or {}
        rss_kb, total_kb = mem.get("VmRSS_kB"), mem.get("MemTotal_kB")
        if isinstance(rss_kb, int) and isinstance(total_kb, int) and total_kb:
            pct = 100.0 * rss_kb / total_kb
            out["rss_pct_of_total"] = round(pct, 1)
            out["memory_pressure"] = pct >= 80.0
        return out


def _run(cmd: list[str], timeout: float) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _fly(args: list[str], app: str, timeout: float) -> str:
    if not shutil.which("flyctl"):
        raise RuntimeError("flyctl not on PATH")
    return _run(["flyctl", *args, "--app", app], timeout=timeout)


def _ssh_exec(app: str, machine: str, script: str, timeout: float) -> str:
    cmd = ["flyctl", "ssh", "console", "--app", app]
    if machine:
        cmd += ["--machine", machine]
    cmd += ["-C", script]
    return _run(cmd, timeout=timeout)


def _parse_kv_kb(text: str, keys: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        for key in keys:
            if line.startswith(key + ":"):
                digits = "".join(ch for ch in line if ch.isdigit())
                if digits:
                    out[f"{key}_kB"] = int(digits)
    return out


def default_collectors(app: str, machine: str) -> list[Collector]:
    """Ordered by evidence decay rate: the log window disappears first."""

    def fly_logs() -> str:
        # The single most perishable artifact. A restart erases it outright.
        return _fly(["logs", "--no-tail"], app, timeout=25)

    def fly_machine_status() -> str:
        args = ["machine", "status"]
        if machine:
            args.insert(2, machine)
        return _fly(args, app, timeout=20)

    def event_loop_probe() -> dict[str, Any]:
        # Timed from inside the VM so that the network, the Fly proxy, and TLS
        # are all excluded. Node is guaranteed present; curl is not (verified
        # absent on this image), so the timing is done in Node itself.
        script = (
            'node -e "const t=Date.now();'
            f"require('http').get('http://localhost:{DEFAULT_HEALTH_PORT}/health',"
            "r=>{r.resume();r.on('end',()=>console.log(JSON.stringify("
            "{status:r.statusCode,ms:Date.now()-t})))})"
            ".on('error',e=>console.log(JSON.stringify({error:e.message})))\""
        )
        raw = _ssh_exec(app, machine, script, timeout=60)
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "ms" in parsed:
                    return {
                        "internal_health_ms": parsed["ms"],
                        "internal_health_status": parsed.get("status"),
                    }
                return parsed
        return {"raw": raw[-2000:]}

    def process_table() -> str:
        return _ssh_exec(
            app, machine, "sh -c 'top -b -n1 2>/dev/null | head -15'", timeout=45
        )

    def proc_status() -> dict[str, Any]:
        script = (
            "sh -c 'cat /proc/loadavg; echo ---; head -4 /proc/meminfo; echo ---; "
            "for p in /proc/[0-9]*; do "
            'if grep -qs "node" "$p/comm" 2>/dev/null; then '
            'echo PID=${p##*/}; grep -E "VmRSS|VmSize|Threads" "$p/status"; fi; done\''
        )
        raw = _ssh_exec(app, machine, script, timeout=45)
        out: dict[str, Any] = {"raw": raw[-4000:]}
        out.update(_parse_kv_kb(raw, ("VmRSS", "VmSize", "MemTotal", "MemAvailable")))
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 3 and all(_is_float(p) for p in parts[:3]):
                out["loadavg"] = parts[:3]
                break
        return out

    def db_file_sizes() -> dict[str, Any]:
        # WAL growth without checkpointing is its own distinct failure, with a
        # different remedy from an event loop stall.
        raw = _ssh_exec(
            app, machine, "sh -c 'ls -la /app/data/ 2>/dev/null'", timeout=45
        )
        sizes: dict[str, int] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 9 and parts[-1].startswith("neotoma"):
                try:
                    sizes[parts[-1]] = int(parts[4])
                except (ValueError, IndexError):
                    pass
        return {"sizes_bytes": sizes, "raw": raw[-2000:]}

    def db_backend() -> dict[str, Any]:
        # Deliberately establishes what the process *loaded*, not what config
        # claims. On 2026-09-01 `NEOTOMA_DB_BACKEND=libsql` was set on a build
        # that ships no libsql client at all, and reasoning from the env var
        # would have sent the investigation into the wrong locking model.
        # Only presence/absence is recorded — never env values, which on this
        # instance include bearer tokens and encryption keys.
        script = (
            "sh -c 'for m in @libsql/client better-sqlite3; do "
            'if [ -d "/app/node_modules/$m" ]; then echo "present:$m"; '
            'else echo "absent:$m"; fi; done; '
            "echo ---adapters---; ls /app/dist/repositories/ 2>/dev/null'"
        )
        raw = _ssh_exec(app, machine, script, timeout=45)
        return {
            "modules_present": [
                line.split(":", 1)[1]
                for line in raw.splitlines()
                if line.startswith("present:")
            ],
            "modules_absent": [
                line.split(":", 1)[1]
                for line in raw.splitlines()
                if line.startswith("absent:")
            ],
            "raw": raw[-1500:],
        }

    return [
        Collector("fly_logs", fly_logs, priority=0),
        Collector("event_loop_probe", event_loop_probe, priority=10),
        Collector("fly_machine_status", fly_machine_status, priority=20),
        Collector("proc_status", proc_status, priority=30),
        Collector("process_table", process_table, priority=40),
        Collector("db_file_sizes", db_file_sizes, priority=50),
        Collector("db_backend", db_backend, priority=60),
    ]


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def capture(
    reason: str,
    *,
    app: str = DEFAULT_APP,
    machine: str = DEFAULT_MACHINE,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    collectors: list[Collector] | None = None,
    directory: Path | None = None,
) -> Snapshot:
    """Collect evidence and write it to durable local disk.

    Never raises for a collector failure: an incident is the worst possible
    time to discover that the diagnostic tool has its own unhandled exception.
    """
    started = time.monotonic()
    snap = Snapshot(
        reason=reason,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    chosen = collectors if collectors is not None else default_collectors(app, machine)
    for collector in sorted(chosen, key=lambda c: c.priority):
        if time.monotonic() - started >= budget_seconds:
            # Recovery is urgent. Stop collecting, keep what we have, say so.
            snap.budget_expired = True
            snap.errors[collector.name] = "skipped: capture budget expired"
            continue
        try:
            snap.items[collector.name] = collector.run()
        except Exception as exc:  # noqa: BLE001 - partial evidence beats none
            snap.errors[collector.name] = f"{type(exc).__name__}: {exc}"
    snap.duration_seconds = time.monotonic() - started
    _write(snap, directory)
    return snap


def _write(snap: Snapshot, directory: Path | None) -> None:
    target_dir = directory or snapshot_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = target_dir / f"snapshot-{stamp}.json"
        # Write-then-rename so a snapshot is never observed half-written, even
        # if the machine dies mid-capture.
        tmp = path.with_suffix(".json.partial")
        tmp.write_text(json.dumps(snap.to_dict(), indent=2, default=str))
        tmp.rename(path)
        snap.path = path
    except Exception as exc:  # noqa: BLE001
        snap.errors["_write"] = f"{type(exc).__name__}: {exc}"


def recover_with_capture(
    reason: str,
    recovery: Callable[[], Any],
    *,
    app: str = DEFAULT_APP,
    machine: str = DEFAULT_MACHINE,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    collectors: list[Collector] | None = None,
    directory: Path | None = None,
) -> tuple[Snapshot, Any]:
    """Capture first, then recover. The ordering is the entire point.

    Recovery is passed in as a callable rather than performed here so that this
    function owns the sequencing. A caller holding only this entry point cannot
    restart without having captured, because the restart is downstream of the
    capture in the same call. That is what makes "diagnose before you recover"
    a property of the code rather than a note in a runbook that whoever is
    awake at the time is expected to recall.

    The capture is bounded and never raises, so it cannot become a reason that
    an outage lasts longer.
    """
    snap = capture(
        reason,
        app=app,
        machine=machine,
        budget_seconds=budget_seconds,
        collectors=collectors,
        directory=directory,
    )
    return snap, recovery()


def pending_snapshots(directory: Path | None = None) -> list[Path]:
    """Snapshots on disk, newest first, for later upload once Neotoma is back.

    Upload is intentionally decoupled: the snapshot's value does not depend on
    the datastore that was down being available again.
    """
    target = directory or snapshot_dir()
    if not target.exists():
        return []
    return sorted(target.glob("snapshot-*.json"), reverse=True)
