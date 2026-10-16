#!/usr/bin/env python3
"""PreToolUse hook — refuse a `gh` mutating call staged with an EMPTY GH_TOKEN.

Companion to execution/scripts/verify_gh_identity.py, which does the
authoritative live check (asks GitHub who a token actually resolves to).
This hook catches the same hazard one step earlier, at the shell-command
level, before the network round-trip: a Bash command of the form

    GH_TOKEN="" gh pr create ...
    GH_TOKEN= gh pr create ...

An empty-but-SET GH_TOKEN/GITHUB_TOKEN is not "no token" to `gh` — `gh`
silently treats it the same as unset and falls back to whatever account is
active in the shared keyring session on this host. That produced a PR opened
under the operator's own account instead of the intended agent's, with no
error anywhere in the path (repro: `GH_TOKEN="" gh api user --jq .login`
succeeds and prints the keyring identity; `GH_TOKEN="badtoken" gh api user`
correctly fails loudly instead — the empty-string case is the one gap).

This hook only catches the LITERAL shape of an inline empty-token prefix on
a mutating `gh` invocation (pr create, pr merge, pr close, issue
create/close/comment, api with a mutating verb, release create). It cannot
see a token that is empty because of an *unset* shell variable expanding to
nothing several layers up (e.g. a variable set by a script that itself
silently resolved empty) — that is exactly why
execution/scripts/verify_gh_identity.py exists as the authoritative,
call-anywhere check: run it before spawning a child or invoking `gh` from
any script, not only from an interactively-typed shell command.

Explicitly NOT covered (allowed):
  - Any gh invocation with a non-empty (even if invalid) GH_TOKEN/GITHUB_TOKEN
    prefix — an invalid token already fails loudly at the API, which is
    correct behaviour; this hook only closes the SILENT gap.
  - Read-only gh calls (pr view, pr list, issue view, api GET, auth status).
  - `gh auth login` / `gh auth switch` — those are flagged elsewhere (project
    convention: interactive-only, never run by an agent) and are out of scope
    for this specific empty-token hazard.
  - Any command that doesn't mention `gh`.

Fail-open: any error or unparseable input → exit 0 (never block a session on
our own bug).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

# Matches GH_TOKEN="" / GH_TOKEN='' / GH_TOKEN= (nothing) / GITHUB_TOKEN
# equivalents, optionally after `env `, immediately followed by whitespace
# then a `gh` invocation somewhere later in the same segment.
EMPTY_TOKEN_PREFIX = re.compile(
    r"(?:^|(?<=[\s;&|]))"
    r"(?:env\s+)?"
    r"(GH_TOKEN|GITHUB_TOKEN)="
    r"(?:\"\"|''|)(?=[\s]|$)"
)

# Mutating gh subcommands worth gating. Reads (view/list/status) are allowed —
# an empty-token READ under the wrong identity is not a delivered side effect,
# and gating it too would make every `gh pr view` sensitive to this hook.
MUTATING_GH = re.compile(
    r"\bgh\b.*\b(?:"
    r"pr\s+(?:create|merge|close|edit|comment|review)|"
    r"issue\s+(?:create|close|comment|edit)|"
    r"release\s+(?:create|delete|edit)|"
    r"api\s+.*(?:-X\s*(?:POST|PATCH|PUT|DELETE)|--method\s*(?:POST|PATCH|PUT|DELETE))"
    r")\b"
)

SEGMENT_SPLIT = re.compile(r"&&|[;\n|]")


def log(msg: str) -> None:
    try:
        print(f"[gh_identity_guard] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def deny(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 2


def guidance(var_name: str) -> str:
    return (
        f"Refused: this command prefixes a `gh` call with an EMPTY "
        f"{var_name} (`{var_name}=\"\"` or `{var_name}=` with nothing after it).\n\n"
        f"`gh` treats an empty-but-set {var_name} the same as unset and "
        f"silently falls back to whatever account is active in the shared "
        f"keyring session — this is the exact failure that opened a PR under "
        f"the operator's own account instead of the intended agent's, with "
        f"no error anywhere in the path.\n\n"
        f"Fix: provision the real token before running this command, or run\n"
        f"  python3 execution/scripts/verify_gh_identity.py --expect-login <agent>\n"
        f"first to confirm what identity you actually have. Never let a `gh` "
        f"mutating call proceed on an empty token."
    )


def find_violation(command: str):
    for segment in SEGMENT_SPLIT.split(command):
        normalized = segment.strip()
        if not normalized or "gh" not in normalized:
            continue
        m = EMPTY_TOKEN_PREFIX.search(normalized)
        if m and MUTATING_GH.search(normalized):
            return m.group(1)
    return None


def main() -> int:
    payload = read_hook_input()
    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "gh" not in command:
        return 0

    var_name = find_violation(command)
    if var_name is None:
        return 0

    log(f"blocking empty {var_name} on a mutating gh call")
    return deny(guidance(var_name))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
