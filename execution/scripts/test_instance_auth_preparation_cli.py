"""Actual owned HTTP/CLI/protected-consumer paths; no external credentials."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from execution.lib.instance_auth_profile_guard import (
    AUTH_PHASES,
    AUTH_SECRET_NAMES,
    expected_auth_config,
)
from execution.lib.instance_profile_guard import digest, stable_source
from execution.scripts import test_instance_auth_profile_adapter as adapter_tests
from execution.scripts.check_instance_auth_preparation import runtime_digest


class NaturalAuthPreflight(unittest.TestCase):
    def setUp(self):
        fixture = adapter_tests.ProtectedMetadataAdapter()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.manifest = copy.deepcopy(fixture.manifest)
        self.manifest["protected_fields"] = fixture.refs
        self.manifest["candidate"]["guard_source_sha256"] = runtime_digest()
        self.canonical = copy.deepcopy(fixture.canonical)
        self.requests = []
        self.changed_after = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(
                    (self.command, self.path, self.headers.get("Authorization"))
                )
                payload = outer.canonical
                if (
                    outer.changed_after is not None
                    and len(outer.requests) >= outer.changed_after
                ):
                    payload = {
                        **payload,
                        "snapshot": {
                            **payload["snapshot"],
                            "public_domain": "foreign.example.test",
                        },
                    }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        before_config = {
            "env": {"UNCHANGED": "preserved"},
            "guest": {"memory_mb": 512, "cpus": 1},
            "services": [{"internal_port": 8080, "checks": [{"path": "/health"}]}],
            "image": "registry.example.test/owned@sha256:" + "0" * 64,
            "metadata": {},
        }
        source = {
            "id": "owned-machine",
            "state": "started",
            "region": "owned-region",
            "config": before_config,
            "image_ref": {"digest": "sha256:" + "0" * 64},
        }
        self.before = {
            "inventory": [source],
            "saved": {"env": {"UNCHANGED": "preserved"}, "vm": [{"memory": "512mb"}]},
            "secrets": [
                {
                    "name": "THEODORE_PASSWORD",
                    "status": "Deployed",
                    "version": "unchanged-v1",
                }
            ],
            "volumes": [],
        }
        self.after = copy.deepcopy(self.before)
        self.after["saved"] = expected_auth_config(
            self.before["saved"], self.manifest["binding"]
        )
        self.after["inventory"][0]["config"] = expected_auth_config(
            before_config, self.manifest["binding"]
        )
        image = self.manifest["candidate"]["image"]
        self.after["inventory"][0]["config"]["image"] = image
        self.after["inventory"][0]["image_ref"] = {"digest": image.split("@", 1)[1]}
        self.phases = {}
        self.before_refs = {}
        self.synthetic_values = {
            name: "owned-tenant"
            if name.endswith("TENANT_ID")
            else "owned-signin-client"
            if name.endswith("CLIENT_ID")
            else "synthetic-private-canary"
            for name in AUTH_SECRET_NAMES
        }
        self.op_log = self.root / "op-log.jsonl"
        executable = self.root / "op"
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import sys,json\n"
            "from pathlib import Path\n"
            "root=Path(__file__).parent\n"
            "with (root/'op-log.jsonl').open('a') as f:f.write(json.dumps(sys.argv[1:])+chr(10))\n"
            "assert sys.argv[1:3]==['read','--no-newline']\n"
            "print(json.loads((root/'values.json').read_text())[sys.argv[3].rsplit('/',1)[1]],end='')\n"
        )
        executable.chmod(0o700)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def setup_phases(self):
        for phase in AUTH_PHASES:
            current = copy.deepcopy(
                self.after if phase == "auth_deploy_after" else self.before
            )
            if phase != "auth_stage_before":
                status = "Deployed" if phase == "auth_deploy_after" else "Staged"
                old = {row["name"]: row for row in current["secrets"]}
                for name in sorted(AUTH_SECRET_NAMES):
                    old[name] = {
                        **old.get(name, {"name": name, "version": "oidc-v1"}),
                        "status": status,
                    }
                current["secrets"] = list(old.values())
            self.phases[phase] = current
        self.after["secrets"] = copy.deepcopy(
            self.phases["auth_deploy_after"]["secrets"]
        )
        self.manifest["profiles"] = {
            "before_config_sha256": digest(self.before["saved"]),
            "after_config_sha256": digest(self.after["saved"]),
            "before_static_sha256": digest(
                stable_source(self.before["inventory"][0]["config"])
            ),
            "after_static_sha256": digest(
                stable_source(self.after["inventory"][0]["config"])
            ),
            "before_secret_metadata_sha256": digest(self.before["secrets"]),
        }
        packet = {
            "before": self.before,
            "after": self.after,
            "phase_sha256": {k: digest(v) for k, v in self.phases.items()},
            "before_protected_fields": self.before_refs,
        }
        registration_path = self.write(
            "registration.json", self.manifest["registration"]
        )
        record = {
            "version": 2,
            "selected": {
                "action_ref": self.manifest["action_ref"],
                "manifest_sha256": digest(self.manifest),
                "environment": "dev",
                "source_sha256": self.manifest["references"]["source_sha256"],
                "canonical_sha256": digest(self.canonical["snapshot"]),
                "registration_sha256": digest(self.manifest["registration"]),
            },
            "canonical": {
                "entity_id": self.canonical["entity_id"],
                "snapshot_sha256": digest(self.canonical["snapshot"]),
                "environment_mapping": {"canonical": "development", "runtime": "dev"},
            },
            "registration": {
                "path": str(registration_path),
                "sha256": digest(self.manifest["registration"]),
            },
            "profiles": {
                "path": str(self.write("profiles.json", packet)),
                "sha256": digest(packet),
            },
        }
        self.record = record
        self.write("record.json", record)
        self.write("manifest.json", self.manifest)
        self.write("values.json", self.synthetic_values)

    def cli(self, phase, *, optimized=False):
        self.write("current.json", self.phases[phase])
        output = self.root / "receipt.json"
        output.unlink(missing_ok=True)
        env = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "NEOTOMA_BASE_URL": f"http://localhost:{self.server.server_port}",
            "NEOTOMA_BEARER_TOKEN": "synthetic-owned-bearer",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [
                sys.executable,
                *(["-O"] if optimized else []),
                str(Path(__file__).with_name("check_instance_auth_preparation.py")),
                "--manifest",
                str(self.root / "manifest.json"),
                "--evidence",
                str(self.root / "current.json"),
                "--action-record",
                str(self.root / "record.json"),
                "--action-record-sha256",
                digest(self.record),
                "--phase",
                phase,
                "--output",
                str(output),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotIn("synthetic-private-canary", result.stdout + result.stderr)
        return result, json.loads(output.read_text()) if output.exists() else None

    def test_natural_four_phases_canonical_http_op_consumption_private_receipt(self):
        self.setup_phases()
        for optimized in (False, True):
            for phase in sorted(AUTH_PHASES):
                with self.subTest(phase=phase, optimized=optimized):
                    result, receipt = self.cli(phase, optimized=optimized)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(receipt["action_authorized"])
                    self.assertFalse(receipt["deployment_command_emitted"])
                    self.assertTrue(receipt["phase"]["whole_machine_profile_matches"])
                    self.assertNotIn("argv", receipt)
                    self.assertEqual(
                        (self.root / "receipt.json").stat().st_mode & 0o777, 0o600
                    )
        self.assertEqual(len(self.requests), 16)
        self.assertTrue(
            all(
                m == "GET"
                and p == "/entities/" + self.canonical["entity_id"]
                and a == "Bearer synthetic-owned-bearer"
                for m, p, a in self.requests
            )
        )
        self.assertEqual(len(self.op_log.read_text().splitlines()), 24)

    def test_actual_phase_drift_refuses_before_any_protected_reads(self):
        self.setup_phases()
        phase = "auth_deploy_before"
        for section, mutation in (
            ("inventory", lambda v: v[0]["config"]["guest"].update(memory_mb=8192)),
            ("saved", lambda v: v["env"].update(UNSELECTED="bad")),
            ("secrets", lambda v: v.append({"name": "FOREIGN", "status": "Staged"})),
            ("volumes", lambda v: v.append({"id": "foreign"})),
        ):
            original = copy.deepcopy(self.phases[phase])
            mutation(self.phases[phase][section])
            with self.subTest(section=section):
                result, receipt = self.cli(phase)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(receipt)
                self.assertFalse(self.op_log.exists())
            self.phases[phase] = original

    def test_missing_pins_canonical_and_runtime_drift_refuse(self):
        self.setup_phases()
        original = copy.deepcopy(self.record)
        self.record["profiles"]["sha256"] = "0" * 64
        self.write("record.json", self.record)
        result, _ = self.cli("auth_stage_before")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.op_log.exists())
        self.record = original
        self.write("record.json", self.record)
        self.changed_after = 1
        result, _ = self.cli("auth_stage_before")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.op_log.exists())

    def test_independently_pinned_extra_after_field_still_refuses(self):
        # Matching hashes do not authorize an extra runtime delta. Change only
        # an independently pinned after saved profile, keeping native state.
        self.after["saved"]["vm"][0]["memory"] = "8192mb"
        self.setup_phases()
        result, receipt = self.cli("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        self.assertFalse(self.op_log.exists())

    def test_selected_runtime_pin_is_not_a_template_attestation(self):
        self.manifest["candidate"]["guard_source_sha256"] = "0" * 64
        self.setup_phases()
        result, receipt = self.cli("auth_stage_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        self.assertFalse(self.op_log.exists())

    def test_protected_read_failure_and_wrong_target_are_sanitized(self):
        self.setup_phases()
        for value in ("foreign-synthetic-tenant", None):
            with self.subTest(value=value):
                values = dict(self.synthetic_values)
                if value is None:
                    values.pop("THEODORE_OIDC_TENANT_ID")
                else:
                    values["THEODORE_OIDC_TENANT_ID"] = value
                self.write("values.json", values)
                result, receipt = self.cli("auth_deploy_before")
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(receipt)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn(
                    "foreign-synthetic-tenant", result.stderr + result.stdout
                )

    def test_late_canonical_change_has_no_receipt_and_suppressed_errors(self):
        self.setup_phases()
        self.changed_after = 2
        result, receipt = self.cli("auth_stage_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        self.assertEqual(len(self.op_log.read_text().splitlines()), 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_existing_oidc_value_and_version_continuity_is_explicit(self):
        name = "THEODORE_OIDC_CLIENT_SECRET"
        self.before["secrets"].append(
            {"name": name, "status": "Deployed", "version": "oidc-v1"}
        )
        self.before_refs = {name: self.manifest["protected_fields"][name]}
        self.setup_phases()
        result, receipt = self.cli("auth_deploy_before")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["phase"]["old_versions_exposed"], [name])
        self.assertEqual(receipt["protected"]["continuity_names"], [name])
        self.phases["auth_deploy_before"]["secrets"][-1]["version"] = "changed"
        result, receipt = self.cli("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)

    def test_old_protected_value_mismatch_has_no_receipt(self):
        name = "THEODORE_OIDC_CLIENT_SECRET"
        self.before["secrets"].append(
            {"name": name, "status": "Deployed", "version": "oidc-v1"}
        )
        self.before_refs = {name: "op://Private/" + "b" * 26 + "/previous-secret"}
        self.synthetic_values["previous-secret"] = "different-synthetic-old-value"
        self.setup_phases()
        result, receipt = self.cli("auth_stage_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)

    def test_repinned_phase_cannot_change_existing_provider_version(self):
        name = "THEODORE_OIDC_CLIENT_SECRET"
        self.before["secrets"].append(
            {"name": name, "status": "Deployed", "version": "oidc-v1"}
        )
        self.before_refs = {name: self.manifest["protected_fields"][name]}
        self.setup_phases()
        phase = "auth_deploy_before"
        current = self.phases[phase]
        next(row for row in current["secrets"] if row["name"] == name)["version"] = (
            "changed"
        )
        packet = json.loads((self.root / "profiles.json").read_text())
        packet["phase_sha256"][phase] = digest(current)
        self.write("profiles.json", packet)
        self.record["profiles"]["sha256"] = digest(packet)
        self.write("record.json", self.record)
        result, receipt = self.cli(phase)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        self.assertFalse(self.op_log.exists())


if __name__ == "__main__":
    unittest.main()
