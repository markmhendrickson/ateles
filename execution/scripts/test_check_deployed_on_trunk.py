#!/usr/bin/env python3
"""Tests for check_deployed_on_trunk.py.

The GitHub API and the version endpoint are mocked, so these run offline and
never touch a token. The property under test is the exit-code contract: 0 only
when the compare API says `behind` or `identical`, 1 when `ahead` or
`diverged`, and 2 for everything that means "could not check" (never a pass).
"""
import http.client
import http.server
import io
import json
import sys
import threading
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import check_deployed_on_trunk as ctd  # noqa: E402

SHA = "a" * 40
REPO = "owner/app"


def _run(argv, api=None, fetch=None):
    """Run main() with the API and endpoint fetchers mocked; return (code, stdout)."""
    out = io.StringIO()
    with mock.patch.object(ctd, "api_get", api or mock.Mock(side_effect=AssertionError("unexpected api call"))):
        with mock.patch.object(ctd, "fetch_json_url", fetch or mock.Mock(side_effect=AssertionError("unexpected fetch"))):
            with mock.patch.object(sys, "stdout", out):
                code = ctd.main(argv)
    return code, out.getvalue()


def _compare_api(status):
    def api(path):
        if path == f"/repos/{REPO}":
            return {"default_branch": "main"}
        if "/compare/" in path:
            return {"status": status}
        raise AssertionError(path)

    return api


class CompareStatusTests(unittest.TestCase):
    def test_behind_is_on_trunk(self):
        code, out = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("behind"))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("ON TRUNK"))

    def test_identical_is_on_trunk(self):
        code, _ = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("identical"))
        self.assertEqual(code, 0)

    def test_ahead_is_not_on_trunk(self):
        code, out = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("ahead"))
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("NOT ON TRUNK"))

    def test_diverged_is_not_on_trunk(self):
        code, _ = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("diverged"))
        self.assertEqual(code, 1)

    def test_unrecognised_status_is_unknown_not_a_pass(self):
        code, out = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("sideways"))
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("UNKNOWN"))

    def test_missing_status_is_unknown_not_a_pass(self):
        code, _ = _run(["--repo", REPO, "--sha", SHA, "--branch", "main"], api=lambda p: {})
        self.assertEqual(code, 2)

    def test_explicit_branch_skips_default_branch_lookup(self):
        seen = []

        def api(path):
            seen.append(path)
            return {"status": "identical"}

        code, _ = _run(["--repo", REPO, "--sha", SHA, "--branch", "release/1"], api=api)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("release%2F1...", seen[0])

    def test_output_is_one_line(self):
        _, out = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("behind"))
        self.assertEqual(len(out.strip().splitlines()), 1)


class ErrorTests(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, io.BytesIO(b"{}"))

    def test_404_from_api_is_unknown(self):
        with mock.patch.object(ctd, "_open", side_effect=self._http_error(404)):
            code, out = _run_real_api(["--repo", REPO, "--sha", SHA, "--branch", "main"])
        self.assertEqual(code, 2)
        self.assertIn("404", out)

    def test_network_error_is_unknown(self):
        with mock.patch.object(ctd, "_open", side_effect=urllib.error.URLError("dns down")):
            code, out = _run_real_api(["--repo", REPO, "--sha", SHA, "--branch", "main"])
        self.assertEqual(code, 2)
        self.assertIn("network error", out)

    def test_auth_refusal_is_unknown(self):
        with mock.patch.object(ctd, "_open", side_effect=self._http_error(403)):
            code, _ = _run_real_api(["--repo", REPO, "--sha", SHA, "--branch", "main"])
        self.assertEqual(code, 2)

    def test_malformed_sha_is_unknown(self):
        code, _ = _run(["--repo", REPO, "--sha", "not-a-sha"])
        self.assertEqual(code, 2)

    def test_malformed_repo_is_unknown(self):
        code, _ = _run(["--repo", "nonsense", "--sha", SHA])
        self.assertEqual(code, 2)

    def test_no_sha_source_is_unknown(self):
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch.object(sys, "stderr", io.StringIO()):
                ctd.main(["--repo", REPO])
        self.assertEqual(ctx.exception.code, 2)


def _run_real_api(argv):
    out = io.StringIO()
    with mock.patch.object(sys, "stdout", out):
        code = ctd.main(argv)
    return code, out.getvalue()


class FromUrlTests(unittest.TestCase):
    def test_reads_sha_from_endpoint(self):
        code, out = _run(
            ["--repo", REPO, "--from-url", "https://app.example/version", "--json-field", "commit"],
            api=_compare_api("behind"),
            fetch=lambda url: {"commit": SHA},
        )
        self.assertEqual(code, 0)
        self.assertIn("app.example/version", out)

    def test_dotted_field(self):
        code, _ = _run(
            ["--repo", REPO, "--from-url", "https://app.example/h", "--json-field", "build.commit"],
            api=_compare_api("ahead"),
            fetch=lambda url: {"build": {"commit": SHA}},
        )
        self.assertEqual(code, 1)

    def test_missing_field_is_unknown(self):
        code, _ = _run(
            ["--repo", REPO, "--from-url", "https://app.example/version", "--json-field", "commit"],
            fetch=lambda url: {"status": "ok"},
        )
        self.assertEqual(code, 2)

    def test_health_ok_string_is_not_a_sha(self):
        code, _ = _run(
            ["--repo", REPO, "--from-url", "https://app.example/health", "--json-field", "commit"],
            fetch=lambda url: {"commit": "ok"},
        )
        self.assertEqual(code, 2)

    def test_requires_json_field(self):
        code, _ = _run(["--repo", REPO, "--from-url", "https://app.example/version"], fetch=lambda url: {})
        self.assertEqual(code, 2)

    def test_endpoint_unreachable_is_unknown(self):
        def fetch(url):
            raise ctd.CheckError("network error reaching version endpoint: down")

        code, _ = _run(
            ["--repo", REPO, "--from-url", "https://app.example/version", "--json-field", "commit"],
            fetch=fetch,
        )
        self.assertEqual(code, 2)

    def test_non_http_url_rejected(self):
        with self.assertRaises(ctd.CheckError):
            ctd.fetch_json_url("file:///etc/passwd")


def _deployments_api(deployments, statuses, compare="behind"):
    """deployments: list of {id, sha}; statuses: {id: [state, ...] newest first}."""

    def api(path):
        if path == f"/repos/{REPO}":
            return {"default_branch": "main"}
        if "/compare/" in path:
            return {"status": compare}
        if "/statuses" in path:
            dep_id = int(path.split("/deployments/")[1].split("/")[0])
            return [{"state": s} for s in statuses.get(dep_id, [])][:1]
        if "/deployments?" in path:
            assert "environment=production" in path
            return deployments
        raise AssertionError(path)

    return api


class FromDeploymentsTests(unittest.TestCase):
    ARGS = ["--repo", REPO, "--from-deployments", "production"]

    def test_uses_latest_successful_deployment(self):
        newest, older = "b" * 40, "c" * 40
        seen = {}
        base = _deployments_api(
            [{"id": 2, "sha": newest}, {"id": 1, "sha": older}], {2: ["success"], 1: ["success"]}
        )

        def api(path):
            if "/compare/" in path:
                seen["sha"] = path.split("...")[1].split("?")[0]
            return base(path)

        code, _ = _run(self.ARGS, api=api)
        self.assertEqual(code, 0)
        self.assertEqual(seen["sha"], newest)

    def test_skips_failed_and_in_progress_to_last_success(self):
        good = "d" * 40
        seen = {}
        base = _deployments_api(
            [{"id": 3, "sha": "b" * 40}, {"id": 2, "sha": "c" * 40}, {"id": 1, "sha": good}],
            {3: ["failure", "in_progress"], 2: ["in_progress"], 1: ["success"]},
        )

        def api(path):
            if "/compare/" in path:
                seen["sha"] = path.split("...")[1].split("?")[0]
            return base(path)

        code, _ = _run(self.ARGS, api=api)
        self.assertEqual(code, 0)
        self.assertEqual(seen["sha"], good)

    def test_a_later_failure_hides_an_earlier_success_on_the_same_deployment(self):
        # Only the newest status of a deployment decides its state.
        api = _deployments_api([{"id": 1, "sha": SHA}], {1: ["failure", "success"]})
        code, _ = _run(self.ARGS, api=api)
        self.assertEqual(code, 2)

    def test_no_deployments_is_unknown(self):
        code, out = _run(self.ARGS, api=_deployments_api([], {}))
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("UNKNOWN"))

    def test_successful_deployment_off_trunk_fails(self):
        api = _deployments_api([{"id": 1, "sha": SHA}], {1: ["success"]}, compare="diverged")
        code, _ = _run(self.ARGS, api=api)
        self.assertEqual(code, 1)

    def test_sources_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            with mock.patch.object(sys, "stderr", io.StringIO()):
                ctd.main(["--repo", REPO, "--sha", SHA, "--from-deployments", "production"])
        self.assertEqual(ctx.exception.code, 2)


class TokenSafetyTests(unittest.TestCase):
    def test_token_never_printed(self):
        secret = "ghp_" + "x" * 36
        with mock.patch.dict("os.environ", {"GH_TOKEN": secret}):
            code, out = _run(["--repo", REPO, "--sha", SHA], api=_compare_api("behind"))
        self.assertEqual(code, 0)
        self.assertNotIn(secret, out)

    def test_api_get_sends_bearer_when_token_set(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode()

        def fake_open(url, headers, **kwargs):
            captured.update(headers)
            return FakeResponse()

        with mock.patch.dict("os.environ", {"GH_TOKEN": "tok"}):
            with mock.patch.object(ctd, "_open", side_effect=fake_open):
                self.assertEqual(ctd.api_get("/repos/o/r"), {"ok": True})
        self.assertEqual(captured["Authorization"], "Bearer tok")


class HardeningTests(unittest.TestCase):
    """Behaviours added after lens review. Each fails on the previous code."""

    def test_unexpected_exception_is_unknown_exit_2_without_traceback(self):
        api = mock.Mock(side_effect=RuntimeError("secret detail that must not leak"))
        code, out = _run(["--repo", REPO, "--sha", SHA], api=api)
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "UNKNOWN: could not check (RuntimeError)")

    def test_incomplete_read_is_unknown(self):
        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"par")

        with mock.patch.object(ctd, "_open", return_value=Boom()):
            code, out = _run_real_api(["--repo", REPO, "--sha", SHA, "--branch", "main"])
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("UNKNOWN"))
        self.assertIn("network error", out)

    def test_incomplete_read_from_version_endpoint_is_checkerror(self):
        with mock.patch.object(ctd, "_open", side_effect=http.client.RemoteDisconnected("gone")):
            with self.assertRaises(ctd.CheckError):
                ctd.fetch_json_url("https://app.example/version")

    def test_non_object_deployment_element_is_unknown(self):
        for bad in (["x"], [None], [7], [[1]]):
            with self.subTest(bad=bad):
                api = _deployments_api(bad, {})
                code, out = _run(FromDeploymentsTests.ARGS, api=api)
                self.assertEqual(code, 2)
                self.assertTrue(out.startswith("UNKNOWN"))

    def test_non_object_status_element_is_unknown(self):
        def api(path):
            if "/statuses" in path:
                return ["success"]
            if "/deployments?" in path:
                return [{"id": 1, "sha": SHA}]
            raise AssertionError(path)

        code, _ = _run(FromDeploymentsTests.ARGS, api=api)
        self.assertEqual(code, 2)

    def test_short_sha_is_rejected(self):
        for bad in ("abcdef1", "a" * 39, "a" * 41, SHA + "\n" + SHA):
            with self.subTest(bad=bad):
                code, _ = _run(["--repo", REPO, "--sha", bad])
                self.assertEqual(code, 2)

    def test_short_sha_from_endpoint_is_rejected(self):
        code, _ = _run(
            ["--repo", REPO, "--from-url", "https://app.example/version", "--json-field", "commit"],
            fetch=lambda url: {"commit": "abcdef1"},
        )
        self.assertEqual(code, 2)

    def test_api_calls_do_not_follow_redirects_so_the_token_stays_on_github(self):
        seen = {"target_auth": None, "target_hit": False}

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["target_hit"] = True
                seen["target_auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()

            def log_message(self, *a):
                pass

        origin = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        threads = [threading.Thread(target=srv.serve_forever, daemon=True) for srv in (target, origin)]
        for t in threads:
            t.start()
        try:
            with mock.patch.object(ctd, "API_ROOT", f"http://127.0.0.1:{origin.server_port}"):
                with mock.patch.dict("os.environ", {"GH_TOKEN": "tok-should-not-travel"}):
                    with self.assertRaises(ctd.CheckError):
                        ctd.api_get("/repos/o/r")
        finally:
            origin.shutdown()
            target.shutdown()
        self.assertFalse(seen["target_hit"], "the redirect was followed")
        self.assertIsNone(seen["target_auth"])


class CaveatTests(unittest.TestCase):
    def test_help_states_the_as_of_now_limit(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit):
                ctd.main(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("as of NOW", text)
        self.assertIn("deployed-then-merged from merged-then-deployed", text)
        self.assertIn("deploy-time guard is the check that binds", text)


if __name__ == "__main__":
    unittest.main()
