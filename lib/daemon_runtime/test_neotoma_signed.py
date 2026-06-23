"""Unit tests for the per-agent AAuth signed-request wrapper (option A).

Stdlib-only (unittest) so it runs in minimal daemon environments without pytest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import neotoma_signed as ns  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
