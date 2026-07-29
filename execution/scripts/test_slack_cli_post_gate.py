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


def _parses_ok(argv):
    """True if build_parser().parse_args(argv) succeeds without SystemExit."""
    try:
        args = slack_cli.build_parser().parse_args(argv)
    except SystemExit:
        return None
    return args


def test_json_flag_parses_after_search_subcommand():
    # Documented form: docstring usage block + docs/slack_integration.md:81
    args = _parses_ok(["search", "manju leads deck", "--count", "20", "--json"])
    assert args is not None, "documented invocation failed to parse"
    assert args.json is True
    assert args.count == 20
    print("ok: search '<query>' --count N --json parses (docstring usage form)")


def test_json_flag_parses_after_search_subcommand_query_only():
    # docs/slack_integration.md:81 ("leads deck" --json, no --count)
    args = _parses_ok(["search", "leads deck", "--json"])
    assert args is not None, "documented invocation failed to parse"
    assert args.json is True
    print("ok: search '<query>' --json parses (docs/slack_integration.md form)")


def test_history_without_json_parses():
    # docs/slack_integration.md:78 — sanity check, no --json
    args = _parses_ok(["history", "C0123ABC", "--limit", "100"])
    assert args is not None, "documented invocation failed to parse"
    assert args.json is False
    assert args.limit == 100
    print("ok: history <channel> --limit N parses without --json")


def test_json_flag_parses_after_channels_subcommand():
    # docstring usage block: channels [--types public_channel] [--json]
    args = _parses_ok(["channels", "--types", "public_channel", "--json"])
    assert args is not None, "documented invocation failed to parse"
    assert args.json is True
    assert args.types == "public_channel"
    print("ok: channels --types public_channel --json parses (docstring usage form)")


if __name__ == "__main__":
    test_dry_run_without_yes_sends_nothing_and_exits_2()
    test_empty_message_refused()
    test_stdin_body_read_in_dry_run()
    test_yes_reaches_send_path()
    test_json_flag_parses_after_search_subcommand()
    test_json_flag_parses_after_search_subcommand_query_only()
    test_history_without_json_parses()
    test_json_flag_parses_after_channels_subcommand()
    print("\nALL PASS")
