#!/usr/bin/env python3
"""Install, check or revert the Ateles Codex turn guard in a Codex home.

What this changes under ``$CODEX_HOME`` (default ``~/.codex``), and nothing else:

  hooks.json                      four handler entries for codex_turn_guard.py
                                  (SessionStart/compact, UserPromptSubmit,
                                  PreToolUse, Stop), merged beside unrelated hooks
  ateles/turn_guard/              this installer's own directory:
      manifest.json               what was changed, and where the backups are
      mode.json                   per-path mode: goal and heartbeat start as
                                  "shadow" (log only), answered as "enforce"
      decisions.log               written by the hook, one line per decision
      backup/                     verbatim copies of every file before it was edited
  automations/<id>/automation.toml   ONLY with --heartbeats, ONLY heartbeat kind:
                                  the ``prompt`` line is replaced by a short guard stub
                                  and ``updated_at`` is bumped; the original prompt is
                                  kept verbatim as ateles/turn_guard/heartbeats/<id>/brief.md

It never edits config.toml. Codex asks the operator to trust a new or changed hook
definition: open /hooks in Codex and approve the Ateles entries; this script does not
manufacture that decision.

The goal and heartbeat paths ship as SHADOW: the hook decides and logs but never denies
a tool call or blocks a prompt. Read decisions.log for a while, then turn a path on with
``--enforce goal`` and/or ``--enforce heartbeat``; ``--shadow`` turns it back off.

Revert with ``--uninstall`` (preview with ``--uninstall --dry-run``): it removes exactly
the entries it added from hooks.json (formatting may differ from the original, content
does not) and puts each converted automation's ORIGINAL prompt line back, leaving every
other field as the operator has it now. An automation whose prompt was edited since is
skipped, with a warning, and stays in the manifest so a later run can retry it.
``--check`` exits 1 when anything is stale or absent.

The installed command is guarded: if the checkout holding the hook script is later
removed or switched to a revision without it, the command exits 0 and does nothing,
instead of exiting 2, which Codex reads as a block on every prompt and tool call. Even
so, install from a long-lived checkout, not a linked worktree or a temporary path; the
installer refuses those unless given ``--allow-worktree``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import string
import sys
import tempfile
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_TEMPLATE = REPO_ROOT / ".codex" / "turn_guard_hooks.json"
PROMPT_TEMPLATE = REPO_ROOT / "templates" / "codex" / "heartbeat_guard_prompt.md"
SCRIPT_NAME = "codex_turn_guard.py"
MARKER_PREFIX = "<!-- ateles-turn-guard:"
SAFE_ID = re.compile(r"\A[A-Za-z0-9._-]+\Z")
PATH_NAMES = ("answered", "goal", "heartbeat")
DEFAULT_MODES = {"answered": "enforce", "goal": "shadow", "heartbeat": "shadow"}
# Kept equal to the hook's heartbeat defaults (HEARTBEAT_BASE_S / HEARTBEAT_CAP_S).
SKELETON_WATCH = {"watch": [], "base_s": 600, "cap_s": 3600}


class Skip(Exception):
    """An item that cannot be handled safely; reported, never fatal to the batch."""


def default_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def guard_dir(home: Path) -> Path:
    return home / "ateles" / "turn_guard"


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------- files
def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a symlink and keep the target's permissions."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
    handle = tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.", delete=False
    )
    try:
        with handle:
            handle.write(data)
        os.chmod(handle.name, mode)
        os.replace(handle.name, target)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError(f"{path} must be a JSON object with an object 'hooks'")
    return value


def _backup(home: Path, src: Path, rel: str) -> Path | None:
    """Copy src verbatim to backup/<rel>.<timestamp>; never overwrites."""
    if not src.exists():
        return None
    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10**9:09d}"
    dest = guard_dir(home) / "backup" / f"{rel}.{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


# --------------------------------------------------------------------- checkout
def checkout_problem(repo_root: Path) -> str | None:
    """Why this checkout is a poor home for a hook script that runs for every
    Codex thread: it can disappear, or change under a branch switch."""
    if (repo_root / ".git").is_file():
        return "a linked git worktree, which is deleted when its work ends"
    resolved = os.fspath(repo_root.resolve())
    tmp = os.path.realpath(tempfile.gettempdir())
    if "/.claude/worktrees/" in resolved or resolved.startswith(
        (tmp + os.sep, "/tmp/", "/private/tmp/")
    ):
        return "a temporary or worktree path"
    return None


# ----------------------------------------------------------------------- hooks
def _is_ours(handler: dict) -> bool:
    command = handler.get("command", "")
    return isinstance(command, str) and SCRIPT_NAME in command


def _guarded_command(script: Path) -> str:
    quoted = shlex.quote(os.fspath(script))
    return f"test -f {quoted} || exit 0; exec python3 {quoted}"


def _render_groups(groups: list[dict], repo_root: Path) -> list[dict]:
    script = repo_root / ".claude" / "hooks" / SCRIPT_NAME
    if not script.is_file():
        raise FileNotFoundError(f"hook script does not exist: {script}")
    rendered = []
    for group in groups:
        item = dict(group)
        item["hooks"] = []
        for handler in group.get("hooks", []):
            handler = dict(handler)
            handler["command"] = _guarded_command(script)
            item["hooks"].append(handler)
        rendered.append(item)
    return rendered


def _without_ours(groups: list[dict]) -> list[dict]:
    kept = []
    for group in groups:
        handlers = [h for h in group.get("hooks", []) if not _is_ours(h)]
        if handlers:
            item = dict(group)
            item["hooks"] = handlers
            kept.append(item)
    return kept


def desired_hooks(existing: dict, repo_root: Path) -> dict:
    template = json.loads(HOOKS_TEMPLATE.read_text(encoding="utf-8"))
    document = dict(existing)
    hooks = dict(existing.get("hooks", {}))
    for event, groups in template["hooks"].items():
        hooks[event] = _without_ours(hooks.get(event, [])) + _render_groups(
            groups, repo_root
        )
    document["hooks"] = hooks
    return document


def has_ours(document: dict) -> bool:
    return any(
        _is_ours(h)
        for groups in document.get("hooks", {}).values()
        for group in groups
        for h in group.get("hooks", [])
    )


def hooks_without_ours(existing: dict) -> dict:
    document = dict(existing)
    hooks = {}
    for event, groups in existing.get("hooks", {}).items():
        kept = _without_ours(groups)
        if kept:
            hooks[event] = kept
    document["hooks"] = hooks
    return document


def installed_scripts(document: dict) -> set[str]:
    found = set()
    for groups in document.get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                if not _is_ours(handler):
                    continue
                try:
                    tokens = shlex.split(handler.get("command", ""))
                except ValueError:
                    continue
                found.update(t for t in tokens if t.endswith(SCRIPT_NAME))
    return found


# ----------------------------------------------------------------------- modes
def read_modes(home: Path) -> dict | None:
    try:
        value = json.loads((guard_dir(home) / "mode.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def desired_modes(home: Path, enforce: list[str], shadow: list[str]) -> dict:
    modes = dict(DEFAULT_MODES)
    modes.update({k: v for k, v in (read_modes(home) or {}).items() if k in PATH_NAMES})
    for name in enforce:
        modes[name] = "enforce"
    for name in shadow:
        modes[name] = "shadow"
    return modes


# ----------------------------------------------------------------- automations
def _automation_files(home: Path) -> list[Path]:
    root = home / "automations"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/automation.toml"))


def _stub_prompt(automation_id: str, brief: Path) -> str:
    template = string.Template(PROMPT_TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute(
        automation_id=automation_id, brief_path=os.fspath(brief)
    ).strip()


def _replace_line(text: str, key: str, rendered: str) -> str:
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(f"{key} = ")]
    if len(hits) != 1:
        raise Skip(f"expected exactly one '{key} = ' line, found {len(hits)}")
    lines[hits[0]] = f"{key} = {rendered}"
    return "\n".join(lines)


def _line_value(text: str, key: str) -> str:
    hits = [line for line in text.split("\n") if line.startswith(f"{key} = ")]
    if len(hits) != 1:
        raise Skip(f"expected exactly one '{key} = ' line, found {len(hits)}")
    return hits[0][len(f"{key} = ") :]


def plan_heartbeat(home: Path, path: Path) -> dict | None:
    """Everything needed to convert one automation, validated, with no writes.
    None when it is not a heartbeat; Skip when it cannot be converted safely."""
    raw = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise Skip(f"not valid TOML ({exc})") from exc
    if data.get("kind") != "heartbeat":
        return None
    automation_id = path.parent.name
    if not SAFE_ID.match(automation_id) or automation_id in {".", ".."}:
        raise Skip(f"directory name {automation_id!r} is not a safe automation id")
    if str(data.get("id", automation_id)) != automation_id:
        raise Skip(
            f"id {data.get('id')!r} inside the file differs from its directory name"
        )
    if str(data.get("prompt", "")).startswith(MARKER_PREFIX):
        return {"id": automation_id, "state": "already-guarded"}
    base = guard_dir(home) / "heartbeats"
    brief = base / automation_id / "brief.md"
    watch = base / f"{automation_id}.json"
    for target in (brief, watch):
        if not within(target, guard_dir(home)):
            raise Skip(f"{target} would fall outside {guard_dir(home)}")
    stub = _stub_prompt(automation_id, brief)
    new = _replace_line(raw, "prompt", json.dumps(stub, ensure_ascii=False))
    if "updated_at = " in new:
        new = _replace_line(new, "updated_at", str(int(time.time() * 1000)))
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        raise Skip(
            f"unsupported prompt layout, e.g. a multi-line string ({exc})"
        ) from exc
    for key, value in data.items():
        if key not in {"prompt", "updated_at"} and after.get(key) != value:
            raise Skip(f"rewrite would change '{key}'")
    return {
        "id": automation_id,
        "state": "convert",
        "path": path,
        "new": new,
        "prompt": str(data.get("prompt", "")),
        "brief": brief,
        "watch": watch,
    }


def describe(plan: dict, mode: str) -> list[str]:
    cap = SKELETON_WATCH["cap_s"] // 60
    return [
        f"automation {plan['id']}: prompt {len(plan['prompt'])} chars -> guard stub "
        f"(original kept at {plan['brief']})",
        f"  watch file: {plan['watch']} (empty: only operator messages and local repository "
        "state are watched)",
        f"  when the heartbeat path is enforced (now: {mode}), an idle run is skipped; back-off "
        f"{SKELETON_WATCH['base_s'] // 60} min doubling to {cap} min. Anything outside the "
        "repository is seen only on that schedule unless you add watch commands "
        "(templates/codex/heartbeat_watch.example.json).",
    ]


def apply_heartbeat(home: Path, plan: dict) -> dict:
    path = plan["path"]
    backup = _backup(home, path, f"automations/{plan['id']}/automation.toml")
    plan["brief"].parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(plan["brief"], plan["prompt"].encode("utf-8"))
    if not plan["watch"].exists():
        _atomic_write(plan["watch"], _json_bytes(SKELETON_WATCH))
    _atomic_write(path, plan["new"].encode("utf-8"))
    return {
        "path": os.fspath(path),
        "backup": os.fspath(backup),
        "brief": os.fspath(plan["brief"]),
        "watch": os.fspath(plan["watch"]),
    }


def restore_heartbeat(
    home: Path, name: str, entry: dict, dry_run: bool
) -> tuple[bool, str]:
    """Put the ORIGINAL prompt line back; touch nothing else. (done?, message)"""
    path, backup = Path(entry["path"]), Path(entry["backup"])
    brief, watch = Path(entry["brief"]), Path(entry["watch"])
    if not within(path, home / "automations"):
        return (
            False,
            f"skipped {name}: manifest path {path} is outside the automations folder",
        )
    if not all(within(p, guard_dir(home)) for p in (backup, brief, watch)):
        return False, f"skipped {name}: a manifest path is outside {guard_dir(home)}"
    if not backup.is_file() or not path.is_file():
        return False, f"skipped {name}: backup or automation file is missing"
    current = path.read_text(encoding="utf-8")
    try:
        prompt = tomllib.loads(current).get("prompt", "")
    except tomllib.TOMLDecodeError:
        prompt = ""
    if not str(prompt).startswith(MARKER_PREFIX):
        return (
            False,
            f"skipped {name}: prompt edited since install; backup kept at {backup}",
        )
    original_line = _line_value(backup.read_text(encoding="utf-8"), "prompt")
    restored = _replace_line(current, "prompt", original_line)
    if "updated_at = " in restored:
        restored = _replace_line(restored, "updated_at", str(int(time.time() * 1000)))
    if dry_run:
        return True, f"restore the original prompt of {name}"
    _atomic_write(path, restored.encode("utf-8"))
    notes = []
    if brief.exists():
        brief.unlink()
        if brief.parent.is_dir() and not any(brief.parent.iterdir()):
            brief.parent.rmdir()
    if watch.exists():
        try:
            unchanged = json.loads(watch.read_text(encoding="utf-8")) == SKELETON_WATCH
        except ValueError:
            unchanged = False
        if unchanged:
            watch.unlink()
        else:
            notes.append(f"your edited watch file was kept at {watch}")
    return True, f"restored the original prompt of {name}" + (
        f" ({'; '.join(notes)})" if notes else ""
    )


# ---------------------------------------------------------------------- driver
def manifest_path(home: Path) -> Path:
    return guard_dir(home) / "manifest.json"


def load_manifest(home: Path) -> dict:
    try:
        manifest = json.loads(manifest_path(home).read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and isinstance(manifest.get("automations"), dict):
            return manifest
    except (OSError, ValueError):
        pass
    return {"automations": {}}


def save_manifest(home: Path, manifest: dict, repo_root: Path) -> None:
    manifest.update(
        installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"), repo_root=os.fspath(repo_root)
    )
    _atomic_write(manifest_path(home), _json_bytes(manifest))


def select_heartbeats(home: Path, names: list[str]) -> list[Path]:
    paths = _automation_files(home)
    if not names:
        return paths
    present = {p.parent.name for p in paths}
    missing = [n for n in names if n not in present]
    if missing:
        raise ValueError(
            f"no automation named {', '.join(missing)} in {home / 'automations'} "
            f"(found: {', '.join(sorted(present)) or 'none'})"
        )
    return [p for p in paths if p.parent.name in names]


def do_install(
    home: Path,
    repo_root: Path,
    heartbeats: list[str] | None,
    enforce: list[str],
    shadow: list[str],
    dry_run: bool,
    allow_worktree: bool,
) -> int:
    problem = checkout_problem(repo_root)
    if problem and not allow_worktree:
        print(
            f"ERROR: {repo_root} is {problem}. The hook script runs for every Codex thread, so "
            "install from a long-lived checkout, or pass --allow-worktree to accept the risk.",
            file=sys.stderr,
        )
        return 2
    if problem:
        print(
            f"WARNING: installing from {problem}; the guarded command does nothing if the "
            "checkout is later removed."
        )
    hooks_path = home / "hooks.json"
    existing = _load_json(hooks_path)
    desired = desired_hooks(existing, repo_root)
    modes = desired_modes(home, enforce, shadow)
    plans = []
    skipped = []
    if heartbeats is not None:
        for path in select_heartbeats(home, heartbeats):
            try:
                plan = plan_heartbeat(home, path)
            except Skip as why:
                skipped.append(f"automation {path.parent.name}: SKIPPED, {why}")
                continue
            if plan and plan["state"] == "convert":
                plans.append(plan)
    manifest = load_manifest(home)
    changed = []
    if existing != desired:
        changed.append(
            f"hooks.json: add or refresh the codex_turn_guard entries ({hooks_path})"
        )
    if read_modes(home) != modes:
        changed.append(
            f"mode.json: goal={modes['goal']}, heartbeat={modes['heartbeat']}, "
            f"answered={modes['answered']}"
        )
    for plan in plans:
        changed.extend(describe(plan, modes["heartbeat"]))
    prefix = "would: " if dry_run else ""
    for line in (changed or ["nothing to change: already current"]) + skipped:
        print(prefix + line)
    if dry_run:
        return 0
    if existing != desired:
        backup = _backup(home, hooks_path, "hooks.json")
        manifest["hooks_backup"] = os.fspath(backup) if backup else ""
        _atomic_write(hooks_path, _json_bytes(desired))
        save_manifest(home, manifest, repo_root)
    if read_modes(home) != modes:
        fresh = read_modes(home) is None
        _atomic_write(guard_dir(home) / "mode.json", _json_bytes(modes))
        if fresh:
            manifest["modes_written"] = modes
        save_manifest(home, manifest, repo_root)
    for plan in plans:
        manifest["automations"][plan["id"]] = apply_heartbeat(home, plan)
        # After EACH file, so a later failure leaves everything done so far revertible.
        save_manifest(home, manifest, repo_root)
    if any(c.startswith("hooks.json") for c in changed):
        print(
            "Next: open /hooks in Codex, review the Ateles codex_turn_guard definitions, and trust them."
        )
    print(f"Decisions are logged to {guard_dir(home) / 'decisions.log'}.")
    return 0


def do_check(home: Path, repo_root: Path, heartbeats: list[str] | None) -> int:
    stale = []
    existing = _load_json(home / "hooks.json")
    if existing != desired_hooks(existing, repo_root):
        stale.append("hooks.json lacks current codex_turn_guard entries")
    for script in sorted(installed_scripts(existing)):
        if not Path(script).is_file():
            stale.append(f"the installed hook script is missing: {script}")
    if heartbeats is not None:
        for path in select_heartbeats(home, heartbeats):
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if data.get("kind") == "heartbeat" and not str(
                data.get("prompt", "")
            ).startswith(MARKER_PREFIX):
                stale.append(
                    f"automation {path.parent.name} still carries its full prompt"
                )
    for line in stale:
        print(f"STALE: {line}")
    if not stale:
        print(
            f"OK: the Ateles Codex turn guard is installed and current in {home}. "
            "Whether you trusted it in /hooks cannot be verified here."
        )
    return 1 if stale else 0


def do_uninstall(home: Path, dry_run: bool) -> int:
    hooks_path = home / "hooks.json"
    existing = _load_json(hooks_path)
    prefix = "would: " if dry_run else ""
    nothing = True
    if has_ours(existing):
        nothing = False
        print(
            f"{prefix}hooks.json: remove the codex_turn_guard entries (other hooks are kept)"
        )
        if not dry_run:
            _backup(home, hooks_path, "hooks.json.pre-uninstall")
            _atomic_write(hooks_path, _json_bytes(hooks_without_ours(existing)))
    manifest = load_manifest(home)
    for name, entry in sorted(manifest["automations"].items()):
        nothing = False
        try:
            done, message = restore_heartbeat(home, name, entry, dry_run)
        except (Skip, KeyError, OSError, ValueError) as exc:
            done, message = False, f"skipped {name}: {exc}"
        print(prefix + message)
        if done and not dry_run:
            del manifest["automations"][name]
    modes_file = guard_dir(home) / "mode.json"
    if (
        not dry_run
        and modes_file.exists()
        and read_modes(home) == manifest.get("modes_written")
    ):
        modes_file.unlink()
        manifest.pop("modes_written", None)
    if not dry_run and manifest_path(home).exists():
        _atomic_write(manifest_path(home), _json_bytes(manifest))
    if nothing:
        print(
            "nothing to remove: no codex_turn_guard entries or converted automations found"
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--codex-home", type=Path, default=default_home())
    parser.add_argument(
        "--heartbeats",
        nargs="*",
        metavar="AUTOMATION_ID",
        default=None,
        help="also convert heartbeat automations to the guard stub (all of them when no id is given)",
    )
    parser.add_argument(
        "--enforce",
        nargs="+",
        choices=PATH_NAMES,
        default=[],
        metavar="PATH",
        help="turn a path from shadow (log only) to enforce: goal, heartbeat, answered",
    )
    parser.add_argument(
        "--shadow",
        nargs="+",
        choices=PATH_NAMES,
        default=[],
        metavar="PATH",
        help="turn a path back to shadow (log only)",
    )
    parser.add_argument(
        "--allow-worktree",
        action="store_true",
        help="install even though this checkout is a linked worktree or a temporary path",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change; write nothing"
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if anything is stale or absent"
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="revert everything this script changed"
    )
    args = parser.parse_args(argv)
    if args.check and (args.uninstall or args.dry_run):
        parser.error("--check cannot be combined with --uninstall or --dry-run")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    home = args.codex_home.expanduser().resolve()
    heartbeats = None if args.heartbeats is None else list(args.heartbeats)
    try:
        if args.uninstall:
            return do_uninstall(home, args.dry_run)
        if args.check:
            return do_check(home, REPO_ROOT, heartbeats)
        return do_install(
            home,
            REPO_ROOT,
            heartbeats,
            args.enforce,
            args.shadow,
            args.dry_run,
            args.allow_worktree,
        )
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
