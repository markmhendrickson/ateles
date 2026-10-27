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
import shutil
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
    "client_secret=synthetic_attached_pattern_canary_not_real\n"
    "ANOTHER_SECRET=zzz_fake_value\n"
)
ENV_EXAMPLE_FILE = NEOTOMA_DIR / ".env.example"
ENV_EXAMPLE_FILE.write_text(
    "NEOTOMA_BEARER_TOKEN=\nNEOTOMA_BASE_URL=\nANOTHER_SECRET=\n"
)
NEOTOMA_OTHER_FILE = NEOTOMA_DIR / "config.json"
NEOTOMA_OTHER_FILE.write_text('{"synthetic": "not-a-real-value"}\n')
LAUNCH_AGENTS_DIR = FIXTURE_ROOT / "Library" / "LaunchAgents"
LAUNCH_AGENTS_DIR.mkdir(parents=True)
PLIST_FILE = LAUNCH_AGENTS_DIR / "com.example.agent.plist"
PLIST_FILE.write_text("<plist><dict>synthetic-not-real</dict></plist>\n")
PLAIN_FILE = FIXTURE_ROOT / "notes.txt"
PLAIN_FILE.write_text("nothing sensitive here\n")
PLAIN_ENV_FILE = (
    FIXTURE_ROOT / "plain.env"
)  # deliberately NOT under NEOTOMA_DIR, still *.env
PLAIN_ENV_FILE.write_text("UNRELATED=1\n")

ENV = str(ENV_FILE)
ENV_EXAMPLE = str(ENV_EXAMPLE_FILE)
PLAIN = str(PLAIN_FILE)
NEOTOMA_OTHER = str(NEOTOMA_OTHER_FILE)
PLIST = str(PLIST_FILE)
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
    ("ripgrep -L follows symlinks and prints content", f"rg -L TOKEN {ENV}"),
    ("grep attached regexp operand", f"grep -eclient_secret {ENV}"),
    ("ripgrep attached regexp operand", f"rg -eclient_secret {ENV}"),
    ("grep separate regexp operand", f"grep -e client_secret {ENV}"),
    ("ripgrep separate regexp operand", f"rg -e client_secret {ENV}"),
    ("grep ambiguous count and files modes", f"grep -cl TOKEN {ENV}"),
    ("ripgrep unrecognized json output mode", f"rg --json TOKEN {ENV}"),
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
    # Security-review round-5 finding (ateles#1302, Falco, BLOCKING):
    # _extract_paths()'s wrapper-punctuation strip previously ran as two
    # sequential single-charset passes (quotes, then brackets/backticks/
    # dollar), so a token whose trailing characters INTERLEAVE both
    # charsets survived only partially cleaned — `echo "$(cat "<cred>")"`
    # whitespace-splits to a trailing token ending in `)"`, which a
    # quote-only pass then a bracket-only pass each strip one character of
    # and stop, leaving a dangling quote glued to the path. That defeated
    # the credential-glob match entirely, silently disabling every
    # downstream content-dump check for the whole command. Fixed by
    # stripping the combined charset to a fixed point
    # (`_strip_wrapper_punctuation`). Covers the exact reported shapes
    # (cat, head, grep) plus the sibling shapes the fix must also close:
    # backticks and process substitution.
    (
        "double-quoted command substitution wrapping a double-quoted path (cat)",
        f'echo "$(cat "{ENV}")"',
    ),
    (
        "captured-variable double-quoted substitution then echoed (cat)",
        f'result=$(cat "{ENV}"); echo "$result"',
    ),
    (
        "double-quoted command substitution wrapping a double-quoted path (head)",
        f'echo "$(head -3 "{ENV}")"',
    ),
    (
        "double-quoted command substitution wrapping a double-quoted path (grep)",
        f'echo "$(grep NEOTOMA "{ENV}")"',
    ),
    (
        "printf wrapping a double-quoted command substitution (cat)",
        f'printf "%s" "$(cat "{ENV}")"',
    ),
    (
        "double-quoted command substitution wrapping a single-quoted path (cat)",
        f"echo \"$(cat '{ENV}')\"",
    ),
    (
        "backtick substitution wrapping a double-quoted path (cat)",
        f'echo `cat "{ENV}"`',
    ),
    (
        "process substitution wrapped in command substitution (cat)",
        f'echo "$(cat <({ENV}))"',
    ),
    (
        "nested process substitution (cat)",
        f'cat <(cat "{ENV}")',
    ),
    # Security-review round-5 finding (ateles#1302, Falco, NON-BLOCKING):
    # `column` is a plain unrecognized-reader-binary gap — it reformats and
    # prints a credential file's content unconditionally, with no safe
    # flag-free invocation, and was simply absent from _CONTENT_DUMP_CMDS.
    ("column reformats and prints file content", f"column {ENV}"),
    # --- Credential containment tightening -------------------------------
    # Process-environment listings. The first group was already refused by the
    # full-command checks; these pin the evasion spellings so a refactor of
    # the option parser cannot silently reopen them.
    ("pgrep -l then -f as separate flags", "pgrep -l -f example-agent"),
    ("pgrep clustered -lf", "pgrep -lf example-agent"),
    ("pgrep clustered -fil", "pgrep -fil example-agent"),
    ("pgrep -af", "pgrep -af example-agent"),
    ("absolute-path pgrep -fl", "/usr/bin/pgrep -fl example-agent"),
    ("pgrep behind env wrapper", "env FOO=1 pgrep -fl example-agent"),
    ("pgrep behind command substitution", 'echo "$(pgrep -fl example-agent)"'),
    ("pgrep behind xargs", "echo example-agent | xargs pgrep -fl"),
    ("ps -E environment flag", "ps -E -p 123"),
    ("ps -E with a safe field list still prints env", "ps -E -p 123 -o pid=,comm="),
    ("ps clustered -Eww", "ps -Eww -p 123"),
    ("ps BSD e modifier", "ps e -p 123"),
    ("ps BSD e modifier after the pid", "ps -p 123 e"),
    ("ps BSD wwe cluster", "ps wwe -p 123"),
    ("ps eww behind sudo", "sudo ps eww -p 123"),
    ("ps eww behind command wrapper", "command ps eww -p 123"),
    ("ps eww behind command substitution", 'echo "$(ps eww -p 123)"'),
    ("ps eww behind xargs", "echo 123 | xargs ps eww -p"),
    # Shell tracing prints every expanded assignment and command, so it
    # re-creates an environment dump from inside a sourcing command.
    ("set -x before sourcing", f"set -x; source {ENV}"),
    ("set -x after sourcing", f"source {ENV}; set -x; curl -s https://example.test"),
    ("set -o xtrace before sourcing", f"set -o xtrace; source {ENV}"),
    ("set -oxtrace attached form", f"set -oxtrace; source {ENV}"),
    ("set clustered -ax", f"set -ax; source {ENV}; set +a"),
    ("set separate flags -a -x", f"set -a -x; source {ENV}; set +a"),
    ("set clustered -eux", f"set -eux; . {ENV}; python3 script.py"),
    ("set -vx", f"set -vx; source {ENV}"),
    ("set -x on its own line", f"set -x\nsource {ENV}\npython3 script.py"),
    ("set -x combined with -o pipefail", f"set -x -o pipefail; source {ENV}"),
    ("zsh setopt xtrace", f"setopt xtrace; source {ENV}"),
    ("zsh setopt XTRACE", f"setopt XTRACE; source {ENV}"),
    (
        "shell -x flag around a sourcing wrapper",
        f"bash -x -c 'source {ENV}; python3 script.py'",
    ),
    (
        "zsh -x flag around a sourcing wrapper",
        f"zsh -x -c '. {ENV}; python3 script.py'",
    ),
    ("SHELLOPTS xtrace assignment", f"SHELLOPTS=xtrace; source {ENV}"),
    ("set -x with a sourced *.env file", f"set -x; source {PLAIN_ENV}"),
    # Launchd job definitions carry a service's environment; listings and
    # plist printers expose it exactly as an environment dump does.
    ("cat of a LaunchAgents plist", f"cat {PLIST}"),
    ("plutil -p of a LaunchAgents plist", f"plutil -p {PLIST}"),
    ("plutil convert to stdout", f"plutil -convert xml1 -o - {PLIST}"),
    (
        "plutil extract the environment dictionary",
        f"plutil -extract EnvironmentVariables xml1 -o - {PLIST}",
    ),
    ("PlistBuddy print", f"/usr/libexec/PlistBuddy -c Print {PLIST}"),
    ("defaults read of a plist", f"defaults read {PLIST}"),
    ("grep content of a plist", f"grep -i token {PLIST}"),
    (
        "launchctl list with a job label prints its dictionary",
        "launchctl list com.example.agent",
    ),
    ("launchctl dumpstate", "launchctl dumpstate"),
    ("launchctl procinfo", "launchctl procinfo 123"),
    (
        "launchctl list with a label behind sudo",
        "sudo launchctl list com.example.agent",
    ),
    # Any other file in the credential directory, not only the dotenv files.
    ("cat of a non-dotenv file in the credential directory", f"cat {NEOTOMA_OTHER}"),
    (
        "head of a non-dotenv file in the credential directory",
        f"head -3 {NEOTOMA_OTHER}",
    ),
    # `awk ... getline < <path>` reaches the same sink as the tokenizer
    # defect above (the path lives inside the awk program string, corrupted
    # by the same interleaved quote/paren stripping) — already caught by
    # _SED_AWK_RE matching bare `awk`, but only once _extract_paths can
    # actually recognize the path; regression-covered here so a future
    # tokenizer change can't silently reopen it.
    (
        "awk getline reads the file via its program string",
        f'awk \'BEGIN{{while((getline line < "{ENV}") > 0) print line}}\'',
    ),
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
    ("ripgrep -c count", f"rg -c TOKEN {ENV}"),
    ("ripgrep -l files only", f"rg -l TOKEN {ENV}"),
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
    # --- Credential containment tightening: must stay allowed -------------
    ("set -x without any credential source", "set -x; echo hello"),
    (
        "set +x turns tracing off before sourcing",
        f"set +x; source {ENV}; python3 script.py",
    ),
    ("set -e then source then a program", f"set -e; source {ENV}; python3 script.py"),
    (
        "set -euo pipefail then source then a program",
        f"set -euo pipefail; source {ENV}; python3 script.py",
    ),
    (
        "set -o pipefail is not tracing",
        f"set -o pipefail; source {ENV}; python3 script.py",
    ),
    ("commit message naming set -x", 'git commit -m "refuse set -x with source"'),
    ("echo of set -x text", f"echo 'set -x; source {ENV}'"),
    ("bash -c without tracing", f"bash -c 'source {ENV}; python3 script.py'"),
    ("listing a LaunchAgents directory", f"ls -la {LAUNCH_AGENTS_DIR}"),
    ("launchctl list with no label", "launchctl list"),
    ("counting matches in a plist", f"grep -c token {PLIST}"),
    (
        "reading a template in the credential directory",
        f"cat {NEOTOMA_DIR}/.env.example",
    ),
    ("pgrep exact name still allowed", "pgrep -x example-agent"),
    ("ps identity fields still allowed", "ps -p 123 -o pid=,comm="),
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
    ("Read on a LaunchAgents plist", {"file_path": PLIST}),
    (
        "Read on a non-dotenv file in the credential directory",
        {"file_path": NEOTOMA_OTHER},
    ),
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
    (
        "content mode on a LaunchAgents plist",
        {"path": PLIST, "output_mode": "content", "pattern": "TOKEN"},
    ),
    (
        "content mode on a non-dotenv file in the credential directory",
        {"path": NEOTOMA_OTHER, "output_mode": "content", "pattern": "x"},
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
        "count mode on a LaunchAgents plist",
        {"path": PLIST, "pattern": "TOKEN", "output_mode": "count"},
    ),
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


@pytest.mark.parametrize(
    "command,canary",
    [
        (
            f'echo "$(cat "{ENV}")"',
            "fake_test_token_not_real_0000",
        ),
        (
            f'echo "$(head -3 "{ENV}")"',
            "fake_test_token_not_real_0000",
        ),
        (
            f'echo "$(grep NEOTOMA "{ENV}")"',
            "fake_test_token_not_real_0000",
        ),
    ],
    ids=["nested-quote-cat", "nested-quote-head", "nested-quote-grep"],
)
def test_interleaved_wrapper_punctuation_bypass_has_real_effect_then_is_blocked(
    command, canary
):
    """Round-5 finding (ateles#1302, Falco, BLOCKING): prove the ordinary
    `echo "$(<reader> "<cred>")"` shape actually printed fixture content
    before the tokenizer fix, then prove it is refused now.

    This is the live-demonstrated regression for `_extract_paths()`'s
    two-pass, single-charset punctuation strip: the trailing `)"` on the
    whitespace-split path token survived only partially cleaned, which
    silently disabled the credential-glob match — and therefore every
    downstream content-dump check — for this entirely ordinary command
    shape, across cat, head, and grep alike.
    """
    unguarded = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=True
    )
    assert canary in unguarded.stdout

    guarded = run_result("Bash", {"command": command})
    assert guarded.returncode == 2
    assert canary not in guarded.stdout + guarded.stderr


def test_column_bypass_has_a_real_canary_effect_then_is_blocked():
    """Round-5 finding (ateles#1302, Falco, NON-BLOCKING): `column` was a
    plain unrecognized-reader-binary gap, absent from _CONTENT_DUMP_CMDS."""
    canary = "fake_test_token_not_real_0000"
    command = f"column {ENV}"

    if shutil.which("column"):
        unguarded = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=True
        )
        assert canary in unguarded.stdout

    guarded = run_result("Bash", {"command": command})
    assert guarded.returncode == 2
    assert canary not in guarded.stdout + guarded.stderr


def test_awk_getline_bypass_has_a_real_canary_effect_then_is_blocked():
    """Round-5 finding (ateles#1302, Falco, NON-BLOCKING): the path inside
    an awk program string was corrupted by the same tokenizer defect as the
    main finding above; regression-covered separately so a future tokenizer
    change can't silently reopen this call shape even though _SED_AWK_RE
    already matches bare `awk`."""
    canary = "fake_test_token_not_real_0000"
    command = f'awk \'BEGIN{{while((getline line < "{ENV}") > 0) print line}}\''

    unguarded = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=True
    )
    assert canary in unguarded.stdout

    guarded = run_result("Bash", {"command": command})
    assert guarded.returncode == 2
    assert canary not in guarded.stdout + guarded.stderr


@pytest.mark.parametrize(
    "command,canary",
    [
        (
            f"rg -L TOKEN {ENV}",
            "fake_test_token_not_real_0000",
        ),
        (
            f"grep -eclient_secret {ENV}",
            "synthetic_attached_pattern_canary_not_real",
        ),
        (
            f"rg -eclient_secret {ENV}",
            "synthetic_attached_pattern_canary_not_real",
        ),
    ],
    ids=["ripgrep-follow-symlinks", "grep-attached-regexp", "ripgrep-attached-regexp"],
)
def test_command_specific_grep_bypasses_have_real_effect_then_are_blocked(
    command, canary
):
    """Prove each command prints a synthetic value, then prove pre-exec denial.

    The effect half needs the real binary. Where it is not installed (ripgrep is
    absent on the CI runner) that half cannot run, but the denial half still
    must: the guard refuses the command whether or not the binary exists.
    """
    if shutil.which(command.split()[0]):
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
    """Every permitted grep/rg output mode stays useful and value-free.

    The guard-permits-it assertion (`run_bash(command) == 0`) needs no real
    binary — it only exercises the hook's own text-based classifier. The
    effect half below it (actually running the command to prove it stays
    value-free) needs the real binary on PATH, which `rg` is not guaranteed
    to be on every CI runner; that half is skipped, per binary, exactly like
    `test_command_specific_grep_bypasses_have_real_effect_then_are_blocked`."""
    canaries = (
        "fake_test_token_not_real_0000",
        "synthetic_attached_pattern_canary_not_real",
        "zzz_fake_value",
    )
    commands = (
        f"grep -o '^[A-Z_]*=' {ENV}",
        f"grep --only-matching '^[A-Za-z_][A-Za-z0-9_]*=' {ENV}",
        f"grep -c '^NEOTOMA_BEARER_TOKEN=' {ENV}",
        f"grep -l TOKEN {ENV}",
        f"grep -L NOT_PRESENT {ENV}",
        f"rg -c TOKEN {ENV}",
        f"rg -l TOKEN {ENV}",
        f"grep -ceclient_secret {ENV}",
        f"grep -leclient_secret {ENV}",
        f"rg -ceclient_secret {ENV}",
        f"rg -leclient_secret {ENV}",
        f"grep --count --regexp=client_secret {ENV}",
        f"rg --count --regexp=client_secret {ENV}",
    )
    for command in commands:
        assert run_bash(command) == 0, command
        if not shutil.which(command.split()[0]):
            continue
        actual = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=False
        )
        assert actual.returncode in {0, 1}, command
        assert all(
            canary not in actual.stdout + actual.stderr for canary in canaries
        ), command


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Credential-containment hardening: shell verbose tracing, links, stdin
# redirection into an interpreter, directory-rooted searches. Every credential
# file here is a FAKE in a temp directory; HOME points at that directory so the
# home-relative credential directories resolve inside it.
# ---------------------------------------------------------------------------

HOME_ENV = {"HOME": str(FIXTURE_ROOT)}
LINK_DIR = FIXTURE_ROOT / "links"
LINK_DIR.mkdir()
INNOCUOUS_LINK = LINK_DIR / "notes-link"
INNOCUOUS_LINK.symlink_to(ENV_FILE)
TEMPLATE_NAMED_LINK = LINK_DIR / "looks-safe.env.example"
TEMPLATE_NAMED_LINK.symlink_to(ENV_FILE)
DIR_LINK = LINK_DIR / "dir-link"
DIR_LINK.symlink_to(NEOTOMA_DIR)
PLAIN_LINK = LINK_DIR / "plain-link"
PLAIN_LINK.symlink_to(PLAIN_FILE)
EXAMPLE_LINK = LINK_DIR / "example-link"
EXAMPLE_LINK.symlink_to(ENV_EXAMPLE_FILE)
OTHER_DIR = FIXTURE_ROOT / "somewhere-else"
OTHER_DIR.mkdir()
(OTHER_DIR / "readme.txt").write_text("nothing sensitive\n")
HOME_DIR = str(FIXTURE_ROOT)
FAKE_VALUE = "fake_test_token_not_real_0000"


def run_home(tool_name, tool_input):
    return run(tool_name, tool_input, env=HOME_ENV)


def run_bash_in(cmd, cwd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    return subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
    ).returncode


VERBOSE_BLOCK = [
    ("set -v", f"set -v; source {ENV}"),
    ("set -o verbose", f"set -o verbose; source {ENV}"),
    ("set -xv cluster", f"set -xv; source {ENV}"),
    ("set -ev cluster", f"set -ev; source {ENV}"),
    ("bash -v -c", f"bash -v -c 'source {ENV}'"),
    ("bash -o verbose -c", f"bash -o verbose -c 'source {ENV}'"),
    ("sh -v wrapper", f"sh -v -c '. {ENV}'"),
    ("setopt verbose", f"setopt verbose; source {ENV}"),
    ("SHELLOPTS verbose", f"SHELLOPTS=verbose bash -c 'source {ENV}'"),
    ("verbose on a separate line", f"set -v\nsource {ENV}"),
]
VERBOSE_ALLOW = [
    ("set +v", f"set +v; source {ENV}"),
    ("set +o verbose", f"set +o verbose; source {ENV}"),
    ("set -e", f"set -e; source {ENV}"),
    ("set -o pipefail", f"set -o pipefail; source {ENV}"),
    ("verbose with no credential source", "set -v; echo hello"),
]


@pytest.mark.parametrize("label,cmd", VERBOSE_BLOCK, ids=[x for x, _ in VERBOSE_BLOCK])
def test_verbose_tracing_after_credential_source_is_refused(label, cmd):
    assert run_bash(cmd) == 2, label


@pytest.mark.parametrize("label,cmd", VERBOSE_ALLOW, ids=[x for x, _ in VERBOSE_ALLOW])
def test_disabling_or_unrelated_shell_options_stay_allowed(label, cmd):
    assert run_bash(cmd) == 0, label


def test_verbose_tracing_really_prints_sourced_values_so_the_refusal_is_not_cosmetic():
    # Control: the shell effect the refusal exists to stop, on a fake file.
    bash_env = {"PATH": "/usr/bin:/bin"}
    on = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"set -v; source {ENV}"],
        capture_output=True,
        text=True,
        env=bash_env,
    )
    assert FAKE_VALUE in on.stderr
    off = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"set +v; source {ENV}"],
        capture_output=True,
        text=True,
        env=bash_env,
    )
    assert FAKE_VALUE not in off.stderr


LINK_BASH_BLOCK = [
    ("cat through an innocuous link", f"cat {INNOCUOUS_LINK}"),
    ("head through a link", f"head -3 {INNOCUOUS_LINK}"),
    ("cat through a template-named link", f"cat {TEMPLATE_NAMED_LINK}"),
    ("cat inside a linked credential directory", f"cat {DIR_LINK}/.env"),
    ("grep through a link", f"grep TOKEN {INNOCUOUS_LINK}"),
    ("sourcing then dumping through a link", f"source {INNOCUOUS_LINK}; env"),
    ("link relative to the working directory", "cat ./notes-link"),
    ("bare link word relative to the working directory", "cat notes-link"),
]
LINK_BASH_ALLOW = [
    ("cat through a link to a plain file", f"cat {PLAIN_LINK}"),
    ("cat through a link to a template", f"cat {EXAMPLE_LINK}"),
]


@pytest.mark.parametrize("label,cmd", LINK_BASH_BLOCK, ids=[x for x, _ in LINK_BASH_BLOCK])
def test_link_to_credential_is_refused_in_bash(label, cmd):
    # Relative cases need the hook process to share the working directory.
    assert run_bash_in(cmd, LINK_DIR) == 2, label


@pytest.mark.parametrize("label,cmd", LINK_BASH_ALLOW, ids=[x for x, _ in LINK_BASH_ALLOW])
def test_link_to_ordinary_file_stays_allowed_in_bash(label, cmd):
    assert run_bash_in(cmd, LINK_DIR) == 0, label


def test_link_to_credential_is_refused_by_read_and_content_grep():
    assert run("Read", {"file_path": str(INNOCUOUS_LINK)}) == 2
    assert run("Read", {"file_path": str(TEMPLATE_NAMED_LINK)}) == 2
    content = {"output_mode": "content", "pattern": "x"}
    assert run("Grep", {"path": str(INNOCUOUS_LINK), **content}) == 2
    assert run_home("Grep", {"path": str(DIR_LINK), **content}) == 2
    # names-only stays allowed, and so do links to ordinary files
    assert run("Grep", {"path": str(INNOCUOUS_LINK), "pattern": "x"}) == 0
    assert run("Read", {"file_path": str(PLAIN_LINK)}) == 0
    assert run("Read", {"file_path": str(EXAMPLE_LINK)}) == 0


def test_glob_returns_names_only_so_a_link_is_not_refused():
    assert run("Glob", {"pattern": "*", "path": str(LINK_DIR)}) == 0


def test_creating_a_link_to_a_credential_is_refused():
    assert run_bash(f"ln -s {ENV} {LINK_DIR}/new-link") == 2
    assert run_bash(f"ln -s {PLAIN} {LINK_DIR}/new-plain-link") == 0


STDIN_PROGRAM = "import sys; print(sys.stdin.read())"
STDIN_BLOCK = [
    ("python reads stdin", f"python3 -c '{STDIN_PROGRAM}' < {ENV}"),
    ("python with no program", f"python3 < {ENV}"),
    ("python script file", f"python3 helper.py < {ENV}"),
    ("node reads stdin", f"node -e 'process.stdin.pipe(process.stdout)' < {ENV}"),
    ("perl loop", f"perl -ne 'print' < {ENV}"),
    ("ruby loop", f"ruby -e 'puts STDIN.read' < {ENV}"),
    ("fd-prefixed redirect", f"python3 -c '{STDIN_PROGRAM}' 0< {ENV}"),
    ("redirect first", f"< {ENV} python3 -c '{STDIN_PROGRAM}'"),
    ("quoted path", f"python3 -c '{STDIN_PROGRAM}' < \"{ENV}\""),
    ("compound command", f"{{ python3 helper.py; }} < {ENV}"),
    ("through a link", f"python3 helper.py < {INNOCUOUS_LINK}"),
    ("exec opens the file", f"exec 3< {ENV}"),
    ("jq", f"jq . < {ENV}"),
    ("home-relative path", "python3 helper.py < ~/.config/neotoma/.env"),
    ("home variable", "python3 helper.py < $HOME/.config/neotoma/.env"),
]
STDIN_ALLOW = [
    ("redirect from an ordinary file", f"python3 helper.py < {PLAIN}"),
    ("redirect from a template", f"python3 helper.py < {ENV_EXAMPLE}"),
    ("interpreter with no redirect", "python3 helper.py"),
    ("count-only redirect", f"wc -l < {ENV}"),
    ("heredoc is not a file redirect", "python3 helper.py <<'EOF'\nx = 1\nEOF"),
]


@pytest.mark.parametrize("label,cmd", STDIN_BLOCK, ids=[x for x, _ in STDIN_BLOCK])
def test_interpreter_fed_a_credential_on_stdin_is_refused(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 2, label


@pytest.mark.parametrize("label,cmd", STDIN_ALLOW, ids=[x for x, _ in STDIN_ALLOW])
def test_stdin_redirects_that_are_not_a_credential_read_stay_allowed(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 0, label


def test_interpreter_stdin_read_really_prints_the_value_so_the_refusal_is_not_cosmetic():
    with open(ENV_FILE) as handle:
        probe = subprocess.run(
            [sys.executable, "-c", STDIN_PROGRAM],
            stdin=handle,
            capture_output=True,
            text=True,
        )
    assert FAKE_VALUE in probe.stdout


def test_shell_run_on_a_credential_file_is_refused():
    assert run_bash(f"bash {ENV}") == 2
    assert run_bash(f"bash -v {ENV}") == 2
    assert run_bash("bash helper.sh") == 0
    assert run_bash(f"bash -c 'source {ENV}; python3 helper.py'") == 0


TERRITORY_GREP_BLOCK = [
    ("the credential directory itself", {"path": str(NEOTOMA_DIR)}),
    ("a directory inside the credential tree", {"path": str(NEOTOMA_DIR / "sub")}),
    ("the home directory above it", {"path": HOME_DIR}),
    ("the filesystem root", {"path": "/"}),
    ("a link to the credential directory", {"path": str(DIR_LINK)}),
    ("a launch agents directory", {"path": str(LAUNCH_AGENTS_DIR)}),
    ("a dotenv glob under a broad path", {"path": str(OTHER_DIR), "glob": "*.env"}),
    (
        "a braced glob naming a dotenv",
        {"path": str(OTHER_DIR), "glob": "{notes.txt,.env}"},
    ),
    ("a dotenv variant glob", {"path": str(OTHER_DIR), "glob": "**/.env.local"}),
]
TERRITORY_GREP_ALLOW = [
    (
        "names only on the credential directory",
        {"path": str(NEOTOMA_DIR), "output_mode": "files_with_matches"},
    ),
    ("count on the home directory", {"path": HOME_DIR, "output_mode": "count"}),
    ("an unrelated directory", {"path": str(OTHER_DIR)}),
    (
        "an unrelated directory with an ordinary glob",
        {"path": str(OTHER_DIR), "glob": "*.txt"},
    ),
]


@pytest.mark.parametrize(
    "label,ti", TERRITORY_GREP_BLOCK, ids=[x for x, _ in TERRITORY_GREP_BLOCK]
)
def test_content_grep_rooted_at_credential_territory_is_refused(label, ti):
    request = {"output_mode": "content", "pattern": "TOKEN", **ti}
    assert run_home("Grep", request) == 2, label


@pytest.mark.parametrize(
    "label,ti", TERRITORY_GREP_ALLOW, ids=[x for x, _ in TERRITORY_GREP_ALLOW]
)
def test_grep_that_cannot_print_credential_text_stays_allowed(label, ti):
    request = {"pattern": "TOKEN", "output_mode": "content", **ti}
    assert run_home("Grep", request) == 0, label


TERRITORY_BASH_BLOCK = [
    ("recursive grep of the credential directory", f"grep -r TOKEN {NEOTOMA_DIR}"),
    ("rg of the credential directory", f"rg TOKEN {NEOTOMA_DIR}"),
    ("recursive grep of the home directory", f"grep -rn TOKEN {HOME_DIR}"),
    ("rg through a directory link", f"rg TOKEN {DIR_LINK}"),
    (
        "find -exec cat from the home directory",
        f"find {HOME_DIR} -name '*.env' -exec cat {{}} +",
    ),
    (
        "find -exec cat in the credential directory",
        f"find {NEOTOMA_DIR} -type f -exec cat {{}} \\;",
    ),
]
TERRITORY_BASH_ALLOW = [
    ("names-only recursive grep", f"grep -rl TOKEN {NEOTOMA_DIR}"),
    ("count recursive grep", f"grep -rc TOKEN {HOME_DIR}"),
    ("recursive grep of an unrelated directory", f"grep -r readme {OTHER_DIR}"),
    ("find without a reader", f"find {NEOTOMA_DIR} -type f"),
]


@pytest.mark.parametrize(
    "label,cmd", TERRITORY_BASH_BLOCK, ids=[x for x, _ in TERRITORY_BASH_BLOCK]
)
def test_recursive_search_over_credential_territory_is_refused(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 2, label


@pytest.mark.parametrize(
    "label,cmd", TERRITORY_BASH_ALLOW, ids=[x for x, _ in TERRITORY_BASH_ALLOW]
)
def test_searches_that_cannot_print_credential_text_stay_allowed(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 0, label


def test_credential_directories_are_kept_in_step_with_the_path_globs():
    sys.path.insert(0, str(Path(HOOK).parent))
    import credential_read_guard as guard

    for directory, sample in guard.CREDENTIAL_DIRECTORIES.items():
        assert guard.is_credential_path(f"{directory}/{sample}"), directory
    assert guard.is_credential_path("~/.config/neotoma")


# ---------------------------------------------------------------------------
# Round-two coverage: compact and quoted redirections, shell-quoted source
# operands, search roots that are not path-shaped, find readers beyond cat.
# FAKE files only; HOME points at the fixture directory.
# ---------------------------------------------------------------------------

SPACED_DIR = FIXTURE_ROOT / "a b"
SPACED_DIR.mkdir()
SPACED_ENV_FILE = SPACED_DIR / "demo.env"
SPACED_ENV_FILE.write_text("NEOTOMA_FAKE_SETTING=fake_test_token_not_real_0000\n")
SPACED_ENV = str(SPACED_ENV_FILE)
SPACED_ESCAPED = SPACED_ENV.replace(" ", "\\ ")


def run_home_in(cmd, cwd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    env = dict(os.environ)
    env["HOME"] = str(FIXTURE_ROOT)
    return subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    ).returncode


COMPACT_STDIN_BLOCK = [
    ("redirect glued to the quoted program", f"python3 -c '{STDIN_PROGRAM}'<{ENV}"),
    ("quoted operand containing a space", f"python3 -c '{STDIN_PROGRAM}' < '{SPACED_ENV}'"),
    ("double-quoted operand containing a space", f'python3 -c "{STDIN_PROGRAM}" < "{SPACED_ENV}"'),
    ("escaped space in the operand", f"python3 helper.py < {SPACED_ESCAPED}"),
    ("interpreter glued to the redirect", f"python3<{ENV}"),
    ("operand built from quoted pieces", f"python3 helper.py < {ENV[:-1]}'v'"),
    ("escaped character in the operand", f"python3 helper.py < {ENV[:-3]}en\\v"),
    ("inside a shell -c body", f"bash -c 'python3 helper.py < {ENV}'"),
    ("redirect after a pipe stage", f"echo go | python3 helper.py <{ENV}"),
]
COMPACT_STDIN_ALLOW = [
    ("compact redirect from an ordinary file", f"python3<{PLAIN}"),
    ("quoted ordinary operand with a space", f"python3 helper.py < '{FIXTURE_ROOT}/a b/other.txt'"),
    ("interpreter name inside a quoted word only", f"echo 'run python3 later' > {OTHER_DIR}/note.txt"),
]


@pytest.mark.parametrize(
    "label,cmd", COMPACT_STDIN_BLOCK, ids=[x for x, _ in COMPACT_STDIN_BLOCK]
)
def test_compact_and_quoted_stdin_redirects_into_an_interpreter_are_refused(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 2, label


@pytest.mark.parametrize(
    "label,cmd", COMPACT_STDIN_ALLOW, ids=[x for x, _ in COMPACT_STDIN_ALLOW]
)
def test_compact_redirects_that_read_ordinary_files_stay_allowed(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 0, label


def test_compact_stdin_redirect_really_prints_the_value_so_the_refusal_is_not_cosmetic():
    probe = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"python3 -c '{STDIN_PROGRAM}'<{ENV}"],
        capture_output=True,
        text=True,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"},
    )
    assert FAKE_VALUE in probe.stdout


SOURCE_FORMS = [
    ("option terminator", f"source -- {ENV}"),
    ("single-quoted operand with a space", f"source '{SPACED_ENV}'"),
    ("double-quoted operand with a space", f'source "{SPACED_ENV}"'),
    ("escaped space", f"source {SPACED_ESCAPED}"),
    ("quoted piece inside the name", f"source {ENV[:-1]}'v'"),
    ("escaped character inside the name", f"source {ENV[:-3]}en\\v"),
    ("dot builtin with terminator", f". -- {ENV}"),
    ("builtin prefix", f"builtin source {ENV}"),
]


@pytest.mark.parametrize("flag", ["set -x", "set -v", "set -o xtrace", "set -o verbose"])
@pytest.mark.parametrize("label,source", SOURCE_FORMS, ids=[x for x, _ in SOURCE_FORMS])
def test_tracing_then_a_shell_decoded_source_operand_is_refused(flag, label, source):
    assert run_home("Bash", {"command": f"{flag}; {source}"}) == 2, label


def test_shell_decoded_source_operand_inside_a_wrapper_is_refused():
    assert run_bash(f"bash -x -c 'source {SPACED_ESCAPED}'") == 2
    assert run_bash(f"bash -v -c \"source '{SPACED_ENV}'\"") == 2
    assert run_bash(f"sh -x -c '. -- {ENV}'") == 2


def test_untraced_or_untraced_off_source_of_the_same_forms_stays_allowed():
    assert run_bash(f"source -- {ENV}") == 0
    assert run_bash(f"set +x; source -- {ENV}") == 0
    assert run_bash(f"set +v; source '{SPACED_ENV}'") == 0
    assert run_bash(f"set -x; source -- {PLAIN}") == 0


def test_traced_source_with_terminator_really_prints_the_value():
    probe = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"set -x; source -- {ENV}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert FAKE_VALUE in probe.stderr


ROOT_FORMS_BLOCK = [
    ("bare tilde root", "rg --hidden X ~"),
    ("tilde with slash", "rg --hidden X ~/"),
    ("home variable root", "rg --hidden X $HOME"),
    ("braced home variable root", "rg --hidden X ${HOME}"),
    ("glob under home", "rg --hidden X ~/*"),
    ("quoted tilde-less home", f"rg --hidden X '{FIXTURE_ROOT}'"),
    ("recursive grep tilde", "grep -rn X ~"),
    ("flag value before the root", "rg -g '*.py' X ~"),
    ("ag at the home directory", "ag X ~"),
    ("ack at the home directory", "ack X ~/"),
    ("pattern flag then root", "rg -e X ~"),
    ("find tilde root with cat", "find ~ -type f -exec cat {} +"),
    ("find tilde-slash root with cat", "find ~/ -type f -exec cat {} +"),
    ("find with sed", "find ~/ -type f -exec sed -n p {} +"),
    ("find with awk", "find ~/ -type f -exec awk 1 {} +"),
    ("find with grep", "find ~/ -type f -exec grep X {} +"),
    ("find with an interpreter", "find ~/ -type f -exec python3 helper.py {} +"),
    ("find execdir with sed", "find ~ -type f -execdir sed -n p {} +"),
    ("find with escaped terminator", "find ~ -type f -exec sed -n p {} \\;"),
    ("find in the credential directory with sed", f"find {NEOTOMA_DIR} -type f -exec sed -n p {{}} +"),
    ("find in the credential directory with awk", f"find {NEOTOMA_DIR} -type f -exec awk 1 {{}} +"),
]
ROOT_FORMS_ALLOW = [
    ("rg outside credential territory", f"rg X {OTHER_DIR}"),
    ("names-only rg", "rg -l X ~"),
    ("count grep", "grep -rc X ~"),
    ("find with no reader", "find ~ -type f"),
    ("find printing names", "find ~/ -type f -print"),
    ("non-recursive grep of an ordinary file", f"grep X {PLAIN}"),
]


@pytest.mark.parametrize("label,cmd", ROOT_FORMS_BLOCK, ids=[x for x, _ in ROOT_FORMS_BLOCK])
def test_search_roots_that_are_not_path_shaped_are_refused(label, cmd):
    assert run_home_in(cmd, OTHER_DIR) == 2, label


@pytest.mark.parametrize("label,cmd", ROOT_FORMS_ALLOW, ids=[x for x, _ in ROOT_FORMS_ALLOW])
def test_searches_that_cannot_print_credential_text_stay_allowed_with_new_roots(label, cmd):
    assert run_home_in(cmd, OTHER_DIR) == 0, label


CWD_FORMS_BLOCK = [
    ("omitted root from the home directory", "rg --hidden X", FIXTURE_ROOT),
    ("dot root from the home directory", "rg --hidden X .", FIXTURE_ROOT),
    ("omitted root after cd tilde", "cd ~; rg --hidden X", OTHER_DIR),
    ("dot root after cd tilde", "cd ~; rg --hidden X .", OTHER_DIR),
    ("omitted root after cd to the credential directory", f"cd {NEOTOMA_DIR} && rg X", OTHER_DIR),
    ("relative root after cd", f"cd {FIXTURE_ROOT} && grep -r X .config", OTHER_DIR),
    ("cd home variable", "cd $HOME && rg X", OTHER_DIR),
    ("cd to an unresolvable directory then a relative root", "cd $UNSET_DIR_FOR_TEST && rg X .", OTHER_DIR),
    ("cd inside a subshell then a search", "(cd ~); cd ~ && rg X", OTHER_DIR),
    ("find with omitted root after cd", "cd ~ && find -type f -exec cat {} +", OTHER_DIR),
]
CWD_FORMS_ALLOW = [
    ("omitted root in an ordinary directory", "rg X", OTHER_DIR),
    ("dot root after cd to an ordinary directory", f"cd {OTHER_DIR} && rg X .", FIXTURE_ROOT / "links"),
    ("names-only after cd tilde", "cd ~ && rg -l X", OTHER_DIR),
]


@pytest.mark.parametrize("label,cmd,cwd", CWD_FORMS_BLOCK, ids=[x[0] for x in CWD_FORMS_BLOCK])
def test_implicit_roots_and_directory_changes_are_resolved_before_judging(label, cmd, cwd):
    assert run_home_in(cmd, cwd) == 2, label


@pytest.mark.parametrize("label,cmd,cwd", CWD_FORMS_ALLOW, ids=[x[0] for x in CWD_FORMS_ALLOW])
def test_implicit_roots_in_ordinary_directories_stay_allowed(label, cmd, cwd):
    assert run_home_in(cmd, cwd) == 0, label


def test_the_payload_working_directory_is_used_when_present():
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "rg --hidden X"},
            "cwd": str(FIXTURE_ROOT),
        }
    )
    env = dict(os.environ)
    env["HOME"] = str(FIXTURE_ROOT)
    result = subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True,
        cwd=str(OTHER_DIR), env=env,
    )
    assert result.returncode == 2


def test_find_with_a_sed_callback_really_prints_the_value():
    probe = subprocess.run(
        ["find", str(NEOTOMA_DIR), "-type", "f", "-name", ".env", "-exec", "sed", "-n", "p", "{}", "+"],
        capture_output=True,
        text=True,
    )
    assert FAKE_VALUE in probe.stdout


DIRECT_SEARCH_BLOCK = [
    ("ag on the credential directory", f"ag X {NEOTOMA_DIR}"),
    ("ack on the credential directory", f"ack X {NEOTOMA_DIR}"),
    ("ag on a credential file", f"ag X {ENV}"),
    ("ack on a credential file", f"ack X {NEOTOMA_OTHER}"),
]


@pytest.mark.parametrize("label,cmd", DIRECT_SEARCH_BLOCK, ids=[x for x, _ in DIRECT_SEARCH_BLOCK])
def test_ag_and_ack_are_search_readers_like_grep_and_rg(label, cmd):
    assert run_home("Bash", {"command": cmd}) == 2, label


# ---------------------------------------------------------------------------
# Relative operands are judged against the directory they are read from.
#
# Every direct reader, source, redirect and link check resolves a relative
# operand against the payload's working directory, the hook process's own,
# and every literal `cd`/`pushd` target in the command, with `~`, `..` and
# symbolic links resolved, so a protected file is refused whether it is named
# absolutely or relative to wherever the shell will be. The hook process runs
# in an ordinary directory in every case below, so the payload directory or
# the `cd` is the only way it can know where the operand points.
# ---------------------------------------------------------------------------

DIR_LINK = FIXTURE_ROOT / "relative-links" / "settings-dir"
DIR_LINK.parent.mkdir()
DIR_LINK.symlink_to(NEOTOMA_DIR, target_is_directory=True)


def run_in(tool_name, tool_input, process_cwd, payload_cwd=None):
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if payload_cwd is not None:
        payload["cwd"] = str(payload_cwd)
    env = dict(os.environ)
    env["HOME"] = str(FIXTURE_ROOT)
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(process_cwd),
        env=env,
    ).returncode


RELATIVE_BASH_BLOCK = [
    ("cd then a reader", f"cd {NEOTOMA_DIR}; cat config.json"),
    ("cd and-chain then a reader", f"cd {NEOTOMA_DIR} && head -1 config.json"),
    ("cd then an interpreter on stdin", f"cd {NEOTOMA_DIR}; python3 < config.json"),
    ("cd then a compact stdin redirect", f"cd {NEOTOMA_DIR}; python3<config.json"),
    ("cd then a plist reader", f"cd {LAUNCH_AGENTS_DIR}; plutil -p com.example.agent.plist"),
    ("cd tilde then a nested relative reader", "cd ~ && cat .config/neotoma/config.json"),
    ("cd tilde subdirectory then a reader", "cd ~/.config/neotoma && cat config.json"),
    ("cd through dot-dot then a reader", f"cd {OTHER_DIR}/../.config/neotoma && cat config.json"),
    ("dot-dot operand after cd", f"cd {OTHER_DIR} && cat ../.config/neotoma/config.json"),
    ("cd then a bare file name", f"cd {NEOTOMA_DIR}; cat settings"),
    ("pushd then a reader", f"pushd {NEOTOMA_DIR} >/dev/null; cat config.json"),
    ("cd through a directory link then a reader", f"cd {DIR_LINK}; cat config.json"),
    ("cd then source then dump", f"cd {NEOTOMA_DIR}; set -a; source settings; set +a; env"),
    ("cd then an inline program naming the file", f"cd {NEOTOMA_DIR}; python3 -c \"print(open('config.json').read())\""),
    ("cd then a piped reader", f"cd {NEOTOMA_DIR}; echo config.json | xargs cat"),
    ("cd then a read loop", f"cd {NEOTOMA_DIR}; while read l; do echo $l; done < config.json"),
    ("cd then a link", f"cd {NEOTOMA_DIR}; ln -s config.json {OTHER_DIR}/innocent"),
    ("cd in a subshell then a reader", f"(cd {NEOTOMA_DIR} && cat config.json)"),
    ("cd inside sh -c then a reader", f"sh -c 'cd {NEOTOMA_DIR} && cat config.json'"),
]
RELATIVE_BASH_ALLOW = [
    ("cd to an ordinary directory then a reader", f"cd {OTHER_DIR}; cat readme.txt"),
    ("cd then a template reader", f"cd {NEOTOMA_DIR}; cat .env.example"),
    ("cd then a names-only listing", f"cd {NEOTOMA_DIR} && ls"),
    ("cd then a count-only grep", f"cd {NEOTOMA_DIR} && grep -c X config.json"),
    ("cd to the agents directory then a listing", f"cd {LAUNCH_AGENTS_DIR} && ls -la"),
    ("dot-dot to an ordinary file", f"cd {OTHER_DIR} && cat ../notes.txt"),
    ("relative reader with no cd in an ordinary directory", "cat readme.txt"),
]


@pytest.mark.parametrize("label,cmd", RELATIVE_BASH_BLOCK, ids=[x for x, _ in RELATIVE_BASH_BLOCK])
def test_relative_operands_after_a_directory_change_are_refused(label, cmd):
    assert run_in("Bash", {"command": cmd}, OTHER_DIR) == 2, label


@pytest.mark.parametrize("label,cmd", RELATIVE_BASH_ALLOW, ids=[x for x, _ in RELATIVE_BASH_ALLOW])
def test_relative_operands_that_do_not_reach_a_credential_stay_allowed(label, cmd):
    assert run_in("Bash", {"command": cmd}, OTHER_DIR) == 0, label


PAYLOAD_CWD_BLOCK = [
    ("Bash", {"command": "cat config.json"}, NEOTOMA_DIR),
    ("Bash", {"command": "python3 < config.json"}, NEOTOMA_DIR),
    ("Bash", {"command": "plutil -p com.example.agent.plist"}, LAUNCH_AGENTS_DIR),
    ("Bash", {"command": "cat ../.config/neotoma/config.json"}, OTHER_DIR),
    ("Bash", {"command": "cat config.json"}, DIR_LINK),
    ("Read", {"file_path": "config.json"}, NEOTOMA_DIR),
    ("Read", {"file_path": "../.config/neotoma/config.json"}, OTHER_DIR),
    ("Grep", {"pattern": "X", "path": ".", "output_mode": "content"}, NEOTOMA_DIR),
    ("Grep", {"pattern": "X", "path": "config.json", "output_mode": "content"}, NEOTOMA_DIR),
]
PAYLOAD_CWD_ALLOW = [
    ("Bash", {"command": "cat readme.txt"}, OTHER_DIR),
    ("Bash", {"command": "cat .env.example"}, NEOTOMA_DIR),
    ("Read", {"file_path": "readme.txt"}, OTHER_DIR),
    ("Grep", {"pattern": "X", "path": "config.json", "output_mode": "count"}, NEOTOMA_DIR),
]


@pytest.mark.parametrize(
    "tool,tool_input,payload_cwd",
    PAYLOAD_CWD_BLOCK,
    ids=[f"{t}-{i}" for i, (t, _, _) in enumerate(PAYLOAD_CWD_BLOCK)],
)
def test_relative_operands_are_judged_from_the_payload_directory(tool, tool_input, payload_cwd):
    # The hook process runs in an ordinary directory, as a project hook does.
    assert run_in(tool, tool_input, OTHER_DIR, payload_cwd) == 2


@pytest.mark.parametrize(
    "tool,tool_input,payload_cwd",
    PAYLOAD_CWD_ALLOW,
    ids=[f"{t}-{i}" for i, (t, _, _) in enumerate(PAYLOAD_CWD_ALLOW)],
)
def test_relative_ordinary_operands_from_the_payload_directory_stay_allowed(
    tool, tool_input, payload_cwd
):
    assert run_in(tool, tool_input, OTHER_DIR, payload_cwd) == 0


def test_absolute_and_relative_forms_of_the_same_read_get_the_same_decision():
    absolute = run_in("Bash", {"command": f"cat {NEOTOMA_OTHER}"}, OTHER_DIR)
    relative = run_in("Bash", {"command": f"cd {NEOTOMA_DIR}; cat config.json"}, OTHER_DIR)
    assert absolute == relative == 2


def test_relative_read_after_cd_really_prints_the_file_so_the_refusal_is_not_cosmetic():
    probe = subprocess.run(
        ["sh", "-c", f"cd {NEOTOMA_DIR}; cat config.json"],
        capture_output=True,
        text=True,
        cwd=str(OTHER_DIR),
    )
    assert "not-a-real-value" in probe.stdout


# ---------------------------------------------------------------------------
# Operand shapes: every spelling a shell decodes to the same path is judged as
# that path.
#
# For each protected target (a dotenv file, a non-dotenv file in the
# credential directory, a launchd plist) the same read is written in every
# operand shape: quoting and escaping inside a path component, concatenated
# and empty quotes, ANSI-C and locale quoting, option terminators, attached
# and `=`-joined option values, assignments, redirects, brace alternatives,
# globs, variables and `~`, pipeline operands, and a quoted command word.
# Each spelling is first decoded by a real shell (`printf`) to prove it names
# the protected file, so a refusal is not a guess about shell semantics.
# ---------------------------------------------------------------------------


def _mangle(component: str) -> dict[str, str]:
    """Shell spellings of *component* that all decode to it."""
    head, mid, tail = component[:2], component[2:-1], component[-1]
    ansi = "".join(f"\\x{ord(c):02x}" for c in mid)
    return {
        "single-quoted piece": f"{head}'{mid}'{tail}",
        "double-quoted piece": f'{head}"{mid}"{tail}',
        "backslash escape": f"{head}\\{mid}{tail}",
        "empty quotes": f"{head}''{mid}\"\"{tail}",
        "ansi-c escapes": f"{head}$'{ansi}'{tail}",
        "locale quoting": f'{head}$"{mid}"{tail}',
    }


_OPERAND_TARGETS = {
    "dotenv": (ENV, ".env"),
    "credential-directory file": (NEOTOMA_OTHER, "neotoma"),
    "launchd plist": (PLIST, "LaunchAgents"),
}


def _operand_spellings(path: str, component: str) -> dict[str, str]:
    spellings = {
        name: path.replace(component, mangled, 1) if component != ".env" else path[: -len(component)] + mangled
        for name, mangled in _mangle(component).items()
    }
    spellings["whole word single-quoted"] = f"'{path}'"
    spellings["whole word ansi-c"] = f"$'{path}'"
    return spellings


def _shell_decodes_to(spelling: str, path: str) -> bool:
    probe = subprocess.run(
        ["bash", "-c", f"printf '%s' {spelling}"], capture_output=True, text=True
    )
    return probe.stdout == path


_SHAPE_TEMPLATES = {
    "plain reader": "cat {p}",
    "option terminator": "cat -- {p}",
    "reader with options": "head -n 1 {p}",
    "stdin redirect to a reader": "cat < {p}",
    "nonrecursive search": "grep X {p}",
    "attached option value": "grep -f{p} /dev/null",
    "long option value": "grep --file={p} /dev/null",
    "assignment operand": "dd if={p}",
    "pipeline operand": "printf '%s\\n' {p} | xargs cat",
    "quoted command word": "c'a't {p}",
    "escaped command word": "\\cat {p}",
    "plist reader": "plutil -p {p}",
    "read loop": "while read l; do echo $l; done < {p}",
    "interpreter on stdin": "python3 < {p}",
    "inside sh -c": "sh -c \"cat {p}\"",
}

_SHAPE_CASES = [
    (f"{target} / {spelling_name} / {shape}", template.format(p=spelling), path, spelling)
    for target, (path, component) in _OPERAND_TARGETS.items()
    for spelling_name, spelling in _operand_spellings(path, component).items()
    for shape, template in _SHAPE_TEMPLATES.items()
    # A double quote inside the double-quoted `sh -c` body would end it early,
    # so that pairing would not name the target at all.
    if not (shape == "inside sh -c" and '"' in spelling)
]


@pytest.mark.parametrize(
    "label,cmd,path,spelling", _SHAPE_CASES, ids=[c[0] for c in _SHAPE_CASES]
)
def test_every_shell_spelling_of_a_protected_operand_is_refused(label, cmd, path, spelling):
    assert _shell_decodes_to(spelling, path), f"fixture spelling is wrong: {spelling}"
    assert run_in("Bash", {"command": cmd}, OTHER_DIR) == 2, label


_EXPANSION_CASES = [
    ("brace alternatives", "cat {root}/.config/{{neotoma,unused}}/config.json"),
    ("glob in a directory", "cat {root}/.config/neot?ma/config.json"),
    ("glob in a file name", "cat {root}/.config/neotoma/conf*.json"),
    ("glob over plists", "cat {root}/Library/LaunchAgents/*.plist"),
    ("glob over a dotenv", "cat {root}/.config/neotoma/.e[n]v"),
    ("home variable", "cat $HOME/.config/neotoma/config.json"),
    ("braced home variable", "cat ${{HOME}}/.config/neotoma/config.json"),
    ("tilde", "cat ~/.config/neotoma/config.json"),
    ("at-file reference", "grep X @{root}/.config/neotoma/config.json"),
    ("path list", "cat x:{root}/.config/neotoma/config.json"),
]


@pytest.mark.parametrize(
    "label,template", _EXPANSION_CASES, ids=[c[0] for c in _EXPANSION_CASES]
)
def test_expanded_operand_forms_are_refused(label, template):
    cmd = template.format(root=FIXTURE_ROOT)
    assert run_in("Bash", {"command": cmd}, OTHER_DIR) == 2, label


_SHAPE_ALLOW = [
    ("quoted ordinary file", f"cat {PLAIN[:-3]}'{PLAIN[-3:]}'"),
    ("ansi-c ordinary file", f"cat $'{PLAIN}'"),
    ("quoted template", f"cat {ENV_EXAMPLE[:-4]}'{ENV_EXAMPLE[-4:]}'"),
    ("count-only search of a quoted credential", f"grep -c X {ENV[:-2]}'{ENV[-2:]}'"),
    ("names-only listing of a quoted credential directory", f"ls {str(NEOTOMA_DIR)[:-3]}'{str(NEOTOMA_DIR)[-3:]}'"),
    ("glob over ordinary files", f"cat {OTHER_DIR}/*.txt"),
    ("brace over ordinary files", f"cat {FIXTURE_ROOT}/{{notes.txt,somewhere-else/readme.txt}}"),
    ("option value that is not a path", "grep --color=auto X readme.txt"),
]


@pytest.mark.parametrize("label,cmd", _SHAPE_ALLOW, ids=[c[0] for c in _SHAPE_ALLOW])
def test_shell_spellings_of_ordinary_operands_stay_allowed(label, cmd):
    assert run_in("Bash", {"command": cmd}, OTHER_DIR) == 0, label


def test_a_split_quoted_operand_really_reads_the_protected_file():
    spelling = _operand_spellings(NEOTOMA_OTHER, "neotoma")["single-quoted piece"]
    probe = subprocess.run(["bash", "-c", f"cat {spelling}"], capture_output=True, text=True)
    assert "not-a-real-value" in probe.stdout


# ---------------------------------------------------------------------------
# The guard's own output never carries file content or matched values.
#
# Two sides: (1) a mode the guard lets through as names/count-only must not
# be able to print surrounding lines, so grep context options are never a safe
# mode; (2) the refusal and the log line name a path or a fixed description,
# never text that could be content or a value.
# ---------------------------------------------------------------------------

_FIXTURE_CONTENT_MARKERS = (
    "fake_test_token_not_real_0000",
    "synthetic_attached_pattern_canary_not_real",
    "zzz_fake_value",
    "not-a-real-value",
    "synthetic-not-real",
)

CONTEXT_MODE_BLOCK = [
    ("names-only with after-context", f"grep -o -A 1 '^[A-Z_]*=' {ENV}"),
    ("names-only with attached before-context", f"grep -o -B1 '^[A-Z_]*=' {ENV}"),
    ("names-only with context", f"grep -o -C 2 '^[A-Z_]*=' {ENV}"),
    ("names-only with long context", f"grep -o --context=1 '^[A-Z_]*=' {ENV}"),
    ("names-only with long after-context", f"grep -o --after-context 1 '^[A-Z_]*=' {ENV}"),
    ("count with context", f"grep -c -A1 X {ENV}"),
    ("files with context", f"grep -l -B 2 X {ENV}"),
    ("ripgrep names-only with context", f"rg -o -C1 '^[A-Za-z_][A-Za-z0-9_]*=' {ENV}"),
]


@pytest.mark.parametrize("label,cmd", CONTEXT_MODE_BLOCK, ids=[c[0] for c in CONTEXT_MODE_BLOCK])
def test_context_options_are_never_a_safe_grep_mode(label, cmd):
    assert run_bash(cmd) == 2, label


def test_context_with_names_only_can_print_values_on_this_platform():
    probe = subprocess.run(
        ["grep", "-o", "-A", "1", "^[A-Z_]*=", ENV], capture_output=True, text=True
    )
    if FAKE_VALUE not in probe.stdout and "client_secret" not in probe.stdout:
        pytest.skip("this grep does not print context lines in -o mode")
    assert run_bash(f"grep -o -A 1 '^[A-Z_]*=' {ENV}") == 2


_OUTPUT_PROBES = [
    *[("Bash", {"command": cmd}) for _, cmd in BASH_BLOCK],
    *[("Bash", {"command": cmd}) for _, cmd in CONTEXT_MODE_BLOCK],
    ("Read", {"file_path": ENV}),
    ("Read", {"file_path": NEOTOMA_OTHER}),
    ("Read", {"file_path": PLIST}),
    ("Grep", {"pattern": "X", "path": ENV, "output_mode": "content"}),
]


@pytest.mark.parametrize(
    "tool,tool_input", _OUTPUT_PROBES, ids=[f"{t}-{i}" for i, (t, _) in enumerate(_OUTPUT_PROBES)]
)
def test_a_refusal_never_contains_file_content(tool, tool_input):
    result = run_result(tool, tool_input)
    assert result.returncode == 2
    output = result.stdout + result.stderr
    for marker in _FIXTURE_CONTENT_MARKERS:
        assert marker not in output, marker


@pytest.mark.parametrize(
    "cmd",
    [
        f'cat "{NEOTOMA_DIR}/x synthetic_value_marker_77"',
        f"cat '{NEOTOMA_DIR}/a=synthetic_value_marker_77'",
        f'cat "{NEOTOMA_DIR}/$(printf synthetic_value_marker_77)"',
    ],
    ids=["space", "assignment", "expansion"],
)
def test_a_refusal_names_only_a_path_or_a_fixed_description(cmd):
    result = run_result("Bash", {"command": cmd})
    assert result.returncode == 2
    assert "synthetic_value_marker_77" not in result.stdout + result.stderr


def test_safe_modes_the_guard_allows_print_no_content_when_run():
    for argv in (
        ["grep", "-c", "^NEOTOMA_BEARER_TOKEN=", ENV],
        ["grep", "-o", "^[A-Z_]*=", ENV],
        ["grep", "-l", "TOKEN", ENV],
    ):
        cmd = " ".join(argv[:-1]) .replace("^", "'^").replace("=", "='") + f" {ENV}"
        assert run_bash(cmd) == 0, cmd
        printed = subprocess.run(argv, capture_output=True, text=True).stdout
        for marker in _FIXTURE_CONTENT_MARKERS:
            assert marker not in printed, (argv, marker)


# ---------------------------------------------------------------------------
# Search patterns are data, not operands (Neotoma task ent_66d9c92e8343408876e3542b)
#
# A grep/rg/git grep whose PATTERN merely names a credential path or a tripwire
# command (searching source for the text of the dotenv path, `env -u NAME`,
# `printenv`) read as the credential read itself and was refused. That blocked
# diagnosing a credential defect in the guard's own source. The pattern is
# data; the file operands are what the program reads, and those stay judged.
# ---------------------------------------------------------------------------

SOURCE_FILES = {}
for _name in (
    "neotoma_bearer_token.py",
    "credential_notes.py",
    "neotoma_env_loader.py",
    "secrets_management.md",
):
    _path = FIXTURE_ROOT / _name
    _path.write_text("NEOTOMA_BEARER_TOKEN = None  # source mentions the variable name\n")
    SOURCE_FILES[_name] = str(_path)

SRC = SOURCE_FILES["neotoma_env_loader.py"]

PATTERN_TEXT_ALLOW = [
    ("grep dotenv path fragment as pattern", f"grep -n 'neotoma/.env' {PLAIN}"),
    ("grep full credential path as pattern", f"grep -n '{ENV}' {PLAIN}"),
    ("grep recursive dotenv fragment", f"grep -rn '.config/neotoma/.env' {PLAIN}"),
    ("rg dotenv path fragment as pattern", f"rg -n 'neotoma/.env' {PLAIN}"),
    ("rg pattern with regex and path text", f"rg 'source .*neotoma/.env' {PLAIN}"),
    ("rg env -u NAME as pattern", f"rg 'env -u NEOTOMA_BEARER_TOKEN' {PLAIN}"),
    ("grep printenv as pattern", f"grep -rn 'printenv' {PLAIN}"),
    ("grep env pipe grep as pattern", f"grep -rn 'env | grep' {PLAIN}"),
    ("rg launchctl print as pattern", f"rg 'launchctl print' {PLAIN}"),
    ("rg pgrep -f as pattern", f"rg 'pgrep -f' {PLAIN}"),
    ("grep ps -E as pattern", f"grep -n 'ps -E' {PLAIN}"),
    ("egrep alternation of credential names", f"egrep -n 'neotoma/.env|claude.json' {PLAIN}"),
    ("grep -e twice", f"grep -e 'neotoma/.env' -e 'claude.json' {PLAIN}"),
    ("grep clustered -ie", f"grep -ie 'neotoma/.env' {PLAIN}"),
    ("grep --regexp separate", f"grep --regexp 'neotoma/.env' {PLAIN}"),
    ("grep pattern after --", f"grep -n -- 'neotoma/.env' {PLAIN}"),
    ("grep pattern, option after the file", f"grep 'neotoma/.env' {PLAIN} -n"),
    ("grep pattern with context option", f"grep -A 2 'neotoma/.env' {PLAIN}"),
    ("git grep dotenv fragment", f"git grep -n 'neotoma/.env' -- {PLAIN}"),
    ("git -C grep dotenv fragment", f"git -C /tmp grep -n 'neotoma/.env' -- {PLAIN}"),
    ("pipeline into grep for the path text", f"cat {PLAIN} | grep 'neotoma/.env'"),
    ("env prefix and path pattern", f"env LC_ALL=C grep -n 'neotoma/.env' {PLAIN}"),
    ("pattern in a later command of a chain", f"cd /tmp && rg 'neotoma/.env' {PLAIN}"),
]

# Reads of NON-credential source files whose path or content mentions the
# credential vocabulary. These were already allowed; pinned so the masking
# cannot make them regress.
SOURCE_READ_ALLOW = [
    ("cat a source file named for the token", f"cat {SOURCE_FILES['neotoma_bearer_token.py']}"),
    ("cat a source file named for credentials", f"cat {SOURCE_FILES['credential_notes.py']}"),
    ("sed -n a loader named for the env file", f"sed -n 1,5p {SRC}"),
    ("head a doc about secrets", f"head -5 {SOURCE_FILES['secrets_management.md']}"),
    ("grep the token variable name in a source file", f"grep -n NEOTOMA_BEARER_TOKEN {SRC}"),
    ("rg the token variable name in a source file", f"rg -n NEOTOMA_BEARER_TOKEN {SRC}"),
]


@pytest.mark.parametrize(
    "label,cmd", PATTERN_TEXT_ALLOW, ids=[label for label, _ in PATTERN_TEXT_ALLOW]
)
def test_search_pattern_text_naming_a_credential_is_allowed(label, cmd):
    assert run_bash(cmd) == 0, label


@pytest.mark.parametrize(
    "label,cmd", SOURCE_READ_ALLOW, ids=[label for label, _ in SOURCE_READ_ALLOW]
)
def test_reading_source_files_that_mention_credential_names_is_allowed(label, cmd):
    assert run_bash(cmd) == 0, label


# What the masking must NOT release. Each names the credential as something the
# search program READS, or hides it behind a shape the masking declines.
PATTERN_MASKING_BLOCK = [
    ("pattern then the credential file", f"grep 'neotoma/.env' {ENV}"),
    ("benign pattern then the credential file", f"grep foo {ENV}"),
    ("credential as the second file operand", f"grep foo {PLAIN} {ENV}"),
    ("credential file after --", f"grep foo -- {ENV}"),
    ("-e pattern then the credential file", f"grep -e 'neotoma/.env' {ENV}"),
    ("clustered -ie then the credential file", f"grep -ie foo {ENV}"),
    ("--regexp then the credential file", f"grep --regexp foo {ENV}"),
    ("--regexp= then the credential file", f"grep --regexp=foo {ENV}"),
    ("-e and -- then the credential file", f"grep -e foo -- {ENV}"),
    ("pattern file is the credential", f"grep -f {ENV} {PLAIN}"),
    ("--file pattern file is the credential", f"grep --file {ENV} {PLAIN}"),
    ("--file= pattern file is the credential", f"grep --file={ENV} {PLAIN}"),
    ("context option then pattern then credential", f"grep -A 2 foo {ENV}"),
    ("-C context option then credential", f"grep -C 2 foo {ENV}"),
    ("max-count then credential", f"grep -m 1 foo {ENV}"),
    ("option after the credential file", f"grep foo {ENV} -n"),
    ("egrep then the credential file", f"egrep 'a|b' {ENV}"),
    ("fgrep then the credential file", f"fgrep -n foo {ENV}"),
    ("absolute-path grep then the credential file", f"/usr/bin/grep foo {ENV}"),
    ("env prefix then the credential file", f"env LC_ALL=C grep foo {ENV}"),
    ("rg pattern then the credential file", f"rg 'neotoma/.env' {ENV}"),
    ("rg -e then the credential file", f"rg -e foo {ENV}"),
    ("rg pattern file is the credential", f"rg -f {ENV} {PLAIN}"),
    ("rg searches the credential directory", f"rg -n . {NEOTOMA_DIR}"),
    ("rg --files style listing of the directory with a pattern", f"rg --files {NEOTOMA_DIR}"),
    ("git grep --no-index the credential file", f"git grep --no-index foo -- {ENV}"),
    ("git grep -e then the credential file", f"git grep --no-index -e foo -- {ENV}"),
    ("stdin redirect from the credential", f"grep 'neotoma/.env' < {ENV}"),
    ("command substitution in the pattern", f'grep "$(cat {ENV})" {PLAIN}'),
    ("bare command substitution in the pattern", f"grep $(cat {ENV}) {PLAIN}"),
    ("backtick substitution in the pattern", f"grep `cat {ENV}` {PLAIN}"),
    ("masked search then a credential read", f"grep -n 'neotoma/.env' {PLAIN}; cat {ENV}"),
    ("masked search piped to a credential read", f"rg 'x' {PLAIN} && head -1 {ENV}"),
    ("recursive search over the credential directory", f"grep -rn 'neotoma/.env' {NEOTOMA_DIR}"),
    ("cd into the credential directory then read", f"cd {NEOTOMA_DIR} && grep 'neotoma/.env' .env"),
    ("cd then recursive search of the directory", f"cd {NEOTOMA_DIR} && rg 'neotoma/.env'"),
    ("interpreter inline program still refused", f"python3 -c \"print(open('{ENV}').read())\""),
    ("names-only pattern with a value-printing sibling", f"grep -o '^[A-Z_]*=.*' {ENV}"),
]


@pytest.mark.parametrize(
    "label,cmd", PATTERN_MASKING_BLOCK, ids=[label for label, _ in PATTERN_MASKING_BLOCK]
)
def test_pattern_masking_never_releases_a_credential_read(label, cmd):
    assert run_bash(cmd) == 2, label


def _guard():
    sys.path.insert(0, str(Path(HOOK).parent))
    import importlib

    import credential_read_guard as guard

    return importlib.reload(guard)


def test_search_in_a_credential_working_directory_is_still_refused():
    """The pattern is masked but the omitted search root is the working
    directory; when that is credential territory the search is refused."""
    guard = _guard()
    for cmd in (
        "grep -rn 'neotoma/.env'",
        "rg 'neotoma/.env'",
        "git grep -n 'neotoma/.env'",
    ):
        assert guard.check_bash(cmd, cwd=str(NEOTOMA_DIR)), cmd
    assert guard.check_bash("grep -rn 'neotoma/.env' lib", cwd=str(FIXTURE_ROOT)) is None


def test_masking_replaces_only_literal_pattern_words():
    guard = _guard()
    mask = guard._MASKED_PATTERN
    assert guard._mask_search_patterns("grep -n 'a b' f") == f"grep -n {mask} f"
    assert guard._mask_search_patterns("rg -e 'x y' -e z f") == f"rg -e {mask} -e {mask} f"
    assert guard._mask_search_patterns("git grep -n x -- f") == f"git grep -n {mask} -- f"
    assert guard._mask_search_patterns("grep -- x f") == f"grep -- {mask} f"
    # What it leaves alone: the file operands and every other command.
    assert guard._mask_search_patterns("grep x f g") == f"grep {mask} f g"
    assert guard._mask_search_patterns("cat f; grep x f") == f"cat f; grep {mask} f"
    assert guard._mask_search_patterns("echo x") == "echo x"


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -f pats f",  # a pattern FILE is an operand, and then no positional is a pattern
        "grep --file=pats f",
        "grep -C 2 x f",  # arity differs between grep implementations: decline
        "grep --context 2 x f",
        "grep --reg x f",  # abbreviated long option: decline
        "grep -X x f",  # unknown option: decline
        "rg --files d",
        "rg --pre cat x f",
        "rg --unknown x f",
        "grep -e",  # option missing its value: decline
    ],
)
def test_masking_declines_what_it_does_not_fully_understand(cmd):
    guard = _guard()
    assert guard._mask_search_patterns(cmd) == cmd


def test_masking_leaves_a_code_shaped_file_operand_for_the_checks():
    guard = _guard()
    assert (
        guard._mask_search_patterns("grep x $(cat f)")
        == f"grep {guard._MASKED_PATTERN} $(cat f)"
    )


@pytest.mark.parametrize(
    "pattern",
    ["$(id)", '"$(id)"', '"a$B"', "`id`", "*.env", "{a,b}", "~/x", "[a-z]*", "a?b"],
)
def test_masking_never_treats_shell_code_or_globs_as_data(pattern):
    guard = _guard()
    cmd = f"grep {pattern} f"
    assert guard._mask_search_patterns(cmd) == cmd


def test_lex_spans_slice_each_token_back_to_its_raw_text():
    guard = _guard()
    command = "grep -n 'a b' \"c d\" e\\ f > out; rg x"
    spans = []
    tokens = guard._lex(command, spans)
    assert len(tokens) == len(spans)
    for (kind, text), (begin, end) in zip(tokens, spans):
        raw = command[begin:end]
        if kind == "word":
            assert guard._lex(raw) == [("word", text)], (raw, text)
        else:
            assert raw == text


def test_names_only_and_pattern_masking_share_one_pattern_set():
    guard = _guard()
    assert "^[A-Z_]*=" in guard._NAMES_ONLY_PATTERNS
    cmd = f"grep -o '^[A-Z_]*=' {ENV}"
    assert guard._mask_search_patterns(cmd) == cmd
    assert guard.check_bash(cmd) is None


def test_credential_in_a_file_slot_is_refused_across_search_shapes():
    """Property sweep: wherever the credential file sits as something the
    search program reads, no tool, flag prefix or trailing option combination
    lets it through."""
    guard = _guard()
    tools = ["grep", "egrep", "fgrep", "rg", "git grep --no-index", "env grep", "command grep"]
    prefixes = ["", "-n ", "-rn ", "-i ", "-F ", "-w ", "-m 3 ", "-A 2 ", "-H ", "-s ", "-I ", "-- "]
    suffixes = ["", " -n", " -i", " -C 2", " -X", " --files", " -r"]
    slots = [
        "{pat} {cred}",
        "-e {pat} {cred}",
        "-ie {pat} {cred}",
        "--regexp {pat} {cred}",
        "--regexp={pat} {cred}",
        "{pat} -- {cred}",
        "-e {pat} -- {cred}",
        "-f {cred} {benign}",
        "--file {cred} {benign}",
        "--file={cred} {benign}",
        "{pat} {benign} {cred}",
    ]
    checked = 0
    for tool in tools:
        for prefix in prefixes:
            for suffix in suffixes:
                for slot in slots:
                    if prefix == "-- " and slot.startswith(("-e", "-ie", "--", "-f")):
                        continue  # options after `--` are operands; judged by the other slots
                    body = slot.format(pat="'neotoma/.env'", cred=ENV, benign=PLAIN)
                    cmd = f"{tool} {prefix}{body}{suffix}"
                    assert guard.check_bash(cmd), cmd
                    checked += 1
    assert checked > 3000
