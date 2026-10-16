#!/usr/bin/env python3
"""Exercise git_stash_guard.py against block/allow cases.

Case commands are assembled from fragments so this file never contains a
literal `git stash` invocation adjacent to a real git binary — and, more to
the point, so running this suite never actually stashes anything. Every case
is synthetic input piped to the hook's stdin; the hook itself runs no git.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).with_name("git_stash_guard.py"))
S = "st" + "ash"  # avoid a literal `git stash` token in this file's own text
GS = f"git {S}"
OVR = "ATELES_ALLOW_GIT_STASH=1"

BLOCK = [
    # --- the mutating subcommands ---
    ("bare (implicit push)", GS),
    ("push", f"{GS} push"),
    ("push -m", f"{GS} push -m wip"),
    ("save", f"{GS} save wip"),
    ("pop", f"{GS} pop"),
    ("apply", f"{GS} apply"),
    ("apply with ref", f"{GS} apply {S}@{{0}}"),
    ("drop", f"{GS} drop"),
    ("clear", f"{GS} clear"),
    ("branch", f"{GS} branch recovery"),
    ("create", f"{GS} create"),
    ("store", f"{GS} store -m wip deadbeef"),
    ("bare with -u flag", f"{GS} -u"),
    ("bare with --include-untracked", f"{GS} --include-untracked"),
    # --- git-level path/dir flags ---
    ("-C path", f"git -C /some/repo {S} pop"),
    ("--git-dir=", f"git --git-dir=/some/repo/.git {S} push"),
    ("--git-dir space form", f"git --git-dir /some/repo/.git {S}"),
    ("--work-tree", f"git --work-tree /some/repo {S} pop"),
    ("-c config then stash", f"git -c core.pager=cat {S} drop"),
    # --- compound commands ---
    ("cd then stash", f"cd foo && {GS}"),
    ("hidden after innocuous segment", f"ls -la && {GS} pop"),
    ("semicolon chain", f"true ; {GS} push"),
    ("newline chain", f"ls\n{GS} pop"),
    ("|| chain", f"false || {GS} pop"),
    ("background &", f"true & {GS} pop"),
    ("pipe chain", f"echo x | {GS} store"),
    # --- interpreters must NOT be exempt (the gmail-gate bypass class) ---
    ("python3 -c executing it", f"python3 -c \"subprocess.run(['git','{S}'])\""),
    ("python3 -c argv list with subcommand", f"python3 -c \"subprocess.run(['git','{S}','pop'])\""),
    ("node execFileSync argv form", f'node -e \'execFileSync("git", ["{S}", "drop"])\''),
    ("python3 -c shell string form", f"python3 -c \"os.system('{GS} pop')\""),
    ("node -e executing it", f"node -e '{GS} pop'"),
    ("sh -c wrapper", f"sh -c '{GS}'"),
    ("bash -c wrapper", f'bash -c "{GS} pop"'),
    ("cat heredoc then real stash", f"cat <<EOF\nnote\nEOF\n{GS} pop"),
    # --- other evasion shapes ---
    ("backslash line continuation", f"git {S} \\\n pop"),
    ("command substitution $()", f"$({GS} create)"),
    ("backtick substitution", f"`{GS} create`"),
    ("subshell parens", f"({GS} pop)"),
    ("absolute path to git", f"/usr/bin/git {S} pop"),
    ("relative path to git", f"./git {S} pop"),
    ("nohup wrapper", f"nohup {GS} pop"),
    ("env non-override prefix", f"env FOO=1 {GS} pop"),
    ("xargs indirection", f"echo x | xargs git {S}"),
    ("extra inner whitespace", f"git    {S}     pop"),
    ("tab separated", f"git\t{S}\tpop"),
    # --- override must not leak across segments ---
    ("SMUGGLED override on earlier segment", f"{OVR} echo ok && {GS} pop"),
    ("override on seg2, stash on seg3", f"echo a ; {OVR} echo b ; {GS} drop"),
    ("override at end, stash earlier", f"{GS} pop && {OVR} echo done"),
    # --- text-bearing leaders excuse only their OWN segment ---
    ("real stash chained after echo", f"echo staging && {GS} pop"),
    ("real stash chained after git commit", f'git commit -m "wip" && {GS} pop'),
    ("real stash chained after grep", f"grep -q x file && {GS} drop"),
    ("real stash chained after gh pr create", f'gh pr create --body "blocks {GS}" && {GS} clear'),
    # --- a read-only subcommand does not shield a mutating one ---
    ("list then pop", f"{GS} list && {GS} pop"),
]

ALLOW = [
    # --- the deliberate read-only carve-out ---
    ("list", f"{GS} list"),
    ("list with format (the recovery command)", f"{GS} list --format='%H %gs'"),
    ("show", f"{GS} show"),
    ("show -p with ref", f"{GS} show -p {S}@{{0}}"),
    ("list chained after cd", f"cd foo && {GS} list"),
    # --- unrelated git and non-git commands ---
    ("git status", "git status"),
    ("git commit", "git commit -am wip"),
    ("git log", "git log --oneline -5"),
    ("no stash at all", "ls -la"),
    ("word 'stash' in an unrelated path", "ls /tmp/stashed-files"),
    ("stash as a non-git argument", "grep -rn stash docs/"),
    # --- override PREFIXES the stashing segment ---
    ("override prefixes bare", f"{OVR} {GS}"),
    ("override prefixes pop", f"{OVR} {GS} pop"),
    ("env-form override", f"env {OVR} {GS} drop"),
    ("override after a safe segment", f"echo ok && {OVR} {GS} pop"),
    # --- text-bearing commands: the pattern is prose, nothing is invoked ---
    ("git commit documenting the command", f'git commit -m "block {GS} in this repo"'),
    ("echo of the command", f"echo '{GS} pop'"),
    ("grep for the pattern", f"grep -rn '{GS}' docs/"),
    ("gh pr create describing it", f'gh pr create --body "hook blocks {GS} pop"'),
    ("printf of the command", f"printf '%s\\n' '{GS} drop'"),
    # The argv-list pattern must stay narrow enough that prose with real words
    # between the two quoted tokens does not match.
    ("prose with words between the tokens",
     f"""echo "the 'git' program has a '{S}' subcommand" """),
    # A read-only subcommand in argv form gets the same carve-out as the
    # whitespace form.
    ("argv-form list is read-only", f"python3 -c \"subprocess.run(['git','{S}','list'])\""),
]

# An EXPORTED override must NOT approve anything. Honouring an ambient env var
# would let one `export` silently approve every stash for the rest of the
# session — the "approval carries forward" failure the inline-prefix design
# exists to close. These run the hook with the variable actually set in its
# environment, which the inline-prefix cases cannot exercise.
EXPORTED = [
    ("exported override + bare", GS),
    ("exported override + pop", f"{GS} pop"),
    ("exported override + drop", f"{GS} drop"),
]


def run(command, tool="Bash", env=None):
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
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

    print("\n=== EXPORTED OVERRIDE MUST NOT APPROVE (expect 2) ===")
    for label, cmd in EXPORTED:
        rc = run(cmd, env={"ATELES_ALLOW_GIT_STASH": "1"})
        ok = rc == 2
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    print("\n=== FAIL OPEN ON MALFORMED / IRRELEVANT INPUT (expect 0) ===")
    edges = [
        ("non-Bash tool", lambda: run(f"{GS} pop", tool="Edit")),
        ("malformed json", lambda: subprocess.run(
            [sys.executable, HOOK], input="not json", capture_output=True, text=True
        ).returncode),
        ("empty stdin", lambda: subprocess.run(
            [sys.executable, HOOK], input="", capture_output=True, text=True
        ).returncode),
        ("null tool_input", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Bash", "tool_input": None}),
            capture_output=True, text=True,
        ).returncode),
        ("command is not a string", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": 42}}),
            capture_output=True, text=True,
        ).returncode),
        ("missing tool_name", lambda: subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_input": {"command": f"{GS} pop"}}),
            capture_output=True, text=True,
        ).returncode),
        ("json array instead of object", lambda: subprocess.run(
            [sys.executable, HOOK], input="[1,2,3]", capture_output=True, text=True
        ).returncode),
    ]
    for label, fn in edges:
        rc = fn()
        ok = rc == 0
        print(f"  [{'ok' if ok else 'FAIL'}] exit={rc}  {label}")
        if not ok:
            failures.append(label)

    total = len(BLOCK) + len(ALLOW) + len(EXPORTED) + len(edges)
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("FAILURES: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
