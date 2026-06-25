"""Tests for the ``ateles`` CLI contract (the W0/W1 spine).

Co-located pytest module following the repo convention
(``lib/daemon_runtime/test_*.py``). Locks the verb surface that downstream
workstreams build on — the verb set, the exit codes (3 = not-yet-built, distinct
from argparse's usage error 2), help/version output — and guards the stdlib-only
import invariant. Run with ``pytest ateles/`` or ``python -m pytest``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ateles import __version__
from ateles.cli import IMPLEMENTED, NOT_IMPLEMENTED_EXIT, VERBS, build_parser, main

STUB_VERBS = sorted(set(VERBS) - IMPLEMENTED)


def test_bare_invocation_prints_help_and_exits_zero(capsys):
    assert main([]) == 0
    assert "usage: ateles" in capsys.readouterr().out


def test_version_flag_prints_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_lists_every_verb_with_its_workstream(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for verb, (workstream, _intent) in VERBS.items():
        assert verb in out
        assert workstream in out


@pytest.mark.parametrize("verb", STUB_VERBS)
def test_each_unimplemented_verb_is_a_stub_exiting_three(verb, capsys):
    assert main([verb]) == NOT_IMPLEMENTED_EXIT
    out = capsys.readouterr().out
    assert f"ateles {verb}" in out
    assert VERBS[verb][0] in out          # workstream label is surfaced
    assert "docs/installability.md" in out  # roadmap pointer is surfaced


def test_invalid_verb_is_an_argparse_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-verb"])
    # 2 (argparse usage error) must stay distinct from 3 (stub not-implemented).
    assert exc.value.code == 2


def test_six_verb_spine_is_stable():
    # Guard rail: renaming/removing a verb or changing the stub exit code is a
    # breaking change to the operator contract and must be deliberate.
    assert set(VERBS) == {"init", "doctor", "provision", "run", "deploy", "mirror"}
    assert NOT_IMPLEMENTED_EXIT == 3


def test_parser_program_name():
    assert build_parser().prog == "ateles"


def test_doctor_runs_read_only_and_returns_status(capsys):
    # doctor is read-only; --no-network keeps it offline. In an unconfigured
    # environment it reports problems and returns 1, but it must never raise.
    rc = main(["doctor", "--no-network"])
    assert rc in (0, 1)
    assert "python" in capsys.readouterr().out


def test_provision_runs_read_only_and_returns_status(capsys):
    # provision (dry-run) writes nothing; in an unconfigured environment it
    # reports incomplete config and returns 1, but must never raise.
    rc = main(["provision"])
    assert rc in (0, 1)
    assert capsys.readouterr().out  # produced some output


def test_init_subcommand_accepts_non_interactive_flag():
    # Parse only — running init would write a config file (exercised against a
    # tmp path in test_initialize.py).
    args = build_parser().parse_args(["init", "--non-interactive"])
    assert args.command == "init"
    assert args.non_interactive is True


def test_cli_import_graph_is_stdlib_only():
    """``python -m ateles`` must run before ``pip install`` — i.e. the package's
    import graph pulls in no third-party module. Checked in a fresh interpreter
    (this test process already has pytest etc. loaded)."""
    repo_root = Path(__file__).resolve().parents[1]
    probe = (
        "import sys, json\n"
        "import ateles, ateles.cli, ateles.__main__, "
        "ateles.config, ateles.doctor, ateles.initialize, "
        "ateles.provision, ateles.secrets\n"
        "third = ['httpx','apprise','cryptography','jwt','uvicorn','a2a',"
        "'watchdog','pandas','numpy','pyarrow','openai','asana','requests',"
        "'dotenv','websockets']\n"
        "print(json.dumps([m for m in third if m in sys.modules]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    leaked = json.loads(result.stdout.strip() or "[]")
    assert leaked == [], f"ateles import graph pulled third-party modules: {leaked}"
