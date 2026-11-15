#!/usr/bin/env python3
"""PreToolUse hook — refuse Bash commands that skip a repository's git hooks.

The rule is "never --no-verify" (CLAUDE.md, Standing constraints). When tests
must be skipped, the sanctioned form is the hook's own escape hatch,
``SKIP_TESTS=1 SKIP_TESTS_REASON="<why>" git commit ...``, which still runs the
type-check, lint and format stages and records the reason.

Why it is mechanical: the Phoenicurus release-prepare agent (a headless
``claude --print`` run) used ``--no-verify`` on at least six release runs,
including v0.29.2 on 2026-11-15, although both its repository's rules and the
operator's rules forbid it. A brief alone did not hold, so the prepare runner
loads this hook into the agent through ``--settings``
(``execution/daemons/phoenicurus-release/prepare.py``).

Refused (each skips or disables the hooks):
  - ``--no-verify`` on any git subcommand, including unambiguous abbreviations
    git accepts (``--no-veri``, ``--no-verif``)
  - ``git commit -n``, including inside a short-flag cluster (``-an``, ``-nm``)
  - ``git -c core.hooksPath=...`` and ``git config core.hooksPath <value>``
  - ``HUSKY=0`` as a command prefix, through ``env``, or via ``export``
  - the same inside ``sh|bash|zsh -c '...'``, ``eval``, ``$(...)`` and
    backticks, and an inline ``python -c`` / ``node -e`` / ``perl -e`` /
    ``ruby -e`` program that names ``--no-verify``, ``core.hooksPath`` or
    ``HUSKY`` together with git

Not refused: text that merely mentions the flag. Commands are tokenised with
shell quoting, so a commit message, PR body, grep pattern or echo that contains
``--no-verify`` is one quoted token and never matches; only a bare argument to
a git invocation does. ``git config --get core.hooksPath`` (a read) is allowed.

Known limits (a tripwire, not a boundary): a script file written to disk and
then executed, an edited or deleted hook file, ``GIT_CONFIG_PARAMETERS`` and
other indirection are not seen. The real backstop is CI on the pull request.

There is no override. Unlike the stash and Gmail gates, skipping hooks has no
approved form: the approved way to skip tests is SKIP_TESTS, which this hook
does not touch.

Fail-open: any internal error or unreadable input exits 0, so a bug here never
blocks a session.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

# Long options git accepts by unambiguous prefix. `--no-ver` is ambiguous
# with `--no-verbose` on commit, so the shortest refused spelling is
# `--no-veri`.
_NO_VERIFY = re.compile(r"^--no-veri(?:f|fy)?$")
_OPERATOR = re.compile(r"^[;&|()<>]+$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_INTERPRETERS = {
    "python": "-c", "python3": "-c", "node": "-e", "perl": "-e", "ruby": "-e",
}
# Wrappers whose remaining arguments are themselves a command.
_PASSTHROUGH = {"command", "nohup", "exec", "builtin", "time", "sudo", "xargs"}
# git global options that take a separate value token.
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--exec-path", "--super-prefix", "--config-env"}
# `git commit` short options that consume the rest of the cluster or the next
# token as their value.
_COMMIT_VALUE_FLAGS = set("mFCct")
_CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--list", "-l",
                      "--show-origin", "--show-scope", "--get-urlmatch"}
_QUICK = re.compile(r"no-veri|hookspath|husky|\s-[A-Za-z]*n", re.IGNORECASE)
_INLINE_PROGRAM_HIT = re.compile(r"--no-veri|core\.hookspath|husky", re.IGNORECASE)
_MAX_DEPTH = 4


def log(msg: str) -> None:
    try:
        print(f"[git_hook_bypass_guard] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never block
        pass


def deny(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 2


def guidance(label: str) -> str:
    return (
        f"Refused: `{label}` skips this repository's git hooks. The standing "
        f"rule is never --no-verify, and the hooks are not to be disabled any "
        f"other way (git commit -n, HUSKY=0, core.hooksPath).\n\n"
        f"If the test stage cannot pass for a reason unrelated to your change, "
        f"use the hook's own escape hatch, which still runs type-check, lint "
        f"and format and records why:\n"
        f"  SKIP_TESTS=1 SKIP_TESTS_REASON=\"<why>\" git commit ...\n"
        f"and state that reason in your report.\n\n"
        f"If any other stage fails, or the hook fails for an environment "
        f"reason, stop and report it. There is no override for this guard."
    )


def _tokens(command: str) -> list[str] | None:
    # Newlines and backticks separate commands. Inside a quoted string the
    # substitution only changes that one token's text, which is never judged.
    text = re.sub(r"\\[ \t]*\n", " ", command).replace("\n", " ; ").replace("`", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if _OPERATOR.match(tok):
            if cur:
                out.append(cur)
            cur = []
            continue
        # Command substitution: `$(git` -> `$` `(` `git` after punctuation
        # splitting; the bare `$` is not a command.
        if tok == "$":
            continue
        cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _strip_leading(cmd: list[str]) -> tuple[list[str], str | None]:
    """Drop leading assignments and pass-through wrappers. Returns the bare
    command and a label if a HUSKY=0 assignment was among the prefixes."""
    label = None
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if _ASSIGNMENT.match(tok):
            if tok.split("=", 1)[0] == "HUSKY" and tok.split("=", 1)[1] == "0":
                label = "HUSKY=0"
            i += 1
            continue
        base = os.path.basename(tok)
        if base == "env":
            i += 1
            while i < len(cmd) and (cmd[i].startswith("-") or _ASSIGNMENT.match(cmd[i])):
                if cmd[i] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    i += 1
                elif cmd[i] == "HUSKY=0":
                    label = "HUSKY=0"
                i += 1
            continue
        if base in _PASSTHROUGH:
            i += 1
            while i < len(cmd) and cmd[i].startswith("-"):
                i += 1
            continue
        if base in ("timeout", "nice"):
            i += 1
            while i < len(cmd) and cmd[i].startswith("-"):
                i += 1
            if base == "timeout" and i < len(cmd):
                i += 1  # the duration
            continue
        break
    return cmd[i:], label


def _git_violation(args: list[str]) -> str | None:
    i = 0
    sub = None
    while i < len(args):
        tok = args[i]
        if tok in ("-c", "--config-env") and i + 1 < len(args):
            if args[i + 1].lower().startswith("core.hookspath"):
                return "git -c core.hooksPath=..."
            i += 2
            continue
        if tok.lower().startswith("-ccore.hookspath"):
            return "git -c core.hooksPath=..."
        if tok in _GIT_GLOBAL_WITH_VALUE and i + 1 < len(args):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub = tok
        i += 1
        break
    rest = args[i:]
    for tok in rest:
        if _NO_VERIFY.match(tok):
            return f"git {sub or ''} {tok}".replace("  ", " ")
    if sub == "commit":
        j = 0
        while j < len(rest):
            tok = rest[j]
            if tok == "--":
                break
            if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
                for k, ch in enumerate(tok[1:]):
                    if ch == "n":
                        return "git commit -n"
                    if ch in _COMMIT_VALUE_FLAGS:
                        if k == len(tok) - 2:
                            j += 1  # value is the next token
                        break
            j += 1
    if sub == "config":
        lowered = [t.lower() for t in rest]
        # Setting it disables the repository's hooks; reading or unsetting it
        # (which restores the default hooks directory) does not.
        if ("core.hookspath" in lowered and "--unset" not in rest
                and not (set(rest) & _CONFIG_READ_FLAGS)):
            return "git config core.hooksPath"
    return None


def find_bypass(command: str, depth: int = 0) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    tokens = _tokens(command)
    if tokens is None:
        # Unbalanced quoting: judge the raw text conservatively.
        if re.search(r"\bgit\b[^;&|]*\s--no-veri", command):
            return "git ... --no-verify"
        return None
    for cmd in _simple_commands(tokens):
        cmd, label = _strip_leading(cmd)
        if label:
            return label
        if not cmd:
            continue
        base = os.path.basename(cmd[0])
        if base == "export" and any(t == "HUSKY=0" for t in cmd[1:]):
            return "export HUSKY=0"
        if base == "git":
            hit = _git_violation(cmd[1:])
            if hit:
                return hit
            continue
        if base in _SHELLS and "-c" in cmd[1:]:
            idx = cmd.index("-c", 1)
            if idx + 1 < len(cmd):
                hit = find_bypass(cmd[idx + 1], depth + 1)
                if hit:
                    return hit
            continue
        if base == "eval":
            hit = find_bypass(" ".join(cmd[1:]), depth + 1)
            if hit:
                return hit
            continue
        flag = _INTERPRETERS.get(re.sub(r"[0-9.]+$", "", base)) or _INTERPRETERS.get(base)
        if flag and flag in cmd[1:]:
            idx = cmd.index(flag, 1)
            program = cmd[idx + 1] if idx + 1 < len(cmd) else ""
            if "git" in program and _INLINE_PROGRAM_HIT.search(program):
                return f"{base} {flag} program skipping git hooks"
    return None


def main() -> int:
    payload = read_hook_input()
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not _QUICK.search(command):
        return 0
    label = find_bypass(command)
    if label is None:
        return 0
    log(f"blocking {label}")
    return deny(guidance(label))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
