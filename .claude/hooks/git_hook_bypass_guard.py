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
  - ``HUSKY`` and git-config environment overrides as a command prefix,
    through ``env``, or via ``export``
  - the same inside ``sh|bash|zsh -c '...'``, ``eval``, ``$(...)`` and
    backticks, and an inline ``python -c`` / ``node -e`` / ``perl -e`` /
    ``ruby -e`` program that names ``--no-verify``, ``core.hooksPath`` or
    ``HUSKY`` together with git

Not refused: text that merely mentions the flag. Commands are tokenised with
shell quoting, so a commit message, PR body, grep pattern or echo that contains
``--no-verify`` is one quoted token and never matches; only a bare argument to
a git invocation does. ``git config --get core.hooksPath`` (a read) is allowed.

This is a tripwire against accidental bypass, not a security boundary. A script
written to disk, or an indirection this parser cannot see, can still skip hooks.
The real controls are the prepare child's allowlisted environment and the
release checks that run after the agent.

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
_SENSITIVE_ASSIGNMENT_NAME = re.compile(
    r"^(?:HUSKY|GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+|PARAMETERS|GLOBAL|SYSTEM))$"
)
_SHELL_EXPANSION = re.compile(
    r"\$\(|`|\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)"
)
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_INTERPRETERS = {
    "python": "-c", "python3": "-c", "node": "-e", "perl": "-e", "ruby": "-e",
}
# Wrappers whose remaining arguments are themselves a command.
_PASSTHROUGH = {"command", "nohup", "exec", "builtin", "time", "sudo", "xargs"}
# git global options that take a separate value token.
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--exec-path", "--super-prefix", "--config-env"}
# `git commit` options whose next token is data, even when it looks like an
# option or contains shell expansion.
_COMMIT_SHORT_VALUE_FLAGS = set("mFCct")
_COMMIT_LONG_VALUE_FLAGS = {
    "--message", "--file", "--reuse-message", "--reedit-message", "--template",
    "--author", "--date", "--cleanup", "--fixup", "--squash",
    "--pathspec-from-file",
}
_CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch",
                      "--list", "-l"}
_CONFIG_READ_ACTIONS = {"get", "list"}
_CONFIG_UNSET_FLAGS = {"--unset", "--unset-all"}
_CONFIG_SET_FLAGS = {"--add", "--replace-all"}
_QUICK = re.compile(
    r"no-veri|hookspath|husky|git_config_|\s-[A-Za-z]*n|['\"]-n|\$|`",
    re.IGNORECASE,
)
_HEREDOC_START = re.compile(
    r"(?m)^[ \t]*(?:/[^\s]+/)?"
    r"(?P<program>python(?:3(?:\.\d+)*)?|node|perl|ruby|sh|bash|zsh|dash|ksh)"
    r"\b[^\n]*<<-?[ \t]*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)[^\n]*\n"
)
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
    # Newlines separate commands. Backticks stay in their argument so a git
    # option assembled by command substitution is visible as unjudgeable;
    # standalone backtick bodies are recursed into separately in find_bypass.
    text = re.sub(r"\\[ \t]*\n", " ", command).replace("\n", " ; ")
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
        cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _sensitive_assignment(tok: str) -> str | None:
    if not _ASSIGNMENT.match(tok):
        return None
    name = tok.split("=", 1)[0]
    return name if _SENSITIVE_ASSIGNMENT_NAME.fullmatch(name) else None


def _strip_leading(cmd: list[str]) -> tuple[list[str], str | None]:
    """Drop leading assignments and pass-through wrappers.

    Returns the bare command and the name of any environment assignment that
    can redirect or disable git's hook configuration.
    """
    label = None
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if _ASSIGNMENT.match(tok):
            label = label or _sensitive_assignment(tok)
            i += 1
            continue
        base = os.path.basename(tok)
        if base == "env":
            i += 1
            while i < len(cmd) and (cmd[i].startswith("-") or _ASSIGNMENT.match(cmd[i])):
                if cmd[i] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    i += 1
                else:
                    label = label or _sensitive_assignment(cmd[i])
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


def _has_shell_expansion(tok: str) -> bool:
    return bool(_SHELL_EXPANSION.search(tok))


def _config_sets_hooks_path(rest: list[str]) -> bool:
    lowered = [tok.lower() for tok in rest]
    if "core.hookspath" not in lowered:
        return False
    if set(lowered) & _CONFIG_UNSET_FLAGS:
        return False
    if set(lowered) & _CONFIG_READ_FLAGS:
        return False
    action = next((tok for tok in lowered if not tok.startswith("-")), "")
    if action in _CONFIG_READ_ACTIONS or action == "unset":
        return False
    if action == "set" or set(lowered) & _CONFIG_SET_FLAGS:
        return True
    key_index = lowered.index("core.hookspath")
    # Legacy syntax with one positional argument is a read. A second
    # positional argument is the value and therefore a set.
    return any(not tok.startswith("-") for tok in lowered[key_index + 1:])


def _commit_violation(rest: list[str]) -> str | None:
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok == "--":
            return None
        if tok.startswith("--"):
            name, has_equals, _value = tok.partition("=")
            if name in _COMMIT_LONG_VALUE_FLAGS:
                if not has_equals:
                    j += 1
                j += 1
                continue
            if _NO_VERIFY.match(tok):
                return f"git commit {tok}"
            if _has_shell_expansion(tok):
                return "git commit with an expanded option"
            j += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            cluster = tok[1:]
            for k, ch in enumerate(cluster):
                if ch == "n":
                    return "git commit -n"
                if ch == "u":
                    # -uno / -uall: the remainder is the optional mode value,
                    # not a cluster containing -n.
                    if k + 1 < len(cluster):
                        break
                    continue
                if ch in _COMMIT_SHORT_VALUE_FLAGS:
                    if k + 1 == len(cluster):
                        j += 1
                    break
            else:
                if _has_shell_expansion(tok):
                    return "git commit with an expanded option"
            j += 1
            continue
        if tok.startswith("$") or "`" in tok:
            return "git commit with an expanded option"
        j += 1
    return None


def _git_violation(args: list[str]) -> str | None:
    i = 0
    sub = None
    while i < len(args):
        tok = args[i]
        if _has_shell_expansion(tok):
            return "git global option assembled by shell expansion"
        if tok.startswith("--config-env="):
            if tok.split("=", 1)[1].lower().startswith("core.hookspath="):
                return "git --config-env core.hooksPath=..."
            i += 1
            continue
        if tok in ("-c", "--config-env") and i + 1 < len(args):
            value = args[i + 1]
            if _has_shell_expansion(value):
                return "git global option assembled by shell expansion"
            if value.lower().startswith("core.hookspath"):
                return "git -c core.hooksPath=..."
            i += 2
            continue
        if tok.lower().startswith("-ccore.hookspath"):
            return "git -c core.hooksPath=..."
        if tok in _GIT_GLOBAL_WITH_VALUE and i + 1 < len(args):
            if _has_shell_expansion(args[i + 1]):
                return "git global option assembled by shell expansion"
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub = tok
        i += 1
        break
    rest = args[i:]
    if sub == "commit":
        return _commit_violation(rest)
    if sub == "config":
        for tok in rest:
            if tok == "--":
                break
            if _has_shell_expansion(tok) and (tok.startswith(("-", "$")) or "`" in tok):
                return "git config option assembled by shell expansion"
        if _config_sets_hooks_path(rest):
            return "git config core.hooksPath"
        return None
    for tok in rest:
        if tok == "--":
            break
        if _NO_VERIFY.match(tok):
            return f"git {sub or ''} {tok}".replace("  ", " ")
        if _has_shell_expansion(tok) and (tok.startswith(("-", "$")) or "`" in tok):
            return f"git {sub or ''} option assembled by shell expansion".replace("  ", " ")
    return None


def _program_words(program: str) -> list[str]:
    return re.findall(r"--?[A-Za-z0-9_.$/:=-]+|[A-Za-z0-9_.$/:=-]+", program)


def _inline_program_violation(program: str) -> str | None:
    words = _program_words(program)
    if not any(os.path.basename(tok) == "git" for tok in words):
        return None
    if any(tok.upper().startswith("HUSKY") for tok in words):
        return "inline program sets HUSKY around git"
    for i, tok in enumerate(words):
        if os.path.basename(tok) == "git":
            hit = _git_violation(words[i + 1:])
            if hit:
                return hit
    return None


def _heredoc_violation(command: str) -> str | None:
    for match in _HEREDOC_START.finditer(command):
        delimiter = re.escape(match.group("delimiter"))
        end = re.search(rf"(?m)^[ \t]*{delimiter}[ \t]*$", command[match.end():])
        if end is None:
            continue
        body = command[match.end():match.end() + end.start()]
        if _inline_program_violation(body):
            return f"{match.group('program')} heredoc program skipping git hooks"
    return None


def _shell_program_index(cmd: list[str]) -> int | None:
    for i, tok in enumerate(cmd[1:], start=1):
        if tok == "-c":
            return i + 1
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            return i + 1
        if not tok.startswith("-"):
            return None
    return None


def find_bypass(command: str, depth: int = 0) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    heredoc_hit = _heredoc_violation(command)
    if heredoc_hit:
        return heredoc_hit
    for body in re.findall(r"`([^`]*)`", command, flags=re.DOTALL):
        hit = find_bypass(body, depth + 1)
        if hit:
            return hit
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
        if base == "export":
            assignment = next(
                (name for tok in cmd[1:] if (name := _sensitive_assignment(tok))),
                None,
            )
            if assignment:
                return f"export {assignment}"
        if base == "git":
            hit = _git_violation(cmd[1:])
            if hit:
                return hit
            continue
        if base in _SHELLS:
            program_index = _shell_program_index(cmd)
            if program_index is not None and program_index < len(cmd):
                hit = find_bypass(cmd[program_index], depth + 1)
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
            if _inline_program_violation(program):
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
