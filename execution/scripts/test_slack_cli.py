#!/usr/bin/env python3
"""Tests for slack_cli.py: read-path commands + Slack API error handling.

Runs offline: no token, no network. `_call` is monkeypatched per-test to
return canned Slack API payloads (success or `ok: false` error shapes), and
these tests assert the CLI renders them correctly and, for `_check`, that
each documented error code raises a SystemExit with the matching hint.
Run: python3 execution/scripts/test_slack_cli.py
"""
from __future__ import annotations

import contextlib
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Import the module under test by path (it lives beside this file).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli  # noqa: E402

_REAL_CALL = slack_cli._call


@contextlib.contextmanager
def _faking_call(fake_call):
    """Patch slack_cli._call for the duration of the block, always restoring it.

    Tests run under both a plain `python3` __main__ loop and pytest, whose
    collection/execution order isn't guaranteed to match — an unrestored
    monkeypatch here would leak a fake `_call` into whichever test runs next.
    """
    slack_cli._call = fake_call  # type: ignore[assignment]
    try:
        yield
    finally:
        slack_cli._call = _REAL_CALL


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


def _parses_ok(argv):
    """True if build_parser().parse_args(argv) succeeds without SystemExit."""
    try:
        with redirect_stderr(io.StringIO()):
            args = slack_cli.build_parser().parse_args(argv)
    except SystemExit:
        return None
    return args


# --- parser coverage (documented invocation forms) --------------------------


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


def test_post_subcommand_removed():
    # #248 scopes this integration read-only; `post` must not exist as a subcommand.
    args = _parses_ok(["post", "C0123ABC", "--text", "hello"])
    assert args is None, "`post` subcommand should no longer parse (read-only scope)"
    print("ok: `post` subcommand is not registered (read-only scope enforced)")


# --- behavioral coverage: read-path commands --------------------------------


def test_cmd_whoami_renders_identity():
    def fake_call(method, params=None):
        assert method == "auth.test"
        return {"ok": True, "user": "castor", "user_id": "U1", "team": "bottega8", "team_id": "T1", "url": "https://x.slack.com/"}
    with _faking_call(fake_call):
        code, out = _run(["whoami"])
    assert code == 0
    assert "castor" in out and "bottega8" in out
    print("ok: whoami renders user/team from auth.test")


def test_cmd_whoami_json():
    def fake_call(method, params=None):
        return {"ok": True, "user": "castor", "user_id": "U1"}
    with _faking_call(fake_call):
        code, out = _run(["whoami", "--json"])
    assert code == 0
    assert '"user": "castor"' in out
    print("ok: whoami --json emits raw payload")


def test_cmd_search_renders_matches():
    def fake_call(method, params=None):
        assert method == "search.messages"
        assert params["query"] == "leads deck"
        return {
            "ok": True,
            "messages": {
                "total": 1,
                "matches": [
                    {
                        "channel": {"name": "sales"},
                        "username": "alex",
                        "ts": "123.456",
                        "text": "here's the   leads deck",
                        "permalink": "https://x.slack.com/p123",
                    }
                ],
            },
        }
    with _faking_call(fake_call):
        code, out = _run(["search", "leads deck"])
    assert code == 0
    assert "#sales" in out and "alex" in out and "leads deck" in out
    assert "https://x.slack.com/p123" in out
    print("ok: search renders channel/user/text/permalink from search.messages")


def test_cmd_search_json():
    def fake_call(method, params=None):
        return {"ok": True, "messages": {"matches": [{"ts": "1"}]}}
    with _faking_call(fake_call):
        code, out = _run(["search", "q", "--json"])
    assert code == 0
    assert '"ts": "1"' in out
    print("ok: search --json emits raw matches array")


def test_cmd_history_renders_messages_and_files():
    def fake_call(method, params=None):
        assert method == "conversations.history"
        assert params["channel"] == "C0123ABC"
        return {
            "ok": True,
            "messages": [
                {
                    "ts": "111.1",
                    "user": "U2",
                    "text": "the   export is attached",
                    "files": [{"name": "contacts.csv", "filetype": "csv", "permalink": "https://x.slack.com/f1"}],
                }
            ],
        }
    with _faking_call(fake_call):
        code, out = _run(["history", "C0123ABC"])
    assert code == 0
    assert "export is attached" in out
    assert "FILE: contacts.csv (csv) https://x.slack.com/f1" in out
    print("ok: history renders messages and attached file metadata")


def test_cmd_history_json():
    def fake_call(method, params=None):
        return {"ok": True, "messages": [{"ts": "1"}]}
    with _faking_call(fake_call):
        code, out = _run(["history", "C0123ABC", "--json"])
    assert code == 0
    assert '"ts": "1"' in out
    print("ok: history --json emits raw messages array")


def test_cmd_channels_renders_sorted_list():
    def fake_call(method, params=None):
        assert method == "conversations.list"
        return {
            "ok": True,
            "channels": [
                {"id": "C2", "name": "zeta", "is_member": False},
                {"id": "C1", "name": "alpha", "is_member": True},
            ],
        }
    with _faking_call(fake_call):
        code, out = _run(["channels"])
    assert code == 0
    assert out.index("#alpha") < out.index("#zeta"), "channels should render sorted by name"
    assert "member" in out
    print("ok: channels renders channel list sorted by name with membership flag")


def test_cmd_channels_json():
    def fake_call(method, params=None):
        return {"ok": True, "channels": [{"id": "C1", "name": "alpha"}]}
    with _faking_call(fake_call):
        code, out = _run(["channels", "--json"])
    assert code == 0
    assert '"name": "alpha"' in out
    print("ok: channels --json emits raw channel array")


# --- behavioral coverage: _check() error paths ------------------------------


def test_check_missing_scope_raises_with_hint():
    payload = {"ok": False, "error": "missing_scope", "needed": "search:read.public", "provided": "channels:read"}
    try:
        slack_cli._check(payload, "search.messages")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        msg = str(exc)
        assert "missing_scope" in msg
        assert "search:read.public" in msg and "channels:read" in msg
    print("ok: _check raises with needed/provided scopes on missing_scope")


def test_check_invalid_auth_raises_with_hint():
    for err in ("invalid_auth", "not_authed", "token_revoked"):
        try:
            slack_cli._check({"ok": False, "error": err}, "auth.test")
            raise AssertionError(f"expected SystemExit for {err}")
        except SystemExit as exc:
            assert "invalid or revoked" in str(exc)
    print("ok: _check raises re-issue hint for invalid_auth/not_authed/token_revoked")


def test_check_not_in_channel_raises_with_hint():
    try:
        slack_cli._check({"ok": False, "error": "not_in_channel"}, "conversations.history")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "not a member of that channel" in str(exc)
    print("ok: _check raises join-channel hint on not_in_channel")


def test_check_channel_not_found_raises_with_hint():
    try:
        slack_cli._check({"ok": False, "error": "channel_not_found"}, "conversations.history")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "no such channel id" in str(exc)
    print("ok: _check raises no-such-channel hint on channel_not_found")


def test_check_unknown_error_raises_without_hint_crash():
    try:
        slack_cli._check({"ok": False, "error": "something_else"}, "search.messages")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "something_else" in str(exc)
    print("ok: _check raises plainly on an undocumented error code")


def test_check_ok_payload_passes_through():
    payload = {"ok": True, "user": "x"}
    assert slack_cli._check(payload, "auth.test") is payload
    print("ok: _check returns the payload unchanged when ok is true")


def test_call_without_token_raises():
    old_token = slack_cli.TOKEN
    slack_cli.TOKEN = ""
    try:
        try:
            slack_cli._call("auth.test")
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            assert "SLACK_USER_TOKEN is not set" in str(exc)
    finally:
        slack_cli.TOKEN = old_token
    print("ok: _call refuses to run without SLACK_USER_TOKEN set")


if __name__ == "__main__":
    test_json_flag_parses_after_search_subcommand()
    test_json_flag_parses_after_search_subcommand_query_only()
    test_history_without_json_parses()
    test_json_flag_parses_after_channels_subcommand()
    test_post_subcommand_removed()
    test_cmd_whoami_renders_identity()
    test_cmd_whoami_json()
    test_cmd_search_renders_matches()
    test_cmd_search_json()
    test_cmd_history_renders_messages_and_files()
    test_cmd_history_json()
    test_cmd_channels_renders_sorted_list()
    test_cmd_channels_json()
    test_check_missing_scope_raises_with_hint()
    test_check_invalid_auth_raises_with_hint()
    test_check_not_in_channel_raises_with_hint()
    test_check_channel_not_found_raises_with_hint()
    test_check_unknown_error_raises_without_hint_crash()
    test_check_ok_payload_passes_through()
    test_call_without_token_raises()
    print("\nALL PASS")
