#!/usr/bin/env python3
"""PreToolUse hook: action-time gate for swarm-owned operations.

The Ateles operating rule is "delegate through the swarm — you are the
orchestrator, not the workhorse" (docs/agents/ateles.md). That rule was prose in
a prompt, so a session agent could hand-roll a swarm-owned operation (e.g. cut a
release by hand) simply by not remembering it. This hook makes the rule
*enforced at the moment of action* rather than *suggested at prompt time*.

It intercepts the specific, deterministic Bash commands that a swarm process
owns end-to-end and DENIES them with a redirect to the owning process. It does
NOT try to guess intent from prose — it matches the actual command, so it fires
regardless of how the session arrived at the action.

Design constraints (match the other hooks in this dir):
- **Fail open.** Any error (unreadable stdin, bad JSON, unexpected shape) exits 0
  with no output — never block or delay a legitimate action on a hook bug.
- **Deterministic.** Regex on the command string, not topic scoring.
- **Escapable.** A deliberate, reviewed exception carries the literal override
  token ``SWARM_OVERRIDE`` in the command (e.g. as a trailing ``# SWARM_OVERRIDE``
  comment); the gate then allows it. This keeps the operator/agent in control for
  the genuine one-off while making the default path the swarm path.
- Stdlib-only, dependency-free.

Rules are data (``GATED``), so adding a new swarm-owned surface is one entry.

Test mode:
  echo '{"tool_name":"Bash","tool_input":{"command":"npm publish"}}' | python3 swarm_delegation_gate.py
  python3 swarm_delegation_gate.py --selftest
"""

from __future__ import annotations

import json
import re
import sys

# Literal token that, when present anywhere in the command, bypasses the gate.
# Use for a deliberate, reviewed one-off (the gate names it in every denial).
OVERRIDE_TOKEN = "SWARM_OVERRIDE"

# Each rule: a compiled pattern over the Bash command + the redirect message.
# Keep patterns tight (anchored on the actual tool/verb) to avoid false positives
# on unrelated commands that merely mention the word.
GATED: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bnpm\s+publish\b"),
        "Publishing Neotoma to npm is owned by the phoenicurus-release pipeline "
        "(execution/daemons/phoenicurus-release/publish.py), which handles the "
        "automation token, sandbox deploy, post-deploy probes, release_result "
        "tracking, and Telegram confirmation. Route the release through: "
        "prepare.py (or /release PREPARE) -> operator approval -> publish.py "
        "--version vX.Y.Z. Do not run `npm publish` by hand.",
    ),
    (
        re.compile(r"\bgh\s+release\s+create\b"),
        "Creating a GitHub Release for Neotoma is a phoenicurus-release step "
        "(publish.py does merge->tag->npm publish->GitHub Release->deploy->probes "
        "as one deterministic sequence). Don't create the release by hand — route "
        "through the release pipeline so the tag/publish/deploy stay consistent.",
    ),
    (
        # A release version tag (vX.Y.Z). publish.py owns tagging; a hand tag
        # desyncs the pipeline (its preflight refuses when the tag already exists).
        # Only gate tag *creation* — read-only listing (`--list`/`-l`) is fine.
        re.compile(
            r"\bgit\s+tag\b(?!\s+(?:--list|-l|--verify|-v|-d|--delete)\b)"
            r".*\bv\d+\.\d+\.\d+\b"
        ),
        "Tagging a release version (vX.Y.Z) is owned by phoenicurus-release "
        "(publish.py tags after operator approval). A hand-made tag desyncs the "
        "pipeline — its preflight refuses to publish when the tag already exists. "
        "Route the release through prepare.py -> approve -> publish.py instead.",
    ),
]


def decision_for(command: str) -> tuple[bool, str]:
    """Return (blocked, reason). blocked=False when nothing matches or overridden."""
    if not command or OVERRIDE_TOKEN in command:
        return (False, "")
    for pattern, reason in GATED:
        if pattern.search(command):
            return (True, reason)
    return (False, "")


def _emit_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        reason
                        + f"\n\n(If this is a deliberate, reviewed exception, re-run "
                        f"the command with a trailing `# {OVERRIDE_TOKEN}` marker to "
                        f"bypass this gate.)"
                    ),
                }
            }
        )
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail open

    try:
        if event.get("tool_name") != "Bash":
            return 0
        command = (event.get("tool_input") or {}).get("command", "")
        blocked, reason = decision_for(command)
        if blocked:
            _emit_deny(reason)
        return 0
    except Exception:
        return 0  # fail open


def _selftest() -> int:
    cases = [
        ("npm publish", True),
        ("NODE_AUTH_TOKEN=x npm publish", True),
        ("npm publish # SWARM_OVERRIDE", False),
        ("gh release create v0.18.8 --notes-file x", True),
        ("git tag -a v0.18.8 -m 'x'", True),
        ("git tag some-feature-tag", False),  # not a version tag
        ("npm test", False),
        ("npm run publish:local", False),  # not `npm publish`
        ("gh release view v0.18.8", False),  # view, not create
        ("git tag --list v0.18.8", False),  # list, not create — read-only
    ]
    ok = True
    for cmd, want in cases:
        got, _ = decision_for(cmd)
        mark = "ok" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{mark}] blocked={got} (want {want}): {cmd}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--command" in sys.argv:
        i = sys.argv.index("--command")
        blocked, reason = decision_for(sys.argv[i + 1] if i + 1 < len(sys.argv) else "")
        print(json.dumps({"blocked": blocked, "reason": reason}, indent=2))
        sys.exit(0)
    sys.exit(main())
