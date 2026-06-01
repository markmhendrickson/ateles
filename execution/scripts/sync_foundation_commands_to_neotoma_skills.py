#!/usr/bin/env python3
"""
Update .cursor/skills stubs for foundation cursor commands (repo-sourced only, no Neotoma).

Foundation-command-derived skills are NOT stored in Neotoma. Content is loaded from
foundation/agent_instructions/cursor_commands/*.md when the skill is invoked.

Foundation commands were removed; skills in foundation/agent_instructions/cursor_skills/ are the source of truth. Run foundation/scripts/setup_cursor_copies.sh to copy them into .cursor/skills/. This script no longer writes foundation-command stubs (setup does the copy).

Run: python3 execution/scripts/sync_foundation_commands_to_neotoma_skills.py

For each foundation command: writes/updates .cursor/skills/{slug}/SKILL.md with
source_path pointing to the foundation .md file. Ateles-only skills (e.g. email-triage,
write-blog-post) remain in Neotoma and are not modified by this script.
"""

from __future__ import annotations

import sys
from datetime import UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FOUNDATION_COMMANDS = (
    REPO_ROOT / "foundation" / "agent_instructions" / "cursor_commands"
)
CURSOR_SKILLS = REPO_ROOT / ".cursor" / "skills"

# Foundation command filename (no .md) -> (skill_slug, short_description, triggers)
COMMAND_TO_SKILL = {
    "commit": (
        "commit",
        "Commit submodules and/or main repo with structured messages; supports /commit repo, /commit <submodule>.",
        [
            "commit",
            "commit repo",
            "commit foundation",
            "submodule commit",
            "/commit",
            "commit",
        ],
    ),
    "setup_cursor_copies": (
        "setup-cursor-copies",
        "Setup foundation cursor rules and commands in .cursor/ (run setup script).",
        [
            "setup cursor copies",
            "sync cursor files",
            "/setup_cursor_copies",
            "setup-cursor-copies",
        ],
    ),
    "sync_env_from_1password": (
        "sync-env-from-1password",
        "Sync .env from 1Password using env_var_mappings.",
        [
            "sync env from 1password",
            "/sync_env_from_1password",
            "sync-env-from-1password",
        ],
    ),
    "analyze": (
        "analyze",
        "Analyze codebase or context per foundation analyze command.",
        ["analyze", "/analyze"],
    ),
    "push": (
        "push",
        "Push current branch to origin; supports /push <submodule>.",
        ["push", "/push"],
    ),
    "pull": (
        "pull",
        "Pull from origin; supports /pull <submodule>.",
        ["pull", "/pull"],
    ),
    "publish": (
        "publish",
        "Publish workflow per foundation publish command.",
        ["publish", "/publish"],
    ),
    "run_feature_workflow": (
        "run-feature-workflow",
        "Run feature workflow per foundation command.",
        ["run feature workflow", "/run_feature_workflow", "run-feature-workflow"],
    ),
    "create_rule": (
        "create-rule",
        "Create Cursor rule for persistent AI guidance.",
        ["create rule", "/create_rule", "create-rule"],
    ),
    "create_prototype": (
        "create-prototype",
        "Create prototype per foundation command.",
        ["create prototype", "/create_prototype", "create-prototype"],
    ),
    "report": (
        "report",
        "Generate report per foundation report command.",
        ["report", "/report"],
    ),
    "report_error": (
        "report-error",
        "Report error per foundation command.",
        ["report error", "/report_error", "report-error"],
    ),
    "debug": (
        "debug",
        "Debug workflow per foundation command.",
        ["debug", "/debug"],
    ),
    "manage_error_debugging": (
        "manage-error-debugging",
        "Manage error debugging per foundation command.",
        ["manage error debugging", "/manage_error_debugging", "manage-error-debugging"],
    ),
    "final_review": (
        "final-review",
        "Final review workflow per foundation command.",
        ["final review", "/final_review", "final-review"],
    ),
    "setup_commands": (
        "setup-commands",
        "Setup commands in .cursor/commands.",
        ["setup commands", "/setup_commands", "setup-commands"],
    ),
    # Foundation-command skills (repo-sourced; do not store in Neotoma)
    "create_release": (
        "create-release",
        "Create a new software release with planning, manifest, and execution schedule.",
        [
            "new release",
            "create release",
            "plan release",
            "create-release",
            "/create-release",
        ],
    ),
    "fix_feature_bug": (
        "fix-feature-bug",
        "Fix bugs using structured workflow with error classification and regression tests.",
        [
            "bug",
            "error",
            "fix",
            "broken",
            "not working",
            "failing",
            "fix-feature-bug",
            "/fix-feature-bug",
        ],
    ),
    "create_feature_unit": (
        "create-feature-unit",
        "Create a new feature unit with spec, manifest, and test structure.",
        [
            "create feature",
            "new feature",
            "create feature unit",
            "create-feature-unit",
            "/create-feature-unit",
        ],
    ),
}


def read_command_content(filename: str) -> str:
    path = FOUNDATION_COMMANDS / f"{filename}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def extract_title_and_description(content: str, slug: str) -> tuple[str, str]:
    """First line is often # title; use as name. First paragraph as description."""
    lines = content.strip().split("\n")
    name = slug.replace("-", " ").title()
    desc = ""
    for line in lines:
        line = line.strip()
        if line.startswith("# "):
            name = line[2:].strip()
            continue
        if line and not line.startswith("#") and not line.startswith("---"):
            desc = line[:200] + ("..." if len(line) > 200 else "")
            break
    return name, desc


def write_stub(
    slug: str,
    name: str,
    description: str,
    triggers: list[str],
    source_path: str,
    synced_at: str,
) -> None:
    """Write a foundation-command skill stub (repo-sourced; no Neotoma entity_id)."""
    stub_dir = CURSOR_SKILLS / slug
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_file = stub_dir / "SKILL.md"
    trigger_yaml = "\n".join(f"  - {t}" for t in triggers)
    body = f"Load and follow the workflow in the file at `{source_path}` (relative to repo root). Do not fetch from Neotoma; this skill is foundation-sourced only."
    frontmatter = f"""---
name: {slug}
description: {description}
triggers:
{trigger_yaml}
source_path: {source_path}
synced_at: "{synced_at}"
---

{body}
"""
    stub_file.write_text(frontmatter, encoding="utf-8")


def main() -> int:
    from datetime import datetime, timezone

    synced_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for filename, (slug, default_desc, triggers) in COMMAND_TO_SKILL.items():
        content = read_command_content(filename)
        if not content:
            print(f"Skip {filename}: no file", file=sys.stderr)
            continue
        name, desc = extract_title_and_description(content, slug)
        if not desc:
            desc = default_desc
        source_path = f"foundation/agent_instructions/cursor_commands/{filename}.md"
        write_stub(slug, name, desc, triggers, source_path, synced_at)
        print(f"{slug}: source_path={source_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
