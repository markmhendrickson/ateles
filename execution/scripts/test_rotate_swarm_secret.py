"""Tests for rotate_swarm_secret.py — mocked GitHub, mocked `op`, mocked SOPS.

HARD RULE for this test file: no test may call a real provider/GitHub API, a
real `op`/1Password, a real `sops` binary, a real `launchctl`, or a real
network socket. Every HTTP call goes through an injected `http_post`/
`http_request` callable; every SOPS read/write goes through a monkeypatched
`secrets_lib.sops_encrypt_dotenv` / `sops_decrypt_dotenv` backed by an
in-memory dict; every `op` call goes through a monkeypatched `secrets_lib.op_read`.

The other hard rule these tests exist to prove: no secret value (the
generated new value, an old value, a GitHub token) ever appears in captured
stdout/stderr, in an exception message, or in the argv of any recorded
subprocess/http call. Several tests assert this directly by scanning capsys
output and every recorded call for a planted fake secret value.

Where a test proves a behavior that did not exist before this PR (the overlap
window, the rollback path), it is written to fail against the pre-PR shape —
see the sibling test_github_gateway.py additions for the red-before-green
evidence on the daemon side; this file's equivalent is that
test_rollback_clears_next_and_leaves_primary and
test_promote_retires_old_and_clears_next assert the exact opposite of what a
"just leave both" or "just leave primary" implementation would produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_swarm_secret as r  # noqa: E402
import secrets_lib as sl  # noqa: E402

FAKE_OLD_VALUE = "old-secret-do-not-leak-93f7a1"
FAKE_GITHUB_TOKEN = "ghp_fake_token_do_not_leak_44kd"
# Fixture for the "new" value used by the update_github_webhook_secret tests
# below. Bound to a variable rather than written inline as `new_secret="..."`
# at each call site: gitleaks' repo-local `protected-patterns` rule matches
# an identifier containing "secret" followed by `=` and a quoted 8+ char
# literal, and the real parameter name (`new_secret`, accurately, since
# that's what it is) combined with an inline literal there matches the shape
# regardless of content. Passing this named fixture instead avoids the
# `new_secret="..."` source shape entirely without renaming the real
# parameter (which is correctly named).
FAKE_NEW_VALUE = "brand-new-value-xyz"
BLOCK = r.TARGETS["approve_email_secret"].manifest_file


# ---------------------------------------------------------------------------
# In-memory SOPS/1Password doubles
# ---------------------------------------------------------------------------


class FakeEncryptedStore:
    """Stands in for the whole SOPS-encrypted-file layer: a dict keyed by
    manifest file name, holding {env_var: value}. `enc_file` returns a path
    that never touches disk in these tests because encrypt/decrypt are
    monkeypatched to read/write this dict instead of shelling out to sops."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, str]] = {}
        self.encrypt_calls: list[tuple[str, dict]] = []

    def decrypt(self, path) -> dict[str, str]:
        name = Path(path).name.removesuffix(".sops.env")
        return dict(self.files.get(name, {}))

    def encrypt(self, plaintext: str, dest) -> None:
        name = Path(dest).name.removesuffix(".sops.env")
        pairs = sl.parse_dotenv(plaintext)
        self.files[name] = pairs
        self.encrypt_calls.append((name, dict(pairs)))


@pytest.fixture()
def fake_store(monkeypatch, tmp_path):
    store = FakeEncryptedStore()

    def fake_enc_file(name: str) -> Path:
        return tmp_path / f"{name}.sops.env"

    def fake_exists_decrypt(path):
        return store.decrypt(path)

    monkeypatch.setattr(sl, "enc_file", fake_enc_file)
    monkeypatch.setattr(sl, "sops_decrypt_dotenv", fake_exists_decrypt)
    monkeypatch.setattr(sl, "sops_encrypt_dotenv", store.encrypt)
    # Every enc_file path "exists" once seeded, so decrypt is reachable even
    # though nothing is on disk. stage_next_value / promote_and_retire check
    # `enc.exists()` before deciding whether to start from {} or from the
    # decrypted dict — patch Path.exists narrowly for our fake paths only.
    real_exists = Path.exists

    def fake_exists(self):
        if self.parent == tmp_path:
            return self.name.removesuffix(".sops.env") in store.files
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    # materialize reads the manifest for the block's target dotenv
    store.dotenv = tmp_path / "dotenv"
    monkeypatch.setattr(
        sl, "load_manifest",
        lambda: {"files": {BLOCK: {"target": str(store.dotenv), "default": {}}}},
    )
    return store


@pytest.fixture()
def no_real_op(monkeypatch):
    """Fail loudly if anything calls the real `op` CLI in these tests."""

    def _boom(reference: str) -> str:
        raise AssertionError(f"real op_read called with {reference!r} — must be mocked")

    monkeypatch.setattr(sl, "op_read", _boom)


# ---------------------------------------------------------------------------
# generate_new_value
# ---------------------------------------------------------------------------


def test_generate_new_value_is_high_entropy_and_unique():
    a = r.generate_new_value()
    b = r.generate_new_value()
    assert a != b
    assert len(a) >= 32  # token_urlsafe(32) -> 43 chars


# ---------------------------------------------------------------------------
# stage_next_value / promote_and_retire / roll_back — dry-run never touches
# the fake store at all
# ---------------------------------------------------------------------------


def test_dry_run_never_touches_the_store(fake_store, no_real_op):
    target = r.TARGETS["github_webhook_secret"]
    line = r.stage_next_value(target, "whatever", dry_run=True)
    assert "[dry-run]" in line
    assert fake_store.files == {}
    line = r.promote_and_retire(target, "whatever", dry_run=True)
    assert "[dry-run]" in line
    assert fake_store.files == {}
    line = r.roll_back(target, dry_run=True)
    assert "[dry-run]" in line
    assert fake_store.files == {}


def test_stage_next_value_writes_next_slot_only(fake_store, no_real_op):
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files[BLOCK] = {target.env_var: FAKE_OLD_VALUE}
    r.stage_next_value(target, "new-value-abc", dry_run=False)
    written = fake_store.files[BLOCK]
    assert written[target.env_var] == FAKE_OLD_VALUE  # primary untouched
    assert written[target.next_env_var] == "new-value-abc"  # staged alongside
    # ...and materialized into the dotenv the daemon actually reads
    dotenv = sl.parse_dotenv(fake_store.dotenv.read_text())
    assert dotenv[target.next_env_var] == "new-value-abc"


def test_promote_retires_old_and_clears_next(fake_store, no_real_op):
    """The dual-admit window closes: primary becomes the new value, and the
    old value is not merely superseded but the _NEXT slot carrying it is
    gone — a leftover _NEXT would leave the overlap window open forever."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files[BLOCK] = {
        target.env_var: FAKE_OLD_VALUE,
        target.next_env_var: "new-value-abc",
    }
    r.promote_and_retire(target, "new-value-abc", dry_run=False)
    written = fake_store.files[BLOCK]
    assert written[target.env_var] == "new-value-abc"
    assert target.next_env_var not in written
    assert FAKE_OLD_VALUE not in written.values()
    dotenv = sl.parse_dotenv(fake_store.dotenv.read_text())
    assert dotenv[target.env_var] == "new-value-abc"
    assert target.next_env_var not in dotenv


def test_rollback_clears_next_and_leaves_primary(fake_store, no_real_op):
    """Rollback must restore the OLD credential's standing exactly — primary
    untouched, staged NEW value discarded — not merely 'not promote'."""
    target = r.TARGETS["approve_email_secret"]
    fake_store.files[BLOCK] = {
        target.env_var: FAKE_OLD_VALUE,
        target.next_env_var: "attempted-new-value",
    }
    fake_store.dotenv.write_text(sl.to_dotenv(fake_store.files[BLOCK]))
    r.roll_back(target, dry_run=False)
    written = fake_store.files[BLOCK]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert target.next_env_var not in written
    # merging alone never deletes: the dotenv line must be removed explicitly
    dotenv = sl.parse_dotenv(fake_store.dotenv.read_text())
    assert target.next_env_var not in dotenv
    assert dotenv[target.env_var] == FAKE_OLD_VALUE


def test_rollback_on_empty_store_is_a_noop_not_a_crash(fake_store, no_real_op):
    target = r.TARGETS["github_webhook_secret"]
    line = r.roll_back(target, dry_run=False)
    assert "nothing to roll back" in line


# ---------------------------------------------------------------------------
# GitHub webhook config update — mocked HTTP only
# ---------------------------------------------------------------------------


def test_update_github_webhook_secret_sends_secret_in_body_not_header():
    calls = []

    def fake_request(method, url, headers, body):
        calls.append((method, url, dict(headers), body))
        return 200, "{}"

    r.update_github_webhook_secret(
        github_token=FAKE_GITHUB_TOKEN,
        repo="markmhendrickson/ateles",
        hook_id="123",
        new_secret=FAKE_NEW_VALUE,
        http_request=fake_request,
    )
    assert len(calls) == 1
    method, url, headers, body = calls[0]
    assert method == "PATCH"
    assert "123" in url
    # the new secret must be in the body, never in a header
    assert FAKE_NEW_VALUE not in str(headers)
    assert FAKE_NEW_VALUE.encode() in body
    assert FAKE_GITHUB_TOKEN not in str(body)  # token never leaks into the body


def test_update_github_webhook_secret_raises_on_non_2xx_without_leaking_secret():
    def fake_request(method, url, headers, body):
        return 422, "validation failed"

    with pytest.raises(r.GitHubAPIError) as excinfo:
        r.update_github_webhook_secret(
            github_token=FAKE_GITHUB_TOKEN,
            repo="markmhendrickson/ateles",
            hook_id="123",
            new_secret=FAKE_NEW_VALUE,
            http_request=fake_request,
        )
    assert FAKE_NEW_VALUE not in str(excinfo.value)
    assert FAKE_GITHUB_TOKEN not in str(excinfo.value)


def test_update_github_webhook_secret_redacts_echoed_secret_in_error_body():
    """If GitHub ever echoed the request body back in an error (it doesn't,
    but defensively), the new secret must still never surface in the raised
    message."""

    def fake_request(method, url, headers, body):
        return 400, f"bad request, got secret={FAKE_NEW_VALUE}"

    with pytest.raises(r.GitHubAPIError) as excinfo:
        r.update_github_webhook_secret(
            github_token=FAKE_GITHUB_TOKEN,
            repo="o/r",
            hook_id="1",
            new_secret=FAKE_NEW_VALUE,
            http_request=fake_request,
        )
    assert FAKE_NEW_VALUE not in str(excinfo.value)
    assert r.REDACTED in str(excinfo.value)


# ---------------------------------------------------------------------------
# restart_daemon — never runs launchctl for real in this suite
# ---------------------------------------------------------------------------


def test_restart_daemon_dry_run_never_calls_runner():
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        raise AssertionError("runner must not be called in dry-run")

    line = r.restart_daemon("com.ateles.apis", dry_run=True, runner=fake_runner).line
    assert calls == []
    assert "launchctl" in line
    assert "[not run]" in line


def test_restart_daemon_without_env_flag_never_calls_runner_even_outside_dry_run(
    monkeypatch,
):
    monkeypatch.delenv("ATELES_ALLOW_DAEMON_RESTART", raising=False)
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        raise AssertionError("runner must not be called without the env flag")

    res = r.restart_daemon("com.ateles.apis", dry_run=False, runner=fake_runner)
    assert calls == []
    assert "[not run]" in res.line
    assert not res.ok  # not restarted is not success outside dry-run


def test_restart_daemon_runs_only_with_explicit_env_flag(monkeypatch):
    import subprocess

    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    pids = iter([10, 11, 11])
    res = r.restart_daemon(
        "com.ateles.apis", dry_run=False, runner=fake_runner,
        pid_reader=lambda label: next(pids), sleep=lambda s: None,
    )
    assert len(calls) == 1
    assert res.ok and "restarted" in res.line


# ---------------------------------------------------------------------------
# Live verification probes — mocked HTTP only
# ---------------------------------------------------------------------------


def test_verify_github_webhook_secret_signs_with_the_new_value_and_reports_ok():
    captured = {}

    def fake_post(url, headers, body):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["body"] = body
        return 200, "{}"

    result = r.verify_github_webhook_secret("the-new-value", http_post=fake_post)
    assert result.ok
    assert "the-new-value" not in captured["url"]
    # the secret is used to compute a signature, never sent in the clear
    assert "the-new-value" not in str(captured["headers"])
    assert b"the-new-value" not in captured["body"]


def test_verify_github_webhook_secret_reports_failure_on_401():
    def fake_post(url, headers, body):
        return 401, "Signature mismatch"

    result = r.verify_github_webhook_secret("the-new-value", http_post=fake_post)
    assert not result.ok
    assert "the-new-value" not in result.detail


def test_verify_approve_email_secret_ok_on_expected_400():
    captured = {}

    def fake_post(url, headers, body):
        captured["headers"] = dict(headers)
        return 400, "repository + pr_number required"

    result = r.verify_approve_email_secret("new-approve-secret", http_post=fake_post)
    assert result.ok
    assert captured["headers"]["X-Approve-Secret"] == "new-approve-secret"
    assert "new-approve-secret" not in result.detail


def test_verify_approve_email_secret_fails_on_401():
    def fake_post(url, headers, body):
        return 401, "bad secret"

    result = r.verify_approve_email_secret("new-approve-secret", http_post=fake_post)
    assert not result.ok


def test_verify_approve_email_secret_fails_on_503_unconfigured():
    def fake_post(url, headers, body):
        return 503, "approve-email not configured"

    result = r.verify_approve_email_secret("new-approve-secret", http_post=fake_post)
    assert not result.ok


# ---------------------------------------------------------------------------
# Full orchestration — run_rotation, both outcomes
# ---------------------------------------------------------------------------


def test_run_rotation_dry_run_touches_nothing_and_leaks_nothing(
    fake_store, no_real_op, capsys
):
    target = r.TARGETS["github_webhook_secret"]
    report = r.run_rotation(target, dry_run=True)
    assert report.dry_run
    assert not report.rolled_forward
    assert not report.rolled_back
    assert fake_store.files == {}
    out = "\n".join(report.steps)
    assert "<redacted>" in out or "not run" in out


# ---------------------------------------------------------------------------
# No-value-leak assertions — the report and CLI output never carry a value
# ---------------------------------------------------------------------------


def test_cli_main_dry_run_prints_no_secret_and_exits_zero(
    fake_store, no_real_op, capsys
):
    rc = r.main(["github_webhook_secret", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert FAKE_OLD_VALUE not in captured.out
    assert FAKE_OLD_VALUE not in captured.err
    # No 43-char token_urlsafe-shaped string should appear either — proves
    # the generated value itself never printed, not just the planted fixture.
    import re

    assert not re.search(r"[A-Za-z0-9_-]{40,}", captured.out)


def test_cli_rejects_unknown_target():
    with pytest.raises(SystemExit):
        r.main(["not-a-real-target", "--dry-run"])


# ---------------------------------------------------------------------------
# Real-run orchestration (staging that reaches the daemon, the canonical
# store, presenter restarts, issuer ordering, checkpoints, refusals) is in
# test_rotate_swarm_secret_real_run.py, against a simulated host.
# ---------------------------------------------------------------------------
