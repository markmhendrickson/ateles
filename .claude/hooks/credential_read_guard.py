#!/usr/bin/env python3
"""PreToolUse hook — stop an agent from putting a credential file's CONTENT
into model context, across Bash, Read, Grep, and Glob.

The hazard (Neotoma task ent_cbf9bdbdf475eda8900d6b49): three agents read
~/.config/neotoma/.env whole rather than reading the one variable they
needed. On 2026-09-07 a subagent grepped the file broadly and printed every
matching line — including live secret values — into its transcript. On
2026-09-25 a `drafts update` investigation printed eight secrets into a
transcript the same way. On 2026-09-26 secrets were seen again while an
agent was looking for unrelated, non-secret context in the same file. A
prose brief telling agents not to do this has been written and broken three
times; this hook makes the read structurally refused rather than
instruction-dependent.

Ateles PR #1296 established that a PROJECT-level hook (this checkout's
`.claude/settings.json`) fires for SUBAGENT tool calls too, not only the
top-level session's — which is exactly the gap this task exists to close, so
this hook is wired at the project level like its siblings, not left to a
per-agent brief.

WHAT COUNTS AS A CREDENTIAL PATH — see CREDENTIAL_PATH_GLOBS below. That list
is the single data source: every check in this file (Bash, Read, Grep, Glob)
resolves against it, so a new credential location is added in one place.

WHAT IS REFUSED (would put file content into context):
  - Read tool on a credential path.
  - Grep tool on a credential path in "content" mode (the default), or any
    mode that prints matched TEXT rather than just file names or counts.
  - Glob tool is refused only when its own pattern targets a credential path
    for something a Glob call cannot need — Glob returns file names, never
    content, so it is allowed by default; see `check_glob`.
  - Bash: cat, head, tail, less, more, bat, sed -n (print mode), awk, cut
    (field extraction, e.g. `cut -d= -f2`), dd, and a plain `grep`/`rg` with
    no -c/-l/-o-names-only mode, run against a credential path. Also `env`,
    `printenv`, a bare `set` dump, or `declare -p`/`export -p`/`typeset -p`
    AFTER sourcing one (a source alone does not print anything; the dump
    does). Also base64/xxd/od/hexdump/strings against a credential path —
    encoding the bytes is still exfiltrating them into context. Also an
    interpreter's inline program (`python3 -c`, `perl -e`, `ruby -e`,
    `node -e`) whose source text NAMES a credential path — this hook cannot
    parse arbitrary interpreted code, so any inline program that mentions
    the path at all is treated as unsafe. Also a `while read`/`read`/`xargs`
    construct fed the file via `<` input redirection.

WHAT IS ALLOWED (mirrors the task spec — none of these print a value):
  - `grep -c '^NAME='  <file>`             — existence, a count, no value.
  - `grep -o '^[A-Z_]*=' <file>`           — names only; `-o` with a pattern
    that captures the KEY up to and including `=` but not the value.
  - `set -a; source <file>; set +a`        — used inside a command that
    never dumps the resulting environment afterward.
  - Reading a `*.example`/`*.sample`/`*.template` file — these are
    placeholders by convention, never real secrets.
  - `grep -l` (files matching, no content) and `wc -l <file>` (a line count).

Compound commands are split the way gmail_send_gate.py splits them — on
`&&`, `;`, `|`, and newlines, with backslash-newline line continuations
folded first — so a risky read hidden after a safe first segment is still
caught. Wrappers
(`bash -c`, `sh -c`, `python3 -c`, `node -e`), command/backtick substitution,
subshells, and `xargs` indirection are covered the same way
gmail_send_gate.py's adversarial pass covered them for its own domain: by
refusing to special-case any leader that can itself execute a further
command (see `TEXT_BEARING_LEADERS`'s docstring in that file for why
interpreters must never be exempted).

Fail-open: any error, missing field, or unparseable input -> exit 0. This
hook must never itself become the reason a session breaks; the credential
exposure it prevents is worse than a rare missed catch, but a hook that
crashes the harness is worse than either.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

# ---------------------------------------------------------------------------
# Single data source: every credential-path check in this file resolves
# against this list. Extend HERE, nowhere else. Patterns are glob-style
# (fnmatch), evaluated against the user-expanded, normalized path.
# ---------------------------------------------------------------------------
CREDENTIAL_PATH_GLOBS = [
    "*/.config/neotoma/*.env",
    "*/.config/neotoma/.env",
    "*.env",
    "*.env.*",
    "*/.claude.json",
    "*/.config/sops/age/*",
    "*/repos/ateles-private/keys/*",
    "*/.neotoma/aauth*/*private*",
    "*.jwk",
    "*.jwks",
    "*private*.jwk*",
    "*/.netrc",
    "*1password*export*",
    "*1password*.csv",
    "*op-export*",
]

# Templates/placeholders are never real secrets, regardless of which
# credential glob they'd otherwise match — a `.env.example` living right
# next to `.env` must stay readable, or agents lose the one safe way to see
# what a file's shape is without touching the real values.
SAFE_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

# Directories reads through: 1Password's own "export" nomenclature can also
# appear in perfectly ordinary paths (e.g. a doc titled "1password-export-
# notes.md"); the globs above are deliberately narrow (require .csv or the
# word "export" adjacent to "1password") to avoid over-blocking prose.


def log(msg: str) -> None:
    try:
        print(f"[credential_read_guard] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never block
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


_PROVENANCE_NOTE = (
    "Three agents have read a credential file whole and printed live secret "
    "values into a transcript (2026-09-07, 2026-09-25, 2026-09-26) — this is "
    "mechanical enforcement of the brief that did not hold on its own "
    "(Neotoma task ent_cbf9bdbdf475eda8900d6b49)."
)


def _safe_alternative(path_desc: str, tool: str = "Bash") -> str:
    """The remedy must match the TOOL that was actually denied — a Read or
    Grep denial suggesting a Bash command tells the agent to reach for a
    tool it wasn't using, which is the UX finding from ateles#1302 round 2
    (ux/non-blocking): 'Read/Grep denials shouldn't suggest a Bash
    command.' Each tool gets its own remedy in its own vocabulary; only the
    Bash remedy talks about shell commands at all."""
    header = (
        f"Refused: this would put the CONTENT of a credential file ({path_desc}) into "
        f"model context.\n\n"
    )
    if tool == "Read":
        body = (
            f"Read has no safe mode for a credential file — it always returns the "
            f"whole file, values included, and there is no Read-tool option that "
            f"limits this to a name, a count, or an in-place use.\n\n"
            f"If you need to USE a variable's value, or check that one EXISTS or "
            f"which names a file defines, do that through the Bash tool instead — "
            f"never through Read on this path.\n\n"
        )
    elif tool == "Grep":
        body = (
            f"Safe alternatives (use the Grep tool's own safe output modes):\n"
            f"  - To check whether a pattern EXISTS: output_mode: \"count\"\n"
            f"  - To list which FILES match, not their content: output_mode: "
            f"\"files_with_matches\" (the default)\n"
            f"Grep's \"content\" mode prints matched lines verbatim, which is the "
            f"one thing refused here.\n\n"
        )
    else:  # Bash (and anything else — the general shell remedy is the safest default)
        body = (
            f"Safe alternatives:\n"
            f"  - To USE a variable's value in a command, source it without echoing:\n"
            f"      set -a; source <file>; set +a\n"
            f"    then reference it as $VAR_NAME — never print $VAR_NAME itself.\n"
            f"  - To check whether a variable EXISTS, count it rather than reading it:\n"
            f"      grep -c '^VAR_NAME=' <file>\n"
            f"  - To list which variable NAMES a file defines (no values):\n"
            f"      grep -o '^[A-Z_]*=' <file>\n\n"
        )
    return header + body + _PROVENANCE_NOTE


def _normalize_path(raw: str) -> str:
    try:
        expanded = os.path.expanduser(raw)
    except Exception:  # noqa: BLE001
        expanded = raw
    return expanded.replace("\\", "/")


def _fnmatch_any(path: str, globs) -> bool:
    from fnmatch import fnmatch

    norm = path.replace("\\", "/")
    # Match both the normalized absolute-ish form and the bare basename, so
    # a glob written as "*.env" catches "/some/dir/x.env" and a relative
    # "x.env" alike, without needing every glob to be written twice.
    base = norm.rsplit("/", 1)[-1]
    for g in globs:
        if fnmatch(norm, g) or fnmatch(base, g):
            return True
    return False


def is_credential_path(raw: str) -> bool:
    """True if `raw` names a credential file per CREDENTIAL_PATH_GLOBS,
    and it is not a safe template/placeholder variant."""
    if not raw or not isinstance(raw, str):
        return False
    norm = _normalize_path(raw)
    if norm.lower().endswith(SAFE_TEMPLATE_SUFFIXES):
        return False
    # A path ending in one of the safe suffixes with an extra dotted
    # segment after it (e.g. ".env.example") is also safe — checked above
    # via endswith directly against the templated suffixes list, which
    # already covers ".env.example" since ".example" is the final suffix.
    return _fnmatch_any(norm, CREDENTIAL_PATH_GLOBS)


# ---------------------------------------------------------------------------
# Bash: command-text analysis, mirroring gmail_send_gate.py / git_stash_guard.py
# ---------------------------------------------------------------------------

SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|&]")

# Commands whose FIRST token means "this segment is executing nothing, it is
# only carrying the pattern as TEXT" — a commit message, a grep for the
# hazard string itself, documentation. Copied verbatim from
# gmail_send_gate.py / git_stash_guard.py: this list must contain ONLY
# commands that cannot themselves execute a further command. An interpreter
# (`python -c`, `node -e`, `bash -c`, `sh -c`) or a heredoc-consuming reader
# (`cat`) must NEVER be added here — those were LIVE bypasses in an earlier
# revision of gmail_send_gate.py, because a gated command riding along as
# the "exempt" leader's own argument sailed straight through.
TEXT_BEARING_LEADERS = re.compile(
    r"^(?:git\s+(?:commit|tag|notes)|echo|printf|"
    r"gh\s+(?:pr|issue|release))\b"
)

# A text-bearing leader is exempt only when its ARGUMENT is a literal
# string. `echo $(cat f.env)` and `` echo `cat f.env` `` do not carry the
# hazard as text — the substitution EXECUTES `cat` and echo then prints
# its output. A segment containing either substitution form must never be
# treated as text-bearing regardless of its leader; this was a live bypass
# found while testing this hook (an earlier revision let both through
# because "echo" alone was enough to exempt the whole segment).
_HAS_SUBSTITUTION_RE = re.compile(r"\$\(|`")


def _is_text_bearing(segment: str) -> bool:
    if _HAS_SUBSTITUTION_RE.search(segment):
        return False
    return bool(TEXT_BEARING_LEADERS.match(segment))


def _join_line_continuations(command: str) -> str:
    return re.sub(r"\\[ \t]*\n", " ", command)


def _split_segments(command: str):
    return [
        s for s in (seg.strip() for seg in SEGMENT_SPLIT.split(_join_line_continuations(command)))
        if s
    ]


# Readers that print file CONTENT verbatim, unconditionally, when pointed at
# a credential path. `cut` (field extraction, e.g. `cut -d= -f2`) is here
# too: it prints exactly the VALUE half of a `NAME=value` line, which is the
# one thing this hook exists to stop — this was a live bypass found in
# security review (arch/security lens, ateles#1302 round 1). `dd` copies
# raw bytes to stdout by default (`dd if=<path>` with no `of=`) — the same
# class of hazard, added cheaply alongside the other three findings from
# that review even though it was flagged non-blocking.
_CONTENT_DUMP_CMDS = re.compile(
    r"\b(cat|head|tail|less|more|bat|nl|tac|od|hexdump|xxd|strings|"
    r"base64|cut|dd)\b"
)

# `while read ...; done < <path>` (and similarly `read line < <path>`) feeds
# the file to a loop that typically echoes it — a shell-native alternative
# to `cat`. Detected as a bare `<` input redirection from a credential path
# combined with a `read`/`cat`/`while` construct anywhere in the same
# segment, which is loose by design (this hook does defense-in-depth, not a
# hermetic seal — see `_extract_paths`'s docstring) but costs nothing extra
# to check since the path was already being extracted.
_STDIN_LOOP_RE = re.compile(r"\b(while\s+read|read\s+-r?\s*\w+|xargs)\b")

# `sed -n '...p'` / bare `sed` without -n prints the whole stream; `sed -n`
# WITHOUT a print command prints nothing, but that construction is rare
# enough (and hard to tell apart from a real print reliably) that any `sed`
# touching a credential path is treated as a dump. Same reasoning for `awk`
# with no program that could plausibly avoid printing fields — awk's default
# action on a matched pattern is to print the whole record.
_SED_AWK_RE = re.compile(r"\b(sed|awk)\b")

# A plain `grep`/`rg`/`egrep`/`fgrep` that is NOT restricted to a safe mode.
# Safe modes: -c/--count, -l/--files-with-matches, or -o/--only-matching
# whose pattern is a NAMES-ONLY capture (this hook cannot verify the pattern
# semantically, so -o is allowed only per the documented safe form; see
# `_grep_is_safe_mode`). `-L` (files WITHOUT match) is likewise safe — no
# content is ever printed.
_GREP_RE = re.compile(r"\b(?:egrep|fgrep|grep|rg)\b")

# `env` / `printenv` with no args dumps the whole environment; `set` with no
# args (POSIX builtin, bare) dumps every shell variable including anything
# just sourced. Both are refused when they follow a `source`/`.` of a
# credential path anywhere earlier in the SAME command. `set -a; ...; set +a`
# is the sanctioned safe form — those are flag-toggle invocations, not dumps,
# so they are excluded here explicitly (`set -a` / `set +a` / `set -e` etc.
# never appear bare).
_ENV_DUMP_RE = re.compile(r"(?:^|\s)(env|printenv)(?:\s|$)")
_BARE_SET_DUMP_RE = re.compile(r"(?:^|;|&&|\n)\s*set\s*(?:$|;|&&|\n)")

# `declare -p` / `export -p` / `typeset -p` (bash/ksh builtins) print every
# variable's current value, same hazard as a bare `env`/`set` dump after
# sourcing a credential file — a live bypass found in security review
# (ateles#1302 round 1). `-p` may appear with other flags attached
# (`declare -px`) or as a separate token; matched loosely on the builtin
# name plus a `-p` flag anywhere on the same invocation.
_DECLARE_DUMP_RE = re.compile(r"\b(declare|export|typeset)\b[^;&|\n]*(?:^|\s)-\w*p\w*\b")

_SOURCE_RE = re.compile(r"(?:^|\s)(?:source|\.)\s+(\S+)")


def _grep_is_safe_mode(segment: str) -> bool:
    """True when a grep/rg invocation cannot print matched line CONTENT."""
    tokens = segment.split()
    has_count = any(t in ("-c", "--count") for t in tokens) or bool(
        re.search(r"\B-\w*c\w*\b", segment)
    )
    has_files_only = any(
        t in ("-l", "--files-with-matches", "-L", "--files-without-match") for t in tokens
    )
    has_only_matching = any(t in ("-o", "--only-matching") for t in tokens)
    if has_count or has_files_only:
        return True
    if has_only_matching:
        # Safe ONLY for the documented names-only form: a pattern that
        # matches a variable NAME up through "=" and nothing past it, e.g.
        # '^[A-Z_]*=' or '^[A-Za-z_][A-Za-z0-9_]*='. Anything else with -o
        # could still print a captured secret substring, so it is refused.
        names_only_pattern = re.compile(r"""\^\[[A-Za-z_][^"'\s]*\]\*=""")
        return bool(names_only_pattern.search(segment))
    return False


def _extract_paths(segment: str):
    """Cheap tokenization: return every whitespace-delimited token that
    looks like a path (contains '/' or a dot-extension), stripped of shell
    quoting AND of wrapper punctuation ($(...), backticks, subshell parens)
    that a wrapped invocation leaves stuck to the path text — without this,
    `$(cat f.env)`, `` `cat f.env` ``, and `(cat f.env)` all mangle the
    trailing token into "f.env)" / "f.env`", which then fails the credential
    match and lets the wrapper through. This was a live bypass found while
    testing this hook against gmail_send_gate.py's own evasion-vector list.
    Good enough for a fail-open guard — false negatives here are tolerable
    (defense in depth, not a hermetic seal, exactly like
    sibling_repo_worktree_guard.py's own stated limitation)."""
    out = []
    for tok in segment.split():
        cand = tok.strip("'\"")
        cand = cand.strip("()`$")
        cand = cand.rstrip(";,")
        if not cand or cand.startswith("-"):
            continue
        # A `key=value` token (`dd if=<path>`, `--file=<path>`) carries the
        # path after the `=`, not as the whole token — strip a leading
        # `bareword=`/`--flag=` prefix so `if=/x/.env` yields `/x/.env`
        # rather than failing every credential-glob match outright. Only
        # ONE `=` is stripped (rsplit not needed — dd's own value never
        # contains another `=`), and only when what follows still looks
        # path-shaped.
        eq_idx = cand.find("=")
        if 0 < eq_idx < len(cand) - 1 and re.match(r"^[\w-]+$", cand[:eq_idx]):
            after = cand[eq_idx + 1:]
            if "/" in after or "." in after:
                cand = after
        if "/" in cand or "." in cand:
            out.append(cand)
    return out


# Path-shaped substrings ANYWHERE in a string, not just whitespace-delimited
# tokens — needed for an interpreter's inline program, where the target path
# sits inside a quoted literal with no surrounding whitespace of its own,
# e.g. `open('/x/.env')`. Matches a run of "path-ish" characters (no
# whitespace, no quote, no shell metacharacter) that contains at least one
# `/` or a dot-extension, mirroring `_extract_paths`'s own definition of
# "looks like a path" but without requiring whitespace boundaries.
_EMBEDDED_PATH_RE = re.compile(r"""[^\s'"();`$|&]+""")


def _extract_embedded_paths(text: str):
    out = []
    for cand in _EMBEDDED_PATH_RE.findall(text):
        cand = cand.rstrip(";,)")
        if not cand or cand.startswith("-"):
            continue
        if "/" in cand or "." in cand:
            out.append(cand)
    return out


# Interpreters whose `-c`/`-e` (or `-pe`/`-ne` for perl) flag takes an INLINE
# PROGRAM as its very next argument, rather than a script file path. Only
# these forms are in scope — `python3 script.py <credential path>` passes
# the path as an ordinary argv entry to a FILE-based script this hook cannot
# see into, which is a different (and already out-of-scope, per this hook's
# stated "not a hermetic seal" limitation) problem.
_INTERPRETER_INLINE_RE = re.compile(
    r"\b(?:python[0-9.]*|perl|ruby|node|nodejs)\b[^;&|\n]*?\s(?:-c|-e|-pe|-ne)\b"
)


def _interpreter_inline_credential_hit(segment: str) -> str | None:
    """Refuse `python3 -c '...'` / `perl -e '...'` / `ruby -e '...'` /
    `node -e '...'` whose inline program text NAMES a credential path,
    regardless of what the program does with it — this hook cannot parse
    arbitrary interpreted code to tell a safe access from a dump, so any
    inline program that mentions the path at all is treated as unsafe."""
    if not _INTERPRETER_INLINE_RE.search(segment):
        return None
    for cand in _extract_embedded_paths(segment):
        if is_credential_path(cand):
            return cand
    return None


def _segment_touches_credential(segment: str) -> str | None:
    """Return a human path description if `segment` names a credential path
    in a way that would print its content, else None."""
    paths = _extract_paths(segment)
    cred_paths = [p for p in paths if is_credential_path(p)]
    if not cred_paths:
        return None

    if _CONTENT_DUMP_CMDS.search(segment) or _SED_AWK_RE.search(segment):
        return cred_paths[0]

    if _GREP_RE.search(segment) and not _grep_is_safe_mode(segment):
        return cred_paths[0]

    # `< <credential path>` combined with a read-and-print-shaped construct
    # (`while read`, `read var`, `xargs`) — the shell-native alternative to
    # `cat <path>`.
    if _STDIN_LOOP_RE.search(segment) and re.search(
        r"<\s*" + re.escape(cred_paths[0]), segment
    ):
        return cred_paths[0]

    return None


def _command_sources_credential(command: str) -> bool:
    """True if ANY segment in the whole command sources a credential path
    (used to gate a later bare env/printenv/set dump elsewhere in the same
    command string)."""
    for segment in _split_segments(command):
        m = _SOURCE_RE.search(segment)
        if m and is_credential_path(m.group(1).strip("'\"")):
            return True
    return False


# Chain-level split: `&&`, `;`, `\n` — but NOT `|`, because a pipe carries
# DATA from one stage to the next within the same chain. `echo <credential
# path> | xargs cat` is one chain of two stages: the path is a bare argument
# to `echo` in stage 1, and `cat` in stage 2 has no path argument of its own
# at all — it receives the path as stdin via xargs. Splitting on `|` (as
# `_split_segments` does for the independent-segment case) loses that
# connection entirely, which was a live bypass found while testing this
# hook: neither stage alone names both "a credential path" and "a dump
# command", so `_segment_touches_credential` per SEGMENT_SPLIT-split piece
# never fires. `_command_sources_credential`'s SOURCE_RE-based check is
# unaffected by this distinction since sourcing is never usefully piped.
_CHAIN_SPLIT = re.compile(r"&&|[;\n]")
_PIPE_SPLIT = re.compile(r"\|\||\|")


def _chain_touches_credential_via_pipe(chain: str) -> str | None:
    """Return a path description if ANY stage of a `|`-joined chain names a
    credential path and ANY stage (the same one or a later one) is capable
    of dumping content — covering `cat <path> | ...` (path and dump in the
    same stage, already caught by `_segment_touches_credential`) as well as
    the indirect form `echo <path> | xargs cat` / `printf '%s' <path> | cat`,
    where the path is bare data handed to a later stage through the pipe."""
    stages = [s.strip() for s in _PIPE_SPLIT.split(chain) if s.strip()]
    if len(stages) < 2:
        return None
    cred_path = None
    for stage in stages:
        for p in _extract_paths(stage):
            if is_credential_path(p):
                cred_path = p
                break
        if cred_path:
            break
    if not cred_path:
        return None
    for stage in stages:
        if _CONTENT_DUMP_CMDS.search(stage) or _SED_AWK_RE.search(stage):
            return cred_path
        if _GREP_RE.search(stage) and not _grep_is_safe_mode(stage):
            return cred_path
    return None


def _redirected_stdin_loop_hit(command: str) -> str | None:
    """`while read ...; done < <credential path>` and `read var < <path>`
    split across MULTIPLE `;`-separated segments (`while read ...`, `do
    ...`, `done < <path>` are three different SEGMENT_SPLIT pieces), so
    neither the per-segment nor the pipe-chain check above sees both the
    loop construct and the redirected path together. Checked against the
    WHOLE command text instead, which is safe here specifically because
    both sides of the check (`_STDIN_LOOP_RE`, the `<` redirection) are
    narrow enough that a false hit would need a `while read`/`read`/`xargs`
    keyword AND a credential path literal to co-occur anywhere in the same
    command — a real bypass found in security review (ateles#1302 round 2
    follow-up), same shape as the earlier `xargs` pipe-chain fix."""
    if not _STDIN_LOOP_RE.search(command):
        return None
    for cand in _extract_paths(command):
        if not is_credential_path(cand):
            continue
        if re.search(r"<\s*" + re.escape(cand), command):
            return cand
    return None


def check_bash(command: str):
    if not command or not isinstance(command, str):
        return None
    sourced_a_credential = _command_sources_credential(command)

    hit = _redirected_stdin_loop_hit(command)
    if hit:
        return hit

    # No text-bearing exemption at the CHAIN level: `echo <path> | xargs
    # cat` legitimately starts with `echo`, which is the same leader this
    # hook otherwise treats as "just carrying text" for a lone segment —
    # but here it is the first stage of a pipe that hands the path to a
    # real reader. A chain is only exempt if it is not actually piping
    # anywhere (a single stage, handled separately below).
    joined = _join_line_continuations(command)
    for chain in (c for c in (s.strip() for s in _CHAIN_SPLIT.split(joined)) if c):
        hit = _chain_touches_credential_via_pipe(chain)
        if hit:
            return hit

    for segment in _split_segments(command):
        normalized = " ".join(segment.split())
        if not normalized:
            continue
        if _is_text_bearing(normalized):
            continue

        hit = _segment_touches_credential(normalized)
        if hit:
            return hit

        # env/printenv/bare-set/declare-p/export-p/typeset-p dump AFTER a
        # credential source anywhere in this command. Sourcing alone
        # (`set -a; source f; set +a`) prints nothing — only a dump command
        # makes the values reach context.
        if sourced_a_credential and (
            _ENV_DUMP_RE.search(normalized)
            or _BARE_SET_DUMP_RE.search(normalized)
            or _DECLARE_DUMP_RE.search(normalized)
        ):
            return "environment dump after sourcing a credential file"

        # An interpreter's inline program (`-c`/`-e`) that itself NAMES a
        # credential path is refused outright, regardless of what the
        # program text does with it — this hook cannot parse arbitrary
        # Python/Perl/Ruby/JS to tell a safe access from a dump, and an
        # inline program that mentions the path at all is already the
        # hazard: it is reading that specific file on purpose. Live bypass
        # found in security review (ateles#1302 round 1): `python3 -c
        # "print(open('f.env').read())"` matched no dump-command regex
        # because the actual read happens inside the interpreter, not as a
        # shell word this hook's other checks scan for.
        hit = _interpreter_inline_credential_hit(normalized)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------
# Read tool
# ---------------------------------------------------------------------------

def check_read(tool_input: dict):
    path = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(path, str) and is_credential_path(path):
        return path
    return None


# ---------------------------------------------------------------------------
# Grep tool — refuse only the modes that print matched TEXT.
# ---------------------------------------------------------------------------

def check_grep(tool_input: dict):
    path = tool_input.get("path")
    glob = tool_input.get("glob")
    target = None
    if isinstance(path, str) and is_credential_path(path):
        target = path
    elif isinstance(glob, str) and is_credential_path(glob):
        target = glob
    if target is None:
        return None

    output_mode = tool_input.get("output_mode") or "files_with_matches"
    if output_mode in ("files_with_matches", "count"):
        return None  # names or counts only — no content
    if output_mode == "content":
        # -A/-B/-C context or multiline widen the printed span but the base
        # case is already unsafe; refuse regardless of those flags.
        return target
    # Unknown/unexpected mode: refuse conservatively rather than guess.
    return target


# ---------------------------------------------------------------------------
# Glob tool — returns file NAMES only, never content, so it is not a content
# leak by construction. Nothing to refuse; kept as an explicit no-op so the
# PreToolUse matcher can legitimately include Glob without this file lying
# about what it checks.
# ---------------------------------------------------------------------------

def check_glob(tool_input: dict):  # noqa: ARG001 — intentionally unused
    return None


def main() -> int:
    payload = read_hook_input()
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    if tool == "Bash":
        command = tool_input.get("command")
        hit = check_bash(command)
        if hit:
            log(f"blocking Bash read of credential content: {hit}")
            return deny(_safe_alternative(hit, tool="Bash"))
        return 0

    if tool == "Read":
        hit = check_read(tool_input)
        if hit:
            log(f"blocking Read of credential file: {hit}")
            return deny(_safe_alternative(hit, tool="Read"))
        return 0

    if tool == "Grep":
        hit = check_grep(tool_input)
        if hit:
            log(f"blocking Grep content-mode read of credential file: {hit}")
            return deny(_safe_alternative(hit, tool="Grep"))
        return 0

    if tool == "Glob":
        check_glob(tool_input)
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
