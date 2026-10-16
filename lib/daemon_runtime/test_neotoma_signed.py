"""Unit tests for the per-agent AAuth signed-request wrapper (option A).

Stdlib-only (unittest) so it runs in minimal daemon environments without pytest.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import neotoma_signed as ns  # noqa: E402


def _identity_after_virtual_second(agent_name, *, sub=None, keys_dir=None):
    (Path(keys_dir) / "prepared").write_text("prepared")
    return {"key": "synthetic-unused", "sub": sub or "synthetic", "kid": "synthetic"}


class NeotomaSignedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.keys = Path(self._tmp.name) / "keys"
        self.keys.mkdir()
        self._saved_env = dict(os.environ)
        self._saved_keys_dir = ns.AAUTH_KEYS_DIR

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)
        ns.AAUTH_KEYS_DIR = self._saved_keys_dir

    def _write_key(self, agent, kid="kid-xyz"):
        (self.keys / f"{agent}.jwk.json").write_text(
            json.dumps({"kty": "EC", "crv": "P-256", "kid": kid, "alg": "ES256"})
        )
        ns.AAUTH_KEYS_DIR = str(self.keys)

    def test_via_cli_enabled(self):
        for off in ("", "0", "false", "no", "FALSE"):
            os.environ["NEOTOMA_AAUTH_VIA_CLI"] = off
            self.assertFalse(ns.via_cli_enabled())
        for on in ("1", "true", "yes"):
            os.environ["NEOTOMA_AAUTH_VIA_CLI"] = on
            self.assertTrue(ns.via_cli_enabled())
        os.environ.pop("NEOTOMA_AAUTH_VIA_CLI", None)
        self.assertFalse(ns.via_cli_enabled())

    def test_agent_identity(self):
        self._write_key("apus", kid="apus-kid")
        os.environ.pop("NEOTOMA_AAUTH_SUB", None)
        self.assertEqual(
            ns.agent_identity("apus"),
            {"key": str(self.keys / "apus.jwk.json"), "sub": "apus@ateles-swarm", "kid": "apus-kid"},
        )
        self.assertIsNone(ns.agent_identity("nope"))
        self.assertIsNone(ns.agent_identity(""))
        os.environ["NEOTOMA_AAUTH_SUB"] = "custom@x"
        self.assertEqual(ns.agent_identity("apus")["sub"], "custom@x")

    def test_signed_request_parses_helper_output(self):
        self._write_key("apus")
        helper_out = json.dumps(
            {"status": 200, "ok": True, "body": json.dumps({"entities": [{"id": "e1"}]})}
        )

        def fake_run(cmd, **kw):
            self.assertTrue(kw["env"]["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"].endswith("apus.jwk.json"))
            self.assertEqual(kw["env"]["NEOTOMA_AAUTH_SUB"], "apus@ateles-swarm")
            self.assertGreater(kw["timeout"], 0)
            self.assertLess(kw["timeout"], 20, "preparation must consume the transport budget")
            return subprocess.CompletedProcess(cmd, 0, stdout=helper_out, stderr="")

        with mock.patch.object(ns.subprocess, "run", side_effect=fake_run):
            status, data = ns.signed_request(
                "POST", "http://x/entities/query", {"q": 1}, agent_name="apus"
            )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"entities": [{"id": "e1"}]})

    def test_signed_request_raises_on_helper_error(self):
        self._write_key("apus")
        out = json.dumps({"error": "cliSignedFetch not found"})
        with mock.patch.object(
            ns.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1, stdout=out, stderr=""),
        ):
            with self.assertRaises(RuntimeError) as cm:
                ns.signed_request("GET", "http://x/health", agent_name="apus")
        self.assertIn("cliSignedFetch not found", str(cm.exception))

    def test_signed_request_no_key_raises(self):
        ns.AAUTH_KEYS_DIR = str(Path(self._tmp.name) / "empty")
        with self.assertRaises(RuntimeError) as cm:
            ns.signed_request("GET", "http://x", agent_name="ghost")
        self.assertIn("no AAuth key", str(cm.exception))

    def test_launch_timeout_is_recomputed_after_preparation(self):
        ns.AAUTH_KEYS_DIR = str(self.keys)

        def clock():
            return 101.0 if (self.keys / "prepared").exists() else 100.0

        with mock.patch.object(ns, "agent_identity", _identity_after_virtual_second), mock.patch.object(
            ns.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout='{"status":200,"body":"{}"}'),
        ) as launch:
            ns.signed_request("POST", "http://unused.invalid", {}, timeout=10,
                              deadline=110.0, clock=clock)
        self.assertEqual(launch.call_args.kwargs["timeout"], 9.0)

    def test_expired_request_never_starts_preparation_or_transport(self):
        with mock.patch.object(ns.multiprocessing, "get_context") as context:
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://unused.invalid", {},
                                  deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, "expired")
        context.assert_not_called()

    def test_abandoned_request_never_starts_preparation_or_transport(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(ns.multiprocessing, "get_context") as context:
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://unused.invalid", {}, cancelled=cancelled)
        self.assertEqual(caught.exception.code, "expired")
        context.assert_not_called()

    def _real_helper_fixture(self):
        root = Path(self._tmp.name) / "helper-fixture"
        module = root / "dist" / "cli" / "aauth_signer.js"
        module.parent.mkdir(parents=True)
        (root / "package.json").write_text('{"type":"module"}')
        module.write_text(
            'import fs from "node:fs";\n'
            'export async function cliSignedFetch(url, options) {\n'
            '  fs.writeFileSync(process.env.ATELES_SIGNED_TEST_EFFECT,\n'
            '    JSON.stringify({pid: process.pid, method: options.method}));\n'
            '  return {status: 200, ok: true, text: async () => "{}"};\n'
            '}\n'
        )
        effect = root / "effect.json"
        os.environ["ATELES_SIGNED_TEST_EFFECT"] = str(effect)
        self._write_key("apus")
        return root, effect

    def test_real_helper_fixture_records_success(self):
        root, effect = self._real_helper_fixture()
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            result = ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus")
        self.assertEqual(result, (200, {}))
        self.assertGreater(json.loads(effect.read_text())["pid"], 0)

    def test_cancelled_attempt_has_no_effect_at_process_boundary(self):
        root, effect = self._real_helper_fixture()
        cancelled = threading.Event()
        real_run = subprocess.run

        def cancel_then_run(*args, **kwargs):
            cancelled.set()
            return real_run(*args, **kwargs)

        error = None
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns.subprocess, "run", side_effect=cancel_then_run
        ):
            try:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  cancelled=cancelled)
            except (ns.SignedRequestError, subprocess.TimeoutExpired) as exc:
                error = exc
        self.assertFalse(effect.exists(), "a cancelled attempt produced a real helper effect")
        self.assertIsNotNone(error)

    def test_expired_attempt_has_no_effect_at_process_boundary(self):
        root, effect = self._real_helper_fixture()
        deadline = time.monotonic() + 1.0
        real_run = subprocess.run

        def expire_then_run(*args, **kwargs):
            threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.05)
            return real_run(*args, **kwargs)

        error = None
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns.subprocess, "run", side_effect=expire_then_run
        ):
            try:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  deadline=deadline)
            except (ns.SignedRequestError, subprocess.TimeoutExpired) as exc:
                error = exc
        self.assertFalse(effect.exists(), "an expired attempt produced a real helper effect")
        self.assertIsNotNone(error)

    # ── ateles#795 constraint 2: explicit subject, never ambient ──────────────

    def test_agent_identity_explicit_sub_ignores_ambient_env(self):
        """An explicit `sub` wins even when NEOTOMA_AAUTH_SUB names someone else.

        Reproduces the daemon-process hazard named in ateles#795: a process
        (e.g. Apis) that already carries its OWN NEOTOMA_AAUTH_SUB must still
        be able to sign as a DIFFERENT principal (a reviewing lens) without
        that ambient value leaking into the signature.
        """
        self._write_key("pavo", kid="pavo-kid")
        os.environ["NEOTOMA_AAUTH_SUB"] = "apis@ateles-swarm"
        ident = ns.agent_identity("pavo", sub="pavo@ateles-swarm")
        self.assertEqual(ident["sub"], "pavo@ateles-swarm")
        self.assertNotEqual(ident["sub"], os.environ["NEOTOMA_AAUTH_SUB"])

    def test_agent_identity_no_explicit_sub_keeps_ambient_ladder(self):
        """Omitting `sub` preserves EXACT existing behavior for agent_loader.py."""
        self._write_key("apus", kid="apus-kid")
        os.environ["NEOTOMA_AAUTH_SUB"] = "custom@x"
        self.assertEqual(ns.agent_identity("apus")["sub"], "custom@x")
        os.environ.pop("NEOTOMA_AAUTH_SUB", None)
        self.assertEqual(ns.agent_identity("apus")["sub"], "apus@ateles-swarm")

    def test_signed_request_explicit_sub_reaches_the_subprocess_env(self):
        """`sign_off`'s explicit subject must reach NEOTOMA_AAUTH_SUB in the
        subprocess env — not the daemon process's own ambient value."""
        self._write_key("waxwing", kid="waxwing-kid")
        os.environ["NEOTOMA_AAUTH_SUB"] = "apis@ateles-swarm"
        helper_out = json.dumps({"status": 200, "ok": True, "body": "{}"})

        seen_env = {}

        def fake_run(cmd, **kw):
            seen_env.update(kw["env"])
            return subprocess.CompletedProcess(cmd, 0, stdout=helper_out, stderr="")

        with mock.patch.object(ns.subprocess, "run", side_effect=fake_run):
            ns.signed_request(
                "POST",
                "http://x/correct",
                {"f": 1},
                agent_name="waxwing",
                sub="waxwing@ateles-swarm",
            )
        self.assertEqual(seen_env["NEOTOMA_AAUTH_SUB"], "waxwing@ateles-swarm")
        self.assertNotEqual(seen_env["NEOTOMA_AAUTH_SUB"], "apis@ateles-swarm")


class CheckObservationAttributionTest(unittest.TestCase):
    """PR #1274 round-3 (Falco): this check compares the key thumbprint Neotoma
    records for the observation, not the agent_sub label alone. A verified signature
    actually proves the KEY, which Neotoma records as provenance.agent_thumbprint —
    the value that must be pinned alongside agent_sub, not agent_sub alone."""

    SUB = "anthus@ateles-swarm"
    TIER = "software"
    TP = "expected-thumbprint-abc123"

    def _obs(self, obs_id="obs_1", sub=SUB, tier=TIER, thumbprint="__unset__"):
        prov = {"agent_sub": sub, "attribution_tier": tier}
        if thumbprint != "__unset__":
            prov["agent_thumbprint"] = thumbprint
        return {"id": obs_id, "provenance": prov}

    def test_name_matches_but_thumbprint_does_not_is_not_accepted(self):
        obs = self._obs(thumbprint="a-different-key-thumbprint")
        check = ns.check_observation_attribution([obs], "obs_1", self.SUB, self.TP)
        self.assertFalse(check.ok)
        self.assertIn("agent_thumbprint", check.reason)

    def test_both_sub_and_thumbprint_match_is_accepted(self):
        obs = self._obs(thumbprint=self.TP)
        check = ns.check_observation_attribution([obs], "obs_1", self.SUB, self.TP)
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.agent_thumbprint, self.TP)

    def test_thumbprint_missing_is_not_accepted(self):
        obs = self._obs(thumbprint="__unset__")
        check = ns.check_observation_attribution([obs], "obs_1", self.SUB, self.TP)
        self.assertFalse(check.ok)
        self.assertIn("agent_thumbprint", check.reason)

    def test_thumbprint_empty_string_is_not_accepted(self):
        obs = self._obs(thumbprint="")
        check = ns.check_observation_attribution([obs], "obs_1", self.SUB, self.TP)
        self.assertFalse(check.ok)
        self.assertIn("agent_thumbprint", check.reason)

    def test_no_expected_thumbprint_is_not_accepted(self):
        """PR #1274 round-4 (Falco non-blocking note 1): the sub-only fallback was reachable
        because expected_thumbprint defaulted to None; it is now a required argument, so
        passing None must fail rather than silently accept a sub-only match."""
        obs = self._obs(thumbprint="whatever-or-nothing")
        check = ns.check_observation_attribution([obs], "obs_1", self.SUB, None)
        self.assertFalse(check.ok)
        self.assertIn("agent_thumbprint", check.reason)


if __name__ == "__main__":
    unittest.main()
