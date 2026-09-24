#!/usr/bin/env python3
"""Tests for verify_gh_identity.py — the authoritative GitHub identity check.

Motivated by a wrong-identity PR: an agent authenticated to GitHub as the
operator's own personal account instead of its own, because `gh` silently
falls back to the ambient keyring session whenever GH_TOKEN/GITHUB_TOKEN is
present but empty (confirmed by direct repro against the live `gh` binary —
see verify_gh_identity.py's module docstring). These tests mock the `gh`
subprocess call so they run offline and never touch a real token or the
network; they assert the SCRIPT's decision logic, not live GitHub behaviour.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import verify_gh_identity as vgi  # noqa: E402


def _run_main(argv, env_overrides):
    """Run vgi.main() with argv/env patched, capturing the return code."""
    with mock.patch.object(sys, "argv", ["verify_gh_identity.py", *argv]):
        with mock.patch.dict("os.environ", env_overrides, clear=False):
            return vgi.main()


class TestMissingToken(unittest.TestCase):
    def test_empty_gh_token_and_empty_github_token_fails(self):
        rc = _run_main(
            ["--expect-login", "ateles-agent"],
            {"GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        self.assertEqual(rc, 1)

    def test_unset_both_fails(self):
        import os

        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        with mock.patch.object(sys, "argv", ["verify_gh_identity.py", "--expect-login", "ateles-agent"]):
            with mock.patch.dict("os.environ", env, clear=True):
                rc = vgi.main()
        self.assertEqual(rc, 1)

    def test_falls_back_to_github_token_env_name_when_gh_token_empty(self):
        """GITHUB_TOKEN populated, GH_TOKEN empty: script must still find it
        (matching gh's own GH_TOKEN -> GITHUB_TOKEN precedence) and proceed
        to the live check rather than reporting "no token"."""
        with mock.patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ateles-agent\n", stderr=""
            ),
        ) as run_mock:
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "", "GITHUB_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()


class TestTooShort(unittest.TestCase):
    def test_short_token_fails_before_any_network_call(self):
        with mock.patch.object(subprocess, "run") as run_mock:
            rc = _run_main(["--expect-login", "ateles-agent"], {"GH_TOKEN": "short"})
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()


class TestLiveIdentityCheck(unittest.TestCase):
    """These mock the `gh api user` subprocess call — no real token, no
    network. They assert the script's own pass/fail logic given a resolved
    login, which is the property that actually catches keyring fallback."""

    def _mock_gh(self, returncode, login="", stderr=""):
        return mock.patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=returncode,
                stdout=(login + "\n") if returncode == 0 else "",
                stderr=stderr,
            ),
        )

    def test_matching_login_succeeds(self):
        with self._mock_gh(0, login="ateles-agent"):
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 0)

    def test_wrong_login_fails_even_though_token_is_valid(self):
        """The core regression case: a genuinely VALID token that resolves to
        the WRONG account (e.g. the ambient keyring identity) must hard-fail,
        not silently proceed as that other identity."""
        with self._mock_gh(0, login="markmhendrickson"):
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 1)

    def test_invalid_token_fails_loudly(self):
        with self._mock_gh(1, stderr="gh: Bad credentials (HTTP 401)"):
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 1)

    def test_gh_not_found_fails(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 1)

    def test_timeout_fails_rather_than_proceeding(self):
        with mock.patch.object(
            subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15),
        ):
            rc = _run_main(
                ["--expect-login", "ateles-agent"],
                {"GH_TOKEN": "ghp_" + "x" * 36},
            )
        self.assertEqual(rc, 1)


class TestNeverPrintsTokenValue(unittest.TestCase):
    def test_stdout_and_stderr_never_contain_the_token(self):
        token = "ghp_" + "secretvalue123456789"
        with self._mock_gh_helper(0, login="ateles-agent"):
            with mock.patch.object(sys, "argv", ["verify_gh_identity.py", "--expect-login", "ateles-agent"]):
                with mock.patch.dict("os.environ", {"GH_TOKEN": token}, clear=False):
                    with mock.patch("sys.stdout", new=mock.MagicMock()) as out, \
                         mock.patch("sys.stderr", new=mock.MagicMock()) as err:
                        vgi.main()
        written = "".join(
            str(c.args[0]) for c in out.write.call_args_list + err.write.call_args_list
            if c.args
        )
        self.assertNotIn(token, written)

    def _mock_gh_helper(self, returncode, login=""):
        return mock.patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=login + "\n", stderr="",
            ),
        )


if __name__ == "__main__":
    unittest.main()
