"""
lib/daemon_runtime/cutover_state.py — inventory / merge / read-back for #515.

Keeps co-located daemon-local state across the shared-clone → release-checkout
cutover. XDG relocation is out of scope. Never embeds operator data samples —
only formats and merge rules.

Public surface is pure/testable so ``isolate_daemons_to_rc_src.sh`` can call it
and effect tests can fail red on missing reconciliation or fake PID/path proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The five daemons in scope for ateles#515 cutover.
CUTOVER_DAEMONS: tuple[str, ...] = (
    "cotinga",
    "cyphorhinus",
    "piculet",
    "sylvia",
    "phoenicurus-prepare",
)

#: Relative paths under each daemon directory (or mapped dir) that hold state.
#: Locks are ephemeral and intentionally excluded.
DAEMON_STATE_FILES: dict[str, tuple[str, ...]] = {
    "cotinga": (".cotinga_last_run",),
    "cyphorhinus": (".cyphorhinus_offset",),
    "piculet": ("held_memos.json", "seen_files.json", "seen_meeting_files.json"),
    "sylvia": (".sylvia_last_run",),
    "phoenicurus-prepare": (
        ".phoenicurus_prepare_last_run",
        ".phoenicurus_prepare_last_sha",
        ".phoenicurus_prepare_spawn_count",
        ".phoenicurus_prepare_spawn_pid",
        ".phoenicurus_prepare_stale_escalated",
    ),
}

DAEMON_REL_DIRS: dict[str, str] = {
    "cotinga": "execution/daemons/cotinga",
    "cyphorhinus": "execution/daemons/cyphorhinus",
    "piculet": "execution/daemons/piculet",
    "sylvia": "execution/daemons/sylvia",
    "phoenicurus-prepare": "execution/daemons/phoenicurus-release",
}

PLIST_LABELS: dict[str, str] = {
    "cotinga": "com.ateles.cotinga",
    "cyphorhinus": "com.ateles.cyphorhinus",
    "piculet": "com.ateles.piculet",
    "sylvia": "com.ateles.sylvia",
    "phoenicurus-prepare": "com.ateles.phoenicurus-prepare",
}


@dataclass(frozen=True)
class StateFileRef:
    daemon: str
    rel_path: str
    shared_path: Path
    rc_path: Path


@dataclass
class InventoryItem:
    ref: StateFileRef
    shared_exists: bool
    rc_exists: bool
    shared_hash: str | None
    rc_hash: str | None
    identical: bool


@dataclass
class MergeResult:
    ref: StateFileRef
    status: str  # identical | copied_shared | copied_rc | merged | unresolved
    detail: str = ""


@dataclass
class ReadbackResult:
    daemon: str
    label: str
    ok: bool
    pre_pid: str | None
    post_pid: str | None
    command: str = ""
    detail: str = ""


@dataclass
class CutoverPlan:
    items: list[InventoryItem] = field(default_factory=list)
    merges: list[MergeResult] = field(default_factory=list)
    backup_dir: Path | None = None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory_state_files(shared_root: Path, rc_root: Path) -> list[InventoryItem]:
    """Enumerate every registered daemon-local state file in both trees."""
    items: list[InventoryItem] = []
    for daemon, rels in DAEMON_STATE_FILES.items():
        rel_dir = DAEMON_REL_DIRS[daemon]
        for rel in rels:
            shared_path = shared_root / rel_dir / rel
            rc_path = rc_root / rel_dir / rel
            # Also discover peer files matching known prefixes in either tree.
            items.append(_item_for(daemon, rel, shared_path, rc_path))
        # Peer discovery: extra files with known prefixes under the daemon dir.
        for root_label, root in (("shared", shared_root), ("rc", rc_root)):
            ddir = root / rel_dir
            if not ddir.is_dir():
                continue
            known = set(rels)
            for p in sorted(ddir.iterdir()):
                if not p.is_file():
                    continue
                name = p.name
                if name in known or name.startswith("."):
                    # include known dotted stamps already covered; peer dotted
                    if name in known:
                        continue
                    if not _looks_like_state_peer(daemon, name):
                        continue
                elif not _looks_like_state_peer(daemon, name):
                    continue
                shared_path = shared_root / rel_dir / name
                rc_path = rc_root / rel_dir / name
                if any(
                    i.ref.rel_path == name and i.ref.daemon == daemon for i in items
                ):
                    continue
                items.append(_item_for(daemon, name, shared_path, rc_path))
    return items


def _looks_like_state_peer(daemon: str, name: str) -> bool:
    if name.endswith(".lock") or name.endswith(".pyc"):
        return False
    if daemon == "piculet" and name.endswith(".json"):
        return name.startswith(("held_", "seen_"))
    if daemon == "cotinga" and name.startswith(".cotinga_"):
        return "lock" not in name
    if daemon == "sylvia" and name.startswith(".sylvia_"):
        return True
    if daemon == "cyphorhinus" and name.startswith(".cyphorhinus_"):
        return True
    if daemon == "phoenicurus-prepare" and name.startswith(".phoenicurus_prepare_"):
        return True
    return False


def _item_for(
    daemon: str, rel: str, shared_path: Path, rc_path: Path
) -> InventoryItem:
    s_ex = shared_path.is_file()
    r_ex = rc_path.is_file()
    s_hash = file_sha256(shared_path) if s_ex else None
    r_hash = file_sha256(rc_path) if r_ex else None
    identical = bool(s_ex and r_ex and s_hash == r_hash)
    return InventoryItem(
        ref=StateFileRef(daemon, rel, shared_path, rc_path),
        shared_exists=s_ex,
        rc_exists=r_ex,
        shared_hash=s_hash,
        rc_hash=r_hash,
        identical=identical,
    )


def snapshot_inventory(
    items: list[InventoryItem], backup_dir: Path
) -> dict[str, Any]:
    """
    Copy every existing side into ``backup_dir`` and return a manifest of hashes.

    Layout: ``backup_dir/{shared,rc}/<daemon>/<filename>``.
    """
    manifest: dict[str, Any] = {"files": []}
    for item in items:
        for side, exists, path, digest in (
            ("shared", item.shared_exists, item.ref.shared_path, item.shared_hash),
            ("rc", item.rc_exists, item.ref.rc_path, item.rc_hash),
        ):
            if not exists:
                continue
            dest = backup_dir / side / item.ref.daemon / item.ref.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            manifest["files"].append(
                {
                    "daemon": item.ref.daemon,
                    "rel_path": item.ref.rel_path,
                    "side": side,
                    "sha256": digest,
                    "backup": str(dest),
                }
            )
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def merge_state_pair(shared_path: Path, rc_path: Path, rel_path: str) -> MergeResult:
    """
    Lossless merge into ``rc_path``. Refuses (unresolved) on ambiguous conflicts
    that cannot be merged without data loss.
    """
    # Synthetic ref for result — caller may overwrite daemon.
    ref = StateFileRef("?", rel_path, shared_path, rc_path)
    s_ex, r_ex = shared_path.is_file(), rc_path.is_file()
    if not s_ex and not r_ex:
        return MergeResult(ref, "identical", "neither side present")
    if s_ex and r_ex and file_sha256(shared_path) == file_sha256(rc_path):
        return MergeResult(ref, "identical", "hashes match")
    if s_ex and not r_ex:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared_path, rc_path)
        return MergeResult(ref, "copied_shared", "rc missing; copied from shared")
    if r_ex and not s_ex:
        return MergeResult(ref, "copied_rc", "shared missing; rc kept")

    # Both exist and differ — format-specific merge.
    name = Path(rel_path).name
    try:
        if name.endswith(".json"):
            _merge_json(shared_path, rc_path, name)
            return MergeResult(ref, "merged", f"json merge applied for {name}")
        if "last_run" in name:
            _merge_max_stamp(shared_path, rc_path)
            return MergeResult(ref, "merged", "max date stamp")
        if name.endswith("_offset") or name == ".cyphorhinus_offset":
            _merge_max_int(shared_path, rc_path)
            return MergeResult(ref, "merged", "max integer offset")
        if "last_sha" in name:
            # Prefer the longer / lexicographically greater only if equal length;
            # if both non-empty and unequal, refuse — SHA conflicts are not
            # safely unionable.
            return MergeResult(
                ref,
                "unresolved",
                "divergent last_sha — refuse rather than guess",
            )
        if "spawn_" in name:
            return MergeResult(
                ref,
                "unresolved",
                "divergent spawn_* state — refuse rather than guess",
            )
        if "stale_escalated" in name:
            # Prefer non-empty; if both non-empty and differ, refuse.
            s = shared_path.read_text().strip()
            r = rc_path.read_text().strip()
            if s == r:
                return MergeResult(ref, "identical")
            if s and not r:
                rc_path.write_text(shared_path.read_text())
                return MergeResult(ref, "merged", "took shared stale_escalated")
            if r and not s:
                return MergeResult(ref, "copied_rc")
            return MergeResult(
                ref,
                "unresolved",
                "divergent stale_escalated entity ids",
            )
        # Unknown divergent text: refuse.
        return MergeResult(
            ref,
            "unresolved",
            f"no lossless merge rule for {name}",
        )
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        return MergeResult(ref, "unresolved", f"merge error: {exc}")


def _merge_json(shared_path: Path, rc_path: Path, name: str) -> None:
    shared_obj = json.loads(shared_path.read_text())
    rc_obj = json.loads(rc_path.read_text())
    if name.startswith("seen_") and isinstance(shared_obj, list) and isinstance(
        rc_obj, list
    ):
        merged = sorted(set(shared_obj) | set(rc_obj))
        rc_path.write_text(json.dumps(merged, indent=2) + "\n")
        return
    if name == "held_memos.json" and isinstance(shared_obj, dict) and isinstance(
        rc_obj, dict
    ):
        out = dict(rc_obj)
        for key, sval in shared_obj.items():
            if key not in out:
                out[key] = sval
                continue
            rval = out[key]
            # Conflict: prefer newer held_at when both are dicts with that field.
            if (
                isinstance(sval, dict)
                and isinstance(rval, dict)
                and "held_at" in sval
                and "held_at" in rval
            ):
                out[key] = sval if str(sval["held_at"]) >= str(rval["held_at"]) else rval
            elif sval != rval:
                raise ValueError(
                    f"held_memos key {key!r} conflicts without comparable held_at"
                )
        rc_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        return
    raise ValueError(f"unsupported JSON shape for {name}")


def _merge_max_stamp(shared_path: Path, rc_path: Path) -> None:
    s = shared_path.read_text().strip()
    r = rc_path.read_text().strip()
    winner = max(s, r)
    rc_path.write_text(winner + ("\n" if not winner.endswith("\n") else ""))


def _merge_max_int(shared_path: Path, rc_path: Path) -> None:
    s = int(shared_path.read_text().strip() or "0")
    r = int(rc_path.read_text().strip() or "0")
    rc_path.write_text(str(max(s, r)) + "\n")


def reconcile_all(
    items: list[InventoryItem],
) -> list[MergeResult]:
    """Merge every divergent pair into the RC tree. Caller checks for unresolved."""
    results: list[MergeResult] = []
    for item in items:
        if not item.shared_exists and not item.rc_exists:
            continue
        if item.identical:
            results.append(
                MergeResult(item.ref, "identical", "already matching")
            )
            continue
        mr = merge_state_pair(
            item.ref.shared_path, item.ref.rc_path, item.ref.rel_path
        )
        # Preserve daemon name on the result.
        results.append(
            MergeResult(
                StateFileRef(
                    item.ref.daemon,
                    item.ref.rel_path,
                    item.ref.shared_path,
                    item.ref.rc_path,
                ),
                mr.status,
                mr.detail,
            )
        )
    return results


def assert_reconciled(merges: list[MergeResult]) -> None:
    """Raise ``RuntimeError`` if any merge is unresolved — fail closed."""
    bad = [m for m in merges if m.status == "unresolved"]
    if bad:
        detail = "; ".join(f"{m.ref.daemon}/{m.ref.rel_path}: {m.detail}" for m in bad)
        raise RuntimeError(f"cutover state reconciliation unresolved: {detail}")


def restore_from_backup(backup_dir: Path, shared_root: Path, rc_root: Path) -> None:
    """Roll back both trees from a snapshot produced by ``snapshot_inventory``."""
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"backup manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest.get("files", []):
        daemon = entry["daemon"]
        rel = entry["rel_path"]
        side = entry["side"]
        src = Path(entry["backup"])
        rel_dir = DAEMON_REL_DIRS[daemon]
        root = shared_root if side == "shared" else rc_root
        dest = root / rel_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def launchctl_pid(label: str) -> str | None:
    """Return the PID string for a launchd label, or None if not running."""
    try:
        p = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in p.stdout.splitlines():
        # PID Status Label
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == label:
            pid = parts[0]
            if pid == "-":
                return None
            return pid
    return None


def process_command_line(pid: str) -> str:
    """Return the process command line for ``pid`` (macOS ``ps``)."""
    p = subprocess.run(
        ["ps", "-p", pid, "-o", "command="],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def verify_release_paths(
    command: str,
    rc_root: Path,
    *,
    shared_root: Path | None = None,
) -> tuple[bool, str]:
    """
    Prove the process command references the release checkout, not the shared clone.

    Rejects fake success: a command that only *mentions* the RC path in an
    argument while still executing from the shared tree fails.
    """
    if not command:
        return False, "empty process command"
    rc = str(rc_root.resolve())
    shared = str(shared_root.resolve()) if shared_root else None

    # Require the interpreter and/or script path to live under rc.
    tokens = command.split()
    under_rc = [t for t in tokens if t.startswith(rc + os.sep) or t == rc]
    if not under_rc:
        return False, f"no path under release checkout ({rc}) in: {command!r}"
    if shared:
        under_shared = [
            t for t in tokens if t.startswith(shared + os.sep) or t == shared
        ]
        # Allow shared only if it is clearly not the executable/script
        # (e.g. ATELES_PRIVATE_KEYS_DIR). Executable tokens are the first
        # absolute paths in the command.
        abs_tokens = [t for t in tokens if t.startswith("/")]
        exec_tokens = abs_tokens[:2]  # python + script typical
        for t in exec_tokens:
            if t.startswith(shared + os.sep):
                return False, f"executable/script still under shared clone: {t}"
    return True, "ok"


def prove_daemon_cutover(
    daemon: str,
    *,
    pre_pid: str | None,
    rc_root: Path,
    shared_root: Path | None = None,
    pid_provider=launchctl_pid,
    command_provider=process_command_line,
) -> ReadbackResult:
    """
    Post-reload proof: new PID and executable/script under ``rc_root``.

    Deliberately rejects:
    - same PID as pre-reload (restart did not take)
    - command paths under the shared clone
    - empty/missing PID claimed as success
    """
    label = PLIST_LABELS[daemon]
    post = pid_provider(label)
    if not post:
        return ReadbackResult(
            daemon, label, False, pre_pid, None, detail="no PID after reload"
        )
    if pre_pid is not None and post == pre_pid:
        return ReadbackResult(
            daemon,
            label,
            False,
            pre_pid,
            post,
            detail="PID unchanged after reload — not a new process",
        )
    cmd = command_provider(post)
    ok, detail = verify_release_paths(cmd, rc_root, shared_root=shared_root)
    return ReadbackResult(daemon, label, ok, pre_pid, post, cmd, detail)


def assert_all_readbacks(results: list[ReadbackResult]) -> None:
    bad = [r for r in results if not r.ok]
    if bad:
        detail = "; ".join(f"{r.daemon}: {r.detail}" for r in bad)
        raise RuntimeError(f"cutover read-back failed: {detail}")


_PATH_OK = re.compile(r"ateles-rc-src")
