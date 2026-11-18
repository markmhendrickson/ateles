#!/usr/bin/env python3
"""PreToolUse hook — the release prepare agent may run only allowlisted git.

The rule is "never --no-verify" (CLAUDE.md, Standing constraints). When tests
must be skipped, the sanctioned form is the hook's own escape hatch,
``SKIP_TESTS=1 SKIP_TESTS_REASON="<why>" git commit ...``, which still runs the
type-check, lint and format stages and records the reason.

Why it is mechanical: the Phoenicurus release-prepare agent (a headless
``claude --print`` run) used ``--no-verify`` on at least six release runs,
including v0.29.2 on 2026-11-15, although both its repository's rules and the
operator's rules forbid it. A brief alone did not hold, so the prepare runner
loads this hook into the agent through ``--settings``
(``execution/daemons/phoenicurus-release/prepare.py``). It is not wired into
interactive sessions.

Why an allowlist: the first version was a denylist over shell syntax, and
three review rounds each found another way past it (shell-assembled options,
``env -S``, ``GIT_CONFIG_*`` variables, unsetting ``core.hooksPath``, git
aliases, a ``bash -c`` body behind another bash option). A denylist over shell
syntax cannot be completed. This hook only serves one agent, so it allows
exactly the git that agent needs and refuses everything else.

The allowlisted surface (``ALLOWED_SUBCOMMANDS``) comes from the agent's brief
(``_build_agent_prompt`` in prepare.py), the Neotoma ``/release`` skill's
prepare phase, and the git commands the agent actually ran in its recorded
release-prep sessions: status, diff, log, show, rev-parse, rev-list, fetch,
add, commit, push, checkout, switch, merge, branch, worktree, ls-remote,
merge-base, describe, cat-file, for-each-ref, reflog, fsck, plus read-only
``git config`` and listing-only ``git tag`` (the agent must not tag). A few
read-only inspections and release-file edits that run no hook are added
(ls-files, ls-tree, show-ref, symbolic-ref, remote, blame, shortlog,
name-rev, check-ignore, restore, rm, mv).

The rule. The command is lexed as shell (quotes, escapes, ``$'...'``,
expansions, substitutions, heredocs, comments) and split into simple commands
on ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines. Every git invocation must:
  - be a plain ``git`` as the command itself: no path-qualified or dashed
    binary, no executable name the shell assembles (variable, substitution,
    brace expansion, glob or concatenated quotes), and no other command
    carrying git as an argument (``env``,
    ``command``, ``exec``, ``xargs``, ``nice``, ``timeout``, ``setsid``,
    ``find -exec`` and any wrapper not named here); commands that only print,
    search or locate the word are exempt
  - use only allowlisted global options; ``-C <path>`` may take a literal or
    quoted expanded path; ``-c``, ``--config-env``, ``--exec-path``, ``--git-dir``,
    ``--work-tree`` and every other global option are refused
  - name an allowlisted subcommand (so an alias, ``git stash``, ``git hook``
    and ``git rebase`` are refused)
  - carry no hook-skipping option: ``--no-verify`` (and the abbreviations git
    accepts) or ``--no-post-rewrite`` on any subcommand, ``-n`` alone or in a
    short-flag cluster on commit; nor an option that makes git run another
    program (``--upload-pack``, ``--receive-pack``, ``--exec``,
    ``ls-remote -u``); no argument may be a brace expansion, and on commit,
    push and merge no argument may start with an expansion (it could expand
    to an option), except a quoted expanded value of ``-m``/``-F`` or ``-C``;
    unquoted expansions in git arguments are refused because they can split
  - not write config: ``git config`` is allowed only as a read (``--get``,
    ``--get-all``, ``--get-regexp``, ``--list``, ``get``, ``list``, or one key
    with ``--show-origin`` and similar modifiers)
  - carry no ``GIT_*`` or ``HUSKY`` assignment, and the command may not
    assign, append to or export one anywhere (prefix, standalone, ``+=``,
    ``export``, ``declare``, ``env``)
A git invocation the lexer cannot read fully (unbalanced quoting, an
unterminated substitution or heredoc) is refused, as is a command or process
substitution, an inline interpreter program (``python -c``, ``perl -e``,
``ruby -e``, ``node -e``, ``sh|bash|zsh -c``, ``eval``, ``source``) or a
heredoc or here-string feeding a shell, whose body is judged by this guard.
Tree writes into protected directories or their ancestors are refused.
Commands with literal executables that neither invoke git nor write protected
paths are allowed;
text that merely mentions a flag (a
commit message, PR body, grep pattern, a heredoc written to a file) is data.

This is a tripwire against accidental bypass, not a sandbox. A program created
and run in one command is refused when its bytes cannot be inspected. An alias
defined outside the command can still reach git unseen. The real controls are the prepare child's
allowlisted environment and the release checks that run after the agent.

There is no override. Skipping hooks has no approved form: the approved way to
skip tests is SKIP_TESTS, which this hook does not touch.

Fail-closed: malformed hook input, missing command data or an internal error
denies with exit 2. Tools outside the prepare agent's guarded matcher pass.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

# --- the allowlisted surface ------------------------------------------------

# git subcommands the release prepare agent legitimately runs. Source: the
# agent brief in prepare.py, the Neotoma /release skill's prepare phase, and
# the agent's recorded release-prep sessions.
ALLOWED_SUBCOMMANDS = frozenset({
    # what the agent ran in its recorded sessions
    "status", "diff", "log", "show", "rev-parse", "rev-list", "fetch", "add",
    "commit", "push", "checkout", "switch", "merge", "branch", "worktree",
    "tag", "config", "ls-remote", "merge-base", "describe", "cat-file",
    "for-each-ref", "reflog", "fsck",
    # read-only inspection and release-file edits a prep plausibly needs; none
    # runs or skips a hook
    "ls-files", "ls-tree", "show-ref", "symbolic-ref", "remote", "blame",
    "shortlog", "name-rev", "check-ignore", "restore", "rm", "mv",
})
# Global options that neither relocate git nor change its configuration.
# `-C <path>` is handled separately because it takes a value.
ALLOWED_GLOBAL_FLAGS = frozenset({
    "--no-pager", "-P", "-p", "--paginate", "--no-optional-locks",
    "--no-advice", "--literal-pathspecs", "--version", "--help",
})
# Long options that skip hooks, with the shortest abbreviation refused (git
# accepts unambiguous prefixes; shorter ones are ambiguous and git rejects
# them anyway, so refusing them costs nothing).
_FORBIDDEN_LONG = (("--no-verify", len("--no-v")),
                   ("--no-post-rewrite", len("--no-po")),
                   # these make fetch, push or ls-remote run any program,
                   # which could run git again unjudged
                   ("--upload-pack", len("--upl")),
                   ("--receive-pack", len("--rece")),
                   ("--exec", len("--exe")))
# Subcommands with a --no-verify-style option: no option may be assembled by
# the shell there, because the assembled text could be that option.
_HOOK_SUBCOMMANDS = frozenset({"commit", "push", "merge"})

# `git commit` options whose value is the next token (or the rest of a short
# cluster); the value is data, so it may be quoted or expanded.
_COMMIT_SHORT_VALUE = set("mFCct")
_COMMIT_SHORT_OPTIONAL_VALUE = set("uS")
_COMMIT_LONG_VALUE = frozenset({
    "--message", "--file", "--reuse-message", "--reedit-message", "--template",
    "--author", "--date", "--cleanup", "--fixup", "--squash", "--trailer",
    "--pathspec-from-file",
})

# `git config` read actions and the modifiers allowed with them.
_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l"})
_CONFIG_READ_ACTIONS = frozenset({"get", "list"})
_CONFIG_MODIFIERS = frozenset({
    "--global", "--system", "--local", "--worktree", "--show-origin",
    "--show-scope", "--null", "-z", "--name-only", "--includes",
    "--no-includes", "--all", "--regexp", "--fixed-value", "--bool", "--int",
    "--bool-or-int", "--path", "--expiry-date", "--no-type",
})
_CONFIG_VALUE_MODIFIERS = frozenset({"-f", "--file", "--blob", "--type", "--default",
                                     "--value", "--url"})

# `git tag` is listing only: the agent must not tag.
_TAG_LIST_FLAGS = frozenset({"-l", "--list", "--column", "--no-column", "-i",
                             "--ignore-case", "--omit-empty"})
_TAG_LIST_VALUE = frozenset({"--sort", "--format", "--contains", "--no-contains",
                             "--merged", "--no-merged", "--points-at", "--color"})

# --- shell vocabulary -------------------------------------------------------

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_SENSITIVE_NAME = re.compile(r"^(?:GIT_[A-Za-z0-9_]*|HUSKY[A-Za-z0-9_]*)$")
_GIT_LOCATION_NAME = frozenset({"HOME", "XDG_CONFIG_HOME"})
_LOADER_NAME = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "GIT_EXEC_PATH", "GIT_SSH",
    "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EXTERNAL_DIFF",
    "GIT_PAGER", "PAGER", "EDITOR", "VISUAL", "GIT_EDITOR", "SHELL",
    "BASH_ENV", "ENV", "IFS", "PYTHONPATH", "PYTHONSTARTUP",
    "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "RUBYLIB",
    "PERL5OPT", "PERL5LIB", "HOME", "XDG_CONFIG_HOME",
})


def _protected_environment_name(name: str) -> bool:
    return name in _LOADER_NAME or name.startswith("DYLD_") or bool(_SENSITIVE_NAME.match(name))
# A word that names git: `git`, a path to it, a dashed builtin (git-commit), a
# remote spec (git@host). Not `github`, `.gitignore` or `digit`.
# A dashed word counts only when it is a git command (git-commit), so a path
# such as scripts/git-hooks/install.sh does not.
_GIT_DASHED = ("commit|push|merge|config|rebase|am|pull|fetch|tag|stash|hook|"
               "cherry-pick|reset|add|checkout|switch|worktree|receive-pack|"
               "upload-pack|sh")
_MENTIONS_GIT = re.compile(
    rf"(?<![A-Za-z0-9_.-])git(?:-(?:{_GIT_DASHED}))?(?![A-Za-z0-9_.-])")
_RESERVED_START = frozenset({"if", "then", "else", "elif", "do", "while", "until",
                             "!", "{", "}", "fi", "done", "esac"})
_EXPORTERS = frozenset({"export", "declare", "typeset", "local", "readonly"})
# Commands whose arguments are themselves a command.
# Each entry is (flags, options with a separate or attached value, leading
# positional operands, environment assignments, opaque command options).
# An unlisted option is indeterminate, so it cannot locate an executable.
_WRAPPER_GRAMMAR = {
    "env": ({"-i", "-0", "-v", "--ignore-environment", "--null", "--debug"},
            {"-u", "--unset", "-C", "--chdir"}, 0, True, {"-S", "--split-string"}),
    "command": ({"-p", "-v", "-V"}, set(), 0, False, set()),
    "exec": ({"-c", "-l"}, {"-a"}, 0, False, set()),
    "builtin": (set(), set(), 0, False, set()),
    "xargs": ({"-0", "-r", "-t", "-p", "-x", "--null", "--no-run-if-empty",
               "--verbose", "--interactive", "--exit"},
              {"-a", "-d", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "-P", "-s",
               "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
               "--max-args", "--max-procs", "--max-chars"}, 0, False, set()),
    "nice": (set(), {"-n", "--adjustment"}, 0, False, set()),
    "timeout": ({"--foreground", "--preserve-status", "-v", "--verbose"},
                {"-s", "--signal", "-k", "--kill-after"}, 1, False, set()),
    "gtimeout": ({"--foreground", "--preserve-status", "-v", "--verbose"},
                 {"-s", "--signal", "-k", "--kill-after"}, 1, False, set()),
    "nohup": (set(), set(), 0, False, set()),
    "time": ({"-p", "-l"}, {"-f", "--format", "-o", "--output"}, 0, False, set()),
    "sudo": ({"-A", "-b", "-E", "-H", "-K", "-k", "-n", "-S", "-v", "-V", "-l",
              "--non-interactive", "--preserve-env"},
             {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
              "-C", "--close-from", "-D", "--chdir", "-R", "--chroot", "-t", "--type"},
             0, False, set()),
    "doas": ({"-n", "-s"}, {"-u"}, 0, False, set()),
    "stdbuf": (set(), {"-i", "-o", "-e"}, 0, False, set()),
    "caffeinate": ({"-d", "-i", "-m", "-s", "-u"}, {"-t", "-w"}, 0, False, set()),
    "unbuffer": ({"-p"}, set(), 0, False, set()),
    "watch": ({"-b", "-c", "-d", "-e", "-g", "-t", "-x"},
              {"-n", "--interval"}, 0, False, set()),
    "parallel": ({"-k", "--keep-order", "--halt-on-error"},
                 {"-j", "--jobs", "-N", "--max-args", "-S", "--sshlogin",
                  "--results", "--joblog"},
                 0, False, set()),
    "script": ({"-q", "-f", "-e"}, {"-a", "-o", "-O", "-I", "-t"},
               1, False, {"-c", "--command"}),
    "flock": ({"-s", "-x", "-u", "-n", "-o", "--shared", "--exclusive",
               "--unlock", "--nonblock", "--close"},
              {"-w", "--timeout", "-E", "--conflict-exit-code"}, 1, False, set()),
    "setsid": ({"-c", "-f", "-w", "--ctty", "--fork", "--wait"},
               set(), 0, False, set()),
    "ionice": ({"-t", "--ignore"}, {"-c", "--class", "-n", "--classdata"},
               0, False, set()),
    "chroot": ({"--skip-chdir"}, {"--userspec", "--groups"}, 1, False, set()),
}
_WRAPPERS = frozenset(_WRAPPER_GRAMMAR)
# These runners add authority or interpret an opaque command, and release
# preparation has no documented use for them. Keep their grammar entries so
# an attempted invocation is recognized and refused before generic handling.
_DENIED_WRAPPERS = frozenset({"script", "parallel", "watch", "chroot", "doas", "sudo"})
_DENIED_DIRECTORY_PROGRAMS = frozenset({
    "pushd", "popd", "tmux", "screen", "nsenter",
    "runuser", "su",
})
# Every wrapper has an explicit write-operand decision. Values name options
# whose operand is opened for writing; True means a leading positional operand
# is created or modified. All other declared operands are read-only or data.
_WRAPPER_OPERAND_POLICY = {
    # Each tuple is (writable option operands, writable leading positional,
    # unrecognized option or operand policy). Entries are deliberately explicit
    # so a new grammar entry needs an audited write decision.
    "env": (frozenset(), False, "deny"),
    "command": (frozenset(), False, "deny"),
    "exec": (frozenset(), False, "deny"),
    "builtin": (frozenset(), False, "deny"),
    "xargs": (frozenset(), False, "deny"),
    "nice": (frozenset(), False, "deny"),
    "timeout": (frozenset(), False, "deny"),
    "gtimeout": (frozenset(), False, "deny"),
    "nohup": (frozenset(), False, "deny"),
    "time": (frozenset({"-o", "--output"}), False, "deny"),
    "sudo": (frozenset(), False, "deny"),
    "doas": (frozenset(), False, "deny"),
    "stdbuf": (frozenset(), False, "deny"),
    "caffeinate": (frozenset(), False, "deny"),
    "unbuffer": (frozenset(), False, "deny"),
    "watch": (frozenset(), False, "deny"),
    "parallel": (frozenset({"--results", "--joblog"}), False, "deny"),
    "script": (frozenset({"-o", "-O", "-I"}), True, "deny"),
    "flock": (frozenset(), True, "deny"),
    "setsid": (frozenset(), False, "deny"),
    "ionice": (frozenset(), False, "deny"),
    "chroot": (frozenset(), False, "deny"),
}
_SHELLS = re.compile(r"^(?:sh|bash|zsh|dash|ksh|mksh|fish|tcsh|csh|busybox)$")
_INTERPRETERS = re.compile(
    r"^(?:python|pypy|node|nodejs|deno|bun|perl|ruby|php|osascript|lua|tclsh|"
    r"awk|gawk|mawk|nawk|sed)(?:-?[0-9]+(?:\.[0-9]+)*)?$"
)


def _interpreter_family(runner: str) -> str:
    """Normalize a versioned executable before applying its operand rules."""
    name = os.path.basename(runner)
    if not _INTERPRETERS.fullmatch(name):
        return name
    base = re.sub(r"-?[0-9]+(?:\.[0-9]+)*$", "", name)
    if base in {"pypy", "python"}:
        return "python"
    if base in {"nodejs", "node", "bun", "deno"}:
        return "node"
    if base in {"gawk", "mawk", "nawk", "awk"}:
        return "awk"
    return base
_EVALUATORS = frozenset({"eval", "source", "."})
# Interpreter options whose value is the next token, so it is not a script.
_INTERPRETER_VALUE_OPTIONS = frozenset({"-o", "-O", "--rcfile", "--init-file", "-W", "-X",
                                        "-r", "--require", "-I", "-f"})
_INTERPRETER_PROGRAM_OPTIONS = frozenset({"-c", "-e", "-E", "-p", "--eval", "--print",
                                          "-m", "--command"})


class Unreadable(Exception):
    """The lexer could not read the command fully."""


@dataclass
class Word:
    text: str = ""
    expanded: bool = False        # contains a parameter, command or arithmetic expansion
    unquoted_expansion: bool = False  # shell field splitting can add arguments
    lead_expansion: bool = False  # the expansion is at the very start
    unquoted: str = ""            # the unquoted characters, for brace and glob checks
    write_glob: bool = False  # glob met outside single quotes, even within double quotes
    quoted: bool = False
    concatenated: bool = False  # adjacent quoted and unquoted executable fragments

    @property
    def braced(self) -> bool:
        """Contains a brace expansion the shell will turn into several words."""
        return bool(re.search(r"\{[^{}]*(?:,|\.\.)[^{}]*\}", self.unquoted))

    @property
    def globbed(self) -> bool:
        """Contains a pathname pattern (`*`, `?`, or a closed `[...]`; a bare
        `[` is the test command)."""
        return bool(re.search(r"[*?]|\[[^\]]+\]", self.unquoted))


@dataclass
class SimpleCommand:
    words: list[Word] = field(default_factory=list)
    stdin: list[str] = field(default_factory=list)  # heredoc bodies, here-strings
    redirects: list[tuple[str, Word]] = field(default_factory=list)
    pipe_in: bool = False
    stdin_dynamic: bool = False


# --- lexer ------------------------------------------------------------------

_OPERATOR_CHARS = set(";&|()<>")
_BLANKS = set(" \t")


def _skip_single(s: str, i: int) -> int:
    end = s.find("'", i)
    if end < 0:
        raise Unreadable("unterminated single quote")
    return end + 1


def _skip_double(s: str, i: int) -> int:
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise Unreadable("unterminated double quote")


def _skip_heredoc_bodies(s: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """i is just after a newline; skip each pending heredoc body in turn."""
    for delimiter, strip_tabs in pending:
        while True:
            if i >= len(s):
                raise Unreadable("unterminated heredoc")
            end = s.find("\n", i)
            line = s[i:] if end < 0 else s[i:end]
            i = len(s) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return i


def _heredoc_delimiter(s: str, i: int) -> tuple[str, bool, int]:
    """i is at `<<` (not `<<<`); return (delimiter, strip_tabs, next index)."""
    j = i + 2
    strip_tabs = s[j:j + 1] == "-"
    if strip_tabs:
        j += 1
    while j < len(s) and s[j] in _BLANKS:
        j += 1
    delimiter = ""
    while j < len(s) and s[j] not in _BLANKS and s[j] != "\n" and s[j] not in _OPERATOR_CHARS:
        if s[j] in "'\"":
            end = s.find(s[j], j + 1)
            if end < 0:
                raise Unreadable("unterminated heredoc delimiter")
            delimiter += s[j + 1:end]
            j = end + 1
            continue
        if s[j] == "\\":
            j += 1
            if j >= len(s):
                break
        delimiter += s[j]
        j += 1
    if not delimiter:
        raise Unreadable("heredoc without a delimiter")
    return delimiter, strip_tabs, j


def _read_parens(s: str, i: int) -> int:
    """i is just after `$(` or `<(`; return the index after the matching `)`.

    Heredoc bodies inside the substitution (the common
    `git commit -m "$(cat <<'EOF' ... EOF)"` form) are skipped as data, so an
    apostrophe or parenthesis in a commit message does not unbalance it.
    """
    depth = 1
    pending: list[tuple[str, bool]] = []
    while i < len(s):
        c = s[i]
        if c == "\n" and pending:
            i = _skip_heredoc_bodies(s, i + 1, pending)
            pending = []
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'":
            i = _skip_single(s, i + 1)
            continue
        if c == '"':
            i = _skip_double(s, i + 1)
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;(|&"):
            end = s.find("\n", i)
            i = len(s) if end < 0 else end
            continue
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            delimiter, strip_tabs, i = _heredoc_delimiter(s, i)
            pending.append((delimiter, strip_tabs))
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Unreadable("unterminated substitution")


def _read_backtick(s: str, i: int) -> int:
    """i is just after the opening backtick."""
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i + 1
        i += 1
    raise Unreadable("unterminated backtick")


def _read_braces(s: str, i: int) -> int:
    """i is just after `${`."""
    depth = 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Unreadable("unterminated ${")


def _decode_ansi_c(body: str) -> str:
    try:
        return body.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except Exception:  # noqa: BLE001 — undecodable escapes stay literal
        return body


class _Lexer:
    def __init__(self, s: str) -> None:
        self.s = s
        self.subs: list[str] = []

    def _expansion(self, word: Word, i: int, *, quoted: bool = False) -> int:
        """s[i] == '$' (unquoted or in double quotes). Returns the next index."""
        s = self.s
        nxt = s[i + 1] if i + 1 < len(s) else ""
        start = i
        if nxt == "(":
            end = _read_parens(s, i + 2)
            self.subs.append(s[i + 2:end - 1])
        elif nxt == "{":
            end = _read_braces(s, i + 2)
            self.subs.append(s[i + 2:end - 1])
        elif nxt and (nxt.isalnum() or nxt == "_"):
            end = i + 1
            while end < len(s) and (s[end].isalnum() or s[end] == "_"):
                end += 1
        elif nxt and nxt in "@*#?$!-":
            end = i + 2
        else:
            word.text += "$"
            return i + 1
        if not word.text:
            word.lead_expansion = True
        word.expanded = True
        word.unquoted_expansion |= not quoted
        word.text += s[start:end]
        return end

    def _backtick(self, word: Word, i: int, *, quoted: bool = False) -> int:
        end = _read_backtick(self.s, i + 1)
        self.subs.append(self.s[i + 1:end - 1])
        if not word.text:
            word.lead_expansion = True
        word.expanded = True
        word.unquoted_expansion |= not quoted
        word.text += self.s[i:end]
        return end

    def read_word(self, i: int) -> tuple[Word | None, int]:
        s = self.s
        word = Word()
        started = False
        while i < len(s):
            c = s[i]
            if c in _BLANKS or c == "\n" or c in _OPERATOR_CHARS:
                break
            started = True
            if c == "\\":
                if i + 1 < len(s) and s[i + 1] == "\n":
                    i += 2
                    continue
                word.text += s[i + 1] if i + 1 < len(s) else ""
                i += 2
            elif c == "'":
                word.concatenated |= bool(word.text) or word.quoted
                word.quoted = True
                end = _skip_single(s, i + 1)
                word.text += s[i + 1:end - 1]
                i = end
            elif c == "$" and i + 1 < len(s) and s[i + 1] == "'":
                word.concatenated |= bool(word.text) or word.quoted
                word.quoted = True
                j = i + 2
                while j < len(s) and s[j] != "'":
                    j += 2 if s[j] == "\\" else 1
                if j >= len(s):
                    raise Unreadable("unterminated $'")
                word.text += _decode_ansi_c(s[i + 2:j])
                i = j + 1
            elif c == '"':
                word.concatenated |= bool(word.text) or word.quoted
                word.quoted = True
                i += 1
                while True:
                    if i >= len(s):
                        raise Unreadable("unterminated double quote")
                    d = s[i]
                    if d == '"':
                        i += 1
                        break
                    if d == "\\" and i + 1 < len(s) and s[i + 1] in '$`"\\\n':
                        if s[i + 1] != "\n":
                            word.text += s[i + 1]
                        i += 2
                    elif d == "$":
                        i = self._expansion(word, i, quoted=True)
                    elif d == "`":
                        i = self._backtick(word, i, quoted=True)
                    else:
                        word.text += d
                        if d in "*?[":
                            word.write_glob = True
                        i += 1
            elif c == "$":
                i = self._expansion(word, i)
            elif c == "`":
                i = self._backtick(word, i)
            else:
                word.concatenated |= word.quoted
                word.text += c
                word.unquoted += c
                if c in "*?[":
                    word.write_glob = True
                i += 1
        return (word if started else None), i

    def lex(self) -> list[SimpleCommand]:
        s = self.s
        commands: list[SimpleCommand] = []
        cur = SimpleCommand()
        pending: list[tuple[str, bool, SimpleCommand]] = []
        pipe_next = False

        def finish(pipe: bool = False) -> None:
            nonlocal cur, pipe_next
            if cur.words or cur.stdin or cur.redirects:
                commands.append(cur)
            cur = SimpleCommand(pipe_in=pipe)
            pipe_next = pipe

        i = 0
        while i < len(s):
            c = s[i]
            if c in _BLANKS:
                i += 1
                continue
            if c == "\\" and i + 1 < len(s) and s[i + 1] == "\n":
                i += 2
                continue
            if c == "\n":
                finish()
                i += 1
                for delimiter, strip_tabs, target in pending:
                    start = i
                    i = _skip_heredoc_bodies(s, i, [(delimiter, strip_tabs)])
                    body = s[start:i].split("\n")
                    target.stdin.append("\n".join(body[:-2] if body[-1] == "" else body[:-1]))
                pending = []
                continue
            if c == "#" :
                end = s.find("\n", i)
                i = len(s) if end < 0 else end
                continue
            if c in "<>" or (c == "&" and s[i + 1:i + 2] == ">"):
                j = i
                while j < len(s) and s[j] in "<>&|-":
                    if s[j] == "-" and j > i and s[j - 1] != "<":
                        break
                    j += 1
                op = s[i:j]
                if j < len(s) and s[j] == "(" and op in ("<", ">"):
                    end = _read_parens(s, j + 1)
                    self.subs.append(s[j + 1:end - 1])
                    cur.words.append(Word(s[i:end], True, True))
                    i = end
                    continue
                i = j
                while i < len(s) and s[i] in _BLANKS:
                    i += 1
                target, i = self.read_word(i)
                if target is None:
                    raise Unreadable(f"redirection {op} without a target")
                if op in ("<<", "<<-"):
                    pending.append((target.text, op == "<<-", cur))
                    cur.stdin_dynamic |= not target.quoted
                elif op == "<<<":
                    cur.stdin.append(target.text)
                    cur.stdin_dynamic |= target.expanded
                else:
                    cur.redirects.append((op, target))
                continue
            if c in ";&|()":
                j = i + 1
                if c in "&|;" and s[j:j + 1] in ("&", "|", ";"):
                    j += 1
                op = s[i:j]
                finish(pipe=op in ("|", "|&"))
                i = j
                continue
            word, i = self.read_word(i)
            if word is None:  # pragma: no cover — read_word always advances here
                raise Unreadable("lexer stalled")
            # An fd number directly before a redirection (`2>&1`) is not a word.
            if word.text.isdigit() and not word.expanded and s[i:i + 1] in ("<", ">"):
                continue
            cur.words.append(word)
        if pending:
            raise Unreadable("heredoc with no body")
        finish()
        return commands


# --- judgement --------------------------------------------------------------

def mentions_git(text: str) -> bool:
    return bool(_MENTIONS_GIT.search(text))


def _forbidden_long(text: str) -> str | None:
    if not text.startswith("--"):
        return None
    name = text.split("=", 1)[0]
    for option, shortest in _FORBIDDEN_LONG:
        if len(name) >= shortest and option.startswith(name):
            return option
    return None


def _commit_violation(rest: list[Word]) -> str | None:
    j = 0
    while j < len(rest):
        w = rest[j]
        t = w.text
        if t == "--" and not w.expanded:
            return None
        if t.startswith("--"):
            name, has_value, _ = t.partition("=")
            if name in _COMMIT_LONG_VALUE:
                j += 1 if has_value else 2
                continue
            if w.expanded:
                return "git commit with an option assembled by the shell"
            j += 1
            continue
        if t.startswith("-") and len(t) > 1:
            consumed_next = False
            for k, ch in enumerate(t[1:]):
                if ch in "$`":
                    return "git commit with an option assembled by the shell"
                if ch == "n":
                    return "git commit -n"
                if ch in _COMMIT_SHORT_VALUE:
                    consumed_next = k + 2 == len(t)
                    break
                if ch in _COMMIT_SHORT_OPTIONAL_VALUE and k + 2 < len(t):
                    break
            j += 2 if consumed_next else 1
            continue
        if w.lead_expansion:
            return (f"git commit with an argument assembled by the shell "
                    f"(`{w.text}` could expand to an option; write it literally, "
                    f"or pass it as the value of -m or -F)")
        j += 1
    return None


def _config_violation(rest: list[Word]) -> str | None:
    if any(w.expanded for w in rest):
        return "git config with an argument assembled by the shell"
    flags: list[str] = []
    positionals: list[str] = []
    j = 0
    while j < len(rest):
        t = rest[j].text
        if t == "--":
            positionals.extend(w.text for w in rest[j + 1:])
            break
        if t.startswith("-") and len(t) > 1:
            name = t.split("=", 1)[0]
            flags.append(name)
            if name in _CONFIG_VALUE_MODIFIERS and "=" not in t:
                j += 1
            j += 1
            continue
        positionals.append(t)
        j += 1
    unknown = [f for f in flags if f not in _CONFIG_READ_FLAGS
               and f not in _CONFIG_MODIFIERS and f not in _CONFIG_VALUE_MODIFIERS]
    if unknown:
        return f"git config {unknown[0]} (only config reads are allowed)"
    if positionals and positionals[0] in _CONFIG_READ_ACTIONS:
        return None
    if set(flags) & _CONFIG_READ_FLAGS:
        return None
    if len(positionals) == 1:
        return None  # `git config <key>` reads it
    return "git config write (only config reads are allowed)"


def _tag_violation(rest: list[Word]) -> str | None:
    listing = False
    positionals = 0
    j = 0
    while j < len(rest):
        w = rest[j]
        t = w.text
        if w.expanded:
            return "git tag with an argument assembled by the shell"
        if t == "--":
            positionals += len(rest) - j - 1
            break
        name = t.split("=", 1)[0]
        if name in _TAG_LIST_VALUE:
            j += 1 if "=" in t else 2
            continue
        if name in _TAG_LIST_FLAGS or re.fullmatch(r"-n[0-9]*", t):
            listing = listing or name in ("-l", "--list")
            j += 1
            continue
        if t.startswith("-"):
            return f"git tag {t} (the prepare agent lists tags only)"
        positionals += 1
        j += 1
    if positionals and not listing:
        return "git tag creating a tag (the prepare agent lists tags only)"
    return None


def git_violation(args: list[Word]) -> str | None:
    """Judge the arguments of a plain `git` invocation against the allowlist."""
    i = 0
    while i < len(args):
        w = args[i]
        if w.text == "-C" and not w.expanded:
            if i + 1 >= len(args):
                return "git -C without a path"
            if args[i + 1].unquoted_expansion:
                return "git -C path with an unquoted expansion"
            i += 2
            continue
        if w.expanded or w.braced:
            return "git global option or subcommand assembled by the shell"
        if w.text in ALLOWED_GLOBAL_FLAGS:
            i += 1
            continue
        if w.text.startswith("-"):
            return f"git {w.text} (global option not on the allowlist)"
        break
    if i >= len(args):
        return None  # bare `git`, `git --version`
    sub = args[i].text
    if sub not in ALLOWED_SUBCOMMANDS:
        return (f"git {sub} (not on the prepare agent's allowlist; allowed: "
                f"{', '.join(sorted(ALLOWED_SUBCOMMANDS))})")
    rest = args[i + 1:]
    for w in rest:
        if w.unquoted_expansion:
            return f"git {sub} with an unquoted expansion"
        if w.text == "--" and not w.expanded:
            break
        if w.braced:
            return f"git {sub} with a brace expansion (write each argument literally)"
        if sub in _HOOK_SUBCOMMANDS and w.globbed:
            return f"git {sub} with a glob argument (write each argument literally)"
        option = _forbidden_long(w.text)
        if option:
            return f"git {sub} {option}"
    if sub == "commit":
        return _commit_violation(rest)
    if sub == "config":
        return _config_violation(rest)
    if sub == "tag":
        return _tag_violation(rest)
    if sub == "ls-remote" and any(w.text == "-u" for w in rest):
        return "git ls-remote -u (runs an arbitrary upload-pack program)"
    if sub in _HOOK_SUBCOMMANDS:
        for w in rest:
            if w.text == "--" and not w.expanded:
                break
            if w.lead_expansion or (w.expanded and w.text.startswith("-")):
                return (f"git {sub} with an argument assembled by the shell "
                        f"(`{w.text}` could expand to an option; write it literally)")
    return None


def _words_mention_git(words: list[Word], extra: tuple[str, ...] | list[str] = ()) -> bool:
    """Whether any word names git, including one glued to a short option
    (``env -S'git ...'`` lexes as ``-Sgit ...``)."""
    for t in [w.text for w in words] + list(extra):
        if mentions_git(t):
            return True
        if t.startswith("-") and not t.startswith("--") and mentions_git(t[2:]):
            return True
    return False


_OTHER_GIT_SUBCOMMANDS = frozenset({"stash", "hook", "rebase", "am", "pull",
                                    "cherry-pick", "reset", "clean"})


def _looks_like_git(head: Word, args: list[Word]) -> bool:
    """For a command name the shell assembles: could it be git?

    True when the variable is named for git (``$GIT``, ``${git_bin}``), when
    the first argument is a git subcommand, or when the arguments would skip
    hooks if git received them.
    """
    if re.search(r"git", head.text, re.IGNORECASE):
        return True
    if args and not args[0].expanded and (
            args[0].text in ALLOWED_SUBCOMMANDS or args[0].text in _OTHER_GIT_SUBCOMMANDS):
        return True
    texts = [w.text for w in args]
    if any(_forbidden_long(t) for t in texts):
        return True
    for k, t in enumerate(texts):
        if t in ("-c", "--config-env") and k + 1 < len(texts) and re.match(
                r"^[A-Za-z][\w-]*\.[^=\s]+=?", texts[k + 1]):
            return True
        if t == "commit":
            return _commit_violation(args[k + 1:]) is not None
    return False


def _reads_program_from_stdin(rest: list[Word]) -> bool:
    """An interpreter with no program option and no script operand reads stdin."""
    j = 1
    while j < len(rest):
        t = rest[j].text
        if t in _INTERPRETER_PROGRAM_OPTIONS:
            return False
        if t in {"-f", "--file", "-i", "--include", "-l", "--load", "--source"}:
            return False
        if t in ("-", "-s", "--"):
            return True
        if t in _INTERPRETER_VALUE_OPTIONS:
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        return False  # a script operand
    return True


def _program_word_expanded(rest: list[Word], is_shell: bool) -> bool:
    """Whether the inline program (the word after -c/-e, or after a shell's
    short cluster containing c) is assembled by the shell. An expanded
    argument to a script file is data for that script, not a program."""
    for k, w in enumerate(rest[1:], start=1):
        t = w.text
        is_program_option = t in _INTERPRETER_PROGRAM_OPTIONS or (
            is_shell and t.startswith("-") and not t.startswith("--") and "c" in t[1:])
        if is_program_option:
            return k + 1 < len(rest) and rest[k + 1].expanded
        if t in _INTERPRETER_VALUE_OPTIONS:
            continue
        if not t.startswith("-"):
            return False  # a script operand
    return False


def _assignment_violation(words: list[Word], *, command_mentions_git: bool = False) -> str | None:
    for w in words:
        if _ASSIGNMENT.match(w.text):
            name = w.text.split("=", 1)[0].rstrip("+")
            if _protected_environment_name(name):
                return f"{name}=... (program lookup or git environment may not be changed)"
            if command_mentions_git and name in _GIT_LOCATION_NAME:
                return f"{name}=... (git config location may not be changed)"
    return None


# Commands that take `git` as data (print it, search for it, look it up).
_GIT_WORD_READERS = frozenset({"echo", "printf", "which", "type", "whereis", "grep",
                               "rg", "egrep", "fgrep", "man", "gh", "brew", "test",
                               "[", "[[", "cat", "ls", "head", "tail", "wc", "sed"})

_EFFECTIVE_CWD: Path | None = None


def _effective_path(path: str) -> Path:
    expanded = Path(os.path.expanduser(path))
    return expanded if expanded.is_absolute() else (_EFFECTIVE_CWD or Path.cwd()) / expanded


def _path_forms(path: str) -> tuple[tuple[str, ...], bool]:
    """Check both spelling and destination; resolve existing symlink components."""
    expanded = str(_effective_path(path))
    literal = os.path.normpath(expanded).casefold()
    try:
        # strict=False may retain a loop in the spelling instead of raising.
        # Resolve the last existing component strictly before allowing a new
        # file beneath it.
        existing = Path(expanded)
        while not os.path.lexists(existing):
            parent = existing.parent
            if parent == existing:
                break
            existing = parent
        existing.resolve(strict=True)
        resolved = str(Path(expanded).resolve(strict=False)).casefold()
    except (OSError, RuntimeError):
        # A loop or unreadable component beneath a protected directory cannot
        # establish a safe destination for a write.
        return (literal,), True
    return (literal, resolved), False


def _matches_protected_path(normalized: str) -> bool:
    return bool(
        re.search(r"(?:^|/)\.git/config$", normalized)
        or re.search(r"(?:^|/)\.git/hooks(?:/|$)", normalized)
        or re.search(r"(?:^|/)\.git/worktrees/[^/]+/config(?:\.worktree)?$", normalized)
        or re.search(r"(?:^|/)\.git/config\.worktree$", normalized)
        or re.search(r"(?:^|/)\.gitconfig$", normalized)
        or re.search(r"(?:^|/)\.config/(?:git|husky)(?:/|$)", normalized)
        or re.search(r"(?:^|/)\.husky(?:/|$)", normalized)
    )


def _protected_path(path: str) -> bool:
    """Paths that can redirect or disable the prepare checkout's hooks."""
    forms, unresolved = _path_forms(path)
    return any(_matches_protected_path(form) for form in forms) or (
        unresolved and _enters_protected_dir(path)
    )


def _enters_protected_dir(path: str) -> bool:
    """A working directory from which a relative write reaches a protected path."""
    forms, _ = _path_forms(path)
    return any(re.search(r"(?:^|/)(?:\.git|\.husky|\.config)(?:/|$)", form)
               for form in forms)


_DIRECT_WRITERS = frozenset({"tee", "sed", "cp", "mv", "ln", "install", "rsync",
                             "rm", "touch", "truncate", "chmod", "chown", "mkdir", "dd",
                             "tar", "bsdtar", "gtar", "unzip", "ditto", "cpio", "pax", "jar", "7z",
                             "curl", "wget"})

_ARCHIVE_TOOLS = frozenset({"tar", "bsdtar", "gtar", "unzip", "ditto", "cpio", "pax", "jar", "7z"})
_ARCHIVE_UNSAFE_OPTIONS = frozenset({
    "-P", "--absolute-names", "--keep-directory-symlink", "--dereference",
    "--transform", "--strip-components", "-s", "--substitute", "--follow-symlinks",
})


# A tar extraction is judged against an explicit set of options that cannot
# keep absolute member names, run a program, rewrite member names or follow
# links; every other option, in any spelling (old-style bundle without a dash,
# bundled short letters, an abbreviated long name), is refused. Blacklisting
# options missed `tar xPf`, `--abs` and the options that execute a command.
_TAR_SAFE_LONG = frozenset({
    "--extract", "--get", "--file", "--directory", "--verbose", "--gzip", "--gunzip",
    "--ungzip", "--bzip2", "--xz", "--zstd", "--lzma", "--auto-compress", "--keep-old-files",
    "--skip-old-files", "--no-same-owner", "--no-same-permissions", "--touch",
    "--preserve-permissions", "--same-permissions", "--to-stdout", "--numeric-owner",
})
_TAR_SAFE_SHORT = frozenset("xvfzjJCkmpaoO")


def _tar_option_violation(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg == "--":
            return None
        if arg.startswith("--"):
            if arg.split("=", 1)[0] not in _TAR_SAFE_LONG:
                return f"uses an option outside the safe extraction set: {arg.split('=', 1)[0]}"
            continue
        dashed = arg.startswith("-") and len(arg) > 1
        if not dashed and i != 0:
            continue
        for ch in arg[1:] if dashed else arg:
            if ch not in _TAR_SAFE_SHORT:
                return f"uses an option letter outside the safe extraction set: {ch}"
            if dashed and ch in "fC":
                break  # the rest of a dashed bundle is that option's value
    return None


def _scratch_destination(path: str) -> bool:
    """Only an absolute directory inside the host's temporary tree is scratch."""
    if not os.path.isabs(path) or _tree_write_target(path):
        return False
    resolved = Path(path).resolve(strict=False)
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(),
             Path("/private/tmp").resolve()}
    return any(root in resolved.parents for root in roots)


def _archive_violation(base: str, args: list[str], destinations: list[str]) -> str | None:
    if base not in _ARCHIVE_TOOLS or not destinations:
        return None
    if base == "ditto" and not any(arg in {"-x", "--extract"} for arg in args):
        return None
    if base in {"cpio", "pax"}:
        return f"{base} extraction may write absolute member paths"
    if base in {"tar", "gtar", "bsdtar"}:
        reason = _tar_option_violation(args)
        if reason:
            return f"{base} extraction {reason}"
    for i, arg in enumerate(args):
        if arg in _ARCHIVE_UNSAFE_OPTIONS or any(
            arg.startswith(option + "=") for option in _ARCHIVE_UNSAFE_OPTIONS
        ) or (base in {"tar", "gtar", "bsdtar"} and arg.startswith("--transform")):
            return f"{base} extraction option can redirect archive members: {arg}"
        if base in {"tar", "gtar", "bsdtar"} and arg.startswith("-") and not arg.startswith("--") and "P" in arg[1:]:
            return f"{base} extraction preserves absolute archive member names"
    scratch = all(_scratch_destination(dest) for dest in destinations)
    # For non-scratch destinations the archive must be readable and each member
    # confined. Formats without a safe local listing are denied here.
    archive = None
    for i, arg in enumerate(args[:-1]):
        if arg in ("-f", "--file", "-Z", "--archive"):
            archive = args[i + 1]
        elif base in {"tar", "gtar", "bsdtar", "jar"} and re.fullmatch(r"-?[A-Za-z]+", arg) and "f" in arg:
            archive = args[i + 1]
    for arg in args:
        if arg.startswith("--file=") or arg.startswith("--archive="):
            archive = arg.split("=", 1)[1]
        elif base in {"tar", "gtar", "bsdtar"}:
            attached = re.fullmatch(r"-?[A-Za-z]*f(.+)", arg)
            if attached:
                archive = attached.group(1)
    if archive is None and base == "ditto" and len(args) >= 2:
        archive = args[-2]
    if archive is None and base in {"unzip", "7z"}:
        archive = next((a for a in args if not a.startswith("-")), None)
        if base == "7z" and archive in {"x", "e"}:
            archive = next((a for a in args[1:] if not a.startswith("-")), None)
    if not archive or archive == "-":
        return None if scratch else f"{base} archive members cannot be inspected"
    try:
        if base in {"tar", "gtar", "bsdtar"}:
            with tarfile.open(archive) as source:
                members = [(m.name, m.issym() or m.islnk()) for m in source]
        elif base in {"unzip", "jar", "ditto"}:
            with zipfile.ZipFile(archive) as source:
                members = [(m.filename, (m.external_attr >> 16) & 0o170000 == 0o120000)
                           for m in source.infolist()]
        else:
            return None if scratch else f"{base} archive members cannot be inspected"
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return None if scratch else f"{base} archive members cannot be inspected"
    for name, link in members:
        parts = Path(name).parts
        if link or os.path.isabs(name) or ".." in parts or any(
            _tree_write_target(os.path.join(dest, name)) or
            _protected_path(os.path.join(dest, name)) for dest in destinations
        ):
            return f"{base} archive member may write outside the destination"
    return None


def _tree_write_target(path: str) -> bool:
    """A tree writer can replace a protected file below this destination."""
    forms, unresolved = _path_forms(path)
    protected = [(_EFFECTIVE_CWD or Path.cwd()) / ".git/config",
                 (_EFFECTIVE_CWD or Path.cwd()) / ".git/hooks",
                 (_EFFECTIVE_CWD or Path.cwd()) / ".husky", Path.home() / ".gitconfig",
                 Path.home() / ".config/git", Path.home() / ".config/husky"]
    protected_paths = [str(p.resolve(strict=False)).casefold() for p in protected]
    for form in forms:
        if _matches_protected_path(form) or re.search(r"(?:^|/)\.git$", form):
            return True
        if any(item == form or item.startswith(form.rstrip("/") + "/")
               for item in protected_paths):
            return True
    return unresolved and _enters_protected_dir(path)


def _tree_destinations(base: str, args: list[str]) -> list[str]:
    """Destinations of commands that copy or extract directory contents."""
    flags = [arg for arg in args if arg.startswith("-")]
    recursive = any(arg in ("-r", "-R", "-a", "--recursive", "--archive") or
                    (arg.startswith("-") and not arg.startswith("--") and
                     any(ch in arg[1:] for ch in "rRa")) for arg in flags)
    if base in {"cp", "rm", "chmod", "chown"} and recursive:
        return args[-1:] if base == "cp" else [a for a in args if not a.startswith("-")]
    if base in {"mv", "rsync", "ditto"}:
        return args[-1:]
    if base in {"tar", "bsdtar", "gtar"}:
        extracting = any(arg in ("--extract", "--get") or
                         (arg.startswith("-") and not arg.startswith("--") and "x" in arg[1:])
                         for arg in flags) or bool(
            args and re.fullmatch(r"[A-Za-z]+", args[0]) and "x" in args[0])
        if extracting:
            dirs: list[str] = []
            current = "."
            i = 0
            while i < len(args):
                arg = args[i]
                directory = None
                if arg in ("-C", "--directory") and i + 1 < len(args):
                    directory = args[i + 1]
                    i += 1
                elif arg.startswith("--directory="):
                    directory = arg.split("=", 1)[1]
                elif arg.startswith("-C") and len(arg) > 2:
                    directory = arg[2:]
                if directory is not None:
                    current = os.path.normpath(os.path.join(current, directory))
                    dirs.append(current)
                i += 1
            return dirs or ["."]
    if base == "unzip" and not any(a in ("-l", "-t", "-v", "-Z") for a in flags):
        return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-d"] or ["."]
    if base in {"cpio", "pax"} and any(
        arg in ("-i", "-r", "--extract") or
        (arg.startswith("-") and not arg.startswith("--") and any(ch in arg[1:] for ch in "ir"))
        for arg in flags
    ):
        return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-D"] or ["."]
    if base == "jar" and args and "x" in args[0]:
        return ["."]
    if base == "7z" and args and args[0] in {"x", "e"}:
        return [arg[2:] for arg in args if arg.startswith("-o") and len(arg) > 2] or ["."]
    return []


def _wrapped_write_violation(cmd: SimpleCommand, words: list[Word]) -> str | None:
    """A writer reached past a wrapper's own options, or under an assembled name.

    Prefix stripping removes a wrapper word but not the wrapper's own options
    or operands (a nice priority, a timeout duration, a lock file), and
    wrappers this guard does not list (setsid, ionice, chroot) take their own
    arguments too. So unless the command only reads, every later writer word
    is judged as if it were the command. A command name the shell assembles
    cannot be known, so it may not take a protected path as an argument.
    """
    if not words:
        return None
    head = words[0]
    if head.expanded or head.braced or head.globbed:
        for w in words[1:]:
            if not w.expanded and (_protected_path(w.text) or _enters_protected_dir(w.text)):
                return f"a command name assembled by the shell, given protected path {w.text}"
        return None
    if os.path.basename(head.text) in _GIT_WORD_READERS:
        return None
    for j in range(1, len(words)):
        w = words[j]
        if not w.expanded and os.path.basename(w.text) in (_DIRECT_WRITERS | {"cd", "pushd"}):
            hit = _direct_write_violation(cmd, words[j:])
            if hit:
                return hit
    return None


def _download_destinations(base: str, args: list[str]) -> tuple[list[str], list[str]]:
    """Output files and directories declared by a download command."""
    output_options = ({"-o", "--output", "-D", "--dump-header",
                       "-c", "--cookie-jar", "--trace", "--trace-ascii"}
                      if base == "curl" else
                      {"-O", "--output-document", "-o", "--output-file",
                       "-a", "--append-output"})
    directory_options = ({"--output-dir"} if base == "curl" else
                         {"-P", "--directory-prefix"})
    files: list[str] = []
    directories: list[str] = []
    for index, arg in enumerate(args):
        for option in output_options | directory_options:
            if arg == option and index + 1 < len(args):
                value = args[index + 1]
            elif (arg.startswith(option + "=") if option.startswith("--") else
                  arg.startswith(option) and len(arg) > len(option)):
                value = arg[len(option):].lstrip("=")
            else:
                continue
            (directories if option in directory_options else files).append(value)
            break
    return files, directories


def _direct_write_violation(cmd: SimpleCommand, words: list[Word]) -> str | None:
    for op, target in cmd.redirects:
        if ">" in op and (target.expanded or target.write_glob
                         or _protected_path(target.text)):
            return f"shell redirection writing protected hook or git config path {target.text}"
    if not words:
        return None
    base = os.path.basename(words[0].text)
    if base in _DIRECT_WRITERS or base in {"cd", "pushd"}:
        if any(w.expanded or w.write_glob
               for w in words[1:]):
            return f"{base} target assembled by shell expansion"
    args = [w.text for w in words[1:]]
    destinations = _tree_destinations(base, args)
    for target in destinations:
        if _tree_write_target(target):
            return f"{base} writing a tree containing protected hook or git config path {target}"
    hit = _archive_violation(base, args, destinations)
    if hit:
        return hit
    targets: list[str] = []
    if base == "tee":
        targets = [arg for arg in args if not arg.startswith("-")]
    elif _interpreter_family(base) == "sed" and any(
        arg == "-i" or (arg.startswith("-i") and len(arg) > 2)
        or arg.startswith("--in-place") for arg in args
    ):
        targets = args
    elif base in {"cp", "mv", "ln", "install", "rsync"}:
        targets = args[-1:] + [args[i + 1] for i, arg in enumerate(args[:-1])
                               if arg in ("-t", "--target-directory")]
        sources = [arg for arg in args[:-1] if not arg.startswith("-")]
        if base == "ln" and any(
            arg in {"-s", "--symbolic"} or (
                arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]
            ) for arg in args
        ):
            for source in sources:
                if _protected_path(source) or any(
                    _protected_path(os.path.join(os.path.dirname(target), source))
                    for target in targets
                ):
                    return "ln creating a symlink to protected hook or git config path"
        # A directory target writes each source under its own name, so
        # `cp config .git/` lands on .git/config.
        targets += [os.path.join(target, os.path.basename(os.path.normpath(source)))
                    for target in list(targets) for source in sources]
        if base == "mv":
            targets += args  # moving a protected source also changes it
    elif base in {"cd", "pushd"}:
        # Entering a git, Husky or config directory lets a later relative
        # write (`cd .git && printf x > config`) reach a protected file.
        for arg in args:
            if not arg.startswith("-") and _enters_protected_dir(arg):
                return f"{base} into protected hook or git config directory {arg}"
    elif base in {"rm", "touch", "truncate", "chmod", "chown", "mkdir"}:
        targets = args
    elif base == "dd":
        targets = [arg[3:] for arg in args if arg.startswith("of=")]
    elif base in {"curl", "wget"}:
        targets, directories = _download_destinations(base, args)
        for directory in directories:
            if _tree_write_target(directory):
                return f"{base} writing protected hook or git config path {directory}"
        if base == "curl" and any(arg in {"-K", "--config"} or arg.startswith("-K")
                                  or arg.startswith("--config=")
                                  for arg in args):
            return "curl output from a config file cannot be inspected"
        if base == "wget" and any(arg == "-e" or arg.startswith("-e")
                                  or arg.startswith("--execute")
                                  for arg in args):
            return "wget output settings cannot be inspected"
    for target in targets:
        if _protected_path(target):
            return f"{base} writing protected hook or git config path {target}"
    return None


def _strip_command_prefix(words: list[Word], *, wrappers: bool = False,
                          assignments: bool = True) -> list[Word]:
    """Find the executable after shell control words, assignments and wrappers."""
    rest = list(words)
    while rest:
        head = rest[0]
        if head.expanded:
            break
        base = os.path.basename(head.text)
        if head.text in _RESERVED_START or (assignments and _ASSIGNMENT.match(head.text)) or (
            wrappers and base in _WRAPPERS
        ):
            rest.pop(0)
            continue
        break
    return rest


def _wrapped_executable(words: list[Word], write_targets: list[Word] | None = None) -> list[Word] | None:
    """Return the literal executable and tail, or deny an indeterminate wrapper."""
    rest = words
    while rest and os.path.basename(rest[0].text) in _WRAPPERS:
        base = os.path.basename(rest[0].text)
        flags, values, positionals, assignments, opaque = _WRAPPER_GRAMMAR[base]
        writable_options, writable_positionals, unknown_policy = _WRAPPER_OPERAND_POLICY[base]
        if unknown_policy != "deny":
            return None
        i = 1
        ended = False
        while i < len(rest):
            word = rest[i]
            option = word.text
            if not ended and option == "--":
                ended = True
                i += 1
                break
            if not ended and (option in opaque or any(
                option.startswith(name + "=") or (name.startswith("-") and
                not name.startswith("--") and option.startswith(name) and len(option) > len(name))
                for name in opaque
            )):
                return None
            if not ended and option in flags:
                i += 1
                continue
            if base == "env" and not ended and re.fullmatch(r"-[i0v]+u", option):
                if i + 1 >= len(rest) or rest[i + 1].text == "--":
                    return None
                i += 2
                continue
            if not ended and option in values:
                if i + 1 >= len(rest) or rest[i + 1].text == "--":
                    return None
                if base == "env" and option in {"-C", "--chdir"}:
                    return None
                if option in writable_options and write_targets is not None:
                    write_targets.append(rest[i + 1])
                i += 2
                continue
            attached = next((name for name in values if (
                option.startswith(name + "=") if name.startswith("--") else
                option.startswith(name) and len(option) > len(name))), None)
            if not ended and attached:
                if base == "env" and attached in {"-C", "--chdir"}:
                    return None
                if attached in writable_options and write_targets is not None:
                    value = option[len(attached):].lstrip("=")
                    write_targets.append(Word(value, expanded=word.expanded,
                                              write_glob=word.write_glob))
                i += 1
                continue
            if not ended and option.startswith("-"):
                return None
            if not ended and assignments and _ASSIGNMENT.match(option):
                i += 1
                continue
            break
        if len(rest) - i <= positionals:
            return None
        if writable_positionals and write_targets is not None:
            write_targets.extend(rest[i:i + positionals])
        i += positionals
        executable = rest[i]
        if (executable.expanded or executable.braced or executable.globbed or
                executable.concatenated):
            return None
        rest = rest[i:]
    return rest if rest else None


def _mentions_protected_text(body: str) -> bool:
    return bool(re.search(r"(?:^|[^\w])(?:~/?|\.git(?:/|\b)|\.husky(?:/|\b)|"
                          r"\.gitconfig\b|\.config/(?:git|husky)(?:/|\b))", body))


_HOOK_BYPASS_TEXT = re.compile(r"--no-verify|core\.hooksPath|\bHUSKY\b|commit\s+-n\b")
def _script_operand(tail: list[Word], is_shell: bool) -> Word | None:
    """The script a shell or interpreter runs from a file, if it names one."""
    i = 0
    while i < len(tail):
        t = tail[i].text
        if t == "--":
            return tail[i + 1] if i + 1 < len(tail) else None
        if t in _INTERPRETER_VALUE_OPTIONS or (is_shell and t in ("+o", "+O")):
            i += 2
            continue
        if t.startswith("-") or (is_shell and t.startswith("+")):
            if is_shell and not t.startswith("--") and "s" in t[1:]:
                return None  # -s: the program is read from stdin
            i += 1
            continue
        return tail[i]
    return None


def _script_file_violation(runner: str, tail: list[Word], is_shell: bool,
                           depth: int) -> tuple[bool, str | None]:
    """Judge the program a shell, interpreter or awk takes as an operand."""
    script = _script_operand(tail, is_shell)
    if script is None:
        return False, None
    if script.expanded or script.globbed or script.braced:
        return True, f"{runner} script assembled by the shell"
    if _interpreter_family(runner) == "awk":
        return True, _inline_program_violation(runner, script.text, depth)
    return True, _program_file_violation(runner, script, depth, is_shell)


# Primitives that decode or evaluate program text at run time. An inline or
# standard-input program for an interpreter that uses one can build any path or
# command after the guard has read it, so it is refused rather than guessed at.
_RUNTIME_EVAL_TEXT = re.compile(
    r"\b(?:eval|exec|execfile|compile|__import__|importlib|marshal|codecs|b64decode|"
    r"base64|decode_base64|atob|unhexlify|fromhex|fromCharCode)\b|Buffer\s*\.\s*from")


def _stdin_program_violation(runner: str, body: str, depth: int) -> str | None:
    """A program read from standard input: a shell parses it as shell, an
    interpreter reads it as that language's code."""
    if _SHELLS.match(runner) or runner in _EVALUATORS:
        return _judge_program_body(body, depth)
    return _inline_program_violation(runner, body, depth)


def _inline_program_violation(runner: str, body: str, depth: int) -> str | None:
    if _SHELLS.match(runner):
        return _judge_program_body(body, depth)
    if _interpreter_family(runner) != "awk" and _RUNTIME_EVAL_TEXT.search(body):
        return f"{runner} program decodes or evaluates program text at run time"
    if _interpreter_family(runner) == "awk":
        for include in re.findall(r'@include\s+["\']([^"\']+)["\']', body):
            hit = _program_file_violation(runner, Word(include), depth + 1)
            if hit:
                return hit
    if _mentions_protected_text(body) or _HOOK_BYPASS_TEXT.search(body) or mentions_git(body):
        return f"{runner} program names a protected path or hook bypass"
    return None


def _program_file_violation(runner: str, script: Word, depth: int,
                            is_shell: bool = False) -> str | None:
    if depth >= _MAX_DEPTH:
        return "nested program exceeds inspection depth"
    if script.expanded or script.globbed or script.braced:
        return f"{runner} program file assembled by the shell"
    if _protected_path(script.text):
        return f"{runner} program file names a protected path"
    try:
        text = _effective_path(script.text).read_text()
    except (OSError, UnicodeError):
        return f"{runner} program file cannot be read"
    if _interpreter_family(runner) == "awk":
        for include in re.findall(r'@include\s+["\']([^"\']+)["\']', text):
            child = _effective_path(script.text).parent / include
            hit = _program_file_violation(runner, Word(str(child)), depth + 1)
            if hit:
                return hit
    if is_shell:
        return _judge_program_body(text, depth)
    if _interpreter_family(runner) == "awk":
        if _mentions_protected_text(text) or _HOOK_BYPASS_TEXT.search(text) or mentions_git(text):
            return f"{runner} program names a protected path or hook bypass"
        return None
    return _inline_program_violation(runner, text, depth)


def _multi_program_violation(runner: str, tail: list[Word], depth: int) -> str | None:
    """Inspect every program source, including repeated and mixed options."""
    family = _interpreter_family(runner)
    if family == "python" and any(w.text == "-c" for w in tail) and any(
        w.text == "-m" for w in tail
    ):
        return f"{runner} has multiple program modes"
    file_options = ({"-f", "--file", "-i", "--include", "-l", "--load"}
                    if family == "awk" else
                    {"-f", "--file", "-e", "--expression"} if family == "sed" else
                    {"-r", "--require"} if family in {"node", "ruby"} else
                    {"-M", "-m"} if family == "perl" else set())
    inline_options = ({"-e", "--source"} if family == "awk" else
                      {"-e", "--expression"} if family == "sed" else
                      {"-e", "-E", "--eval", "-p", "--print"} if family in {"perl", "node", "ruby"} else
                      {"-c"} if family == "python" else set())
    # sed -e is inline, unlike awk -e. Keep it out of file operands.
    if family == "sed":
        file_options = {"-f", "--file"}
    i = 0
    while i < len(tail):
        option = tail[i].text
        kind = "file" if option in file_options else "inline" if option in inline_options else ""
        attached: Word | None = None
        if not kind:
            for name in sorted(file_options | inline_options, key=len, reverse=True):
                suffix = (option[len(name) + 1:] if name.startswith("--") and
                          option.startswith(name + "=") else
                          option[len(name):] if len(name) == 2 and
                          option.startswith(name) and len(option) > 2 else None)
                if suffix is not None:
                    kind = "file" if name in file_options else "inline"
                    attached = Word(suffix, tail[i].expanded, tail[i].unquoted_expansion,
                                    tail[i].lead_expansion, tail[i].unquoted,
                                    tail[i].write_glob, tail[i].quoted)
                    break
        if kind:
            if attached is None and i + 1 >= len(tail):
                return f"{runner} program operand missing"
            value = attached or tail[i + 1]
            if kind == "file":
                hit = _program_file_violation(runner, value, depth)
            elif value.expanded or value.globbed or value.braced:
                hit = f"{runner} program assembled by the shell"
            else:
                hit = _inline_program_violation(runner, value.text, depth)
            if hit:
                return hit
            i += 1 if attached is not None else 2
            continue
        i += 1
    return None


def _judge_program_body(body: str, depth: int) -> str | None:
    global _EFFECTIVE_CWD
    if body.strip() != "git status" and _code_mentions_git(body, depth + 1):
        return "nested program invokes git outside the allowed status inspection"
    prior_cwd = _EFFECTIVE_CWD
    try:
        return _judge(body, depth + 1, False)
    finally:
        _EFFECTIVE_CWD = prior_cwd


def _nested_program_violation(cmd: SimpleCommand, rest: list[Word], depth: int) -> tuple[bool, str | None]:
    """Judge bodies run by shells, evaluators, and inline interpreters."""
    if not rest:
        return False, None
    if depth >= _MAX_DEPTH:
        return True, "nested program exceeds inspection depth"
    base = os.path.basename(rest[0].text)
    if base in {"source", "."} and len(rest) > 1:
        script = rest[1]
        if script.expanded or script.globbed or script.braced:
            return True, "sourced script assembled by the shell"
        path = _effective_path(script.text)
        if path.name.startswith(".env") or path.suffix in {".key", ".pem"}:
            return True, "sourced credential or environment file"
        try:
            return True, _judge_program_body(path.read_text(), depth)
        except (OSError, UnicodeError):
            return True, "sourced script cannot be read"
    if base == "eval":
        body = rest[1:]
        if any(w.expanded or w.globbed or w.braced for w in body):
            return True, "eval body assembled by the shell"
        return True, _judge_program_body(" ".join(w.text for w in body), depth)
    starts = [0]
    if base in _WRAPPERS:
        executable = _wrapped_executable(rest)
        if executable is None:
            return True, "wrapper executable cannot be resolved literally"
        starts.append(len(rest) - len(executable))
    elif base == "find":
        starts += [i for i, w in enumerate(rest[1:], 1)
                   if _SHELLS.match(os.path.basename(w.text)) or
                   _INTERPRETERS.match(os.path.basename(w.text)) or
                   os.path.basename(w.text) == "sed"]
    for start in starts:
        runner = os.path.basename(rest[start].text)
        is_shell = bool(_SHELLS.match(runner))
        if not is_shell and not _INTERPRETERS.fullmatch(runner):
            continue
        tail = rest[start + 1:]
        hit = _multi_program_violation(runner, tail, depth)
        if hit:
            return True, hit
        if _interpreter_family(runner) == "sed":
            continue
        for i, option in enumerate(tail):
            name = option.text
            # A shell's only program option is -c (alone or bundled); its -e
            # is errexit, not a program as it is for perl or ruby.
            is_program = (not is_shell and name in _INTERPRETER_PROGRAM_OPTIONS) or (
                is_shell and name.startswith("-") and not name.startswith("--") and "c" in name[1:])
            if not is_program:
                continue
            if i + 1 >= len(tail):
                return True, "nested program body missing"
            body = tail[i + 1]
            if body.expanded or body.globbed or body.braced:
                return True, "nested program body assembled by the shell"
            if is_shell:
                return True, _judge_program_body(body.text, depth)
            return True, _inline_program_violation(runner, body.text, depth)
        if is_shell and cmd.stdin and _reads_program_from_stdin(rest[start:]):
            return True, next((hit for body in cmd.stdin
                               if (hit := _judge_program_body(body, depth))), None)
        checked, hit = _script_file_violation(runner, tail, is_shell, depth)
        if checked:
            return True, hit
    return False, None


def command_violation(cmd: SimpleCommand, whole_mentions_git: bool, depth: int = 0,
                      whole_mentions_protected: bool = False,
                      pipe_program: str | None = None) -> str | None:
    words = list(cmd.words)
    stripped = _strip_command_prefix(words, wrappers=True)
    hit = _direct_write_violation(cmd, stripped) or _wrapped_write_violation(cmd, stripped)
    if hit:
        return hit
    words = _strip_command_prefix(words, assignments=False)
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i].text):
        i += 1
    hit = _assignment_violation(words[:i], command_mentions_git=whole_mentions_git)
    if hit:
        return hit
    rest = words[i:]
    if not rest:
        return None
    head = rest[0]
    base = os.path.basename(head.text)
    if base in _DENIED_DIRECTORY_PROGRAMS:
        return f"{base} may change the working directory during release preparation"
    if base in {"make", "gmake"} and any(
        word.text in {"-C", "--directory"} or
        word.text.startswith(("-C", "--directory="))
        for word in rest[1:]
    ):
        return f"{base} directory option is not allowed during release preparation"
    if base == "cd" and (len(rest) > 2 or
                          (len(rest) > 1 and rest[1].text == "-")):
        return "cd working directory cannot be resolved literally"
    own_mentions = _words_mention_git(rest[1:], cmd.stdin)
    any_expanded = any(w.expanded for w in rest[1:])

    candidates = rest if base in _WRAPPERS or base == "find" else rest[:1]
    runner = next((os.path.basename(w.text) for w in candidates
                   if _SHELLS.match(os.path.basename(w.text)) or
                   _INTERPRETERS.match(os.path.basename(w.text)) or
                   os.path.basename(w.text) in _EVALUATORS), "")
    if runner:
        start = next(i for i, w in enumerate(rest)
                     if os.path.basename(w.text) == runner)
        tail = rest[start:]
        stdin_program = (runner in {"source", "."} and len(tail) > 1 and
                         tail[1].text in {"/dev/stdin", "/dev/fd/0", "-"}) or (
            runner not in _EVALUATORS and (
                _reads_program_from_stdin(tail) or
                (len(tail) > 1 and tail[1].text in {"/dev/stdin", "/dev/fd/0"})))
        if stdin_program and (cmd.stdin or cmd.pipe_in or cmd.redirects):
            if cmd.stdin_dynamic or any(op.startswith("<") for op, _ in cmd.redirects):
                return "program text cannot be inspected"
            if cmd.stdin:
                for body in cmd.stdin:
                    hit = _stdin_program_violation(runner, body, depth)
                    if hit:
                        return hit
            elif pipe_program is None:
                return "program text cannot be inspected"
            else:
                hit = _stdin_program_violation(runner, pipe_program, depth)
                if hit:
                    return hit
        elif stdin_program:
            return "program text cannot be inspected"

    nested_checked, nested_hit = _nested_program_violation(cmd, rest, depth)
    if nested_hit:
        return nested_hit

    if head.expanded or head.braced or head.globbed or head.concatenated:
        return "executable name assembled by the shell"
    if base in {"alias", "unalias"}:
        return "shell aliases cannot be inspected as literal executables"
    if base in _EXPORTERS:
        for w in rest[1:]:
            name = w.text.split("=", 1)[0].rstrip("+")
            if _protected_environment_name(name) or (
                    whole_mentions_git and name in _GIT_LOCATION_NAME):
                return f"{base} {name} (program lookup or git environment may not be changed)"
        return None
    if base == "git" or base.startswith("git-"):
        if head.text != "git":
            return f"{head.text} (only a plain `git` is allowed)"
        return git_violation(rest[1:])
    if base in _WRAPPERS:
        if base in _DENIED_WRAPPERS:
            return f"{base} is not allowed during release preparation"
        write_targets: list[Word] = []
        executable = _wrapped_executable(rest, write_targets)
        if executable is None:
            return f"{base} executable name cannot be resolved literally"
        if os.path.basename(executable[0].text) in _DENIED_DIRECTORY_PROGRAMS | {"cd"}:
            return "wrapped directory change is not allowed during release preparation"
        if any(os.path.basename(w.text) in _DENIED_WRAPPERS for w in rest[1:]):
            return "a disallowed wrapper is not allowed during release preparation"
        for target in write_targets:
            if target.expanded or target.write_glob or _tree_write_target(target.text):
                return f"{base} writing protected hook or git config path {target.text}"
        hit = _assignment_violation(rest[1:], command_mentions_git=whole_mentions_git)
        if hit:
            return f"{base} {hit}"
        if base == "env":
            for index, word in enumerate(rest[1:], 1):
                option = word.text
                if option == "-S" or option.startswith("-S") or option.startswith("--split-string"):
                    return "env split-string changes program lookup"
                if option in {"-u", "--unset"} and index + 1 < len(rest):
                    if _protected_environment_name(rest[index + 1].text):
                        return "env unsets a protected environment variable"
                if option.startswith("--unset=") and _protected_environment_name(option.partition("=")[2]):
                    return "env unsets a protected environment variable"
                if option.startswith("-u") and len(option) > 2 and _protected_environment_name(option[2:]):
                    return "env unsets a protected environment variable"
        if base == "command" and any(w.text == "-p" for w in rest[1:]):
            return "command -p changes program lookup"
        if base == "exec" and any(w.text == "-a" or w.text.startswith("-a") for w in rest[1:]):
            return "exec -a changes program identity"
        if base == "command" and len(rest) > 1 and rest[1].text in ("-v", "-V"):
            return None
        if (own_mentions and not nested_checked) or (any_expanded and whole_mentions_git and not nested_checked):
            return f"{base} wrapping git (run plain git instead)"
        return None
    if base == "find" and any(w.text in ("-execdir", "-okdir") for w in rest):
        return "find directory-changing execution is not allowed during release preparation"
    if base == "find" and any(w.text in ("-exec", "-ok") for w in rest):
        if any(w.expanded or w.braced or w.globbed or w.concatenated for w in rest[1:]):
            return "find -exec executable name cannot be resolved literally"
        if own_mentions and not nested_checked:
            return "find -exec running git"
        return None
    is_shell = bool(_SHELLS.match(base))
    if is_shell or _INTERPRETERS.match(base) or base in _EVALUATORS:
        if own_mentions and not nested_checked:
            return f"{base} running a program that mentions git"
        program_assembled = (any_expanded if base in _EVALUATORS
                             else _program_word_expanded(rest, is_shell))
        if program_assembled and whole_mentions_git:
            return f"{base} running an assembled program in a command that mentions git"
        return None
    # Any other command carrying git as a word may be a wrapper this guard
    # does not know (setsid, arch, ionice, chroot ...): git must be the
    # command itself. Commands that only print, search or locate are exempt.
    if base not in _GIT_WORD_READERS and (
            any(_code_mentions_git(w.text, depth) for w in rest[1:])
            or any(_code_mentions_git(body, depth) for body in cmd.stdin)):
        return f"{base} running git (run plain git as the command itself)"
    return None


_MAX_DEPTH = 4


def _is_git_invocation(cmd: SimpleCommand) -> bool:
    words = _strip_command_prefix(list(cmd.words))
    if not words:
        return False
    base = os.path.basename(words[0].text)
    return base == "git" or base.startswith("git-")


def _code_mentions_git(text: str, depth: int) -> bool:
    """Whether shell code names git in its words, not in heredoc data."""
    lexer = _Lexer(text)
    try:
        commands = lexer.lex()
    except Unreadable:
        return mentions_git(text)
    if any(mentions_git(w.text) for c in commands for w in c.words):
        return True
    if depth >= _MAX_DEPTH:
        return any(mentions_git(body) for body in lexer.subs)
    return any(_code_mentions_git(body, depth + 1) for body in lexer.subs)


# An unreadable command line that names a shell, an interpreter, an evaluator,
# or a process substitution could run a program the guard never saw.
_UNREADABLE_RUNNER = re.compile(
    r"(?<![\w./-])(?:sh|bash|zsh|dash|ksh|mksh|fish|tcsh|csh|busybox|eval|source|python[\d.]*|"
    r"perl|ruby|node|nodejs|deno|bun|awk|gawk|mawk|php|lua|osascript)(?![\w-])|<\(|>\(")


def _judge(command: str, depth: int, in_substitution: bool) -> str | None:
    global _EFFECTIVE_CWD
    lexer = _Lexer(command)
    try:
        commands = lexer.lex()
    except Unreadable as exc:
        if mentions_git(command) or re.search(r"GIT_|HUSKY", command):
            return f"a git command the guard cannot read fully ({exc})"
        if _UNREADABLE_RUNNER.search(command):
            return f"a command line that runs a program the guard cannot read fully ({exc})"
        return None
    # Whether the command line names git anywhere a shell could act on it:
    # its words and substitutions, not heredoc data written to a file.
    whole = any(mentions_git(w.text) for c in commands for w in c.words) or any(
        _code_mentions_git(body, depth + 1) for body in lexer.subs)
    whole_protected = any(_mentions_protected_text(w.text)
                          for c in commands for w in c.words)
    for body in lexer.subs:
        if depth >= _MAX_DEPTH:
            if mentions_git(body):
                return "command or process substitution around git"
            continue
        prior_cwd = _EFFECTIVE_CWD
        hit = _judge(body, depth + 1, True)
        _EFFECTIVE_CWD = prior_cwd
        if hit:
            return hit
    written_files: set[str] = set()
    written_trees: set[str] = set()
    for index, cmd in enumerate(commands):
        if in_substitution and _is_git_invocation(cmd):
            return "command or process substitution around git"
        stripped = _strip_command_prefix(cmd.words)
        if stripped and os.path.basename(stripped[0].text) in _WRAPPERS:
            stripped = _wrapped_executable(stripped) or []
        if stripped:
            runner = os.path.basename(stripped[0].text)
            if runner in _EVALUATORS and len(stripped) > 1:
                script = stripped[1]
            elif _SHELLS.match(runner) or _INTERPRETERS.match(runner):
                script = _script_operand(stripped[1:], bool(_SHELLS.match(runner)))
            else:
                script = None
            if script:
                script_path = str(_effective_path(script.text).resolve(strict=False))
                if script_path in written_files or any(
                    script_path == tree or script_path.startswith(tree.rstrip("/") + "/")
                    for tree in written_trees
                ):
                    return "program text cannot be inspected"
        producer = commands[index - 1] if cmd.pipe_in and index else None
        pipe_program = _literal_pipe_program(producer) if producer else None
        hit = command_violation(cmd, whole, depth, whole_protected, pipe_program)
        if hit:
            return hit
        for op, target in cmd.redirects:
            if ">" in op:
                written_files.add(str(_effective_path(target.text).resolve(strict=False)))
        if stripped:
            writer = os.path.basename(stripped[0].text)
            args = [w.text for w in stripped[1:]]
            if writer in {"tee", "cp", "mv", "install", "ln", "rsync", "touch", "truncate"}:
                for target in args:
                    if not target.startswith("-"):
                        written_files.add(str(_effective_path(target).resolve(strict=False)))
            if _interpreter_family(writer) == "sed" and any(a == "-i" or a.startswith("--in-place") for a in args):
                for target in args:
                    if not target.startswith("-"):
                        written_files.add(str(_effective_path(target).resolve(strict=False)))
            if writer == "dd":
                for arg in args:
                    if arg.startswith("of="):
                        written_files.add(str(_effective_path(arg[3:]).resolve(strict=False)))
            if writer in {"curl", "wget"}:
                files, directories = _download_destinations(writer, args)
                written_files.update(str(_effective_path(target).resolve(strict=False))
                                     for target in files)
                written_trees.update(str(_effective_path(target).resolve(strict=False))
                                     for target in directories)
            if writer in _ARCHIVE_TOOLS:
                for target in _tree_destinations(writer, args):
                    written_trees.add(str(_effective_path(target).resolve(strict=False)))
            if writer == "cd":
                directory = args[0] if args else str(Path.home())
                _EFFECTIVE_CWD = _effective_path(directory).resolve(strict=False)
    return None


def _literal_pipe_program(cmd: SimpleCommand) -> str | None:
    """Recover program text only from inert, literal text emitters."""
    words = _strip_command_prefix(cmd.words)
    if not words or cmd.pipe_in or cmd.redirects or cmd.stdin_dynamic:
        return None
    if any(w.expanded or w.globbed or w.braced for w in words):
        return None
    base = os.path.basename(words[0].text)
    if base == "cat" and len(words) == 1 and cmd.stdin:
        return "\n".join(cmd.stdin)
    if cmd.stdin:
        return None
    if base == "echo" and len(words) > 1 and not any(w.text.startswith("-") for w in words[1:]):
        return " ".join(w.text for w in words[1:]) + "\n"
    if base == "printf" and len(words) > 1 and "%" not in words[1].text and "\\" not in words[1].text:
        return words[1].text
    return None


def find_violation(command: str) -> str | None:
    """Return a refusal label for the command, or None when it is allowed."""
    global _EFFECTIVE_CWD
    prior_cwd = _EFFECTIVE_CWD
    _EFFECTIVE_CWD = Path.cwd()
    try:
        return _judge(command, 0, False)
    finally:
        _EFFECTIVE_CWD = prior_cwd


# --- hook entry point -------------------------------------------------------

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
        f"Refused: {label}.\n\n"
        f"The release prepare agent may run only plain, allowlisted git "
        f"commands, with their hooks: for example `git -C <path> status`, "
        f"`git -C <path> commit -m \"...\"`, `git -C <path> push origin "
        f"<branch>`. Run git directly, not through a wrapper, alias, variable, "
        f"substitution or inline interpreter program, and never skip or "
        f"redirect hooks (--no-verify, commit -n, core.hooksPath, HUSKY, "
        f"GIT_* variables).\n\n"
        f"If the test stage cannot pass for a reason unrelated to your change, "
        f"use the hook's own escape hatch, which still runs type-check, lint "
        f"and format and records why:\n"
        f"  SKIP_TESTS=1 SKIP_TESTS_REASON=\"<why>\" git commit -m \"...\"\n"
        f"and state that reason in your report.\n\n"
        f"If any other stage fails, or the hook fails for an environment "
        f"reason, stop and report it. There is no override for this guard."
    )


def _record_live_probe(command: str, decision: str, reason: str = "") -> None:
    """Emit a local, opt-in receipt for the pre-deploy hook binding check."""
    path = os.environ.get("ATELES_GUARD_LIVE_AUDIT")
    if not path:
        return
    record = {"command_sha256": hashlib.sha256(command.encode()).hexdigest(),
              "decision": decision, "reason": reason}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        # The live verifier fails inconclusive when the receipt is absent.
        pass


def main() -> int:
    try:
        payload = read_hook_input()
    except Exception:  # noqa: BLE001 — unreadable hook input denies
        return deny(guidance("hook input could not be read"))
    if not isinstance(payload, dict) or not payload:
        return deny(guidance("hook input could not be read"))
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return deny(guidance("tool name is missing or malformed"))
    if tool not in {"Bash", "Edit", "Write", "MultiEdit"}:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny(guidance("tool input is missing or malformed"))
    if tool in {"Edit", "Write", "MultiEdit"}:
        try:
            path = tool_input.get("file_path")
            if not isinstance(path, str):
                return deny(guidance("file-write path is missing or malformed"))
            if _protected_path(path):
                return deny(guidance(f"{tool} writing protected hook or git config path {path}"))
        except Exception:  # noqa: BLE001 — an unjudged file write denies
            return deny(guidance("internal guard error while judging file write"))
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str):
        return deny(guidance("Bash command is missing or malformed"))
    try:
        label = find_violation(command)
    except Exception:  # noqa: BLE001 — an unjudged Bash command denies
        return deny(guidance("internal guard error while judging Bash command"))
    if label is None:
        _record_live_probe(command, "allow")
        return 0
    log(f"blocking: {label}")
    _record_live_probe(command, "deny", label)
    return deny(guidance(label))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — no exception detail in the denial
        sys.exit(deny(guidance("internal guard error")))
