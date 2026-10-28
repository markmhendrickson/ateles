"""A real (non-dry-run) rotation, end to end, against a simulated host.

test_rotate_swarm_secret.py proves each step function in isolation. This file
proves the thing those tests could not see: that a real run actually changes
what the running daemons hold. It simulates the whole path a value travels —

    1Password  ->  secrets/<block>.sops.enc  ->  materialized dotenv  ->  daemon

— with daemons that, like the real apis and turdus, load their secrets only
when restarted, from the file they really read (the materialized dotenv).

Nothing here calls a real `op`, `sops`, `launchctl`, GitHub, or socket.

Three tests below are the acceptance tests for Neotoma task
ent_be38dd0cca491c8c9c757ffd, and were run red against origin/main before the
fix (each fails there on an assertion, not an import or signature error —
every seam they patch exists on main, and the ones that do not are patched
with raising=False so main simply never calls them):

  (1) test_staged_value_reaches_the_dotenv_the_daemon_reads
  (2) test_promotion_writes_the_canonical_store_and_a_republish_keeps_it
  (3) test_approve_promotion_restarts_turdus_and_apis_accepts_what_it_presents
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_swarm_secret as r  # noqa: E402
import secrets_lib as sl  # noqa: E402

OLD_APPROVE = "old-approve-value-do-not-leak-71c2"
OLD_WEBHOOK = "old-webhook-value-do-not-leak-0b9e"
APIS = "com.ateles.apis"
REAL_UPDATE = r.update_github_webhook_secret  # captured before any fixture patches it
TURDUS = "com.ateles.turdus"


class FakeHost:
    """The operator's machine, reduced to what a rotation touches."""

    def __init__(self, tmp_path: Path, monkeypatch, *, daemons_read_from: str = "dotenv"):
        self.tmp = tmp_path
        self.dotenv = tmp_path / "neotoma.env"
        self.daemons_read_from = daemons_read_from
        self.block = r.TARGETS["approve_email_secret"].manifest_file
        self.refs = {
            "APIS_APPROVE_EMAIL_SECRET": "op://Private/swarm-shared/approve_email",
            "APIS_GITHUB_WEBHOOK_SECRET": "op://Private/swarm-shared/github_webhook",
        }
        self.onepassword = {
            self.refs["APIS_APPROVE_EMAIL_SECRET"]: OLD_APPROVE,
            self.refs["APIS_GITHUB_WEBHOOK_SECRET"]: OLD_WEBHOOK,
        }
        self.events: list[tuple[str, str]] = []
        self.pids = {APIS: 100, TURDUS: 200}
        self.github: dict[str, str] = {}  # "repo#hook" -> secret GitHub signs with
        self.unrelated = "UNRELATED_KEY"

        # Starting state: everything agrees on the old values, and the dotenv
        # also carries a key no block manages (it must survive untouched).
        initial = {
            "APIS_APPROVE_EMAIL_SECRET": OLD_APPROVE,
            "APIS_GITHUB_WEBHOOK_SECRET": OLD_WEBHOOK,
        }
        self.snapshot_path(self.block).write_text(sl.to_dotenv(initial))
        self.dotenv.write_text(
            sl.to_dotenv({**initial, self.unrelated: "keep-me"})
        )

        monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setattr(sl, "load_manifest", self.manifest)
        monkeypatch.setattr(sl, "enc_file", self.snapshot_path)
        monkeypatch.setattr(sl, "sops_encrypt_dotenv", self.encrypt)
        monkeypatch.setattr(sl, "sops_decrypt_dotenv", self.decrypt)
        monkeypatch.setattr(sl, "op_read", self.op_read)
        monkeypatch.setattr(r, "op_write_password_field", self.op_write, raising=False)
        monkeypatch.setattr(r, "restart_daemon", self.restart)
        monkeypatch.setitem(r.VERIFIERS, "approve_email_secret", self.probe_approve)
        monkeypatch.setitem(r.VERIFIERS, "github_webhook_secret", self.probe_webhook)
        monkeypatch.setattr(
            r, "PRESENTER_VERIFIERS", {TURDUS: self.turdus_side_check}, raising=False
        )
        monkeypatch.setattr(r, "update_github_webhook_secret", self.github_patch)
        monkeypatch.setattr(r, "github_ping_check", self.github_ping, raising=False)

        # Daemons as currently running: loaded at their last start.
        self.apis_admits = {"approve": {OLD_APPROVE}, "webhook": {OLD_WEBHOOK}}
        self.turdus_presents = OLD_APPROVE

    # -- secrets pipeline -------------------------------------------------
    def manifest(self) -> dict:
        return {"files": {self.block: {"target": str(self.dotenv), "default": dict(self.refs)}}}

    def snapshot_path(self, name: str) -> Path:
        return self.tmp / f"{name}.sops.enc"

    def encrypt(self, plaintext: str, dest) -> None:
        Path(dest).write_text(plaintext)
        self.events.append(("snapshot_write", Path(dest).name))

    def decrypt(self, src) -> dict:
        return sl.parse_dotenv(Path(src).read_text())

    def snapshot(self) -> dict:
        return self.decrypt(self.snapshot_path(self.block))

    def dotenv_values(self) -> dict:
        return sl.parse_dotenv(self.dotenv.read_text())

    def op_read(self, ref: str) -> str:
        return self.onepassword[ref]

    def op_write(self, item_ref: str, field_label: str, value: str) -> None:
        self.onepassword[item_ref] = value
        self.events.append(("onepassword_write", item_ref))

    def republish(self) -> dict:
        """What the next secrets_publish.py would write: 1Password -> snapshot."""
        return {var: self.onepassword[ref] for var, ref in self.refs.items()}

    # -- daemons ----------------------------------------------------------
    def _source(self) -> dict:
        return self.dotenv_values() if self.daemons_read_from == "dotenv" else self.snapshot()

    def restart(self, label: str, *, dry_run: bool, **_kw):
        if dry_run:
            return SimpleNamespace(ok=True, line=f"[not run] {label}")
        env = self._source()
        old = self.pids[label]
        self.pids[label] = old + 1
        self.events.append(("restart", label))
        if label == APIS:
            self.apis_admits = {
                "approve": {v for v in (env.get("APIS_APPROVE_EMAIL_SECRET"),
                                        env.get("APIS_APPROVE_EMAIL_SECRET_NEXT")) if v},
                "webhook": {v for v in (env.get("APIS_GITHUB_WEBHOOK_SECRET"),
                                        env.get("APIS_GITHUB_WEBHOOK_SECRET_NEXT")) if v},
            }
        elif label == TURDUS:
            self.turdus_presents = env.get("APIS_APPROVE_EMAIL_SECRET")
        return SimpleNamespace(ok=True, line=f"restarted {label}: pid {old} -> {old + 1}",
                               old_pid=old, new_pid=old + 1)

    def probe_approve(self, value, base_url=None, http_post=None):
        ok = value in self.apis_admits["approve"]
        return SimpleNamespace(ok=ok, detail="400" if ok else "401", status=400 if ok else 401)

    def probe_webhook(self, value, base_url=None, http_post=None):
        ok = value in self.apis_admits["webhook"]
        return SimpleNamespace(ok=ok, detail="200" if ok else "401", status=200 if ok else 401)

    def turdus_side_check(self, **_kw):
        ok = self.turdus_presents in self.apis_admits["approve"]
        return SimpleNamespace(ok=ok, detail=f"turdus side {'400' if ok else '401'}",
                               status=400 if ok else 401)

    # -- GitHub -----------------------------------------------------------
    def github_patch(self, *, github_token, repo, hook_id, new_secret, **_kw):
        self.github[f"{repo}#{hook_id}"] = new_secret

    def github_ping(self, *, github_token, repo, hook_id, **_kw):
        ok = self.github.get(f"{repo}#{hook_id}", OLD_WEBHOOK) in self.apis_admits["webhook"]
        return SimpleNamespace(ok=ok, detail=f"github ping {'200' if ok else '401'}",
                               status=200 if ok else 401)


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return FakeHost(tmp_path, monkeypatch)


@pytest.fixture()
def host_reading_snapshot(tmp_path, monkeypatch):
    """Daemons that read the snapshot directly — isolates defects (2) and (3)
    from defect (1), so each acceptance test fails on main for its own reason."""
    return FakeHost(tmp_path, monkeypatch, daemons_read_from="snapshot")


# ---------------------------------------------------------------------------
# Acceptance tests (red on origin/main, green with the fix)
# ---------------------------------------------------------------------------


def test_staged_value_reaches_the_dotenv_the_daemon_reads(host):
    """Defect (1): staging wrote only the snapshot, and apis reads the
    materialized dotenv, so the restarted daemon never admitted the new value
    and every real run rolled back."""
    target = r.TARGETS["approve_email_secret"]
    report = r.run_rotation(target, dry_run=False)

    assert report.rolled_forward, report.steps
    env = host.dotenv_values()
    new = env[target.env_var]
    assert new != OLD_APPROVE
    assert target.next_env_var not in env  # the overlap window closed in the file too
    assert host.apis_admits["approve"] == {new}
    assert env[host.unrelated] == "keep-me"  # unmanaged keys survive materialize


def test_promotion_writes_the_canonical_store_and_a_republish_keeps_it(
    host_reading_snapshot,
):
    """Defect (2): promotion never wrote 1Password, so the next
    secrets_publish.py would restore the old, exposed value."""
    host = host_reading_snapshot
    target = r.TARGETS["approve_email_secret"]
    report = r.run_rotation(target, dry_run=False)

    assert report.rolled_forward, report.steps
    promoted = host.snapshot()[target.env_var]
    assert promoted != OLD_APPROVE
    assert host.onepassword[host.refs[target.env_var]] == promoted
    assert host.republish()[target.env_var] == promoted


def test_approve_promotion_restarts_turdus_and_apis_accepts_what_it_presents(
    host_reading_snapshot,
):
    """Defect (3): promotion restarted only apis, so turdus kept presenting
    the retired value and operator email approvals got 401."""
    host = host_reading_snapshot
    target = r.TARGETS["approve_email_secret"]
    report = r.run_rotation(target, dry_run=False)

    assert report.rolled_forward, report.steps
    restarts = [label for kind, label in host.events if kind == "restart"]
    assert TURDUS in restarts
    last_write = max(i for i, (kind, _) in enumerate(host.events) if kind == "snapshot_write")
    turdus_restart = max(i for i, (kind, label) in enumerate(host.events)
                         if kind == "restart" and label == TURDUS)
    assert turdus_restart > last_write  # restarted after it could load the new value
    assert host.turdus_presents in host.apis_admits["approve"]
    assert OLD_APPROVE not in host.apis_admits["approve"]


# ---------------------------------------------------------------------------
# The rest of a real run
# ---------------------------------------------------------------------------


def test_presenters_restart_before_the_verifier_on_promotion(host):
    """No window where apis admits only the new value while turdus sends the old."""
    r.run_rotation(r.TARGETS["approve_email_secret"], dry_run=False)
    restarts = [label for kind, label in host.events if kind == "restart"]
    # stage restart (apis), then promotion: turdus, then apis
    assert restarts == [APIS, TURDUS, APIS]


def test_failed_dual_admit_check_rolls_back_file_and_daemon(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    monkeypatch.setitem(
        r.VERIFIERS, target.name,
        lambda value, base_url=None, http_post=None: r.VerificationResult(False, "401", 401),
    )
    report = r.run_rotation(target, dry_run=False)

    assert report.rolled_back and not report.rolled_forward
    assert host.snapshot()[target.env_var] == OLD_APPROVE
    assert target.next_env_var not in host.snapshot()
    assert target.next_env_var not in host.dotenv_values()  # removed from the file, not just the snapshot
    assert host.apis_admits["approve"] == {OLD_APPROVE}
    assert host.onepassword[host.refs[target.env_var]] == OLD_APPROVE
    assert host.github == {}  # the internal-only target never touches an issuer


def test_canonical_store_failure_rolls_back_before_promotion(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]

    def broken_write(item_ref, field_label, value):
        raise RuntimeError("op: not signed in")

    monkeypatch.setattr(r, "op_write_password_field", broken_write)
    report = r.run_rotation(target, dry_run=False)

    assert report.rolled_back
    assert host.snapshot()[target.env_var] == OLD_APPROVE
    assert target.next_env_var not in host.dotenv_values()
    assert host.apis_admits["approve"] == {OLD_APPROVE}


def test_canonical_read_back_mismatch_is_a_failure(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    monkeypatch.setattr(r, "op_write_password_field", lambda *a: None)  # silently drops
    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_back
    assert any("read-back does not match" in s for s in report.steps)


def test_post_promotion_presenter_failure_reopens_dual_admit_with_old_value(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    monkeypatch.setattr(
        r, "PRESENTER_VERIFIERS",
        {TURDUS: lambda **kw: r.VerificationResult(False, "turdus side 401", 401)},
    )
    report = r.run_rotation(target, dry_run=False)

    assert report.checkpoint_raised and not report.rolled_forward
    env = host.dotenv_values()
    assert env[target.next_env_var] == OLD_APPROVE  # old value admitted again
    assert host.apis_admits["approve"] == {env[target.env_var], OLD_APPROVE}


def test_old_value_still_admitted_after_promotion_is_a_checkpoint(host, monkeypatch):
    """The old value must be REJECTED after promotion — accepted means it was
    never retired."""
    target = r.TARGETS["approve_email_secret"]
    monkeypatch.setitem(
        r.VERIFIERS, target.name,
        lambda value, base_url=None, http_post=None: r.VerificationResult(True, "400", 400),
    )
    report = r.run_rotation(target, dry_run=False)
    assert report.checkpoint_raised
    assert any("old value retired" in s and "FAILED" in s for s in report.steps)


def test_restart_not_confirmed_rolls_back(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    real_restart = host.restart
    calls = {"n": 0}

    def flaky(label, *, dry_run, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return r.RestartResult(False, f"restart NOT CONFIRMED: {label}")
        return real_restart(label, dry_run=dry_run)

    monkeypatch.setattr(r, "restart_daemon", flaky)
    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_back
    assert target.next_env_var not in host.dotenv_values()


# ---------------------------------------------------------------------------
# Preflight refusals — nothing written, nothing restarted
# ---------------------------------------------------------------------------


def _assert_untouched(host, target, report):
    assert report.refused
    assert not any(kind in ("snapshot_write", "restart", "onepassword_write")
                   for kind, _ in host.events)
    assert host.snapshot()[target.env_var] in (OLD_APPROVE, OLD_WEBHOOK)


def test_refuses_without_restart_permission(host, monkeypatch):
    monkeypatch.delenv("ATELES_ALLOW_DAEMON_RESTART")
    target = r.TARGETS["approve_email_secret"]
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_refuses_when_1password_disagrees_with_the_snapshot(host):
    target = r.TARGETS["approve_email_secret"]
    host.onepassword[host.refs[target.env_var]] = "someone-else-changed-it"
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_refuses_when_1password_is_unreachable(host, monkeypatch):
    def down(ref):
        raise RuntimeError("connecting to desktop app timed out")

    monkeypatch.setattr(sl, "op_read", down)
    target = r.TARGETS["approve_email_secret"]
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_refuses_when_dotenv_disagrees_with_the_snapshot(host):
    target = r.TARGETS["approve_email_secret"]
    env = host.dotenv_values()
    env[target.env_var] = "hand-edited"
    host.dotenv.write_text(sl.to_dotenv(env))
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_refuses_on_a_leftover_next_slot(host):
    target = r.TARGETS["approve_email_secret"]
    env = host.dotenv_values()
    env[target.next_env_var] = "left-over"
    host.dotenv.write_text(sl.to_dotenv(env))
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_refuses_when_manifest_does_not_map_the_target(host):
    target = r.TARGETS["approve_email_secret"]
    del host.refs[target.env_var]
    _assert_untouched(host, target, r.run_rotation(target, dry_run=False))


def test_webhook_refuses_without_tripwire_or_hooks(host):
    target = r.TARGETS["github_webhook_secret"]
    _assert_untouched(host, target, r.run_rotation(
        target, dry_run=False, github_token="tok",
        github_hooks=[("o/a", "1")], allow_real_github_write=False))
    _assert_untouched(host, target, r.run_rotation(
        target, dry_run=False, github_token="tok",
        github_hooks=[], allow_real_github_write=True))


def test_cli_exit_code_3_on_refusal(host, monkeypatch, capsys):
    monkeypatch.delenv("ATELES_ALLOW_DAEMON_RESTART")
    assert r.main(["approve_email_secret"]) == 3


# ---------------------------------------------------------------------------
# github_webhook_secret, real run
# ---------------------------------------------------------------------------

HOOKS = [("o/ateles", "11"), ("o/neotoma", "22")]


def test_webhook_rotation_updates_every_hook_and_github_ping_passes(host):
    target = r.TARGETS["github_webhook_secret"]
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.rolled_forward, report.steps
    new = host.dotenv_values()[target.env_var]
    assert new != OLD_WEBHOOK
    assert host.github == {"o/ateles#11": new, "o/neotoma#22": new}
    assert host.onepassword[host.refs[target.env_var]] == new
    assert host.apis_admits["webhook"] == {new}


def test_webhook_issuer_failure_is_a_checkpoint_and_restores_1password(host, monkeypatch):
    target = r.TARGETS["github_webhook_secret"]
    patched = []

    def second_hook_fails(*, github_token, repo, hook_id, new_secret, **kw):
        if hook_id == "22":
            raise r.GitHubAPIError("PATCH -> HTTP 500")
        patched.append(repo)
        host.github[f"{repo}#{hook_id}"] = new_secret

    monkeypatch.setattr(r, "update_github_webhook_secret", second_hook_fails)
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.checkpoint_raised
    snap = host.snapshot()
    assert snap[target.env_var] == OLD_WEBHOOK
    assert snap[target.next_env_var] == host.github["o/ateles#11"]  # dual-admit stays on
    assert host.apis_admits["webhook"] == {OLD_WEBHOOK, host.github["o/ateles#11"]}
    assert host.onepassword[host.refs[target.env_var]] == OLD_WEBHOOK


def test_webhook_ping_failure_after_promotion_reopens_dual_admit(host, monkeypatch):
    target = r.TARGETS["github_webhook_secret"]
    monkeypatch.setattr(
        r, "github_ping_check",
        lambda **kw: r.VerificationResult(False, "github ping 401", 401),
    )
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.checkpoint_raised
    assert OLD_WEBHOOK in host.apis_admits["webhook"]


def test_failed_probe_never_reaches_the_issuer(host, monkeypatch):
    """Ordering fix from the 2026-09-26 review, kept: verify under dual-admit
    with a self-signed probe BEFORE GitHub is told anything, so a failed probe
    leaves GitHub and the daemon both on the old value."""
    target = r.TARGETS["github_webhook_secret"]
    monkeypatch.setitem(
        r.VERIFIERS, target.name,
        lambda value, base_url=None, http_post=None: r.VerificationResult(False, "401", 401),
    )
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.rolled_back
    assert host.github == {}  # never called
    assert host.snapshot()[target.env_var] == OLD_WEBHOOK
    assert host.apis_admits["webhook"] == {OLD_WEBHOOK}


def test_issuer_network_failure_is_a_checkpoint_not_a_guess(host, monkeypatch):
    """The real update function with a transport that times out (status 0)."""
    target = r.TARGETS["github_webhook_secret"]

    def real_update_with_timeout(**kw):
        kw.pop("http_request", None)
        return REAL_UPDATE(**kw, http_request=lambda *a: (0, "<urlopen error timed out>"))

    monkeypatch.setattr(r, "update_github_webhook_secret", real_update_with_timeout)
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.checkpoint_raised
    assert target.next_env_var in host.snapshot()  # dual-admit left on


def test_refused_webhook_run_never_calls_the_issuer(host):
    target = r.TARGETS["github_webhook_secret"]
    r.run_rotation(target, dry_run=False, github_token="tok",
                   github_hooks=HOOKS, allow_real_github_write=False)
    assert host.github == {}


def test_report_notes_never_contain_a_value(host):
    report = r.run_rotation(r.TARGETS["approve_email_secret"], dry_run=False)
    notes = report.as_neotoma_notes()
    new = host.dotenv_values()["APIS_APPROVE_EMAIL_SECRET"]
    assert OLD_APPROVE not in notes and new not in notes


# ---------------------------------------------------------------------------
# No value ever printed, on any path
# ---------------------------------------------------------------------------


def test_real_run_output_never_contains_a_value(host, capsys):
    rc = r.main(["approve_email_secret"])
    out = capsys.readouterr()
    new = host.dotenv_values()["APIS_APPROVE_EMAIL_SECRET"]
    assert rc == 0
    for value in (OLD_APPROVE, new, OLD_WEBHOOK):
        assert value not in out.out and value not in out.err


# ---------------------------------------------------------------------------
# restart_daemon: the pid read-back (defect 4)
# ---------------------------------------------------------------------------


def _kickstart_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_restart_reports_success_only_on_a_new_stable_pid(monkeypatch):
    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
    pids = iter([100, 100, 101, 101])
    res = r.restart_daemon(APIS, dry_run=False, runner=_kickstart_ok,
                           pid_reader=lambda label: next(pids), sleep=lambda s: None)
    assert res.ok
    assert (res.old_pid, res.new_pid) == (100, 101)
    assert "100 -> 101" in res.line


def test_restart_unchanged_pid_is_not_a_restart(monkeypatch):
    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
    res = r.restart_daemon(APIS, dry_run=False, runner=_kickstart_ok,
                           pid_reader=lambda label: 100, sleep=lambda s: None, timeout=2)
    assert not res.ok
    assert "NOT CONFIRMED" in res.line


def test_restart_crash_loop_is_not_a_restart(monkeypatch):
    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")
    pids = iter([100, 101, None])
    res = r.restart_daemon(APIS, dry_run=False, runner=_kickstart_ok,
                           pid_reader=lambda label: next(pids), sleep=lambda s: None)
    assert not res.ok
    assert "UNSTABLE" in res.line


def test_read_launchd_pid_parses_the_list_table_only():
    table = "PID\tStatus\tLabel\n1881\t0\tcom.ateles.turdus\n-\t0\tcom.ateles.apis\n"

    def fake_run(cmd, **kw):
        assert cmd == ["launchctl", "list"]  # never `print`, which dumps the environment
        return subprocess.CompletedProcess(cmd, 0, stdout=table, stderr="")

    assert r.read_launchd_pid(TURDUS, runner=fake_run) == 1881
    assert r.read_launchd_pid(APIS, runner=fake_run) is None
    assert r.read_launchd_pid("com.ateles.absent", runner=fake_run) is None


# ---------------------------------------------------------------------------
# The turdus-side check: what the child is given
# ---------------------------------------------------------------------------


def test_turdus_side_check_strips_inherited_secret_and_reads_status(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_APPROVE_EMAIL_SECRET", "inherited-must-not-be-used")
    monkeypatch.setenv("ATELES_SKIP_DOTENV", "1")
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="status=400\n", stderr="")

    res = r.verify_turdus_presents_accepted_secret(daemon_checkout=tmp_path, runner=fake_run)
    assert res.ok
    assert "APIS_APPROVE_EMAIL_SECRET" not in seen["env"]
    assert "ATELES_SKIP_DOTENV" not in seen["env"]
    assert "inherited-must-not-be-used" not in " ".join(seen["cmd"])

    def rejected(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="status=401\n", stderr="")

    assert not r.verify_turdus_presents_accepted_secret(
        daemon_checkout=tmp_path, runner=rejected).ok


# ---------------------------------------------------------------------------
# GitHub's own ping, read back from the deliveries API
# ---------------------------------------------------------------------------


def test_github_ping_check_reads_the_new_delivery_status():
    import json

    calls = []
    state = {"pinged": False}

    def fake(method, url, headers, body):
        calls.append((method, url))
        if url.endswith("/pings"):
            state["pinged"] = True
            return 204, ""
        deliveries = [{"id": 5, "event": "push", "status_code": 200}]
        if state["pinged"]:
            deliveries.insert(0, {"id": 9, "event": "ping", "status_code": 200})
        return 200, json.dumps(deliveries)

    res = r.github_ping_check(github_token="tok", repo="o/r", hook_id="1",
                              http_request=fake, sleep=lambda s: None)
    assert res.ok
    assert ("POST", "https://api.github.com/repos/o/r/hooks/1/pings") in calls


def test_github_ping_check_fails_on_a_401_delivery():
    import json

    state = {"pinged": False}

    def fake(method, url, headers, body):
        if url.endswith("/pings"):
            state["pinged"] = True
            return 204, ""
        out = [{"id": 9, "event": "ping", "status_code": 401}] if state["pinged"] else []
        return 200, json.dumps(out)

    res = r.github_ping_check(github_token="tok", repo="o/r", hook_id="1",
                              http_request=fake, sleep=lambda s: None)
    assert not res.ok


# ---------------------------------------------------------------------------
# A check that cannot run is a failed check: no exception escapes the
# rollback / re-open paths (security lens, round 1)
# ---------------------------------------------------------------------------


def test_probe_exception_before_promotion_rolls_back(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]

    def reset(value, base_url=None, http_post=None):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setitem(r.VERIFIERS, target.name, reset)
    report = r.run_rotation(target, dry_run=False)
    assert report.rolled_back and not report.checkpoint_raised
    assert target.next_env_var not in host.snapshot()
    assert target.next_env_var not in host.dotenv_values()
    assert host.apis_admits["approve"] == {OLD_APPROVE}
    assert host.onepassword[host.refs[target.env_var]] == OLD_APPROVE


def test_presenter_exception_after_promotion_reopens_dual_admit(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]

    def timeout(**kw):
        raise subprocess.TimeoutExpired(cmd="python3", timeout=60)

    monkeypatch.setattr(r, "PRESENTER_VERIFIERS", {TURDUS: timeout})
    report = r.run_rotation(target, dry_run=False)
    assert report.checkpoint_raised and not report.rolled_forward
    env = host.dotenv_values()
    assert env[target.next_env_var] == OLD_APPROVE
    assert OLD_APPROVE in host.apis_admits["approve"]


def test_unexpected_issuer_exception_is_a_checkpoint(host, monkeypatch):
    target = r.TARGETS["github_webhook_secret"]

    def boom(**kw):
        raise ValueError("unexpected")

    monkeypatch.setattr(r, "update_github_webhook_secret", boom)
    report = r.run_rotation(target, dry_run=False, github_token="tok",
                            github_hooks=HOOKS, allow_real_github_write=True)
    assert report.checkpoint_raised
    assert target.next_env_var in host.snapshot()
    assert host.onepassword[host.refs[target.env_var]] == OLD_WEBHOOK


def test_preflight_exception_is_a_refusal(host, monkeypatch):
    def broken_decrypt(src):
        raise RuntimeError("sops decrypt failed")

    monkeypatch.setattr(sl, "sops_decrypt_dotenv", broken_decrypt)
    report = r.run_rotation(r.TARGETS["approve_email_secret"], dry_run=False)
    assert report.refused
    assert not any(kind in ("snapshot_write", "restart", "onepassword_write")
                   for kind, _ in host.events)


def test_reopen_failure_does_not_claim_dual_admit_is_on(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    monkeypatch.setattr(
        r, "PRESENTER_VERIFIERS",
        {TURDUS: lambda **kw: r.VerificationResult(False, "turdus side 401", 401)},
    )

    def broken_reopen(t, old):
        raise RuntimeError("sops encrypt failed")

    monkeypatch.setattr(r, "reopen_dual_admit", broken_reopen)
    report = r.run_rotation(target, dry_run=False)
    assert report.checkpoint_raised
    text = "\n".join(report.steps)
    assert "re-opening dual-admit FAILED" in text
    assert "is back in" not in text


def test_restart_daemon_never_raises(monkeypatch):
    monkeypatch.setenv("ATELES_ALLOW_DAEMON_RESTART", "1")

    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    res = r.restart_daemon(APIS, dry_run=False, runner=hangs,
                           pid_reader=lambda label: 100, sleep=lambda s: None)
    assert not res.ok and "TimeoutExpired" in res.line


def test_default_post_turns_any_transport_error_into_status_0(monkeypatch):
    def reset(*a, **kw):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(r.urllib.request, "urlopen", reset)
    status, _ = r._default_post("http://127.0.0.1:1/x", {}, b"")
    assert status == 0
    status, _ = r._github_http("GET", "https://api.github.com/x", {}, None)
    assert status == 0


def test_turdus_check_subprocess_timeout_is_a_failed_check(tmp_path):
    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    assert not r.verify_turdus_presents_accepted_secret(
        daemon_checkout=tmp_path, runner=hangs).ok


# ---------------------------------------------------------------------------
# CLI: exit codes, no value on any outcome, notes file
# ---------------------------------------------------------------------------


def _no_value_in(out, host):
    values = {OLD_APPROVE, OLD_WEBHOOK, *host.dotenv_values().values(),
              *host.snapshot().values()}
    for v in values:
        if len(v) >= 12:
            assert v not in out.out and v not in out.err


def test_cli_exit_1_on_rollback_prints_no_value(host, monkeypatch, capsys):
    monkeypatch.setitem(
        r.VERIFIERS, "approve_email_secret",
        lambda value, base_url=None, http_post=None: r.VerificationResult(False, "401", 401),
    )
    assert r.main(["approve_email_secret"]) == 1
    out = capsys.readouterr()
    _no_value_in(out, host)
    assert "git checkout --" in out.out  # discard, not "rotate"


def test_cli_exit_2_on_checkpoint_prints_no_value(host, monkeypatch, capsys):
    monkeypatch.setattr(
        r, "PRESENTER_VERIFIERS",
        {TURDUS: lambda **kw: r.VerificationResult(False, "turdus side 401", 401)},
    )
    assert r.main(["approve_email_secret"]) == 2
    out = capsys.readouterr()
    _no_value_in(out, host)
    assert "Do NOT commit" in out.out


def test_cli_exit_4_on_internal_error(host, monkeypatch, capsys):
    def crash(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(r, "run_rotation", crash)
    assert r.main(["approve_email_secret"]) == 4
    assert "INTERNAL ERROR" in capsys.readouterr().out


def test_cli_usage_error_is_not_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        r.main(["not-a-target"])
    assert exc.value.code == 64


def test_cli_token_unavailable_is_a_refusal(host, monkeypatch, capsys):
    def no_token(login):
        raise RuntimeError("no usable gh keyring token")

    monkeypatch.setattr(r, "github_token_from_gh", no_token)
    rc = r.main(["github_webhook_secret", "--github-hook", "o/a:1",
                 "--github-token-gh-user", "someone", "--i-know-this-calls-real-github"])
    assert rc == 3
    assert not any(kind == "restart" for kind, _ in host.events)


def test_cli_notes_file_is_value_free(host, tmp_path, capsys):
    notes = tmp_path / "notes.txt"
    assert r.main(["approve_email_secret", "--notes-file", str(notes)]) == 0
    text = notes.read_text()
    assert "rolled forward" in text
    new = host.dotenv_values()["APIS_APPROVE_EMAIL_SECRET"]
    assert new not in text and OLD_APPROVE not in text


# ---------------------------------------------------------------------------
# Preflight: other blocks, unsafe base URL
# ---------------------------------------------------------------------------


def test_refuses_when_another_block_maps_the_same_variable(host, monkeypatch):
    target = r.TARGETS["approve_email_secret"]
    base = host.manifest()
    base["files"]["neotoma"] = {"default": {target.env_var: "op://Private/other/x"}}
    monkeypatch.setattr(sl, "load_manifest", lambda: base)
    report = r.run_rotation(target, dry_run=False)
    assert report.refused and "neotoma" in report.steps[0]


@pytest.mark.parametrize("url", ["http://apis.example.com", "http://10.0.0.5:8742"])
def test_refuses_a_clear_text_off_machine_base_url(host, url):
    report = r.run_rotation(r.TARGETS["approve_email_secret"], dry_run=False, base_url=url)
    assert report.refused


# ---------------------------------------------------------------------------
# The turdus-side probe program itself, against a stub turdus and a local
# HTTP server: the child really runs and really POSTs
# ---------------------------------------------------------------------------


def _serve(status_for):
    import http.server
    import threading

    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["header"] = self.headers.get("X-Approve-Secret")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(status_for(seen["header"]))
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _stub_checkout(tmp_path, held, url):
    d = tmp_path / "checkout" / "execution" / "daemons" / "turdus"
    d.mkdir(parents=True)
    (d / "turdus.py").write_text(
        f"APIS_APPROVE_EMAIL_SECRET = {held!r}\nAPIS_APPROVE_URL = {url!r}\n"
    )
    return tmp_path / "checkout"


@pytest.mark.parametrize("accepted,expect_ok", [(True, True), (False, False)])
def test_turdus_probe_program_posts_what_turdus_holds(tmp_path, accepted, expect_ok):
    held = "stub-turdus-held-value"
    server, seen = _serve(lambda hdr: 400 if (accepted and hdr == held) else 401)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/approve-email"
        res = r.verify_turdus_presents_accepted_secret(
            daemon_checkout=_stub_checkout(tmp_path, held, url))
    finally:
        server.shutdown()
    assert seen["header"] == held
    assert res.ok is expect_ok
    assert held not in res.detail


def test_turdus_probe_program_reports_unset(tmp_path):
    res = r.verify_turdus_presents_accepted_secret(
        daemon_checkout=_stub_checkout(tmp_path, "", "http://127.0.0.1:1/x"))
    assert not res.ok and "no approve secret" in res.detail
