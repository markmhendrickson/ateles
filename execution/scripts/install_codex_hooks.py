#!/usr/bin/env python3
"""Install Ateles rule-delivery and guard hooks at Codex user scope.

The committed ``.codex/hooks.json`` is the harness binding. Rule content is
not copied into it: the SessionStart and UserPromptSubmit commands render the
canonical live ``agent_policy`` rows through the same implementation Claude
Code uses. This installer only replaces repository-relative commands with the
absolute path of the checkout being installed, then merges those handlers into
``$CODEX_HOME/hooks.json`` without disturbing unrelated hooks.

Codex deliberately requires the operator to review and trust a changed hook
definition. This script does not bypass or manufacture that trust decision.
After installation, open ``/hooks`` in Codex and approve the Ateles entries.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / ".codex" / "hooks.json"
MANAGED_SCRIPT_NAMES = frozenset(
    {
        "session_rule_index.py",
        "session_rule_delivery.py",
        "rule_injection_gate.py",
        "decision_shape_gate.py",
        "sibling_repo_worktree_guard.py",
        "gmail_send_gate.py",
        "git_stash_guard.py",
        "gh_identity_guard.py",
        "reporting_contract.py",
        "report_quality_gate.py",
    }
)


def _default_out() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (
        Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    ) / "hooks.json"


def _is_managed(handler: dict) -> bool:
    command = handler.get("command", "")
    return isinstance(command, str) and any(
        name in command for name in MANAGED_SCRIPT_NAMES
    )


def _absolute_handler(handler: dict, repo_root: Path) -> dict:
    rendered = dict(handler)
    command = rendered.get("command", "")
    matching = [name for name in MANAGED_SCRIPT_NAMES if name in command]
    if len(matching) != 1:
        raise ValueError(
            "each managed Codex hook command must name exactly one shared "
            f"Ateles script; got {matching!r} in {command!r}"
        )
    script = repo_root / ".claude" / "hooks" / matching[0]
    if not script.is_file():
        raise FileNotFoundError(f"managed hook script does not exist: {script}")
    rendered["command"] = f"python3 {shlex.quote(os.fspath(script))}"
    return rendered


def _render_groups(groups: list[dict], repo_root: Path) -> list[dict]:
    rendered: list[dict] = []
    for group in groups:
        item = dict(group)
        item["hooks"] = [
            _absolute_handler(handler, repo_root) for handler in group.get("hooks", [])
        ]
        rendered.append(item)
    return rendered


def _without_managed(groups: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for group in groups:
        handlers = [h for h in group.get("hooks", []) if not _is_managed(h)]
        if handlers:
            item = dict(group)
            item["hooks"] = handlers
            kept.append(item)
    return kept


def desired_document(existing: dict, repo_root: Path) -> dict:
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    document = dict(existing)
    hooks = dict(existing.get("hooks", {}))
    for event, template_groups in template["hooks"].items():
        current = hooks.get(event, [])
        hooks[event] = _without_managed(current) + _render_groups(
            template_groups, repo_root
        )
    document["hooks"] = hooks
    if "description" not in document:
        document["description"] = template.get("description", "")
    return document


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    if "hooks" in value and not isinstance(value["hooks"], dict):
        raise ValueError(f"{path}: hooks must be a JSON object")
    return value


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_default_out(),
        help="hooks.json to merge (default: $CODEX_HOME/hooks.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the installed Ateles hook entries are stale or absent",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = args.out.expanduser()
    try:
        existing = _load(out)
        desired = desired_document(existing, REPO_ROOT)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not prepare Codex hooks: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if existing == desired:
            print(f"OK: Ateles Codex hooks are current in {out}")
            return 0
        print(f"STALE: Ateles Codex hooks are absent or outdated in {out}")
        return 1

    try:
        _write_atomic(out, desired)
    except OSError as exc:
        print(f"ERROR: could not write {out}: {exc}", file=sys.stderr)
        return 2

    print(f"Installed Ateles Codex hooks in {out}")
    print("Next: open /hooks in Codex, review the Ateles definitions, and trust them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
