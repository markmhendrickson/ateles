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
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os as _os  # noqa: E402

_os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

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


# ── ateles#243: NEOTOMA_BASE_URL fail-fast / fail-loud ──────────────────────


class TestNeotomaQueryRaisesOnConnectionFailure:
    def test_url_error_raises_neotoma_unavailable(self) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        with patch.object(publish.urllib.request, "urlopen", _boom):
            with pytest.raises(publish.NeotomaUnavailableError):
                publish.neotoma_query("release_result")

    def test_success_returns_entities(self) -> None:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"entities": [{"id": "ent_1"}]}).encode()

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch.object(publish.urllib.request, "urlopen", lambda *a, **k: _Resp()):
            assert publish.neotoma_query("release_result") == [{"id": "ent_1"}]


class TestNeotomaFetchEntityRaisesOnConnectionFailure:
    def test_os_error_raises_neotoma_unavailable(self) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("network unreachable")

        with patch.object(publish.urllib.request, "urlopen", _boom):
            with pytest.raises(publish.NeotomaUnavailableError):
                publish.neotoma_fetch_entity("ent_x")


class TestNeotomaStoreStaysBestEffort:
    def test_connection_failure_returns_none_does_not_raise(self) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        with patch.object(publish.urllib.request, "urlopen", _boom):
            # neotoma_store is a status-recording write, not a gate — it must
            # stay catch-and-log so a failed status write never crashes a
            # publish that already succeeded/failed on its own merits.
            result = publish.neotoma_store([{"entity_type": "release_result"}], "key-1")
            assert result is None


class TestFindReleasePropagatesNeotomaUnavailable:
    def test_by_entity_id_propagates(self) -> None:
        def _boom(entity_id: str) -> None:
            raise publish.NeotomaUnavailableError("connection refused")

        with patch.object(publish, "neotoma_fetch_entity", _boom):
            with pytest.raises(publish.NeotomaUnavailableError):
                publish.find_release(None, "ent_x")

    def test_by_version_propagates(self) -> None:
        def _boom(entity_type: str, limit: int = 100) -> None:
            raise publish.NeotomaUnavailableError("connection refused")

        with patch.object(publish, "neotoma_query", _boom):
            with pytest.raises(publish.NeotomaUnavailableError):
                publish.find_release("v1.2.3", None)


class TestMainHardBlocksOnNeotomaUnavailableInsteadOfNoReleaseFound:
    def test_main_returns_2_and_does_not_report_no_release_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["publish.py", "--version", "v1.2.3"])

        def _boom(version: object, entity_id: object) -> None:
            raise publish.NeotomaUnavailableError("connection refused")

        telegram_mock = MagicMock()
        with patch.object(publish, "find_release", _boom), patch.object(
            publish, "telegram_send", telegram_mock
        ):
            exit_code = publish.main()

        assert exit_code == 2
        assert telegram_mock.called
        sent_text = telegram_mock.call_args[0][0].lower()
        assert "unreachable" in sent_text
        assert "no release record" not in sent_text

    def test_main_reports_no_release_found_when_neotoma_reachable_but_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["publish.py", "--version", "v1.2.3"])

        telegram_mock = MagicMock()
        with patch.object(publish, "find_release", lambda v, e: None), patch.object(
            publish, "telegram_send", telegram_mock
        ):
            exit_code = publish.main()

        assert exit_code == 2
        assert telegram_mock.called
        assert "no release record" in telegram_mock.call_args[0][0].lower()


class TestPublishModuleImportFailsLoudWithoutNeotomaBaseUrl:
    def test_import_raises_neotoma_config_error_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import importlib

        from execution.lib.neotoma_config import NeotomaConfigError

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        sys.modules.pop("publish", None)
        try:
            with pytest.raises(NeotomaConfigError):
                importlib.import_module("publish")
        finally:
            monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
            sys.modules.pop("publish", None)
            importlib.import_module("publish")
