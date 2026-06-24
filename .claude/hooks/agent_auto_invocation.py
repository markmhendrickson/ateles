#!/usr/bin/env python3
"""UserPromptSubmit hook: topic-based auto-invocation of Ateles panel agents.

On every prompt submission, scores the prompt against the routing manifest at
``.claude/agent-routing.json`` and, when a panel agent's domain is clearly
implicated, injects a concise note so the session adopts that agent's operating
guidance (or invokes it explicitly via its slash command).

Design constraints:
- Fail open. Any error (missing manifest, bad JSON, unreadable stdin) results in
  exit 0 with no output — never block or delay a user's prompt.
- Read-only and dependency-free (stdlib only).
- Suggest, don't spawn. This nudges the active session toward the right agent's
  guidance rather than forking a nested ``claude`` process; autonomous spawning
  is already handled by the T3 daemons (Anthus/Formica).

Matching:
  score(agent) = signal_weight * (# signal substrings present)
               + strong_signal_weight * (# strong_signal substrings present)
  Agents scoring >= min_score are surfaced, top max_agents by score.

Test mode:
  python3 agent_auto_invocation.py --prompt "implement issue #42 and open a pr"
  python3 agent_auto_invocation.py --selftest
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _manifest_path() -> Path:
    """Locate agent-routing.json relative to the project dir or this script."""
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir) / ".claude" / "agent-routing.json")
    # Fallback: alongside this hook's parent (.claude/hooks/.. -> .claude)
    candidates.append(Path(__file__).resolve().parent.parent / "agent-routing.json")
    for path in candidates:
        if path.is_file():
            return path
    return candidates[-1]


def _load_manifest() -> dict:
    return json.loads(_manifest_path().read_text(encoding="utf-8"))


def score_agents(prompt: str, manifest: dict) -> list[dict]:
    """Return matched agents sorted by descending score, capped to max_agents."""
    config = manifest.get("config", {})
    if not config.get("enabled", True):
        return []

    min_score = config.get("min_score", 2)
    max_agents = config.get("max_agents", 3)
    sig_w = config.get("signal_weight", 1)
    strong_w = config.get("strong_signal_weight", 2)

    text = prompt.lower()
    matches: list[dict] = []

    for agent in manifest.get("agents", []):
        # Skip if the user already invoked this agent explicitly.
        command = agent.get("command", "")
        if command and command.lower() in text:
            continue

        hits: list[str] = []
        score = 0
        for term in agent.get("signals", []):
            if term.lower() in text:
                score += sig_w
                hits.append(term)
        for term in agent.get("strong_signals", []):
            if term.lower() in text:
                score += strong_w
                hits.append(term)

        if score >= min_score:
            matches.append(
                {
                    "name": agent.get("name", "?"),
                    "command": command,
                    "role": agent.get("role", ""),
                    "score": score,
                    # De-duplicate hits while preserving order.
                    "hits": list(dict.fromkeys(hits)),
                }
            )

    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:max_agents]


def render_context(matches: list[dict]) -> str:
    """Render the additionalContext block for the matched agents."""
    lines = [
        "[agent-auto-invocation] This request overlaps the domain of the "
        "following Ateles panel agent(s). Adopt their operating guidance for "
        "this turn, or invoke one explicitly via its slash command. Full "
        "instructions live in .claude/skills/<name>/SKILL.md.",
    ]
    for m in matches:
        matched = ", ".join(m["hits"][:4])
        lines.append(f"- {m['name'].title()} ({m['command']}) — {m['role']} [matched: {matched}]")
    return "\n".join(lines)


def emit_hook_output(context: str) -> None:
    """Print structured UserPromptSubmit additionalContext JSON."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


def _run_selftest() -> int:
    manifest = _load_manifest()
    cases = [
        ("implement issue #42 and open a pr", {"cicada"}),
        ("can you review the pr and squash merge it", {"vanellus"}),
        ("design the user flow and information architecture for onboarding", {"accipiter"}),
        ("draft a contract review for compliance and the privacy policy", {"buteo"}),
        ("what's the weather today", set()),
        ("help me prioritise the roadmap and cut scope for this milestone", {"pavo"}),
    ]
    ok = True
    for prompt, expected in cases:
        got = {m["name"] for m in score_agents(prompt, manifest)}
        passed = expected.issubset(got)
        ok = ok and passed
        flag = "PASS" if passed else "FAIL"
        print(f"[{flag}] {prompt!r}\n        expected>={sorted(expected)} got={sorted(got)}")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return _run_selftest()

    if "--prompt" in args:
        prompt = args[args.index("--prompt") + 1]
        manifest = _load_manifest()
        matches = score_agents(prompt, manifest)
        if matches:
            print(render_context(matches))
        else:
            print("(no agent matched)")
        return 0

    # Hook mode: read JSON event from stdin.
    raw = sys.stdin.read()
    event = json.loads(raw)
    prompt = event.get("prompt", "")
    if not prompt:
        return 0

    manifest = _load_manifest()
    matches = score_agents(prompt, manifest)
    if matches:
        emit_hook_output(render_context(matches))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: never block or delay the user's prompt.
        sys.exit(0)
