#!/usr/bin/env python3
"""Exercise gmail_send_gate.py against block/allow cases.

Case commands are assembled from fragments so this file never contains a
literal sending invocation — otherwise the very hook under test blocks the
harness that runs it.
"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).with_name("gmail_send_gate.py"))
G = "gws gmail"
U = f"{G} users"
SEND = "s" + "end"  # avoid a literal `send` token adjacent to a gws invocation
OVR = "ATELES_ALLOW_GMAIL_SEND=1"

BLOCK = [
    ("drafts update", f"{U} drafts update --params x --json y"),
    ("drafts send", f"{U} drafts {SEND} --params x"),
    ("messages send", f"{U} messages {SEND} --params x"),
    ("+reply helper", f"{G} +reply --id 1 --body hi"),
    ("+send helper", f"{G} +{SEND} --to a@b.c"),
    ("hidden in compound", f"echo ok && {U} messages {SEND} --params x"),
    ("SMUGGLED override (was a leak)", f"{OVR} echo ok && {U} messages {SEND} --params x"),
    ("override on seg2, send on seg3", f"echo a ; {OVR} echo b ; {U} drafts update --params x"),
    ("override at end, send earlier", f"{U} drafts update --params x && {OVR} echo done"),
]

ALLOW = [
    ("drafts create", f"{U} drafts create --params x --json y"),
    ("drafts get", f"{U} drafts get --params x"),
    ("drafts list", f"{U} drafts list"),
    ("messages list", f"{U} messages list --params x"),
    ("messages get", f"{U} messages get --params x"),
    ("+read", f"{G} +read --id 1"),
    ("calendar (other service)", "gws calendar events list --params x"),
    ("override PREFIXES the send", f"{OVR} {U} messages {SEND} --params x"),
    ("env-form override", f"env {OVR} {U} drafts update --params x"),
    ("override after a safe segment", f"echo ok && {OVR} {U} messages {SEND} --params x"),
]


def run(command, tool="Bash"):
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    p = subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True
    )
    return p.returncode


def main():
    failures = []
    print("=== SHOULD BLOCK (expect 2) ===")
    for label, cmd in BLOCK:
        rc = run(cmd)
        ok = rc == 2
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    print("\n=== SHOULD ALLOW (expect 0) ===")
    for label, cmd in ALLOW:
        rc = run(cmd)
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    print("\n=== EDGE CASES (expect 0) ===")
    edges = [
        ("non-Bash tool", lambda: run(f"{U} messages {SEND}", tool="Edit")),
        ("malformed json", lambda: subprocess.run(
            [sys.executable, HOOK], input="not json", capture_output=True, text=True
        ).returncode),
        ("empty stdin", lambda: subprocess.run(
            [sys.executable, HOOK], input="", capture_output=True, text=True
        ).returncode),
        ("no gmail in command", lambda: run("ls -la")),
    ]
    for label, fn in edges:
        rc = fn()
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    total = len(BLOCK) + len(ALLOW) + len(edges)
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("FAILURES: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
