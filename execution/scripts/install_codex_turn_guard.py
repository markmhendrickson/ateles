#!/usr/bin/env python3
"""Install, check or revert the Ateles Codex turn guard in a Codex home.

What this changes under ``$CODEX_HOME`` (default ``~/.codex``), and nothing else:

  hooks.json                      four handler entries for codex_turn_guard.py
                                  (SessionStart/compact, UserPromptSubmit,
                                  PreToolUse, Stop), merged beside unrelated hooks
  ateles/turn_guard/              this installer's own directory:
      manifest.json               what was changed, and where the backups are
      backup/                     verbatim copies of every file before it was edited
  automations/<id>/automation.toml   ONLY with --heartbeats, ONLY heartbeat kind:
                                  the ``prompt`` line is replaced by a short guard stub
                                  and ``updated_at`` is bumped; the original prompt is
                                  kept verbatim as ateles/turn_guard/heartbeats/<id>/brief.md

It never edits config.toml. Codex asks the operator to trust a new or changed hook
definition: open /hooks in Codex and approve the Ateles entries; this script does not
manufacture that decision.

Revert with ``--uninstall``: it removes exactly the entries it added from hooks.json,
and restores each converted automation.toml from its backup (skipping, with a warning,
any file edited since). ``--check`` exits 1 when anything is stale or absent.
"""

from __future__ import annotations

import argparse
import json
import os
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
SKELETON_WATCH = {"watch": [], "base_s": 600, "cap_s": 21600}


def default_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def guard_dir(home: Path) -> Path:
    return home / "ateles" / "turn_guard"


# ----------------------------------------------------------------------- files
def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(data)
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError(f"{path} must be a JSON object with an object 'hooks'")
    return value


def _backup(home: Path, src: Path, rel: str) -> Path | None:
    """Copy src verbatim to backup/<rel>.<timestamp>; never overwrites."""
    if not src.exists():
        return None
    dest = (
        guard_dir(home)
        / "backup"
        / f"{rel}.{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10**9:09d}"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


# ----------------------------------------------------------------------- hooks
def _is_ours(handler: dict) -> bool:
    command = handler.get("command", "")
    return isinstance(command, str) and SCRIPT_NAME in command


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
            handler["command"] = f"python3 {shlex.quote(os.fspath(script))}"
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


def hooks_without_ours(existing: dict) -> dict:
    document = dict(existing)
    hooks = {}
    for event, groups in existing.get("hooks", {}).items():
        kept = _without_ours(groups)
        if kept:
            hooks[event] = kept
    document["hooks"] = hooks
    return document


# ----------------------------------------------------------------- automations
def _automation_files(home: Path, only: list[str]) -> list[Path]:
    root = home / "automations"
    if not root.is_dir():
        return []
    paths = sorted(root.glob("*/automation.toml"))
    if only:
        paths = [p for p in paths if p.parent.name in only]
    return paths


def _stub_prompt(automation_id: str, brief: Path) -> str:
    template = string.Template(PROMPT_TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute(
        automation_id=automation_id, brief_path=os.fspath(brief)
    ).strip()


def _replace_line(text: str, key: str, rendered: str) -> str:
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(f"{key} = ")]
    if len(hits) != 1:
        raise ValueError(
            f"expected exactly one top-level '{key} = ' line, found {len(hits)}"
        )
    lines[hits[0]] = f"{key} = {rendered}"
    return "\n".join(lines)


def convert_heartbeat(home: Path, path: Path, dry_run: bool) -> dict | None:
    raw = path.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    automation_id = str(data.get("id") or path.parent.name)
    if data.get("kind") != "heartbeat":
        return None
    if str(data.get("prompt", "")).startswith(MARKER_PREFIX):
        return {"id": automation_id, "state": "already-guarded"}
    base = guard_dir(home) / "heartbeats"
    brief = base / automation_id / "brief.md"
    watch = base / f"{automation_id}.json"
    stub = _stub_prompt(automation_id, brief)
    new = _replace_line(raw, "prompt", json.dumps(stub, ensure_ascii=False))
    if "updated_at = " in new:
        new = _replace_line(new, "updated_at", str(int(time.time() * 1000)))
    after = tomllib.loads(new)
    for key, value in data.items():
        if key not in {"prompt", "updated_at"} and after.get(key) != value:
            raise ValueError(f"{path}: rewrite would change '{key}'; refusing")
    result = {
        "id": automation_id,
        "state": "converted",
        "prompt_chars": len(data.get("prompt", "")),
    }
    if dry_run:
        return result
    backup = _backup(home, path, f"automations/{automation_id}/automation.toml")
    _atomic_write(brief, str(data.get("prompt", "")).encode("utf-8"))
    if not watch.exists():
        _atomic_write(watch, _json_bytes(SKELETON_WATCH))
    _atomic_write(path, new.encode("utf-8"))
    result.update(
        path=os.fspath(path),
        backup=os.fspath(backup),
        brief=os.fspath(brief),
        watch=os.fspath(watch),
    )
    return result


def restore_heartbeat(entry: dict) -> str:
    path = Path(entry["path"])
    backup = Path(entry["backup"])
    if not backup.is_file():
        return f"skipped {path.parent.name}: backup {backup} is missing"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        prompt = tomllib.loads(current).get("prompt", "")
    except tomllib.TOMLDecodeError:
        prompt = ""
    if not str(prompt).startswith(MARKER_PREFIX):
        return (
            f"skipped {path.parent.name}: edited since install; backup kept at {backup}"
        )
    shutil.copy2(backup, path)
    for key in ("brief", "watch"):
        target = Path(entry[key])
        if target.exists():
            target.unlink()
    brief_dir = Path(entry["brief"]).parent
    if brief_dir.is_dir() and not any(brief_dir.iterdir()):
        brief_dir.rmdir()
    return f"restored {path.parent.name}"


# ---------------------------------------------------------------------- driver
def manifest_path(home: Path) -> Path:
    return guard_dir(home) / "manifest.json"


def load_manifest(home: Path) -> dict:
    try:
        return json.loads(manifest_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"automations": {}}


def do_install(
    home: Path, repo_root: Path, heartbeats: list[str] | None, dry_run: bool
) -> int:
    hooks_path = home / "hooks.json"
    existing = _load_json(hooks_path)
    desired = desired_hooks(existing, repo_root)
    manifest = load_manifest(home)
    changed = []
    if existing != desired:
        changed.append(
            f"hooks.json: add/refresh codex_turn_guard entries ({hooks_path})"
        )
        if not dry_run:
            manifest["hooks_backup"] = os.fspath(
                _backup(home, hooks_path, "hooks.json") or ""
            )
            _atomic_write(hooks_path, _json_bytes(desired))
    if heartbeats is not None:
        for path in _automation_files(home, heartbeats):
            result = convert_heartbeat(home, path, dry_run)
            if result and result["state"] == "converted":
                changed.append(
                    f"automation {result['id']}: prompt {result['prompt_chars']} chars -> guard stub"
                )
                if not dry_run:
                    manifest.setdefault("automations", {})[result["id"]] = {
                        k: result[k] for k in ("path", "backup", "brief", "watch")
                    }
    if not dry_run and changed:
        manifest.update(
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            repo_root=os.fspath(repo_root),
        )
        _atomic_write(manifest_path(home), _json_bytes(manifest))
    for line in changed or ["nothing to change: already current"]:
        print(("would: " if dry_run else "") + line)
    if changed and not dry_run:
        print(
            "Next: open /hooks in Codex, review the Ateles codex_turn_guard definitions, and trust them."
        )
    return 0


def do_check(home: Path, repo_root: Path, heartbeats: list[str] | None) -> int:
    stale = []
    if _load_json(home / "hooks.json") != desired_hooks(
        _load_json(home / "hooks.json"), repo_root
    ):
        stale.append("hooks.json lacks current codex_turn_guard entries")
    if heartbeats is not None:
        for path in _automation_files(home, heartbeats):
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
        print(f"OK: Ateles Codex turn guard is current in {home}")
    return 1 if stale else 0


def do_uninstall(home: Path) -> int:
    hooks_path = home / "hooks.json"
    existing = _load_json(hooks_path)
    reverted = hooks_without_ours(existing)
    if existing != reverted:
        _backup(home, hooks_path, "hooks.json.pre-uninstall")
        _atomic_write(hooks_path, _json_bytes(reverted))
        print("hooks.json: removed codex_turn_guard entries")
    manifest = load_manifest(home)
    for entry in manifest.get("automations", {}).values():
        print(restore_heartbeat(entry))
    manifest["automations"] = {}
    if manifest_path(home).exists():
        _atomic_write(manifest_path(home), _json_bytes(manifest))
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="exit 1 if anything is stale or absent"
    )
    mode.add_argument(
        "--uninstall", action="store_true", help="revert everything this script changed"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="print what would change; write nothing"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    home = args.codex_home.expanduser()
    heartbeats = None if args.heartbeats is None else list(args.heartbeats)
    try:
        if args.uninstall:
            return do_uninstall(home)
        if args.check:
            return do_check(home, REPO_ROOT, heartbeats)
        return do_install(home, REPO_ROOT, heartbeats, args.dry_run)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
