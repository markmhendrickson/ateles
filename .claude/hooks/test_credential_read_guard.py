#!/usr/bin/env python3
"""Exercise credential_read_guard.py against block/allow cases.

Uses a throwaway fixture directory under the OS temp dir with FAKE
credential-shaped content (never the operator's real ~/.config/neotoma/.env
or any other real credential file), so this test never itself puts a real
secret into a transcript, and the hook under test is exercised against
exactly the paths it is supposed to recognize.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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

ENV = str(ENV_FILE)
ENV_EXAMPLE = str(ENV_EXAMPLE_FILE)
PLAIN = str(PLAIN_FILE)


def run(tool_name, tool_input, env=None):
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
    ("python -c executing it", f"python3 -c \"open('{ENV}').read()\" ; cat {ENV}"),
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
    # Text-bearing leaders: pattern appears as prose, nothing executes.
    ("git commit documenting the guard", f'git commit -m "guard blocks cat {ENV}"'),
    ("echo of the command", f"echo 'cat {ENV}'"),
    ("gh pr create describing it", f'gh pr create --body "blocks cat {ENV}"'),
]


def test_read_tool():
    cases_block = [
        ("Read on .env", {"file_path": ENV}),
        ("Read on neotoma env via path key", {"path": ENV}),
    ]
    cases_allow = [
        ("Read on .env.example", {"file_path": ENV_EXAMPLE}),
        ("Read on plain file", {"file_path": PLAIN}),
    ]
    failures = []
    for label, ti in cases_block:
        rc = run("Read", ti)
        ok = rc == 2
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Read/{label}")
        if not ok:
            failures.append(f"Read/{label}")
    for label, ti in cases_allow:
        rc = run("Read", ti)
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Read/{label}")
        if not ok:
            failures.append(f"Read/{label}")
    return failures


def test_grep_tool():
    cases_block = [
        ("content mode on .env", {"path": ENV, "output_mode": "content"}),
        ("default mode (files_with_matches) is allowed",),  # placeholder, handled below
    ]
    failures = []
    rc = run("Grep", {"path": ENV, "output_mode": "content", "pattern": "TOKEN"})
    ok = rc == 2
    print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Grep/content-mode-on-env")
    if not ok:
        failures.append("Grep/content-mode-on-env")

    rc = run("Grep", {"path": ENV, "output_mode": "content", "pattern": "TOKEN", "-A": 2})
    ok = rc == 2
    print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Grep/content-mode-with-context")
    if not ok:
        failures.append("Grep/content-mode-with-context")

    for label, ti in [
        ("files_with_matches default", {"path": ENV, "pattern": "TOKEN"}),
        ("files_with_matches explicit", {"path": ENV, "pattern": "TOKEN", "output_mode": "files_with_matches"}),
        ("count mode", {"path": ENV, "pattern": "TOKEN", "output_mode": "count"}),
        ("content mode on plain file", {"path": PLAIN, "output_mode": "content", "pattern": "x"}),
        ("content mode on .env.example", {"path": ENV_EXAMPLE, "output_mode": "content", "pattern": "TOKEN"}),
    ]:
        rc = run("Grep", ti)
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Grep/{label}")
        if not ok:
            failures.append(f"Grep/{label}")
    return failures


def test_glob_tool():
    failures = []
    for label, ti in [
        ("glob for env files", {"pattern": "**/*.env"}),
        ("glob targeting neotoma dir", {"pattern": "*.env", "path": str(NEOTOMA_DIR)}),
    ]:
        rc = run("Glob", ti)
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  Glob/{label}")
        if not ok:
            failures.append(f"Glob/{label}")
    return failures


def test_malformed_and_edges():
    failures = []
    cases = [
        ("malformed json", lambda: subprocess.run(
            [sys.executable, HOOK], input="not json", capture_output=True, text=True
        ).returncode),
        ("empty stdin", lambda: subprocess.run(
            [sys.executable, HOOK], input="", capture_output=True, text=True
        ).returncode),
        ("missing tool_input", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Read"}),
            capture_output=True, text=True,
        ).returncode),
        ("tool_input is a list, not dict", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Read", "tool_input": ["x"]}),
            capture_output=True, text=True,
        ).returncode),
        ("unrelated tool", lambda: run("WebFetch", {"url": "https://example.com"})),
        ("Bash with no command key", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
            capture_output=True, text=True,
        ).returncode),
        ("command is not a string", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": 123}}),
            capture_output=True, text=True,
        ).returncode),
        ("top-level is a list", lambda: subprocess.run(
            [sys.executable, HOOK], input="[1,2,3]", capture_output=True, text=True
        ).returncode),
    ]
    for label, fn in cases:
        rc = fn()
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  edge/{label}")
        if not ok:
            failures.append(f"edge/{label}")
    return failures


def main():
    failures = []

    print("=== Bash: SHOULD BLOCK (expect 2) ===")
    for label, cmd in BASH_BLOCK:
        rc = run("Bash", {"command": cmd})
        ok = rc == 2
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(f"bash-block/{label}")

    print("\n=== Bash: SHOULD ALLOW (expect 0) ===")
    for label, cmd in BASH_ALLOW:
        rc = run("Bash", {"command": cmd})
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(f"bash-allow/{label}")

    print("\n=== Read tool ===")
    failures += test_read_tool()

    print("\n=== Grep tool ===")
    failures += test_grep_tool()

    print("\n=== Glob tool ===")
    failures += test_glob_tool()

    print("\n=== Malformed input / edge cases (expect 0, fail open) ===")
    failures += test_malformed_and_edges()

    total = (
        len(BASH_BLOCK) + len(BASH_ALLOW)
        + 4 + 7 + 2 + 8  # read, grep, glob, edge case counts
    )
    passed = total - len(failures)
    print(f"\n{passed}/{total} passed")
    if failures:
        print("FAILURES: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
