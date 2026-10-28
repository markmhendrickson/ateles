#!/usr/bin/env python3
"""Render CLAUDE.md standing instructions into the Codex fallback load point.

Recovered from the existing cross-harness renderer. Its extractor, CLI, target
and managed markers remain the same. Policy bodies now come from the source,
not a second manually condensed RULES string. --check compares the complete
managed projection, so body-only changes and lost limiting clauses fail too.

Claude Code mechanism descriptions are retained with explicit harness context;
the Codex header names the measured opt-in hook path without asserting that any
particular installation has trusted it. This file is a fallback, not proof that
hooks ran or a replacement for canonical live agent_policy delivery.

Exit 0: rendered/current; 1: target absent, empty or stale; 2: input/ownership
error (no write). Stdlib only; no network, credentials or configuration writes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

EXIT_USAGE = 2
REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
EXTRACTOR = REPO_ROOT / "scripts" / "verify_claude_md_merge.py"
DEFAULT_AGENTS_MD = (
    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "AGENTS.md"
)
BEGIN = "<!-- BEGIN GENERATED: ateles standing rules -->"
END = "<!-- END GENERATED: ateles standing rules -->"


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def load_extractor():
    """Import `verify_claude_md_merge` for its rule extractor.

    Deliberately reused rather than reimplemented: `lint.sh` gates CLAUDE.md on
    these exact rule identities, and a second parser would drift from the first
    exactly where it matters least visibly.
    """
    if not EXTRACTOR.exists():
        die(f"cannot find the rule extractor at {EXTRACTOR}. The check did NOT run.")
    name = "_ateles_claude_md_extractor"
    spec = importlib.util.spec_from_file_location(name, EXTRACTOR)
    if spec is None or spec.loader is None:
        die(f"cannot load {EXTRACTOR}. The check did NOT run.")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the extractor defines @dataclass classes, and
    # dataclasses resolves a class's own module out of sys.modules while
    # processing it. Executing an unregistered module raises AttributeError on
    # the first @dataclass — which cost a debugging round on Python 3.14.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        die(f"cannot execute {EXTRACTOR}: {exc}. The check did NOT run.")
    return module


HEADER = """# Ateles / Neotoma — standing session instructions (Codex)

Generated from this checkout's CLAUDE.md by
execution/scripts/render_codex_agents_md.py. Do not edit the managed region.
Use --source and --out explicitly for isolated generation; --check writes nothing
and rejects full-text drift, including unchanged rule leads with altered bodies.

## Harness binding

The following source requirements bind an interactive Codex session. Descriptions
of Claude Code hooks, lifecycle events, tools and wiring describe Claude Code,
not evidence that a Codex guard is installed or runs. Apply the requirements by
hand unless this installation's actual enforcement has been verified. Resolve
capability-specific actions through tools exposed by the current harness; an
unavailable tool never supplies authorization or silently substitutes a default.

The supported Codex live-delivery path uses .codex/hooks.json and
execution/scripts/install_codex_hooks.py, followed by the harness's explicit
/hooks trust step. See docs/install.md. Its measured user-scope hook delivery
supersedes the old blanket assertion that Codex has no guards. Presence of this
fallback file proves neither installation, trust nor recipient-path delivery.
Do not install or change hooks or configuration as a consequence of rendering.

All source constraints and deliberately unresolved conflict pairs below retain
their authority. Descriptive implementation evidence is not a new permission.
"""

# Only these exact, reviewed non-policy sections are omitted. A new clause in
# one changes its digest and refuses generation/checking until it is reviewed;
# a heading-pattern filter could silently drop a future standing instruction.
SECTION_OMISSIONS: dict[str, tuple[str, str]] = {
    "Key entity IDs": (
        "0d27085994966edbe4c3787a90c7cec699f7999acaa3ee4507dcb71c4c7bab94",
        "Operator-specific orientation identifiers, not portable standing instructions.",
    ),
    "Current phase blockers (Phase 5–6)": (
        "2b167a790892b3cb9067d82868aeb4e1d39c2f556da1baea997fd198165f47e9",
        "Historical execution/status and credential setup, not standing instructions.",
    ),
    "Recently resolved": (
        "f0d36db14776dc33a166cae777b72f3afa7997e35095efc941ea9e298996a780",
        "Historical implementation changelog; ongoing constraints are preserved in their canonical sections.",
    ),
}

# Kept as named witnesses for the existing rule-identity extractor. Mechanism
# bullets are now retained as Claude-specific references, not silently omitted.
DELIBERATE_OMISSIONS = {
    "Secrets management — SOPS+age, snapshots in the PRIVATE `ateles-private` repo (Design B)": "Historical changelog; exact section omission is digest-bound.",
    "Issues sync runaway": "Historical changelog; exact section omission is digest-bound.",
    "Post-checkout hook in worktrees": "Historical changelog; exact section omission is digest-bound.",
}

# The old renderer put this machine-authored header outside its markers. Only
# that exact generated prefix may migrate; arbitrary human bytes never do.
LEGACY_HEADER = "# Ateles / Neotoma — standing operator instructions (Codex)\n\n**This file is generated. Do not hand-edit inside the generated markers.**\nSource of truth: `CLAUDE.md` on `origin/main` of the `ateles` repo.\nRegenerate with `execution/scripts/render_codex_agents_md.py`; `--check` holds\nit equal to its source in `scripts/lint.sh`.\n\nThese are standing operator instructions, not suggestions. Each was set or\nre-stated because it was broken and cost real work. They are reproduced here\nbecause Codex inlines this file into every session, the same way Claude Code\nre-injects `CLAUDE.md` after every compaction — an instruction that lives only\nin conversation is lost at the first context boundary.\n\n## What is different about running in Codex\n\n`CLAUDE.md` documents several rules as *mechanically enforced* by Claude Code\nhooks (`git_stash_guard.py`, `gmail_send_gate.py`,\n`sibling_repo_worktree_guard.py`, `decision_shape_gate.py`,\n`stop_finalizer.py`). **Those hooks do not run in Codex.** Codex has its own\nhook mechanism (`session_start_command` / `session_end_command` in\n`~/.codex/config.toml`), and as of this writing it carries session-anchoring\nonly, not any of the guards.\n\nSo in Codex every rule below is enforced by **you reading it**, with no\nmechanism behind it. Where a rule names a hook as its enforcement, treat the\nrule as fully binding and the hook as absent. The two that have bitten hardest\nwith no guard in place:\n\n- **`git stash` is not blocked here.** Never run it in any form.\n- **A Gmail send is not gated here.** No `gws gmail ... drafts update`,\n  `drafts send`, or `messages send` without a per-message operator approval.\n\n## Resuming a workstream\n\nTo pick up an Ateles or Neotoma workstream that stopped mid-flight, use the\n`continue-session` skill (`~/.codex/skills/continue-session/`). It derives\npresent state rather than asserting it, and it carries these same constraints.\n"


def project_source(source_text: str) -> tuple[str, list[str]]:
    if not source_text.strip():
        die("source is empty; the check did NOT run")
    if BEGIN in source_text or END in source_text:
        die("source contains target ownership markers; refusing ambiguous input")
    kept, honoured = [], []
    for section in re.split(r"(?=^## )", source_text, flags=re.MULTILINE):
        title = section.splitlines()[0][3:] if section.startswith("## ") else None
        if title in SECTION_OMISSIONS:
            digest, reason = SECTION_OMISSIONS[title]
            if hashlib.sha256(section.encode()).hexdigest() != digest:
                die(
                    f"omitted section {title!r} changed; review its full content before omitting it"
                )
            honoured.append(f"{title}: {reason}")
        else:
            kept.append(section)
    return "".join(kept).strip(), honoured


def render(source_text: str) -> str:
    body, _ = project_source(source_text)
    return f"{BEGIN}\n{HEADER.rstrip()}\n\n## Canonical source (Claude Code mechanisms are contextual)\n\n{body}\n{END}"


def region(text: str) -> tuple[str, str, str]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        die(
            "target needs exactly one complete generated marker pair; refusing to overwrite user content"
        )
    start, end = text.index(BEGIN), text.index(END)
    if start >= end:
        die("generated markers are reversed; refusing to write")
    for marker, index in ((BEGIN, start), (END, end)):
        if (index and text[index - 1] != "\n") or (
            index + len(marker) < len(text) and text[index + len(marker)] not in "\r\n"
        ):
            die("ownership markers must occupy their own lines; refusing to write")
    end += len(END)
    return text[:start], text[start:end], text[end:]


def rule_keys(module, text: str, label: str) -> dict[str, object]:
    return module.extract(text, label).rules


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(text.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--check",
        action="store_true",
        help="Check complete managed text without writing",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_AGENTS_MD)
    p.add_argument("--source", type=Path, default=CLAUDE_MD)
    args = p.parse_args(argv)
    try:
        source_text = args.source.read_text(encoding="utf-8")
        expected = render(source_text)
        _, honoured = project_source(source_text)
        module = load_extractor()
        source_rules = rule_keys(module, source_text, str(args.source))
        projected_rules = rule_keys(module, expected, "projection")
        omitted = {module.normalize(k) for k in DELIBERATE_OMISSIONS}
        missing = [
            r
            for k, r in source_rules.items()
            if r.kind == "bullet" and k not in projected_rules and k not in omitted
        ]
        if missing:
            die(
                "projection lost rule identities: " + ", ".join(r.lead for r in missing)
            )
        if args.out.is_symlink():
            die("target is a symlink; refusing to read or replace its destination")
        existing = args.out.read_bytes().decode("utf-8") if args.out.exists() else ""
        if args.check and not existing:
            print(
                f"FAIL: target {'is empty' if args.out.exists() else 'does not exist'}"
            )
            return 1
        prefix, managed, suffix = region(existing) if existing else ("", "", "\n")
        legacy = prefix.startswith(LEGACY_HEADER + "\n")
        if LEGACY_HEADER in prefix and not legacy:
            die(
                "legacy generated header follows user content; review migration before writing"
            )
        if args.check:
            if managed != expected or legacy:
                print(
                    "FAIL: managed standing instructions differ from source or harness binding"
                )
                target_rules = rule_keys(module, managed, str(args.out))
                lost = [
                    r.lead
                    for k, r in projected_rules.items()
                    if r.kind == "bullet" and k not in target_rules
                ]
                if lost:
                    print("Missing rule identities: " + ", ".join(lost))
                else:
                    print(
                        "Rule bodies, limiting clauses or generated harness context drifted; regenerate from the current source"
                    )
                return 1
        else:
            if legacy:
                prefix = prefix[len(LEGACY_HEADER) + 1 :]
            atomic_write(args.out, prefix + expected + suffix)
            print(f"rendered {args.out.name} ({args.out.stat().st_size} bytes)")
        if honoured:
            print("Deliberate omissions honoured: " + "; ".join(honoured))
        print(
            f"OK: complete source projection current ({len(source_rules)} rule identities checked)"
        )
        return 0
    except (OSError, UnicodeError) as exc:
        die(f"input/output error ({type(exc).__name__}); no verification claim")


if __name__ == "__main__":
    sys.exit(main())
