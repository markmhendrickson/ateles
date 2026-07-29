#!/usr/bin/env python3
"""Tests for the operator-gate on slack_cli.py `post` (the security-critical bit).

Runs offline: no token, no network. The whole point of the gate is that a
`post` WITHOUT --yes must never reach the network — it must dry-run and exit
non-zero. These tests assert exactly that, and that --yes is what flips it to
the send path. Run: python3 execution/scripts/test_slack_cli_post_gate.py
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Import the module under test by path (it lives beside this file).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli  # noqa: E402


def _run(argv, stdin=""):
    """Invoke slack_cli.main(argv), capturing stdout and the exit code."""
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    code = None
    try:
        with redirect_stdout(out):
            code = slack_cli.main(argv)
    except SystemExit as exc:  # argparse / SystemExit raises
        code = exc.code
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue()


def _forbid_network(monkeypatch_calls):
    """Replace _post/_call so any network attempt fails the test loudly."""
    def boom(*a, **k):
        raise AssertionError("network call attempted during a dry-run — GATE LEAK")
    slack_cli._post = boom  # type: ignore[assignment]
    slack_cli._call = boom  # type: ignore[assignment]


def test_dry_run_without_yes_sends_nothing_and_exits_2():
    _forbid_network(None)
    code, out = _run(["post", "C0123ABC", "--text", "hello", "--thread-ts", "1.2"])
    assert code == 2, f"expected exit 2 (dry-run), got {code}"
    assert "DRY RUN" in out, "dry-run banner missing"
    assert "hello" in out, "message body not previewed"
    assert "1.2" in out, "thread ts not shown"
    print("ok: dry-run without --yes sends nothing, exits 2, previews payload")


def test_empty_message_refused():
    _forbid_network(None)
    code, out = _run(["post", "C0123ABC", "--text", "   ", "--yes"])
    # Refusal is a SystemExit with a string message (truthy non-zero).
    assert code not in (0, None), f"empty message should be refused, exit was {code}"
    print("ok: empty message refused even with --yes")


def test_stdin_body_read_in_dry_run():
    _forbid_network(None)
    code, out = _run(["post", "C0123ABC", "--text", "-"], stdin="line one\nline two\n")
    assert code == 2
    assert "line one" in out and "line two" in out, "stdin body not read/previewed"
    print("ok: --text - reads multi-line body from stdin (dry-run)")


def test_yes_reaches_send_path():
    # With --yes, _post MUST be called. Stub it to capture, not hit network.
    called = {}
    def fake_post(method, body):
        called["method"] = method
        called["body"] = body
        return {"ok": True, "ts": "1785.1", "message": {}}
    slack_cli._post = fake_post  # type: ignore[assignment]
    code, out = _run(["post", "C0123ABC", "--text", "go", "--thread-ts", "9.9", "--yes"])
    assert code == 0, f"expected success, got {code}"
    assert called.get("method") == "chat.postMessage"
    assert called["body"]["channel"] == "C0123ABC"
    assert called["body"]["text"] == "go"
    assert called["body"]["thread_ts"] == "9.9"
    print("ok: --yes reaches chat.postMessage with the right payload")


def test_json_flag_accepted_before_and_after_subcommand():
    # Regression for the argparse bug: a parent-parser --json placed AFTER the
    # subcommand is not recognized unless each subparser also accepts it. It must
    # work in both positions, and stay False when absent (a leading --json must
    # not be clobbered by the subparser default).
    parse = slack_cli.build_parser().parse_args
    assert parse(["post", "C", "--text", "x", "--json"]).json is True, "trailing --json"
    assert parse(["--json", "post", "C", "--text", "x"]).json is True, "leading --json"
    assert parse(["post", "C", "--text", "x"]).json is False, "absent --json"
    assert parse(["search", "foo", "--json"]).json is True, "trailing on read cmd"
    print("ok: --json accepted before and after the subcommand, False when absent")


if __name__ == "__main__":
    test_dry_run_without_yes_sends_nothing_and_exits_2()
    test_empty_message_refused()
    test_stdin_body_read_in_dry_run()
    test_yes_reaches_send_path()
    test_json_flag_accepted_before_and_after_subcommand()
    print("\nALL PASS")
