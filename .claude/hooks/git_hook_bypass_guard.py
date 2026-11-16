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
  - ``git -c core.hooksPath=...`` (and ``include.path``), and every
    ``git config`` write, unset, rename or removal that can reach
    ``core.hooksPath`` in any scope (``--global``, ``--system``, ``--local``,
    ``--worktree``, ``--file``), plus ``git config --edit``; an unrecognised
    option on a command naming the key counts as a write
  - ``git --git-dir``, ``--work-tree`` and ``--exec-path=<dir>``
  - ``HUSKY``, every ``GIT_CONFIG*`` variable, ``GIT_DIR``, ``GIT_WORK_TREE``
    and ``GIT_EXEC_PATH`` as a command prefix, through ``env``, or via
    ``export``
  - options the shell assembles (``--no-ver""ify``, ``--no-$'verify'``,
    ``$FLAG``), a command name it assembles, and the command ``env -S`` /
    ``--split-string`` runs, judged recursively
  - the same inside ``sh|bash|zsh -c '...'``, ``eval``, ``$(...)`` and
    backticks, and an inline ``python -c`` / ``node -e`` / ``perl -e`` /
    ``ruby -e`` program that can start a process and names ``--no-verify``,
    ``core.hooksPath`` or ``HUSKY`` together with git

Not refused: text that merely mentions the flag. Commands are tokenised with
shell quoting, so a commit message, PR body, grep pattern or echo that contains
``--no-verify`` is one quoted token and never matches; only a bare argument to
a git invocation does. Reads (``git config --get core.hooksPath``,
``--list``, ``--show-origin``, ``git config get ...``) are allowed, and so is
an inline program with no process-spawning call (``print('--no-verify')``).
The pre-filter only skips commands with no quoting, escape or expansion and
no word that could name git, so shell-assembled text is always parsed.

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
# Every GIT_CONFIG* variable (COUNT, KEY_n, VALUE_n, PARAMETERS, GLOBAL,
# SYSTEM, NOSYSTEM and the legacy GIT_CONFIG itself) changes which config git
# reads and so where core.hooksPath comes from. GIT_DIR and GIT_WORK_TREE move
# git to another repository's config and hooks; GIT_EXEC_PATH swaps the
# programs git runs, git-commit included.
_SENSITIVE_ASSIGNMENT_NAME = re.compile(
    r"^(?:HUSKY|GIT_CONFIG(?:_[A-Za-z0-9_]*)?|GIT_DIR|GIT_WORK_TREE|GIT_EXEC_PATH)$"
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
# `git config` and hook configuration. Any write, unset, rename or removal
# that can reach core.hooksPath is refused; only a recognised read with
# recognised modifiers is allowed. Anything unrecognised counts as a write.
_CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch",
                      "--get-color", "--get-colorbool", "--list", "-l"}
_CONFIG_READ_ACTIONS = {"get", "list"}
_CONFIG_ACTIONS = {"get", "list", "set", "unset", "rename-section",
                   "remove-section", "edit"}
# Modifiers that never write: scope selection and output shape.
_CONFIG_SAFE_FLAGS = {"--global", "--system", "--local", "--worktree",
                      "--show-origin", "--show-scope", "--null", "-z",
                      "--name-only", "--includes", "--no-includes", "--all",
                      "--bool", "--int", "--bool-or-int", "--path",
                      "--expiry-date", "--no-type"}
# Options whose value is the next token unless given as --opt=value.
_CONFIG_VALUE_FLAGS = {"-f", "--file", "--blob", "--type", "--default",
                       "--comment", "--value", "--url"}
_CONFIG_SECTION_FLAGS = {"--rename-section", "--remove-section"}
_CONFIG_EDIT = {"-e", "--edit"}
# git global options that point git at another repository, worktree or set of
# programs, and so at other hooks.
_GIT_RELOCATING_OPTIONS = {"--git-dir", "--work-tree", "--exec-path"}
# Anything that could carry a bypass, including text the shell will assemble
# (quotes, escapes, expansions). A command matching none of these cannot name
# git or set a hook variable, so it is allowed without parsing.
_QUICK = re.compile(
    r"git|husky|hooks|no-?veri|env|export|eval|['\"\\$`]",
    re.IGNORECASE,
)
# Calls by which an inline python/node/perl/ruby program can start a process
# or reach code indirectly. A program with none of them cannot run git, so
# text in it that merely mentions a flag is allowed. Deliberately broad: a
# false match only restores the stricter judgement.
_PROGRAM_CAN_SPAWN = re.compile(
    r"subprocess|\bos\b|system|popen|spawn|exec|child_process|require|"
    r"import|eval|getattr|__|\bqx\b|%x|`|\bopen\b|Open3|\bIO\b|Function|"
    r"binding|pty|ctypes|Process|Kernel|send|globals|compile|Deno|Bun|fork|"
    r"pipe|shell|\brun\b|\bcall\b|\$\(|\|",
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


def _env_split_string(cmd: list[str], i: int) -> tuple[str | None, int]:
    """If cmd[i] is env's -S/--split-string, return (payload, next index).

    Handles ``-S STR``, ``-SSTR``, ``-iS STR`` style clusters and
    ``--split-string[=STR]`` including unambiguous prefixes. Returns
    (None, i) when cmd[i] is not that option.
    """
    tok = cmd[i]
    if tok.startswith("--"):
        name, has_equals, value = tok.partition("=")
        if len(name) > 3 and "--split-string".startswith(name):
            if has_equals:
                return value, i + 1
            return (cmd[i + 1] if i + 1 < len(cmd) else ""), i + 2
        return None, i
    if tok.startswith("-") and len(tok) > 1:
        cluster = tok[1:]
        for k, ch in enumerate(cluster):
            if ch == "S":
                rest = cluster[k + 1:]
                if rest:
                    return rest, i + 1
                return (cmd[i + 1] if i + 1 < len(cmd) else ""), i + 2
            if ch in "uC":
                return None, i  # the remainder is that option's value
    return None, i


def _cluster_value_last(cluster: str) -> bool:
    """True when env's first value-taking short option (u, C) ends the cluster."""
    for k, ch in enumerate(cluster):
        if ch in "uC":
            return k + 1 == len(cluster)
    return False


def _strip_leading(cmd: list[str]) -> tuple[list[str], str | None, str | None]:
    """Drop leading assignments and pass-through wrappers.

    Returns the bare command, the name of any environment assignment that
    can redirect or disable git's hook configuration, and, for ``env -S``,
    the command text env will split and run (judged recursively).
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
                payload, after = _env_split_string(cmd, i)
                if payload is not None:
                    # env splits the string into words and runs them, followed
                    # by any remaining arguments.
                    nested = " ".join([payload, *(shlex.quote(t) for t in cmd[after:])])
                    return [], label, nested
                tok_i = cmd[i]
                takes_value = tok_i in ("--unset", "--chdir") or (
                    # a short cluster ending in u or C (-u, -iu, -iC) takes
                    # the next token as that option's value
                    not tok_i.startswith("--") and len(tok_i) > 1
                    and tok_i.startswith("-") and _cluster_value_last(tok_i[1:])
                )
                if takes_value:
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
    return cmd[i:], label, None


def _has_shell_expansion(tok: str) -> bool:
    return bool(_SHELL_EXPANSION.search(tok))


def _sensitive_config_key(key: str) -> bool:
    """A config key whose value decides which hooks run."""
    key = key.lower()
    return (
        key == "core.hookspath"
        or key == "include.path"
        or (key.startswith("includeif.") and key.endswith(".path"))
    )


def _sensitive_config_section(section: str) -> bool:
    section = section.lower()
    return section in ("core", "include") or section.startswith("includeif")


def _config_violation(rest: list[str]) -> str | None:
    """Judge `git config <rest>`: refuse anything that can change hook config.

    Reads (``--get``, ``--list``, ``get``, the one-argument legacy form) are
    allowed. Every set, unset, edit, section rename or removal touching
    core.hooksPath (or an include that could set it), in any scope, is
    refused; an unrecognised option makes the command count as a write.
    """
    flags: list[str] = []
    positionals: list[str] = []
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok == "--":
            positionals.extend(rest[j + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            name = tok.split("=", 1)[0].lower()
            flags.append(name)
            if name in _CONFIG_VALUE_FLAGS and "=" not in tok:
                j += 1  # its value
            j += 1
            continue
        positionals.append(tok)
        j += 1
    lowered = [p.lower() for p in positionals]
    action = lowered[0] if lowered and lowered[0] in _CONFIG_ACTIONS else None
    args = lowered[1:] if action else lowered

    if action == "edit" or set(flags) & _CONFIG_EDIT:
        return "git config --edit"
    if action in ("rename-section", "remove-section") or set(flags) & _CONFIG_SECTION_FLAGS:
        if not args or any(_sensitive_config_section(a) or _has_shell_expansion(a)
                           for a in args):
            return "git config section rename/removal touching core"
        return None
    # The key is the first argument; one the shell assembles (core.hooks$X)
    # may be core.hooksPath, so it counts as sensitive.
    key_assembled = bool(args) and _has_shell_expansion(args[0])
    if not key_assembled and not any(_sensitive_config_key(a) for a in args):
        return None
    # A sensitive key is named: allow only an unambiguous read.
    unknown = [f for f in flags if f not in _CONFIG_SAFE_FLAGS
               and f not in _CONFIG_VALUE_FLAGS and f not in _CONFIG_READ_FLAGS]
    if unknown:
        return "git config core.hooksPath"
    if action in _CONFIG_READ_ACTIONS:
        return None
    if action is not None:  # set / unset
        return "git config core.hooksPath"
    if set(flags) & _CONFIG_READ_FLAGS:
        return None
    # Legacy syntax: one positional (the key) is a read; a value makes a set.
    if len(args) == 1:
        return None
    return "git config core.hooksPath"


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
            if _sensitive_config_key(tok.split("=", 1)[1].split("=", 1)[0]):
                return "git --config-env core.hooksPath=..."
            i += 1
            continue
        if tok in ("-c", "--config-env") and i + 1 < len(args):
            value = args[i + 1]
            if _has_shell_expansion(value):
                return "git global option assembled by shell expansion"
            if _sensitive_config_key(value.split("=", 1)[0]):
                return "git -c core.hooksPath=..."
            i += 2
            continue
        if tok.startswith("-c") and len(tok) > 2:
            if _sensitive_config_key(tok[2:].split("=", 1)[0]):
                return "git -c core.hooksPath=..."
            i += 1
            continue
        relocating = tok.split("=", 1)[0]
        if relocating in _GIT_RELOCATING_OPTIONS and (
            "=" in tok or relocating != "--exec-path"
        ):
            # --exec-path alone prints the path; with a value it swaps the
            # programs git runs. --git-dir/--work-tree point at other hooks.
            return f"git {relocating}"
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
        return _config_violation(rest)
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


def _inline_program_violation(program: str, interpreted: bool = True) -> str | None:
    """Judge an inline program's text for a git hook bypass.

    ``interpreted`` is True for python/node/perl/ruby: such a program can only
    run git through a process-spawning or indirect call, so one with none of
    those (``print('... --no-verify')``) is allowed. Shell bodies run git
    directly and are always judged.
    """
    words = _program_words(program)
    if not any(os.path.basename(tok) == "git" for tok in words):
        return None
    if interpreted and not _PROGRAM_CAN_SPAWN.search(program):
        return None
    if any(tok.upper().startswith("HUSKY") for tok in words):
        return "inline program sets HUSKY around git"
    for i, tok in enumerate(words):
        if os.path.basename(tok) == "git":
            hit = _git_violation(words[i + 1:])
            if hit:
                return hit
    return None


def _heredoc_violation(command: str, depth: int = 0) -> str | None:
    for match in _HEREDOC_START.finditer(command):
        delimiter = re.escape(match.group("delimiter"))
        end = re.search(rf"(?m)^[ \t]*{delimiter}[ \t]*$", command[match.end():])
        if end is None:
            continue
        body = command[match.end():match.end() + end.start()]
        program = match.group("program")
        if program in _SHELLS:
            if find_bypass(body, depth + 1) or _inline_program_violation(body, False):
                return f"{program} heredoc program skipping git hooks"
        elif _inline_program_violation(body):
            return f"{program} heredoc program skipping git hooks"
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
    heredoc_hit = _heredoc_violation(command, depth)
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
        cmd, label, nested = _strip_leading(cmd)
        if label:
            return label
        if nested is not None:
            hit = find_bypass(nested, depth + 1)
            if hit:
                return f"env -S: {hit}"
            continue
        if not cmd:
            continue
        base = os.path.basename(cmd[0])
        if _has_shell_expansion(cmd[0]):
            # The program name is assembled by the shell ($GIT, `which git`):
            # judge its arguments as git's. Only a specific bypass counts; the
            # generic "assembled by expansion" verdict would refuse ordinary
            # `$PYTHON script "$ARG"` calls.
            if any(_NO_VERIFY.match(t) or _sensitive_config_key(t.split("=", 1)[0])
                   for t in cmd[1:]):
                return "expanded command name with a hook-bypass argument"
            as_git = _git_violation(cmd[1:])
            if as_git and "expansion" not in as_git:
                return f"expanded command name: {as_git}"
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
