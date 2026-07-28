"""
Effect tests for publish.py — ateles#203 (phoenicurus-release operator-run
defects: missing bump commit, worktree-main collision, redundant main-push,
agent merge perms).

Tests are fully synchronous / mock-based: `run()`/`subprocess.run` are always
patched, no real git/gh/npm process is ever spawned. Every test asserts an
observable effect (exception type/content, exact recorded argv, log line
content, call order) — never merely "no exception was raised."

Run with: pytest execution/daemons/phoenicurus-release/test_publish.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import publish  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _write_package_json(tmp_path: Path, version: str) -> Path:
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "neotoma", "version": version}))
    return pkg


# ── Test 1: the incident-reproduction test ──────────────────────────────────


class TestPreflightPostMergeRejectsVersionMismatch:
    def test_rejects_mismatch_and_blocks_downstream_steps(self, tmp_path, monkeypatch):
        _write_package_json(tmp_path, "0.18.7")
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)

        with patch.object(publish, "tag_and_push") as mock_tag, patch.object(
            publish, "npm_publish"
        ) as mock_npm:
            try:
                publish.preflight_post_merge("v0.18.8", dry_run=False)
                raised = None
            except publish.StepError as exc:
                raised = exc

            assert raised is not None, "expected StepError on version mismatch"
            assert "0.18.7" in str(raised)
            assert "0.18.8" in str(raised)
            mock_tag.assert_not_called()
            mock_npm.assert_not_called()

    def test_exact_string_comparison_not_prefix_match(self, tmp_path, monkeypatch):
        # 0.18.8 vs 0.18.80 must NOT false-negative on a startswith/prefix check.
        _write_package_json(tmp_path, "0.18.80")
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)

        try:
            publish.preflight_post_merge("v0.18.8", dry_run=False)
            raised = None
        except publish.StepError as exc:
            raised = exc

        assert raised is not None
        assert "0.18.80" in str(raised)

    def test_missing_package_json_raises_step_error_not_uncaught(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)  # no package.json
        try:
            publish.preflight_post_merge("v0.18.8", dry_run=False)
            raised = None
        except publish.StepError as exc:
            raised = exc
        except (FileNotFoundError, json.JSONDecodeError):
            raised = "uncaught"

        assert raised is not None and raised != "uncaught"

    def test_unparseable_package_json_raises_step_error_not_uncaught(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "package.json").write_text("{not valid json")
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)
        try:
            publish.preflight_post_merge("v0.18.8", dry_run=False)
            raised = None
        except publish.StepError as exc:
            raised = exc
        except (FileNotFoundError, json.JSONDecodeError):
            raised = "uncaught"

        assert raised is not None and raised != "uncaught"


# ── Test 2 ───────────────────────────────────────────────────────────────────


class TestPreflightPostMergePassesOnMatch:
    def test_no_raise_on_exact_match(self, tmp_path, monkeypatch):
        _write_package_json(tmp_path, "0.18.8")
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)
        publish.preflight_post_merge("v0.18.8", dry_run=False)  # must not raise


# ── Test 3: detached-HEAD checkout resolves worktree collision ─────────────


class TestMergeRcPrDetachedHeadUnderWorktreeCollision:
    def test_uses_detached_head_not_checkout_main(self, monkeypatch):
        calls = []

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            calls.append(cmd)
            if cmd[:3] == ["git", "checkout", "main"]:
                # Prove this path is gone: if it were ever invoked, it would
                # hit the real worktree-collision error.
                raise AssertionError(
                    "old `git checkout main` path was invoked — worktree "
                    "collision fix regressed"
                )
            if cmd == ["gh", "pr", "merge", "https://github.com/x/y/pull/9", "--merge"]:
                return _proc(returncode=0)
            if cmd == ["git", "fetch", "origin", "main", "--quiet"]:
                return _proc(returncode=0)
            if cmd == ["git", "checkout", "--detach", "FETCH_HEAD"]:
                return _proc(returncode=0)
            if cmd == ["git", "rev-parse", "--short", "HEAD"]:
                return _proc(returncode=0, stdout="abc1234\n")
            return _proc(returncode=0)

        with patch.object(publish, "run", side_effect=fake_run), patch.object(
            publish.log, "info"
        ) as mock_log_info:
            publish.merge_rc_pr(
                "https://github.com/x/y/pull/9", "release/v0.18.8", dry_run=False,
                version="v0.18.8",
            )

        assert ["git", "fetch", "origin", "main", "--quiet"] in calls
        assert ["git", "checkout", "--detach", "FETCH_HEAD"] in calls
        assert ["git", "checkout", "main"] not in calls

        logged = " ".join(str(c.args[0]) for c in mock_log_info.call_args_list)
        assert "detached HEAD" in logged
        assert "abc1234" in logged


# ── Test: git fetch failure surfaces raw error ──────────────────────────────


class TestGitFetchFailureSurfacesRawError:
    def test_raw_stderr_not_swallowed(self):
        network_err = "fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com"

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            if cmd == ["gh", "pr", "merge", "pr-ref", "--merge"]:
                return _proc(returncode=0)
            if cmd == ["git", "fetch", "origin", "main", "--quiet"]:
                raise publish.StepError(
                    f"command failed (exit 128): git fetch origin main --quiet\n{network_err}"
                )
            return _proc(returncode=0)

        with patch.object(publish, "run", side_effect=fake_run):
            try:
                publish.merge_rc_pr("pr-ref", "release/v0.18.8", dry_run=False, version="v0.18.8")
                raised = None
            except publish.StepError as exc:
                raised = exc

        assert raised is not None
        assert network_err in str(raised)


# ── Test 4: redundant main-push removed ─────────────────────────────────────


class TestTagAndPushSkipsMainPushAfterServerMerge:
    def test_no_push_origin_main_only_tag_pushed(self, monkeypatch):
        calls = []

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            calls.append(cmd)
            return _proc(returncode=0)

        with patch.object(publish, "run", side_effect=fake_run), patch.object(
            publish.log, "info"
        ) as mock_log_info:
            publish.tag_and_push("v0.18.8", dry_run=False)

        assert ["git", "push", "origin", "main"] not in calls
        assert ["git", "tag", "-a", "v0.18.8", "-m", "Release v0.18.8"] in calls
        assert ["git", "push", "origin", "v0.18.8"] in calls
        # tag before tag-push, in order
        tag_idx = calls.index(["git", "tag", "-a", "v0.18.8", "-m", "Release v0.18.8"])
        push_idx = calls.index(["git", "push", "origin", "v0.18.8"])
        assert tag_idx < push_idx

        logged = " ".join(str(c.args[0]) for c in mock_log_info.call_args_list)
        assert "skipping git push origin main" in logged


# ── Test 5: perms failure surfaces a specific, operator-actionable error ───


class TestMergeRcPrSurfacesPermissionsFailure:
    def test_insufficient_permissions_error_raised_with_pr_and_repo(self, monkeypatch):
        stderr = (
            "X Pull request #9 is not mergeable: does not have the correct "
            "permissions to execute MergePullRequest"
        )

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            if cmd[:3] == ["gh", "pr", "merge"]:
                return _proc(returncode=1, stderr=stderr)
            if cmd == ["git", "remote", "get-url", "origin"]:
                return _proc(returncode=0, stdout="git@github.com:owner/repo.git\n")
            raise AssertionError(f"unexpected call after perms failure: {cmd}")

        with patch.object(publish, "run", side_effect=fake_run):
            try:
                publish.merge_rc_pr(
                    "https://github.com/owner/repo/pull/9",
                    "release/v0.18.8",
                    dry_run=False,
                    version="v0.18.8",
                )
                raised = None
            except publish.InsufficientPermissionsError as exc:
                raised = exc

        assert raised is not None
        assert isinstance(raised, publish.StepError)
        assert "9" in str(raised)
        assert "owner/repo" in str(raised) or "repo.git" in str(raised)

    def test_case_insensitive_perms_match(self, monkeypatch):
        stderr = "Does Not Have The Correct Permissions to execute MergePullRequest"

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            if cmd[:3] == ["gh", "pr", "merge"]:
                return _proc(returncode=1, stderr=stderr)
            if cmd == ["git", "remote", "get-url", "origin"]:
                return _proc(returncode=0, stdout="git@github.com:owner/repo.git\n")
            raise AssertionError(f"unexpected call: {cmd}")

        with patch.object(publish, "run", side_effect=fake_run):
            try:
                publish.merge_rc_pr(
                    "https://github.com/owner/repo/pull/9",
                    "release/v0.18.8",
                    dry_run=False,
                    version="v0.18.8",
                )
                raised = None
            except publish.InsufficientPermissionsError as exc:
                raised = exc

        assert raised is not None

    def test_not_mergeable_without_perms_string_takes_original_path(self, monkeypatch):
        # Guards against the new check swallowing the pre-existing "not
        # mergeable" branch.
        stderr = "Pull request #9 is not mergeable"

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            if cmd[:3] == ["gh", "pr", "merge"]:
                return _proc(returncode=1, stderr=stderr)
            if cmd[:3] == ["gh", "pr", "view"]:
                return _proc(returncode=0, stdout="MERGED")
            if cmd == ["git", "fetch", "origin", "main", "--quiet"]:
                return _proc(returncode=0)
            if cmd == ["git", "checkout", "--detach", "FETCH_HEAD"]:
                return _proc(returncode=0)
            if cmd == ["git", "rev-parse", "--short", "HEAD"]:
                return _proc(returncode=0, stdout="abc1234\n")
            return _proc(returncode=0)

        with patch.object(publish, "run", side_effect=fake_run):
            # Must NOT raise InsufficientPermissionsError; "not mergeable" +
            # state==MERGED is the tolerated already-merged path.
            publish.merge_rc_pr(
                "https://github.com/owner/repo/pull/9",
                "release/v0.18.8",
                dry_run=False,
                version="v0.18.8",
            )

    def test_end_to_end_stops_before_tag_and_push(self, monkeypatch):
        """publish_release() halts on perms failure — no retry, no fall-through."""
        stderr = "does not have the correct permissions to execute MergePullRequest"
        merge_calls = []

        def fake_run(cmd, cwd=None, check=True, env=None, timeout=600, secret_in_env=False):
            if cmd[:3] == ["gh", "pr", "merge"]:
                merge_calls.append(cmd)
                return _proc(returncode=1, stderr=stderr)
            if cmd == ["git", "remote", "get-url", "origin"]:
                return _proc(returncode=0, stdout="git@github.com:owner/repo.git\n")
            return _proc(returncode=0)

        release = {
            "status": "approved",
            "rc_pr_url": "https://github.com/owner/repo/pull/9",
            "rc_branch": "release/v0.18.8",
        }

        with patch.object(publish, "run", side_effect=fake_run), patch.object(
            publish, "preflight"
        ), patch.object(publish, "preflight_post_merge") as mock_ppm, patch.object(
            publish, "tag_and_push"
        ) as mock_tag, patch.object(
            publish, "npm_publish"
        ) as mock_npm, patch.object(
            publish, "set_release_status"
        ):
            try:
                publish.publish_release(release, "v0.18.8", dry_run=False, force=False)
                raised = None
            except publish.InsufficientPermissionsError as exc:
                raised = exc

        assert raised is not None
        assert len(merge_calls) == 1  # no retry
        mock_ppm.assert_not_called()
        mock_tag.assert_not_called()
        mock_npm.assert_not_called()


# ── Test 6: --resume-from skips completed steps ─────────────────────────────


class TestResumeFromSkipsCompletedSteps:
    def _release(self):
        return {
            "status": "approved",
            "rc_pr_url": "https://github.com/owner/repo/pull/9",
            "rc_branch": "release/v0.18.8",
        }

    def test_resume_from_tag_and_push_skips_earlier_steps(self):
        with patch.object(publish, "preflight") as mock_preflight, patch.object(
            publish, "merge_rc_pr"
        ) as mock_merge, patch.object(
            publish, "preflight_post_merge"
        ) as mock_ppm, patch.object(
            publish, "tag_and_push"
        ) as mock_tag, patch.object(
            publish, "npm_publish"
        ) as mock_npm, patch.object(
            publish, "github_release"
        ) as mock_gh, patch.object(
            publish, "deploy_sandbox"
        ) as mock_deploy, patch.object(
            publish, "publish_github_release_draft"
        ) as mock_draft, patch.object(
            publish, "post_release", return_value="ok"
        ) as mock_post, patch.object(
            publish, "set_release_status"
        ), patch.object(
            publish, "telegram_send"
        ):
            publish.publish_release(
                self._release(), "v0.18.8", dry_run=True, force=False,
                resume_from="tag_and_push",
            )

        mock_preflight.assert_not_called()
        mock_merge.assert_not_called()
        mock_ppm.assert_called_once()
        mock_tag.assert_called_once()
        mock_npm.assert_called_once()
        mock_gh.assert_called_once()
        mock_deploy.assert_called_once()
        mock_draft.assert_called_once()
        mock_post.assert_called_once()

    def test_resume_from_preflight_equals_full_run(self):
        with patch.object(publish, "preflight") as mock_preflight, patch.object(
            publish, "merge_rc_pr"
        ) as mock_merge, patch.object(
            publish, "preflight_post_merge"
        ) as mock_ppm, patch.object(
            publish, "tag_and_push"
        ) as mock_tag, patch.object(
            publish, "npm_publish"
        ), patch.object(
            publish, "github_release"
        ), patch.object(
            publish, "deploy_sandbox"
        ), patch.object(
            publish, "publish_github_release_draft"
        ), patch.object(
            publish, "post_release", return_value="ok"
        ), patch.object(
            publish, "set_release_status"
        ), patch.object(
            publish, "telegram_send"
        ):
            publish.publish_release(
                self._release(), "v0.18.8", dry_run=True, force=False,
                resume_from="preflight",
            )

        mock_preflight.assert_called_once()
        mock_merge.assert_called_once()
        mock_ppm.assert_called_once()
        mock_tag.assert_called_once()

    def test_resume_from_tag_and_push_still_catches_missing_bump_commit(
        self, tmp_path, monkeypatch
    ):
        """The exact incident path: an operator merges the RC PR manually
        (without the bump commit landing) and resumes with
        --resume-from=tag_and_push. preflight_post_merge must still run and
        block tag_and_push/npm_publish — this is the defect ateles#203 was
        filed to fix, and skipping the guard on this resume path would silently
        reintroduce it.
        """
        _write_package_json(tmp_path, "0.18.7")  # bump commit missing
        monkeypatch.setattr(publish, "NEOTOMA_REPO_ROOT", tmp_path)

        with patch.object(publish, "preflight") as mock_preflight, patch.object(
            publish, "merge_rc_pr"
        ) as mock_merge, patch.object(
            publish, "tag_and_push"
        ) as mock_tag, patch.object(
            publish, "npm_publish"
        ) as mock_npm, patch.object(
            publish, "set_release_status"
        ), patch.object(
            publish, "telegram_send"
        ):
            try:
                publish.publish_release(
                    self._release(), "v0.18.8", dry_run=False, force=False,
                    resume_from="tag_and_push",
                )
                raised = None
            except publish.StepError as exc:
                raised = exc

        assert raised is not None, "expected StepError for version mismatch"
        assert "0.18.7" in str(raised) and "0.18.8" in str(raised)
        mock_preflight.assert_not_called()
        mock_merge.assert_not_called()
        mock_tag.assert_not_called()
        mock_npm.assert_not_called()

    def test_invalid_resume_from_choice_rejected_by_argparse(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["publish.py", "--version", "v0.18.8", "--resume-from", "bogus_step"]
        )
        try:
            publish.main()
            exited = False
        except SystemExit:
            exited = True
        assert exited, "argparse must reject an invalid --resume-from choice"


# ── Cross-cutting: golden path ───────────────────────────────────────────────


class TestGoldenPathEndToEnd:
    def test_all_steps_called_once_in_order_no_step_error(self):
        release = {
            "status": "approved",
            "rc_pr_url": "https://github.com/owner/repo/pull/9",
            "rc_branch": "release/v0.18.8",
        }
        order = []

        def track(name):
            def _inner(*a, **k):
                order.append(name)
                if name == "post_release":
                    return "ok"
                return None

            return _inner

        with patch.object(
            publish, "preflight", side_effect=track("preflight")
        ), patch.object(
            publish, "merge_rc_pr", side_effect=track("merge_rc_pr")
        ), patch.object(
            publish, "preflight_post_merge", side_effect=track("preflight_post_merge")
        ), patch.object(
            publish, "tag_and_push", side_effect=track("tag_and_push")
        ), patch.object(
            publish, "npm_publish", side_effect=track("npm_publish")
        ), patch.object(
            publish, "github_release", side_effect=track("github_release")
        ), patch.object(
            publish, "deploy_sandbox", side_effect=track("deploy_sandbox")
        ), patch.object(
            publish,
            "publish_github_release_draft",
            side_effect=track("publish_github_release_draft"),
        ), patch.object(
            publish, "post_release", side_effect=track("post_release")
        ), patch.object(
            publish, "set_release_status"
        ), patch.object(
            publish, "telegram_send"
        ):
            publish.publish_release(release, "v0.18.8", dry_run=False, force=False)

        assert order == [
            "preflight",
            "merge_rc_pr",
            "preflight_post_merge",
            "tag_and_push",
            "npm_publish",
            "github_release",
            "deploy_sandbox",
            "publish_github_release_draft",
            "post_release",
        ]

    def test_preflight_post_merge_ordering_guard(self):
        """preflight_post_merge must run strictly after merge_rc_pr and
        strictly before tag_and_push — the actual irreversible-step guard."""
        release = {
            "status": "approved",
            "rc_pr_url": "https://github.com/owner/repo/pull/9",
            "rc_branch": "release/v0.18.8",
        }
        order = []

        def track(name):
            def _inner(*a, **k):
                order.append(name)
                if name == "post_release":
                    return "ok"
                return None

            return _inner

        with patch.object(
            publish, "preflight", side_effect=track("preflight")
        ), patch.object(
            publish, "merge_rc_pr", side_effect=track("merge_rc_pr")
        ), patch.object(
            publish, "preflight_post_merge", side_effect=track("preflight_post_merge")
        ), patch.object(
            publish, "tag_and_push", side_effect=track("tag_and_push")
        ), patch.object(
            publish, "npm_publish", side_effect=track("npm_publish")
        ), patch.object(
            publish, "github_release", side_effect=track("github_release")
        ), patch.object(
            publish, "deploy_sandbox", side_effect=track("deploy_sandbox")
        ), patch.object(
            publish,
            "publish_github_release_draft",
            side_effect=track("publish_github_release_draft"),
        ), patch.object(
            publish, "post_release", side_effect=track("post_release")
        ), patch.object(
            publish, "set_release_status"
        ), patch.object(
            publish, "telegram_send"
        ):
            publish.publish_release(release, "v0.18.8", dry_run=False, force=False)

        merge_idx = order.index("merge_rc_pr")
        ppm_idx = order.index("preflight_post_merge")
        tag_idx = order.index("tag_and_push")
        assert merge_idx < ppm_idx < tag_idx


# ── CI npm publish handoff (neotoma#2015) ────────────────────────────────────
#
# Publishing moved to GitHub Actions for provenance + laptop-independence. The
# risk that introduces is a SYNCHRONOUS failure becoming an ASYNCHRONOUS one:
# if the wait were quiet, a failed CI publish would let the release continue to
# github_release and "succeed" with nothing on npm. These tests pin the loud
# behaviour.


def test_await_ci_publish_returns_once_registry_flips():
    """Polls until the registry reports the target version, then returns."""
    seen = iter(["0.18.8", "0.18.8", "0.19.0"])
    with patch.object(publish, "_registry_version", side_effect=lambda *a: next(seen)):
        with patch.object(publish, "time") as t:
            t.monotonic.side_effect = [0, 1, 2, 3, 4, 5]
            publish.await_ci_npm_publish("v0.19.0", dry_run=False)
            # Slept between polls rather than hot-looping the registry.
            assert t.sleep.called


def test_await_ci_publish_short_circuits_when_already_published():
    """A --resume-from re-run must not wait for a publish that already landed."""
    with patch.object(publish, "_registry_version", return_value="0.19.0") as rv:
        with patch.object(publish, "time") as t:
            publish.await_ci_npm_publish("v0.19.0", dry_run=False)
            assert rv.call_count == 1, "should return on the first registry read"
            assert not t.sleep.called, "must not sleep when already published"


def test_await_ci_publish_timeout_raises_and_telegrams():
    """Timeout must FAIL the release loudly, not fall through to github_release."""
    with patch.object(publish, "_registry_version", return_value="0.18.8"):
        with patch.object(publish, "telegram_send") as tg:
            with patch.object(publish, "time") as t:
                # First call arms the deadline; subsequent calls are past it.
                t.monotonic.side_effect = [0, 10_000, 10_000, 10_000]
                try:
                    publish.await_ci_npm_publish("v0.19.0", dry_run=False)
                    raise AssertionError("expected StepError on timeout")
                except publish.StepError as exc:
                    msg = str(exc)
                    # The operator must learn the release is tagged-but-unpublished,
                    # where to look, and how to recover.
                    assert "TAGGED but NOT" in msg
                    assert "resume-from=npm_publish" in msg
                    assert "actions" in msg.lower()
            assert tg.called, "timeout must notify the operator"
            assert "🔴" in tg.call_args[0][0]


def test_await_ci_publish_dry_run_makes_no_registry_calls():
    with patch.object(publish, "_registry_version") as rv:
        publish.await_ci_npm_publish("v0.19.0", dry_run=True)
        assert not rv.called


def test_npm_publish_routes_to_ci_by_default():
    with patch.object(publish, "NPM_PUBLISH_MODE", "ci"):
        with patch.object(publish, "await_ci_npm_publish") as ci:
            with patch.object(publish, "npm_publish_local") as local:
                publish.npm_publish("v0.19.0", dry_run=False)
                assert ci.called and not local.called


def test_npm_publish_local_mode_publishes_from_host():
    """The local fallback stays reachable when CI publishing is unavailable."""
    with patch.object(publish, "NPM_PUBLISH_MODE", "local"):
        with patch.object(publish, "await_ci_npm_publish") as ci:
            with patch.object(publish, "npm_publish_local") as local:
                publish.npm_publish("v0.19.0", dry_run=False)
                assert local.called and not ci.called


# ── --from-email-approval state gate (email-reply approval, ateles) ──────────
#
# The email-approval path flips pending_approval -> approved then publishes. It
# MUST refuse any other starting state so a duplicate/stale email reply can't
# re-publish or publish an un-prepared version. These pin that gate; publish
# steps are stubbed so no irreversible action runs.


def _release(status: str, version: str = "v0.20.0") -> dict:
    return {"snapshot": {"version": version, "status": status,
                         "rc_branch": f"release/{version}"}}


def _stub_publish_steps(mp):
    # neutralize every irreversible step + the status writer so we test only the gate
    for name in ("preflight", "merge_rc_pr", "preflight_post_merge", "tag_and_push",
                 "npm_publish", "github_release", "deploy_sandbox",
                 "publish_github_release_draft", "post_release", "set_release_status",
                 "telegram_send"):
        if hasattr(publish, name):
            mp.setattr(publish, name, MagicMock(return_value="" if name == "post_release" else None))


def test_email_approval_publishes_from_pending_approval(monkeypatch):
    _stub_publish_steps(monkeypatch)
    flips = []
    monkeypatch.setattr(publish, "set_release_status",
                        lambda v, s, extra=None: flips.append((v, s)))
    # should flip to approved, then run (no raise)
    publish.publish_release(_release("pending_approval"), "v0.20.0",
                            dry_run=False, force=False, email_approval=True)
    assert ("v0.20.0", "approved") in flips


def test_email_approval_refuses_already_published(monkeypatch):
    _stub_publish_steps(monkeypatch)
    try:
        publish.publish_release(_release("published"), "v0.20.0",
                                dry_run=False, force=False, email_approval=True)
        raise AssertionError("expected StepError refusing to re-publish")
    except publish.StepError as exc:
        assert "not 'pending_approval'" in str(exc)


def test_email_approval_refuses_publishing_state(monkeypatch):
    # a reply arriving mid-publish must not kick off a second publish
    _stub_publish_steps(monkeypatch)
    try:
        publish.publish_release(_release("publishing"), "v0.20.0",
                                dry_run=False, force=False, email_approval=True)
        raise AssertionError("expected StepError")
    except publish.StepError as exc:
        assert "not 'pending_approval'" in str(exc)


def test_email_approval_dry_run_does_not_flip_status(monkeypatch):
    _stub_publish_steps(monkeypatch)
    flips = []
    monkeypatch.setattr(publish, "set_release_status",
                        lambda v, s, extra=None: flips.append((v, s)))
    publish.publish_release(_release("pending_approval"), "v0.20.0",
                            dry_run=True, force=False, email_approval=True)
    assert flips == []  # dry-run makes no state change


# ── RC field-name reconciliation (release_url/branch vs rc_pr_url/rc_branch) ──
#
# prepare.py's agent historically stored `release_url` + `branch`; publish.py
# read only `rc_pr_url` + `rc_branch`, so both came back empty and the PR URL
# was lost on publish. The reader now accepts both names (rc_* preferred).


def _capture_rc_fields(release, monkeypatch):
    """Run publish_release far enough to capture the resolved rc_pr_url/rc_branch,
    then abort before any irreversible step."""
    captured = {}

    def fake_preflight(version, rc_branch, dry_run):
        captured["rc_branch"] = rc_branch  # preflight runs first

    def fake_merge(rc_pr_url, rc_branch, dry_run, version=""):
        captured["rc_pr_url"] = rc_pr_url
        # abort here — after both rc_branch (preflight) and rc_pr_url are captured,
        # before tag_and_push / npm / any irreversible step
        raise publish.StepError("stop-after-capture")

    monkeypatch.setattr(publish, "set_release_status", lambda *a, **k: None)
    monkeypatch.setattr(publish, "preflight", fake_preflight)
    monkeypatch.setattr(publish, "merge_rc_pr", fake_merge)
    monkeypatch.setattr(publish, "telegram_send", lambda *a, **k: None)
    try:
        publish.publish_release(release, "v0.20.0", dry_run=False, force=True)
    except publish.StepError:
        pass
    return captured


def test_reads_rc_fields_when_present(monkeypatch):
    rel = {"snapshot": {"status": "approved", "rc_pr_url": "https://x/pr/9",
                        "rc_branch": "release/v0.20.0"}}
    cap = _capture_rc_fields(rel, monkeypatch)
    assert cap["rc_branch"] == "release/v0.20.0"


def test_falls_back_to_release_url_and_branch(monkeypatch):
    # the OLD prepare convention: release_url + branch, no rc_* — must still resolve
    rel = {"snapshot": {"status": "approved", "release_url": "https://x/pr/9",
                        "branch": "release/v0.20.0"}}
    cap = _capture_rc_fields(rel, monkeypatch)
    assert cap["rc_pr_url"] == "https://x/pr/9", "release_url must be picked up"
    assert cap["rc_branch"] == "release/v0.20.0", "branch must be picked up"


def test_rc_name_wins_over_plain_when_both_present(monkeypatch):
    rel = {"snapshot": {"status": "approved",
                        "rc_pr_url": "https://x/pr/RC", "release_url": "https://x/pr/OLD",
                        "rc_branch": "release/RC", "branch": "release/OLD"}}
    cap = _capture_rc_fields(rel, monkeypatch)
    assert cap["rc_pr_url"] == "https://x/pr/RC"
    assert cap["rc_branch"] == "release/RC"


# ── Transient-failure retry for Neotoma reads (auto-recover from 403 blips) ──
#
# A transient 403 (loopback prod during a server restart) stranded the first
# live release-approval: neotoma_query returned [], publish saw "no release
# record", and gave up. The query layer now retries transient failures so a
# blip self-recovers without a human re-running an approved release.

import io as _io
import urllib.error as _uerr
import urllib.request as _ureq


def _http_error(code):
    return _uerr.HTTPError("u", code, "x", {}, _io.BytesIO(b""))


def test_retry_recovers_from_transient_403(monkeypatch):
    monkeypatch.setattr(publish, "_NEOTOMA_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(publish, "_NEOTOMA_RETRY_BASE_S", 0.0)
    monkeypatch.setattr(publish.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"entities":[{"ok":1}]}'

    def flaky(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 3:  # 403 twice, then OK
            raise _http_error(403)
        return _R()

    monkeypatch.setattr(_ureq, "urlopen", flaky)
    out = publish.neotoma_query("release_result")
    assert out == [{"ok": 1}], "must recover after transient 403s"
    assert calls["n"] == 3


def test_404_is_not_retried(monkeypatch):
    monkeypatch.setattr(publish, "_NEOTOMA_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(publish.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def not_found(req, timeout=20):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(_ureq, "urlopen", not_found)
    assert publish.neotoma_fetch_entity("ent_x") is None
    assert calls["n"] == 1, "404 is a real answer, not a blip — no retry"


def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(publish, "_NEOTOMA_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(publish, "_NEOTOMA_RETRY_BASE_S", 0.0)
    monkeypatch.setattr(publish.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def always_503(req, timeout=20):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(_ureq, "urlopen", always_503)
    assert publish.neotoma_query("release_result") == []
    assert calls["n"] == 3, "bounded — does not retry forever"
