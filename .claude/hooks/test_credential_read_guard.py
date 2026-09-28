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
import os
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
PLAIN_ENV_FILE = (
    FIXTURE_ROOT / "plain.env"
)  # deliberately NOT under NEOTOMA_DIR, still *.env
PLAIN_ENV_FILE.write_text("UNRELATED=1\n")

ENV = str(ENV_FILE)
ENV_EXAMPLE = str(ENV_EXAMPLE_FILE)
PLAIN = str(PLAIN_FILE)
PLAIN_ENV = str(PLAIN_ENV_FILE)


def run_result(tool_name, tool_input, env=None):
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)
    return subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=child_env,
    )


def run(tool_name, tool_input, env=None):
    return run_result(tool_name, tool_input, env=env).returncode


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
    ("grep -o names prefix plus values", f"grep -o '^[A-Z_]*=.*' {ENV}"),
    (
        "rg only-matching names prefix plus values",
        f"rg --only-matching '^[A-Za-z_][A-Za-z0-9_]*=.+' {ENV}",
    ),
    (
        "grep unsafe pattern plus safe-looking filename token",
        f"grep -o '.*' '^[A-Z_]*=' {ENV}",
    ),
    ("grep count-shaped pattern after option terminator", f"grep -- -c {ENV}"),
    ("grep files-shaped pattern after option terminator", f"grep -- -l {ENV}"),
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
    (
        "python3 -c with double-quoted program",
        f"python3 -c \"print(open('{ENV}').read())\"",
    ),
    (
        "python3 -c with single-quoted program",
        f"python3 -c 'print(open(\"{ENV}\").read())'",
    ),
    ("perl -e reading the file", f"perl -e \"open(F,'{ENV}'); print <F>;\""),
    ("ruby -e reading the file", f"ruby -e \"puts File.read('{ENV}')\""),
    (
        "node -e reading the file",
        f"node -e \"console.log(require('fs').readFileSync('{ENV}'))\"",
    ),
    ("declare -p after source", f"set -a; source {ENV}; set +a; declare -p"),
    ("export -p after source", f"source {ENV}; export -p"),
    ("typeset -p after source", f"source {ENV}; typeset -p"),
    ("cut -d= -f2 standalone", f"cut -d= -f2 {ENV}"),
    ("grep piped into cut", f"grep TOKEN {ENV} | cut -d= -f2"),
    ("awk -F= field extraction", f"awk -F= '{{print $2}}' {ENV}"),
    # Cheap additions volunteered alongside the round-1 findings.
    ("dd if= a credential path", f"dd if={ENV}"),
    (
        "while-read loop redirected from the file",
        f"while read -r line; do echo $line; done < {ENV}",
    ),
    ("read var redirected from the file", f"read -r line < {ENV}"),
    # Security-review round-2 findings (ateles#1302): sourcing a credential
    # file and then printing a SPECIFIC variable — narrower than a bulk
    # env/set dump, and easy to miss because nothing here is a "dump
    # everything" command.
    ("source then echo a variable", f"source {ENV}; echo $VAR"),
    (
        "source then echo the named token var",
        f"source {ENV}; echo $NEOTOMA_BEARER_TOKEN",
    ),
    (
        "set -a source set +a then echo a variable",
        f"set -a; source {ENV}; set +a; echo $X",
    ),
    ("source then printf with format + variable", f'source {ENV}; printf "%s" $VAR'),
    ("source then bare printf of a variable", f"source {ENV}; printf $VAR"),
    ("source then cat here-string of a variable", f"source {ENV}; cat <<< $VAR"),
    ("dot-source then echo a variable", f". {ENV}; echo $VAR"),
    # Cheap, non-blocking addition from the same round-2 review.
    (
        "git diff --no-index against a credential path",
        f"git diff --no-index /dev/null {ENV}",
    ),
    # Security-review round-3 finding (ateles#1302): an agent printed a
    # hosted instance's bearer token by running `env` over a REMOTE shell
    # (`fly ssh console -C "env"`) — the local shell never sourced anything,
    # so every round-1/round-2 check above (all gated on a local `source`)
    # was structurally blind to this. `env`/`printenv` immediately adjacent
    # to a quote character (`-C "env"`) rather than whitespace was itself a
    # sub-bypass, fixed by widening `_ENV_DUMP_RE` to a `\b` boundary.
    ("fly ssh console running env", 'fly ssh console -C "env"'),
    (
        "flyctl ssh console running printenv",
        'flyctl ssh console -a myapp -C "printenv"',
    ),
    ("bare ssh running env", 'ssh host "env"'),
    ("kubectl exec running env", "kubectl exec pod -- env"),
    ("docker exec running env", "docker exec container env"),
    (
        "docker exec wrapped in bash -c running printenv",
        'docker exec -it container bash -c "printenv"',
    ),
    ("ssh reading /proc/*/environ directly", 'ssh host "cat /proc/1/environ"'),
    ("fly ssh console running export -p", 'fly ssh console -C "export -p"'),
    ("ssh running declare -p", 'ssh host "declare -p"'),
    ("ssh running typeset -p", 'ssh host "typeset -p"'),
    # 2026-09-28 recurrence: service and process inspection can expose live
    # credentials without reading a credential file or explicitly requesting
    # an environment dump. These are synthetic command shapes only.
    ("bare local env dump", "env"),
    ("absolute local env dump", "/usr/bin/env"),
    ("command-wrapped local env dump", "command env"),
    ("sudo-wrapped local env dump", "sudo env"),
    ("bare local printenv dump", "printenv"),
    ("targeted printenv still prints a value", "printenv NEOTOMA_BEARER_TOKEN"),
    ("environment dump in command substitution", 'echo "$(env)"'),
    ("printenv dump in backtick substitution", 'echo "`printenv`"'),
    ("environment dump in compact subshell", "(env)"),
    ("environment dump in compact command group", "{env;}"),
    ("environment dump nested in shell wrapper", "bash -c '(env)'"),
    (
        "printenv dump nested in quoted shell wrapper",
        "sh -c 'echo \"$(printenv)\"'",
    ),
    (
        "service dump nested in command substitution",
        'echo "$(systemctl show-environment)"',
    ),
    ("launchctl service dump", "launchctl print gui/501/example.agent"),
    ("launchctl getenv prints a value", "launchctl getenv NEOTOMA_BEARER_TOKEN"),
    ("systemctl service-property dump", "systemctl show example-agent.service"),
    ("systemctl manager environment dump", "systemctl show-environment"),
    ("pgrep full-command match", "pgrep -f example-agent"),
    ("pgrep full-command output", "pgrep -fl example-agent"),
    ("pgrep full-command options after pattern", "pgrep example-agent -fl"),
    ("pgrep long full-command output", "pgrep --full --list-full example-agent"),
    ("bare ps includes a command column", "ps"),
    ("ps aux command lines", "ps aux"),
    ("ps full-format command lines", "ps -ef"),
    ("ps environment and wide command line", "ps eww -p 123"),
    ("ps explicit command column", "ps -p 123 -o command="),
    ("ps explicit args column", "ps -p 123 -o args="),
    ("process tree", "pstree -p 123"),
    ("remote ps environment dump", 'ssh host "ps eww -p 1"'),
    (
        "container full-command discovery",
        "docker exec container pgrep -fl example-agent",
    ),
    ("remote service dump", 'ssh host "launchctl print system/example.agent"'),
]

BASH_ALLOW = [
    ("grep -c count", f"grep -c '^NEOTOMA_BEARER_TOKEN=' {ENV}"),
    ("grep -o names only", f"grep -o '^[A-Z_]*=' {ENV}"),
    (
        "grep long names-only mode",
        f"grep --only-matching '^[A-Za-z_][A-Za-z0-9_]*=' {ENV}",
    ),
    ("grep -l files only", f"grep -l TOKEN {ENV}"),
    ("grep -L files without match", f"grep -L TOKEN {ENV}"),
    ("source without dump", f"set -a; source {ENV}; set +a; echo done"),
    (
        "source then use var, no echo of var",
        f"set -a; source {ENV}; set +a; curl -s https://example.test",
    ),
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
    (
        "while-read loop over a plain file",
        "while read -r line; do echo $line; done < " + PLAIN,
    ),
    ("xargs over a plain file list", "find . -name x.txt | xargs cat"),
    ("dd of a plain file", f"dd if={PLAIN}"),
    # The SANCTIONED idiom (round-2 review, explicitly required as a test):
    # source, then run a PROGRAM that consumes the variables — never echo,
    # printf, or cat<<< them directly.
    (
        "set -a source set +a THEN run a program (sanctioned idiom)",
        f"set -a; source {ENV}; set +a && python3 script.py",
    ),
    ("source then run a program directly", f"source {ENV}; python3 script.py"),
    (
        "source then use a variable inside a program's own argument",
        f'source {ENV}; curl -s https://example.test -H "Authorization: Bearer $TOKEN"',
    ),
    ("echo a variable with no prior credential source", "echo $HOME"),
    ("printf a variable with no prior credential source", 'printf "%s" $HOME'),
    (
        "echo literal text, no variable, after source",
        f"set -a; source {ENV}; set +a; echo done",
    ),
    (
        "git commit documenting the round-2 fix",
        'git commit -m "fix: block source; echo $VAR"',
    ),
    (
        "git diff --no-index between two plain files",
        "git diff --no-index /tmp/a.txt /tmp/b.txt",
    ),
    # Round-3 allow-list: boolean checks and label-only output over a remote
    # shell must NOT be refused — none of them dump an environment, and the
    # operator explicitly required these as tests.
    ("ssh running a harmless command", 'ssh host "echo hello"'),
    ("ssh running ls", 'ssh host "ls -la"'),
    (
        "fly ssh console with a boolean bracket test",
        'fly ssh console -C "[ -n \\"$SECRET_KEY\\" ] && echo present || echo missing"',
    ),
    ("ssh with a POSIX test -n check", 'ssh host "test -n \\"$VAR\\" && echo yes"'),
    ("kubectl exec running a harmless command", "kubectl exec pod -- ls /app"),
    ("local env assignment running a program", "env SAFE_MODE=1 python3 script.py"),
    ("absolute env running a program", "/usr/bin/env SAFE_MODE=1 python3 script.py"),
    ("launchctl list is identity/status only", "launchctl list"),
    (
        "launchctl print-disabled is not a service dump",
        "launchctl print-disabled system",
    ),
    (
        "systemctl fixed-label liveness check",
        "systemctl is-active example-agent.service",
    ),
    ("pgrep exact process name", "pgrep -x example-agent"),
    ("pgrep process names only", "pgrep -l example-agent"),
    ("ps explicit pid and executable identity", "ps -p 123 -o pid=,comm="),
    ("ps explicit pid and state", "ps -p 123 -o pid=,state=,etime="),
    ("docker exec identity-only ps", "docker exec container ps -p 1 -o pid=,comm="),
    (
        "local variable presence check prints only a fixed label",
        '[ -n "${NEOTOMA_BEARER_TOKEN:-}" ] && echo present || echo missing',
    ),
    ("echo process-inspection text", "echo 'ps aux is blocked'"),
    ("commit message naming process inspection", 'git commit -m "block pgrep -fl"'),
    ("unrelated hyphenated env command", "my-env --version"),
    ("unrelated hyphenated printenv command", "my-printenv --version"),
    ("unrelated hyphenated pgrep command", "my-pgrep -fl example-agent"),
    ("unrelated hyphenated ps command", "my-ps aux"),
    ("ssh-keygen is not ssh", "ssh-keygen -t ed25519"),
    ("ssh-add is not ssh", "ssh-add ~/.ssh/id_ed25519"),
    (
        "fly ssh console with a case statement printing only a label",
        'fly ssh console -C "case \\"$ENVIRONMENT\\" in prod) echo PROD;; *) echo OTHER;; esac"',
    ),
]


@pytest.mark.parametrize(
    "label,cmd", BASH_BLOCK, ids=[label for label, _ in BASH_BLOCK]
)
def test_bash_block(label, cmd):
    assert run_bash(cmd) == 2, label


@pytest.mark.parametrize(
    "label,cmd", BASH_ALLOW, ids=[label for label, _ in BASH_ALLOW]
)
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
    (
        "content mode on .env",
        {"path": ENV, "output_mode": "content", "pattern": "TOKEN"},
    ),
    (
        "content mode with context flags",
        {"path": ENV, "output_mode": "content", "pattern": "TOKEN", "-A": 2},
    ),
]
GREP_ALLOW = [
    ("files_with_matches default", {"path": ENV, "pattern": "TOKEN"}),
    (
        "files_with_matches explicit",
        {"path": ENV, "pattern": "TOKEN", "output_mode": "files_with_matches"},
    ),
    ("count mode", {"path": ENV, "pattern": "TOKEN", "output_mode": "count"}),
    (
        "content mode on plain file",
        {"path": PLAIN, "output_mode": "content", "pattern": "x"},
    ),
    (
        "content mode on .env.example",
        {"path": ENV_EXAMPLE, "output_mode": "content", "pattern": "TOKEN"},
    ),
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
    p = subprocess.run(
        [sys.executable, HOOK], input="not json", capture_output=True, text=True
    )
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
    p = subprocess.run(
        [sys.executable, HOOK], input="[1,2,3]", capture_output=True, text=True
    )
    assert p.returncode == 0


# ---------------------------------------------------------------------------
# UX finding (ateles#1302 round 2, non-blocking): the deny message's remedy
# must match the TOOL that was denied — a Read/Grep denial should not tell
# the agent to run a Bash command it wasn't using.
# ---------------------------------------------------------------------------


def _deny_reason(tool_name, tool_input):
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    p = subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True
    )
    assert p.returncode == 2
    out = json.loads(p.stdout)
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_read_denial_does_not_suggest_a_bash_command():
    reason = _deny_reason("Read", {"file_path": ENV})
    assert "set -a; source" not in reason
    assert "grep -c" not in reason
    assert "grep -o" not in reason


def test_grep_denial_does_not_suggest_a_bash_command():
    reason = _deny_reason(
        "Grep", {"path": ENV, "output_mode": "content", "pattern": "TOKEN"}
    )
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


def test_process_discovery_never_emits_synthetic_argv_canary():
    """A real child carries a fake credential-shaped argv value, while every
    supported broad discovery entrance is refused before the shell can return
    that child's command line or environment to the caller."""
    canary = "NEOTOMA_BEARER_TOKEN=synthetic_process_canary_not_real"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", canary],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        commands = (
            f"pgrep -fl {child.pid}",
            f"ps eww -p {child.pid}",
            f"ps -p {child.pid} -o command=",
            f"ps -p {child.pid} -o args=",
            "pstree",
        )
        for command in commands:
            result = run_result("Bash", {"command": command})
            combined = result.stdout + result.stderr
            assert result.returncode == 2, command
            assert canary not in combined, command

        identity_command = f"ps -p {child.pid} -o pid=,comm="
        guarded_identity = run_result("Bash", {"command": identity_command})
        assert guarded_identity.returncode == 0
        assert canary not in guarded_identity.stdout + guarded_identity.stderr
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_value_matching_grep_bypass_has_a_real_canary_effect_then_is_blocked():
    """Prove the rejected suffix prints fixture values before trusting the guard."""
    canary = "fake_test_token_not_real_0000"
    command = f"grep -o '^[A-Z_]*=.*' {ENV}"

    unguarded = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=True
    )
    assert canary in unguarded.stdout

    guarded = run_result("Bash", {"command": command})
    assert guarded.returncode == 2
    assert canary not in guarded.stdout + guarded.stderr


def test_nested_environment_dump_has_a_real_canary_effect_then_is_blocked():
    """Prove a compact substitution dumps values before trusting the boundary check."""
    variable = "CREDENTIAL_GUARD_SYNTHETIC_CANARY"
    canary = "synthetic_environment_canary_not_real"
    command = 'printf "%s\\n" "$(env)"'
    synthetic_env = dict(os.environ)
    synthetic_env[variable] = canary

    unguarded = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        check=True,
        env=synthetic_env,
    )
    assert f"{variable}={canary}" in unguarded.stdout

    guarded = run_result("Bash", {"command": command}, env={variable: canary})
    assert guarded.returncode == 2
    assert canary not in guarded.stdout + guarded.stderr


def test_exact_names_only_and_count_modes_never_emit_fixture_canary():
    """The two permitted grep modes remain useful and value-free end to end."""
    canary = "fake_test_token_not_real_0000"
    commands = (
        f"grep -o '^[A-Z_]*=' {ENV}",
        f"grep --only-matching '^[A-Za-z_][A-Za-z0-9_]*=' {ENV}",
        f"grep -c '^NEOTOMA_BEARER_TOKEN=' {ENV}",
    )
    for command in commands:
        assert run_bash(command) == 0, command
        actual = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=True
        )
        assert canary not in actual.stdout + actual.stderr, command


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
