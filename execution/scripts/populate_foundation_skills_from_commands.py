#!/usr/bin/env python3
"""
OBSOLETE: Foundation command .md files were removed; skills in cursor_skills/ are the source of truth.
This script previously populated SKILL.md from cursor_commands/*.md. It is kept for reference only.
Edit foundation/agent_instructions/cursor_skills/{slug}/SKILL.md directly.
Run from repo root: python3 execution/scripts/populate_foundation_skills_from_commands.py (no-op if cursor_commands missing)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FOUNDATION_COMMANDS = (
    REPO_ROOT / "foundation" / "agent_instructions" / "cursor_commands"
)
FOUNDATION_SKILLS = REPO_ROOT / "foundation" / "agent_instructions" / "cursor_skills"

# Command filename (no .md) -> (skill_slug, short_description, triggers)
COMMAND_TO_SKILL = {
    "commit": (
        "commit",
        "Commit submodules and/or main repo with structured messages; supports /commit repo, /commit <submodule>.",
        ["commit", "commit repo", "commit foundation", "submodule commit", "/commit"],
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


def yaml_escape(s: str) -> str:
    if "\n" in s or ":" in s or s.strip() != s:
        return repr(s)
    return s


def build_frontmatter(slug: str, description: str, triggers: list[str]) -> str:
    lines = [
        "---",
        f"name: {slug}",
        f"description: {yaml_escape(description)}",
        "triggers:",
    ]
    for t in triggers:
        lines.append(f"  - {yaml_escape(t)}")
    lines.append("---")
    return "\n".join(lines)


def main() -> None:
    if not FOUNDATION_COMMANDS.is_dir():
        print(
            "Cursor commands dir removed; skills are the source of truth. Edit foundation/agent_instructions/cursor_skills/ directly."
        )
        return
    FOUNDATION_SKILLS.mkdir(parents=True, exist_ok=True)

    for cmd_name, (slug, description, triggers) in COMMAND_TO_SKILL.items():
        cmd_file = FOUNDATION_COMMANDS / f"{cmd_name}.md"
        if not cmd_file.is_file():
            print(f"Skip (no file): {cmd_file}")
            continue
        body = cmd_file.read_text(encoding="utf-8")
        front = build_frontmatter(slug, description, triggers)
        skill_dir = FOUNDATION_SKILLS / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(front + "\n\n" + body, encoding="utf-8")
        print(f"Wrote {skill_file.relative_to(REPO_ROOT)} ({len(body)} chars body)")


if __name__ == "__main__":
    main()
