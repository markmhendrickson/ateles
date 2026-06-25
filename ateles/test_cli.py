"""Tests for the ``ateles`` CLI contract (the W0 spine).

Co-located pytest module following the repo convention
(``lib/daemon_runtime/test_*.py``). This locks the verb surface that downstream
workstreams W1–W7 build on — the verb set, the exit codes (3 = not-yet-built,
deliberately distinct from argparse's usage error 2), and the help/version
output — so any future PR that changes the operator-facing contract trips a
test. Run with ``pytest ateles/`` or ``python -m pytest``.
"""

from __future__ import annotations

import pytest

from ateles import __version__
from ateles.cli import NOT_IMPLEMENTED_EXIT, VERBS, build_parser, main


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


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_each_verb_is_a_stub_exiting_three(verb, capsys):
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
