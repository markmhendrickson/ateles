"""Unit tests for the per-agent AAuth signed-request wrapper (option A).

Stdlib-only (unittest) so it runs in minimal daemon environments without pytest.
"""

import json
import concurrent.futures
import select
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


_REAL_POPEN = subprocess.Popen


def _popen_at_control(*args, **kwargs):
    """An OS barrier immediately before a real helper process is created."""
    ready = os.open(os.environ["ATELES_SIGNED_TEST_READY"], os.O_WRONLY)
    os.write(ready, str(os.getpid()).encode())
    os.close(ready)
    release = os.open(os.environ["ATELES_SIGNED_TEST_RELEASE"], os.O_RDONLY)
    try:
        os.read(release, 1)
    finally:
        os.close(release)
    return _REAL_POPEN(*args, **kwargs)


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
        root, effect = self._real_helper_fixture()
        os.environ["ATELES_SIGNED_TEST_BODY"] = json.dumps({"entities": [{"id": "e1"}]})
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            status, data = ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus")
        self.assertEqual((status, data), (200, {"entities": [{"id": "e1"}]}))
        observed = json.loads(effect.read_text())
        self.assertEqual(observed["sub"], "apus@ateles-swarm")
        self.assertTrue(observed["key"].endswith("apus.jwk.json"))

    def test_signed_request_raises_on_helper_error(self):
        root, _ = self._real_helper_fixture()
        os.environ["ATELES_SIGNED_TEST_ERROR"] = "fetch failed"
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("GET", "http://fixture.invalid", agent_name="apus")
        self.assertEqual(caught.exception.code, "transport_error")

    def test_non_json_response_body_has_its_own_code(self):
        """A server body that is not JSON (a gateway's HTML page) is not a helper fault."""
        root, _ = self._real_helper_fixture()
        os.environ["ATELES_SIGNED_TEST_BODY"] = "<html>502 Bad Gateway private detail</html>"
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus")
        self.assertEqual(caught.exception.code, "response_not_json")
        self.assertNotIn("private detail", str(caught.exception))

    def _write_public_key(self, agent, x="x-one", sub=None):
        (self.keys / f"{agent}.jwk.json").write_text(
            json.dumps({"kty": "EC", "crv": "P-256", "x": x, "y": "y-one", "kid": "synthetic"})
        )

    def test_principal_pin_blocks_a_changed_principal_before_anything_is_sent(self):
        """The server de-duplicates an idempotency key per principal: a retry signed
        by another principal must not reach the helper at all."""
        root, effect = self._real_helper_fixture()
        self._write_public_key("apus")
        pin = ns.PrincipalPin()
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            first = ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                      principal_pin=pin)
            self.assertEqual(first, (200, {}))
            self.assertIsNotNone(pin.principal[1], "the key thumbprint was not recorded")
            effect.unlink()
            # Same principal: admitted, and the helper runs again.
            self.assertEqual(ns.signed_request("POST", "http://fixture.invalid", {},
                                               agent_name="apus", principal_pin=pin), (200, {}))
            self.assertTrue(effect.exists())
            effect.unlink()
            # The key rotates between attempts: a different thumbprint.
            self._write_public_key("apus", x="x-two")
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  principal_pin=pin)
            self.assertEqual(caught.exception.code, "identity_changed")
            self.assertFalse(effect.exists(), "a retry under a changed principal reached the helper")
            # The subject changes between attempts: a different principal too.
            self._write_public_key("apus")
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  sub="someone-else@ateles-swarm", principal_pin=pin)
            self.assertEqual(caught.exception.code, "identity_changed")
            self.assertFalse(effect.exists())

    def _swap_key_after_admission(self, agent, swapped_x="x-swapped"):
        """Run the real admit, then replace the key file: the swap lands after the
        preparer fingerprinted the key and before transport is authorized, so before
        the helper reads it."""
        real_admit = ns.PrincipalPin.admit

        def admit(pin, principal):
            allowed = real_admit(pin, principal)
            self._write_public_key(agent, x=swapped_x)
            return allowed

        return mock.patch.object(ns.PrincipalPin, "admit", admit)

    def test_helper_refuses_a_key_swapped_after_the_preparers_fingerprint(self):
        """The residual the preparer's check cannot close: the key file changes after the
        fingerprint and before the helper reads it. The helper verifies the key it
        actually read and aborts before signing; nothing is sent."""
        root, effect = self._real_helper_fixture()
        self._write_public_key("apus")
        pin = ns.PrincipalPin()
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            # Control: an unchanged key is signed, and signed with the in-memory key.
            self.assertEqual(ns.signed_request("POST", "http://fixture.invalid", {},
                                               agent_name="apus", principal_pin=pin), (200, {}))
            self.assertEqual(json.loads(effect.read_text())["signed_x"], "x-one")
            effect.unlink()
            with self._swap_key_after_admission("apus"):
                with self.assertRaises(ns.SignedRequestError) as caught:
                    ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                      principal_pin=pin)
            self.assertEqual(caught.exception.code, "identity_changed")
            self.assertNotIn("x-swapped", str(caught.exception))
            self.assertFalse(effect.exists(), "a swapped key was signed with")

    def test_planted_red_without_the_helper_check_the_swapped_key_signs(self):
        """With the helper's verification removed the same swap is signed and sent,
        under a key whose thumbprint the pin never admitted."""
        root, effect = self._real_helper_fixture()
        self._write_public_key("apus")
        pin = ns.PrincipalPin()
        source = (Path(ns.__file__).resolve().parent / "signed_fetch.mjs").read_text()
        needle = "ecThumbprint(configOverride.privateJwk) !== spec.expect_thumbprint"
        self.assertIn(needle, source)
        unchecked = Path(self._tmp.name) / "unchecked" / "signed_fetch.mjs"
        unchecked.parent.mkdir()
        unchecked.write_text(source.replace(needle, "false"))
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns, "_HELPER", unchecked
        ):
            ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                              principal_pin=pin)
            effect.unlink()
            with self._swap_key_after_admission("apus"):
                result = ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                           principal_pin=pin)
        self.assertEqual(result, (200, {}))
        self.assertEqual(json.loads(effect.read_text())["signed_x"], "x-swapped")

    def test_a_pinned_call_fails_closed_when_the_signer_cannot_load_the_key_it_signs_with(self):
        root, effect = self._real_helper_fixture()
        (root / "dist" / "cli" / "aauth_signer.js").write_text(
            'import fs from "node:fs";\n'
            'export async function cliSignedFetch(url, options) {\n'
            '  fs.writeFileSync(process.env.ATELES_SIGNED_TEST_EFFECT, "signed");\n'
            '  return {status: 200, ok: true, text: async () => "{}"};\n}\n')
        self._write_public_key("apus")
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            with self.assertRaises(ns.SignedRequestError):
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  principal_pin=ns.PrincipalPin())
        self.assertFalse(effect.exists(), "an unverifiable pinned call signed anyway")

    def test_a_pinned_call_with_a_key_that_cannot_be_fingerprinted_is_refused(self):
        root, effect = self._real_helper_fixture()  # writes a key without x/y
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  principal_pin=ns.PrincipalPin())
        self.assertEqual(caught.exception.code, "no_key")
        self.assertFalse(effect.exists())

    def test_without_a_pin_a_changed_key_is_not_checked(self):
        """Callers that pass no pin (the loader) keep exactly their previous behavior."""
        root, effect = self._real_helper_fixture()
        self._write_public_key("apus")
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus")
            self._write_public_key("apus", x="x-two")
            effect.unlink()
            self.assertEqual(ns.signed_request("POST", "http://fixture.invalid", {},
                                               agent_name="apus"), (200, {}))
        self.assertTrue(effect.exists())

    def test_principal_pin_first_resolution_wins_and_is_thread_safe(self):
        pin = ns.PrincipalPin()
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(pin.admit(("sub", f"tp{i}"))))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1, "exactly one principal may become the pinned one")
        self.assertTrue(pin.admit(pin.principal))

    def test_signed_request_no_key_raises(self):
        ns.AAUTH_KEYS_DIR = str(Path(self._tmp.name) / "empty")
        with self.assertRaises(RuntimeError) as cm:
            ns.signed_request("GET", "http://x", agent_name="ghost")
        self.assertIn("no AAuth key", str(cm.exception))

    def test_launch_timeout_is_recomputed_after_preparation(self):
        root, effect = self._real_helper_fixture()

        def clock():
            return 101.0 if (self.keys / "prepared").exists() else 100.0

        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns, "agent_identity", _identity_after_virtual_second
        ):
            with self.assertRaises(ns.SignedRequestError) as caught:
                ns.signed_request("POST", "http://fixture.invalid", {}, timeout=10,
                                  deadline=100.5, clock=clock)
        self.assertEqual(caught.exception.code, "expired")
        self.assertFalse(effect.exists())

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
            'export async function loadCliSignerConfig() {\n'
            '  return {privateJwk: JSON.parse(fs.readFileSync(process.env.NEOTOMA_AAUTH_PRIVATE_JWK_PATH, "utf8"))};\n'
            '}\n'
            'export async function cliSignedFetch(url, options) {\n'
            '  if (process.env.ATELES_SIGNED_TEST_ERROR) throw new Error(process.env.ATELES_SIGNED_TEST_ERROR);\n'
            '  fs.writeFileSync(process.env.ATELES_SIGNED_TEST_EFFECT,\n'
            '    JSON.stringify({pid: process.pid, method: options.method, sub: process.env.NEOTOMA_AAUTH_SUB, key: process.env.NEOTOMA_AAUTH_PRIVATE_JWK_PATH,\n'
            '      signed_x: options.configOverride ? options.configOverride.privateJwk.x : null}));\n'
            '  return {status: 200, ok: true, text: async () => process.env.ATELES_SIGNED_TEST_BODY || "{}"};\n'
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

    def test_direct_caller_is_bounded_during_real_process_start(self):
        root, effect = self._real_helper_fixture()
        context = ns.multiprocessing.get_context("spawn")
        entered, release = threading.Event(), threading.Event()
        real_process = context.Process
        failures = []
        children = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        permits = threading.BoundedSemaphore(1)

        def process(*args, **kwargs):
            child = real_process(*args, **kwargs)
            children.append(child)
            start = child.start

            def delayed_start():
                entered.set()
                release.wait(3)
                del child.start
                start()

            child.start = delayed_start
            return child

        def request():
            try:
                ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                  timeout=0.2)
            except (ns.SignedRequestError, subprocess.TimeoutExpired) as exc:
                failures.append(exc)

        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            context, "Process", side_effect=process
        ), mock.patch.object(ns.multiprocessing, "get_context", return_value=context), mock.patch.object(
            ns, "_signed_pool", pool, create=True
        ), mock.patch.object(ns, "_signed_permits", permits, create=True):
            caller = threading.Thread(target=request)
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                caller.join(0.5)
                self.assertFalse(caller.is_alive(), "process creation held the direct caller")
                self.assertTrue(failures)
                with self.assertRaises(ns.SignedRequestError) as busy:
                    ns.signed_request("GET", "http://fixture.invalid", agent_name="apus", timeout=0.1)
                self.assertEqual(busy.exception.code, "busy")
            finally:
                release.set()
                caller.join(3)
                pool.shutdown(wait=True)
        self.assertFalse(effect.exists())
        self.assertTrue(children and all(child.pid and not child.is_alive() for child in children))
        self.assertTrue(permits.acquire(blocking=False), "startup cleanup retained its permit")
        permits.release()

    def test_start_error_recovers_the_same_worker(self):
        root, effect = self._real_helper_fixture()
        context = ns.multiprocessing.get_context("spawn")
        real_process = context.Process
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        permits = threading.BoundedSemaphore(1)

        def process(*args, **kwargs):
            child = real_process(*args, **kwargs)
            child.start = mock.Mock(side_effect=OSError("synthetic startup failure"))
            return child

        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns, "_signed_pool", pool
        ), mock.patch.object(ns, "_signed_permits", permits):
            try:
                with mock.patch.object(context, "Process", side_effect=process), mock.patch.object(
                    ns.multiprocessing, "get_context", return_value=context
                ):
                    with self.assertRaises(OSError):
                        ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="apus")
                pool.submit(lambda: None).result(timeout=2)
                self.assertEqual(ns.signed_request("POST", "http://fixture.invalid", {},
                                                   agent_name="apus"), (200, {}))
                self.assertTrue(effect.exists())
            finally:
                pool.shutdown(wait=True)

    def _helper_creation_control(self, *, cancel, loader=False):
        root, effect = self._real_helper_fixture()
        ready, release = root / "ready", root / "release"
        os.mkfifo(ready)
        os.mkfifo(release)
        os.environ["ATELES_SIGNED_TEST_READY"] = str(ready)
        os.environ["ATELES_SIGNED_TEST_RELEASE"] = str(release)
        ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
        cancelled = threading.Event()
        errors = []
        fallback = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        permits = threading.BoundedSemaphore(1)
        real_request = ns.signed_request
        import agent_loader as al

        def budgeted_request(*args, **kwargs):
            kwargs.update(timeout=1, cancelled=cancelled)
            return real_request(*args, **kwargs)

        def bearer(*args, **kwargs):
            fallback.append(True)
            return mock.Mock(status_code=200, headers={"content-type": "application/json"},
                             json=lambda: {"synthetic_fallback": True})

        def request():
            try:
                if loader:
                    al.AgentLoader("apus")._neotoma("POST", "http://fixture.invalid", {})
                else:
                    real_request("POST", "http://fixture.invalid", {}, agent_name="apus",
                                 timeout=1, cancelled=cancelled)
            except (ns.SignedRequestError, subprocess.TimeoutExpired) as exc:
                errors.append(exc)

        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            ns.subprocess, "Popen", _popen_at_control
        ), mock.patch.object(ns, "_signed_pool", pool, create=True), mock.patch.object(
            ns, "_signed_permits", permits, create=True
        ), mock.patch.object(al, "ns", ns), mock.patch.object(
            ns, "signed_request", budgeted_request
        ), mock.patch.object(al.httpx, "post", side_effect=bearer), mock.patch.dict(
            os.environ, {"NEOTOMA_AAUTH_VIA_CLI": "1"}
        ):
            caller = threading.Thread(target=request)
            caller.start()
            try:
                self.assertTrue(select.select([ready_fd], [], [], 2)[0], "real startup seam not reached")
                worker_pid = int(os.read(ready_fd, 100))
                self.assertGreater(worker_pid, 0)
                if cancel:
                    cancelled.set()
                caller.join(1.5)
                bounded = not caller.is_alive()
            finally:
                os.write(release_fd, b"1")
                caller.join(3)
                pool.shutdown(wait=True)
                os.close(ready_fd)
                os.close(release_fd)
        self.assertTrue(bounded, "helper creation held the direct caller")
        self.assertTrue(fallback if loader else errors, "abandoned attempt was accepted")
        self.assertFalse(effect.exists(), "an abandoned attempt produced a real helper effect")

    def test_cancelled_attempt_has_no_effect_at_process_boundary(self):
        self._helper_creation_control(cancel=True)

    def test_expired_attempt_has_no_effect_at_process_boundary(self):
        self._helper_creation_control(cancel=False)

    def test_loader_signed_path_is_bounded_during_helper_creation(self):
        self._helper_creation_control(cancel=False, loader=True)

    def test_loader_signed_path_uses_the_existing_helper(self):
        import agent_loader as al
        root, effect = self._real_helper_fixture()
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            al, "ns", ns
        ), mock.patch.dict(os.environ, {"NEOTOMA_AAUTH_VIA_CLI": "1"}), mock.patch.object(
            al.httpx, "post"
        ) as bearer:
            self.assertEqual(al.AgentLoader("apus")._neotoma("POST", "http://fixture.invalid", {}), {})
        self.assertTrue(effect.exists())
        bearer.assert_not_called()

    def test_loader_signed_get_without_a_body_returns_the_parsed_response(self):
        """The loader's other request shape: a GET with no body, default limits."""
        import agent_loader as al
        root, effect = self._real_helper_fixture()
        os.environ["ATELES_SIGNED_TEST_BODY"] = json.dumps({"entity": {"id": "e1"}})
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)), mock.patch.object(
            al, "ns", ns
        ), mock.patch.dict(os.environ, {"NEOTOMA_AAUTH_VIA_CLI": "1"}), mock.patch.object(
            al.httpx, "get"
        ) as bearer:
            self.assertEqual(al.AgentLoader("apus")._neotoma("GET", "http://fixture.invalid/e1"),
                             {"entity": {"id": "e1"}})
        self.assertTrue(effect.exists())
        bearer.assert_not_called()

    def test_loader_falls_back_to_bearer_when_every_signing_worker_is_busy(self):
        """Pool capacity: the loader used to start a subprocess per call with no
        bound; the shared pool now answers `busy` at once when all workers are in
        use, and the loader's documented fallback keeps it available."""
        import agent_loader as al
        self._write_key("apus")
        taken = 0
        try:
            while ns._signed_permits.acquire(blocking=False):
                taken += 1
            self.assertGreaterEqual(taken, 1)
            response = mock.Mock(status_code=200, headers={"content-type": "application/json"},
                                 json=lambda: {"via": "bearer"})
            real_request, codes = ns.signed_request, []

            def observed(*args, **kwargs):
                try:
                    return real_request(*args, **kwargs)
                except ns.SignedRequestError as exc:
                    codes.append(exc.code)
                    raise

            with mock.patch.object(al, "ns", ns), mock.patch.object(
                ns, "signed_request", observed
            ), mock.patch.dict(
                os.environ, {"NEOTOMA_AAUTH_VIA_CLI": "1"}
            ), mock.patch.object(al.httpx, "get", return_value=response) as bearer:
                self.assertEqual(al.AgentLoader("apus")._neotoma("GET", "http://fixture.invalid/e1"),
                                 {"via": "bearer"})
            self.assertEqual(codes, ["busy"], "the shared pool did not refuse the call")
            bearer.assert_called_once()
        finally:
            for _ in range(taken):
                ns._signed_permits.release()

    def test_loader_falls_back_to_bearer_on_each_signed_failure_class(self):
        import agent_loader as al
        self._write_key("apus")
        failures = [ns.SignedRequestError("x", code) for code in
                    ("busy", "expired", "helper_timeout", "response_not_json", "transport_error",
                     "helper_error", "no_key")]
        failures.append(subprocess.TimeoutExpired("signed_request", 20))
        for failure in failures:
            with self.subTest(failure=getattr(failure, "code", type(failure).__name__)):
                response = mock.Mock(status_code=200, headers={"content-type": "application/json"},
                                     json=lambda: {"via": "bearer"})
                with mock.patch.object(al, "ns", ns), mock.patch.object(
                    ns, "signed_request", side_effect=failure
                ), mock.patch.dict(os.environ, {"NEOTOMA_AAUTH_VIA_CLI": "1"}), mock.patch.object(
                    al.httpx, "get", return_value=response
                ):
                    self.assertEqual(al.AgentLoader("apus")._neotoma("GET", "http://fixture.invalid"),
                                     {"via": "bearer"})

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
        root, effect = self._real_helper_fixture()
        self._write_key("waxwing", kid="waxwing-kid")
        os.environ["NEOTOMA_AAUTH_SUB"] = "apis@ateles-swarm"
        with mock.patch.object(ns, "NEOTOMA_RC_DIR", str(root)):
            ns.signed_request("POST", "http://fixture.invalid", {}, agent_name="waxwing",
                              sub="waxwing@ateles-swarm")
        observed = json.loads(effect.read_text())
        self.assertEqual(observed["sub"], "waxwing@ateles-swarm")
        self.assertNotEqual(observed["sub"], "apis@ateles-swarm")


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
