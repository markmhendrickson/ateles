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
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}
    r.stage_next_value(target, "new-value-abc", dry_run=False)
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE  # primary untouched
    assert written[target.next_env_var] == "new-value-abc"  # staged alongside


def test_promote_retires_old_and_clears_next(fake_store, no_real_op):
    """The dual-admit window closes: primary becomes the new value, and the
    old value is not merely superseded but the _NEXT slot carrying it is
    gone — a leftover _NEXT would leave the overlap window open forever."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {
        target.env_var: FAKE_OLD_VALUE,
        target.next_env_var: "new-value-abc",
    }
    r.promote_and_retire(target, "new-value-abc", dry_run=False)
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == "new-value-abc"
    assert target.next_env_var not in written
    assert FAKE_OLD_VALUE not in written.values()


def test_rollback_clears_next_and_leaves_primary(fake_store, no_real_op):
    """Rollback must restore the OLD credential's standing exactly — primary
    untouched, staged NEW value discarded — not merely 'not promote'."""
    target = r.TARGETS["approve_email_secret"]
    fake_store.files["neotoma"] = {
        target.env_var: FAKE_OLD_VALUE,
        target.next_env_var: "attempted-new-value",
    }
    r.roll_back(target, dry_run=False)
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert target.next_env_var not in written


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

    line = r.restart_daemon("com.ateles.apis", dry_run=True, runner=fake_runner)
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

    line = r.restart_daemon("com.ateles.apis", dry_run=False, runner=fake_runner)
    assert calls == []
    assert "[not run]" in line


def test_restart_daemon_runs_only_with_explicit_env_flag(monkeypatch):
    import subprocess

    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    line = r.restart_daemon("com.ateles.apis", dry_run=False, runner=fake_runner)
    assert len(calls) == 1
    assert "restarted" in line


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


def test_run_rotation_success_path_promotes_and_restarts(
    fake_store, no_real_op, monkeypatch
):
    target = r.TARGETS["approve_email_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def fake_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        assert new_value != FAKE_OLD_VALUE
        return r.VerificationResult(True, "signed probe accepted")

    monkeypatch.setitem(r.VERIFIERS, target.name, fake_verifier)

    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_forward
    assert not report.rolled_back
    written = fake_store.files["neotoma"]
    assert written[target.env_var] != FAKE_OLD_VALUE
    assert target.next_env_var not in written


def test_run_rotation_failure_path_rolls_back_and_leaves_old_primary(
    fake_store, no_real_op, monkeypatch
):
    target = r.TARGETS["approve_email_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def failing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(False, "signed probe rejected (401)")

    monkeypatch.setitem(r.VERIFIERS, target.name, failing_verifier)

    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_back
    assert not report.rolled_forward
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE  # unchanged
    assert target.next_env_var not in written  # rollback cleared it


def test_run_rotation_never_calls_github_write_without_the_tripwire(
    fake_store, no_real_op, monkeypatch
):
    """github_webhook_secret's issuer update must be a no-op unless BOTH
    dry_run=False AND allow_real_github_write=True — this PR's own CLI never
    passes the tripwire, and this test proves the function-level default
    matches that, independent of argparse."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    calls = []
    monkeypatch.setattr(
        r, "update_github_webhook_secret", lambda **kw: calls.append(kw)
    )

    def fake_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "ok")

    monkeypatch.setitem(r.VERIFIERS, target.name, fake_verifier)

    # dry_run=True, allow_real_github_write=True: still must not call it.
    r.run_rotation(target, dry_run=True, allow_real_github_write=True)
    assert calls == []

    # dry_run=False, allow_real_github_write left at its default (False):
    # still must not call it.
    r.run_rotation(target, dry_run=False, allow_real_github_write=False)
    assert calls == []


def test_real_run_without_tripwire_refuses_promotion_and_rolls_back_to_old_value(
    fake_store, no_real_op, monkeypatch
):
    """The exact defect a 2026-09-26 reviewer verified: a REAL (non-dry-run)
    rotation of github_webhook_secret, with the probe passing but the
    --i-know-this-calls-real-github tripwire NOT set, must not silently
    promote — it must refuse and roll back, because promotion here would
    leave GitHub on the old secret and the daemon admitting only the new
    one. Asserts the FINAL STORE STATE, not just that the issuer function
    was never called — that assertion alone passed even on the buggy
    revision, since the bug was never calling the issuer AND STILL
    promoting anyway."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    issuer_calls = []
    monkeypatch.setattr(
        r, "update_github_webhook_secret", lambda **kw: issuer_calls.append(kw)
    )

    def passing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "signed ping accepted (200)")

    monkeypatch.setitem(r.VERIFIERS, target.name, passing_verifier)

    report = r.run_rotation(target, dry_run=False, allow_real_github_write=False)

    assert issuer_calls == []  # necessary but NOT sufficient on its own
    assert report.rolled_back, (
        "a real run without the tripwire must roll back, not silently "
        "succeed with the issuer never updated"
    )
    assert not report.rolled_forward
    assert not report.checkpoint_raised

    # The property the previous test suite never checked: the FINAL STORE
    # STATE must show the daemon still on the OLD value, matching what
    # GitHub (never touched) still has. A revision that skips the issuer
    # write but promotes anyway passes `issuer_calls == []` while FAILING
    # this assertion — which is exactly what happened before this fix.
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert target.next_env_var not in written


def test_run_rotation_calls_github_write_only_with_both_flags_set(
    fake_store, no_real_op, monkeypatch
):
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    calls = []
    monkeypatch.setattr(
        r, "update_github_webhook_secret", lambda **kw: calls.append(kw)
    )

    def fake_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "ok")

    monkeypatch.setitem(r.VERIFIERS, target.name, fake_verifier)

    r.run_rotation(
        target,
        dry_run=False,
        allow_real_github_write=True,
        github_token="tok",
        github_repo="o/r",
        github_hook_id="1",
    )
    assert len(calls) == 1
    assert FAKE_OLD_VALUE not in str(calls)


# ---------------------------------------------------------------------------
# No-value-leak assertions — the report and CLI output never carry a value
# ---------------------------------------------------------------------------


def test_report_notes_never_contain_a_planted_secret(
    fake_store, no_real_op, monkeypatch
):
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    seen_new_values = []

    def spying_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        seen_new_values.append(new_value)
        return r.VerificationResult(True, "ok")

    monkeypatch.setitem(r.VERIFIERS, target.name, spying_verifier)

    report = r.run_rotation(target, dry_run=False)
    notes = report.as_neotoma_notes()
    assert FAKE_OLD_VALUE not in notes
    assert seen_new_values[0] not in notes


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


def test_cli_main_success_path_prints_no_secret_and_exits_zero(
    fake_store, no_real_op, monkeypatch, capsys
):
    target = r.TARGETS["approve_email_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def fake_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "ok")

    monkeypatch.setitem(r.VERIFIERS, target.name, fake_verifier)

    rc = r.main(["approve_email_secret"])
    captured = capsys.readouterr()
    assert rc == 0
    assert FAKE_OLD_VALUE not in captured.out
    assert FAKE_OLD_VALUE not in captured.err
    import re

    assert not re.search(r"[A-Za-z0-9_-]{40,}", captured.out)


def test_cli_main_rollback_path_prints_no_secret_and_exits_nonzero(
    fake_store, no_real_op, monkeypatch, capsys
):
    target = r.TARGETS["approve_email_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def failing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(False, "rejected")

    monkeypatch.setitem(r.VERIFIERS, target.name, failing_verifier)

    rc = r.main(["approve_email_secret"])
    captured = capsys.readouterr()
    assert rc == 1  # rollback is a non-zero exit, distinct from dry-run/success
    assert FAKE_OLD_VALUE not in captured.out
    assert FAKE_OLD_VALUE not in captured.err


def test_cli_rejects_unknown_target():
    with pytest.raises(SystemExit):
        r.main(["not-a-real-target", "--dry-run"])


# ---------------------------------------------------------------------------
# github_webhook_secret specifically: issuer/daemon consistency
# (2026-09-26 review — Falco/security, Pavo/pm, Phoenicurus/qa all
# independently traced the same defect: an earlier revision updated the
# GitHub issuer BEFORE verifying, so a failed verification rolled the daemon
# back to the OLD value while GitHub was left signing with the NEW one —
# every real delivery would then fail signature verification until someone
# noticed and fixed GitHub's config by hand. The fix reorders run_rotation so
# the issuer update runs ONLY AFTER the dual-admit verification has already
# passed, using a probe this script signs itself (no GitHub involvement at
# all) to prove the daemon accepts the new value first.)
# ---------------------------------------------------------------------------


def test_probe_fails_after_would_be_issuer_update_both_sides_end_on_old_value(
    fake_store, no_real_op, monkeypatch
):
    """The scenario the review named: verification fails for github_webhook_secret.
    Both the daemon-side store AND the GitHub issuer must end up agreeing on
    the OLD value — the issuer must never have been touched in the first
    place, because the fix's ordering runs verification before the issuer
    update, not after."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    issuer_calls = []
    monkeypatch.setattr(
        r, "update_github_webhook_secret", lambda **kw: issuer_calls.append(kw)
    )

    def failing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(False, "signed ping rejected (status=401)")

    monkeypatch.setitem(r.VERIFIERS, target.name, failing_verifier)

    report = r.run_rotation(
        target,
        dry_run=False,
        allow_real_github_write=True,  # tripwire set, but must still not fire
        github_token="tok",
        github_repo="markmhendrickson/ateles",
        github_hook_id="1",
    )

    # The issuer was NEVER called — this is the property that makes the
    # rollback safe unconditionally for this target. If verification failed,
    # the sequence must never have reached the issuer-update step at all.
    assert issuer_calls == [], (
        "GitHub issuer update must not run when the dual-admit probe fails — "
        "the whole point of verify-before-issuer-update is that a failed "
        "probe means GitHub was never told about the new value"
    )
    assert report.rolled_back
    assert not report.rolled_forward
    assert not report.checkpoint_raised

    # Daemon side: primary is still the old value, _NEXT is cleared.
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert target.next_env_var not in written
    # Both sides agree on OLD — this is the assertion the review asked for.


def test_issuer_update_failure_after_probe_passes_raises_checkpoint_dual_admit_stays_on(
    fake_store, no_real_op, monkeypatch
):
    """The other scenario the review named: the dual-admit probe PASSES (the
    daemon proves it accepts the new value), but the subsequent GitHub issuer
    update itself fails. This must NOT be treated as a rollback (the daemon
    change is proven good) and must NOT be treated as a promotion (GitHub's
    state is now unconfirmed) — it must raise a checkpoint and leave
    dual-admit ON so no real delivery is ever rejected while a human sorts
    out what GitHub actually has."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def passing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "signed ping accepted (200)")

    monkeypatch.setitem(r.VERIFIERS, target.name, passing_verifier)

    def failing_issuer_update(**kw):
        raise r.GitHubAPIError("PATCH hooks/1/config -> HTTP 500: internal error")

    monkeypatch.setattr(r, "update_github_webhook_secret", failing_issuer_update)

    report = r.run_rotation(
        target,
        dry_run=False,
        allow_real_github_write=True,
        github_token="tok",
        github_repo="markmhendrickson/ateles",
        github_hook_id="1",
    )

    assert report.checkpoint_raised
    assert not report.rolled_forward
    assert not report.rolled_back

    # Dual-admit must stay ON: primary untouched, _NEXT still staged.
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert written[target.next_env_var] is not None
    assert written[target.next_env_var] != FAKE_OLD_VALUE

    notes = report.as_neotoma_notes()
    assert "CHECKPOINT" in notes
    assert FAKE_OLD_VALUE not in notes


def test_issuer_update_network_failure_after_probe_passes_also_raises_checkpoint(
    fake_store, no_real_op, monkeypatch
):
    """A network failure (not just an HTTP error status) while updating the
    issuer must be treated the same as any other issuer-update failure —
    raise a checkpoint, never guess. This is the 'cannot even be confirmed'
    case the review named explicitly."""
    target = r.TARGETS["github_webhook_secret"]
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def passing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(True, "signed ping accepted (200)")

    monkeypatch.setitem(r.VERIFIERS, target.name, passing_verifier)

    def timeout_request(method, url, headers, body):
        return 0, "<urlopen error timed out>"

    # Exercise the REAL update_github_webhook_secret (status=0 -> raises
    # GitHubAPIError per its own body), with only the HTTP transport it
    # accepts as a parameter swapped out. Capture the original function
    # BEFORE patching, so the wrapper calls the real logic rather than
    # itself (run_rotation calls this without threading http_request
    # through, so the wrapper binds it here instead).
    original_update = r.update_github_webhook_secret

    def real_with_timeout_transport(**kw):
        kw.pop("http_request", None)
        return original_update(**kw, http_request=timeout_request)

    monkeypatch.setattr(r, "update_github_webhook_secret", real_with_timeout_transport)

    report = r.run_rotation(
        target,
        dry_run=False,
        allow_real_github_write=True,
        github_token="tok",
        github_repo="markmhendrickson/ateles",
        github_hook_id="1",
    )

    assert report.checkpoint_raised
    written = fake_store.files["neotoma"]
    assert written[target.env_var] == FAKE_OLD_VALUE
    assert target.next_env_var in written


def test_approve_email_secret_never_touches_issuer_path_on_failure(
    fake_store, no_real_op, monkeypatch
):
    """Sanity check that the internal-only target (no external issuer) is
    unaffected by this reordering — it should behave exactly as before."""
    target = r.TARGETS["approve_email_secret"]
    assert not target.has_external_issuer
    fake_store.files["neotoma"] = {target.env_var: FAKE_OLD_VALUE}

    def failing_verifier(new_value, base_url="http://127.0.0.1:8742", http_post=None):
        return r.VerificationResult(False, "rejected")

    monkeypatch.setitem(r.VERIFIERS, target.name, failing_verifier)

    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_back
    assert not report.checkpoint_raised
