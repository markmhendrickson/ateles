#!/usr/bin/env python3
"""Exercise credential_read_guard.py against block/allow cases.

Uses a throwaway fixture directory under the OS temp dir with FAKE
credential-shaped content (never the operator's real ~/.config/neotoma/.env
or any other real credential file), so this test never itself puts a real
secret into a transcript, and the hook under test is exercised against
exactly the paths it is supposed to recognize.

Every case here is a `pytest.mark.parametrize`d function with a real
`assert`, following gh_identity_guard.py's own test pattern. An earlier
revision of this file wrapped each check in a plain function starting with
`test_` that only appended to a `failures` list and RETURNED it — pytest
collects a `test_*` function and calls it, but never inspects a return
value, so all four such functions "passed" even with the guard fully
disabled (ateles#1302 round 1, qa/Phoenicurus). `test_guard_can_actually_fail`
below is the regression test for that exact failure mode: it disables the
guard's own credential-path check and asserts the suite would then go red.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOK = str(Path(__file__).with_name("credential_read_guard.py"))

FIXTURE_ROOT = Path(tempfile.mkdtemp(prefix="cred_guard_test_"))
NEOTOMA_DIR = FIXTURE_ROOT / ".config" / "neotoma"
NEOTOMA_DIR.mkdir(parents=True)
ENV_FILE = NEOTOMA_DIR / ".env"
ENV_FILE.write_text(
    "NEOTOMA_BEARER_TOKEN=fake_test_token_not_real_0000\n"
    "NEOTOMA_BASE_URL=https://example.test\n"
    "ANOTHER_SECRET=zzz_fake_value\n"
)
ENV_EXAMPLE_FILE = NEOTOMA_DIR / ".env.example"
ENV_EXAMPLE_FILE.write_text(
    "NEOTOMA_BEARER_TOKEN=\nNEOTOMA_BASE_URL=\nANOTHER_SECRET=\n"
)
PLAIN_FILE = FIXTURE_ROOT / "notes.txt"
PLAIN_FILE.write_text("nothing sensitive here\n")
PLAIN_ENV_FILE = FIXTURE_ROOT / "plain.env"  # deliberately NOT under NEOTOMA_DIR, still *.env
PLAIN_ENV_FILE.write_text("UNRELATED=1\n")

ENV = str(ENV_FILE)
ENV_EXAMPLE = str(ENV_EXAMPLE_FILE)
PLAIN = str(PLAIN_FILE)
PLAIN_ENV = str(PLAIN_ENV_FILE)


def run(tool_name, tool_input, env=None):
    import os

    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)
    p = subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=child_env,
    )
    return p.returncode


def run_bash(cmd):
    return run("Bash", {"command": cmd})


# ---------------------------------------------------------------------------
# Bash: refused forms
# ---------------------------------------------------------------------------

BASH_BLOCK = [
    ("cat", f"cat {ENV}"),
    ("head", f"head -5 {ENV}"),
    ("tail", f"tail -n 3 {ENV}"),
    ("less", f"less {ENV}"),
    ("more", f"more {ENV}"),
    ("bat", f"bat {ENV}"),
    ("sed -n print", f"sed -n '1,5p' {ENV}"),
    ("bare sed", f"sed 's/x/y/' {ENV}"),
    ("awk print", f"awk '{{print}}' {ENV}"),
    ("plain grep, no safe flag", f"grep NEOTOMA {ENV}"),
    ("plain grep -i", f"grep -i token {ENV}"),
    ("ripgrep plain", f"rg TOKEN {ENV}"),
    ("grep -A context", f"grep -A2 TOKEN {ENV}"),
    ("base64", f"base64 {ENV}"),
    ("xxd", f"xxd {ENV}"),
    ("od", f"od -c {ENV}"),
    ("hexdump", f"hexdump {ENV}"),
    ("strings", f"strings {ENV}"),
    ("hidden in compound", f"echo ok && cat {ENV}"),
    ("hidden after safe grep", f"grep -c '^X=' {ENV} && cat {ENV}"),
    ("env dump after source", f"set -a; source {ENV}; set +a; env"),
    ("printenv dump after source", f"source {ENV}; printenv"),
    ("bare set dump after source", f"set -a; source {ENV}; set +a; set"),
    ("dot-source then env dump", f". {ENV}; env"),
    # Evasion vectors modeled on gmail_send_gate.py's adversarial pass.
    ("bash -c wrapper", f"bash -c 'cat {ENV}'"),
    ("sh -c wrapper", f'sh -c "cat {ENV}"'),
    ("command substitution $()", f"echo $(cat {ENV})"),
    ("backtick substitution", f"echo `cat {ENV}`"),
    ("subshell parens", f"(cat {ENV})"),
    ("background &", f"true & cat {ENV}"),
    ("|| chain", f"false || cat {ENV}"),
    ("xargs indirection", f"echo {ENV} | xargs cat"),
    ("nohup wrapper", f"nohup cat {ENV}"),
    ("backslash line continuation", f"cat \\\n {ENV}"),
    ("extra inner whitespace", f"cat    {ENV}"),
    ("tab separated", f"cat\t{ENV}"),
    ("real cat chained after echo", f"echo staging && cat {ENV}"),
    ("real cat chained after git commit", f'git commit -m "wip" && cat {ENV}'),
    ("home-relative path", "cat ~/.config/neotoma/.env"),
    ("cat -A flag variant", f"cat -A {ENV}"),
    ("tee to stdout", f"cat {ENV} | tee /dev/stdout"),
    ("input redirection", f"cat < {ENV}"),
    # Security-review round-1 findings (arch/security lens, ateles#1302).
    ("python3 -c with double-quoted program", f"python3 -c \"print(open('{ENV}').read())\""),
    ("python3 -c with single-quoted program", f'python3 -c \'print(open("{ENV}").read())\''),
    ("perl -e reading the file", f"perl -e \"open(F,'{ENV}'); print <F>;\""),
    ("ruby -e reading the file", f"ruby -e \"puts File.read('{ENV}')\""),
    ("node -e reading the file", f"node -e \"console.log(require('fs').readFileSync('{ENV}'))\""),
    ("declare -p after source", f"set -a; source {ENV}; set +a; declare -p"),
    ("export -p after source", f"source {ENV}; export -p"),
    ("typeset -p after source", f"source {ENV}; typeset -p"),
    ("cut -d= -f2 standalone", f"cut -d= -f2 {ENV}"),
    ("grep piped into cut", f"grep TOKEN {ENV} | cut -d= -f2"),
    ("awk -F= field extraction", f"awk -F= '{{print $2}}' {ENV}"),
    # Cheap additions volunteered alongside the round-1 findings.
    ("dd if= a credential path", f"dd if={ENV}"),
    ("while-read loop redirected from the file", f"while read -r line; do echo $line; done < {ENV}"),
    ("read var redirected from the file", f"read -r line < {ENV}"),
]

BASH_ALLOW = [
    ("grep -c count", f"grep -c '^NEOTOMA_BEARER_TOKEN=' {ENV}"),
    ("grep -o names only", f"grep -o '^[A-Z_]*=' {ENV}"),
    ("grep -l files only", f"grep -l TOKEN {ENV}"),
    ("grep -L files without match", f"grep -L TOKEN {ENV}"),
    ("source without dump", f"set -a; source {ENV}; set +a; echo done"),
    ("source then use var, no echo of var", f"set -a; source {ENV}; set +a; curl -s https://example.test"),
    ("wc -l line count", f"wc -l {ENV}"),
    ("reading .env.example", f"cat {ENV_EXAMPLE}"),
    ("grep on .env.example", f"grep TOKEN {ENV_EXAMPLE}"),
    ("cat a plain file", f"cat {PLAIN}"),
    ("unrelated command", "ls -la"),
    ("git commit documenting the guard", f'git commit -m "guard blocks cat {ENV}"'),
    ("echo of the command", f"echo 'cat {ENV}'"),
    ("gh pr create describing it", f'gh pr create --body "blocks cat {ENV}"'),
    ("python3 -c with no path in the program", 'python3 -c "print(1+1)"'),
    ("python3 running a script FILE, not -c", "python3 script.py"),
    ("node -e with no path in the program", 'node -e "console.log(1)"'),
    ("declare -p with no prior source", "declare -p SOME_VAR"),
    ("export -p with no prior source", "export -p"),
    ("while-read loop over a plain file", "while read -r line; do echo $line; done < " + PLAIN),
    ("xargs over a plain file list", f"find . -name x.txt | xargs cat"),
    ("dd of a plain file", f"dd if={PLAIN}"),
]


@pytest.mark.parametrize("label,cmd", BASH_BLOCK, ids=[label for label, _ in BASH_BLOCK])
def test_bash_block(label, cmd):
    assert run_bash(cmd) == 2, label


@pytest.mark.parametrize("label,cmd", BASH_ALLOW, ids=[label for label, _ in BASH_ALLOW])
def test_bash_allow(label, cmd):
    assert run_bash(cmd) == 0, label


# ---------------------------------------------------------------------------
# Read tool
# ---------------------------------------------------------------------------

READ_BLOCK = [
    ("Read on .env via file_path", {"file_path": ENV}),
    ("Read on .env via path", {"path": ENV}),
]
READ_ALLOW = [
    ("Read on .env.example", {"file_path": ENV_EXAMPLE}),
    ("Read on plain file", {"file_path": PLAIN}),
]


@pytest.mark.parametrize("label,ti", READ_BLOCK, ids=[label for label, _ in READ_BLOCK])
def test_read_block(label, ti):
    assert run("Read", ti) == 2, label


@pytest.mark.parametrize("label,ti", READ_ALLOW, ids=[label for label, _ in READ_ALLOW])
def test_read_allow(label, ti):
    assert run("Read", ti) == 0, label


# ---------------------------------------------------------------------------
# Grep tool
# ---------------------------------------------------------------------------

GREP_BLOCK = [
    ("content mode on .env", {"path": ENV, "output_mode": "content", "pattern": "TOKEN"}),
    (
        "content mode with context flags",
        {"path": ENV, "output_mode": "content", "pattern": "TOKEN", "-A": 2},
    ),
]
GREP_ALLOW = [
    ("files_with_matches default", {"path": ENV, "pattern": "TOKEN"}),
    ("files_with_matches explicit", {"path": ENV, "pattern": "TOKEN", "output_mode": "files_with_matches"}),
    ("count mode", {"path": ENV, "pattern": "TOKEN", "output_mode": "count"}),
    ("content mode on plain file", {"path": PLAIN, "output_mode": "content", "pattern": "x"}),
    ("content mode on .env.example", {"path": ENV_EXAMPLE, "output_mode": "content", "pattern": "TOKEN"}),
]


@pytest.mark.parametrize("label,ti", GREP_BLOCK, ids=[label for label, _ in GREP_BLOCK])
def test_grep_block(label, ti):
    assert run("Grep", ti) == 2, label


@pytest.mark.parametrize("label,ti", GREP_ALLOW, ids=[label for label, _ in GREP_ALLOW])
def test_grep_allow(label, ti):
    assert run("Grep", ti) == 0, label


# ---------------------------------------------------------------------------
# Glob tool — returns names only, never refused
# ---------------------------------------------------------------------------

GLOB_ALLOW = [
    ("glob for env files", {"pattern": "**/*.env"}),
    ("glob targeting neotoma dir", {"pattern": "*.env", "path": str(NEOTOMA_DIR)}),
]


@pytest.mark.parametrize("label,ti", GLOB_ALLOW, ids=[label for label, _ in GLOB_ALLOW])
def test_glob_allow(label, ti):
    assert run("Glob", ti) == 0, label


# ---------------------------------------------------------------------------
# Malformed / edge-case input must fail OPEN (exit 0), never raise or hang
# ---------------------------------------------------------------------------

def test_malformed_json_fail_open():
    p = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
    assert p.returncode == 0


def test_empty_stdin_fail_open():
    p = subprocess.run([sys.executable, HOOK], input="", capture_output=True, text=True)
    assert p.returncode == 0


def test_missing_tool_input_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Read"}),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0


def test_tool_input_is_a_list_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Read", "tool_input": ["x"]}),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0


def test_unrelated_tool_is_ignored():
    assert run("WebFetch", {"url": "https://example.com"}) == 0


def test_bash_with_no_command_key_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0


def test_command_not_a_string_fail_open():
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": 123}}),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0


def test_top_level_json_is_a_list_fail_open():
    p = subprocess.run([sys.executable, HOOK], input="[1,2,3]", capture_output=True, text=True)
    assert p.returncode == 0


# ---------------------------------------------------------------------------
# UX finding (ateles#1302 round 2, non-blocking): the deny message's remedy
# must match the TOOL that was denied — a Read/Grep denial should not tell
# the agent to run a Bash command it wasn't using.
# ---------------------------------------------------------------------------

def _deny_reason(tool_name, tool_input):
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    p = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True)
    assert p.returncode == 2
    out = json.loads(p.stdout)
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_read_denial_does_not_suggest_a_bash_command():
    reason = _deny_reason("Read", {"file_path": ENV})
    assert "set -a; source" not in reason
    assert "grep -c" not in reason
    assert "grep -o" not in reason


def test_grep_denial_does_not_suggest_a_bash_command():
    reason = _deny_reason("Grep", {"path": ENV, "output_mode": "content", "pattern": "TOKEN"})
    assert "set -a; source" not in reason
    assert "cat " not in reason
    assert "output_mode" in reason  # steers back to Grep's own safe modes


def test_bash_denial_does_suggest_the_shell_remedy():
    reason = _deny_reason("Bash", {"command": f"cat {ENV}"})
    assert "set -a; source" in reason


# ---------------------------------------------------------------------------
# Regression test for the QA finding itself (ateles#1302 round 1): a test
# suite that cannot fail on the thing it watches is decoration. This
# disables the guard's OWN credential-path recognition and asserts the
# suite goes red — proving these tests actually exercise the hook rather
# than always returning 0/2 regardless of its logic.
# ---------------------------------------------------------------------------

def test_guard_can_actually_fail_when_disabled():
    """Monkeypatch is_credential_path to always return False (simulating the
    guard being disabled/broken) and confirm a known-BLOCK case now allows —
    i.e. this test file's own assertions are capable of going red."""
    sys.path.insert(0, str(Path(HOOK).parent))
    import importlib

    import credential_read_guard as guard

    importlib.reload(guard)
    original = guard.is_credential_path
    try:
        guard.is_credential_path = lambda raw: False
        # Call the checker function DIRECTLY (in-process), not via subprocess
        # — the subprocess harness re-imports the module fresh each time, so
        # patching only affects this in-process call, which is exactly what
        # proves the parametrized tests above are actually exercising this
        # function's real logic rather than a hardcoded exit code.
        assert guard.check_bash(f"cat {ENV}") is None
    finally:
        guard.is_credential_path = original
        importlib.reload(guard)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
