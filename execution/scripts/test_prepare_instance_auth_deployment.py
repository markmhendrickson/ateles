"""Natural auth argv composition with inherited executed local candidate gates."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from execution.lib.instance_auth_profile_guard import expected_auth_config
from execution.lib.instance_auth_profile_preflight import render_auth_toml
from execution.lib.instance_profile_guard import Refused, digest
from execution.scripts import test_instance_auth_preparation_cli as auth_tests
from execution.scripts import test_prepare_instance_deployment as base_tests


class AuthDeploymentCLI(unittest.TestCase):
    def setUp(self):
        self.auth = auth_tests.NaturalAuthPreflight()
        self.auth.setUp()
        self.addCleanup(self.auth.doCleanups)
        self.base = base_tests.LocalCLI()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        auth, base = self.auth, self.base
        self.root = auth.root
        base.evidence["canonical"]["entity_id"] = auth.canonical["entity_id"]
        base.evidence["canonical"]["entity_type"] = "deployment_configuration"
        base.evidence["canonical"]["snapshot"]["public_domain"] = "queue.example.test"
        auth.canonical = copy.deepcopy(base.evidence["canonical"])
        base.manifest["deployment_configuration"] = auth.canonical["entity_id"]
        base.manifest["canonical_sha256"] = digest(auth.canonical["snapshot"])
        auth.manifest["references"].update(
            canonical_ref=auth.canonical["entity_id"],
            canonical_sha256=digest(auth.canonical["snapshot"]),
        )
        auth.manifest["binding"]["machine_id"] = "source"
        auth.manifest["candidate"].update(commit=base.commit, image=base.image)
        base.evidence["saved"]["http_service"].update(
            auto_stop_machines="stop", min_machines_running=0
        )
        base.raw.write_text(
            base.raw.read_text()
            .replace('auto_stop_machines="off"', 'auto_stop_machines="stop"')
            .replace("min_machines_running=1", "min_machines_running=0")
        )
        base.raw.chmod(0o600)
        base.manifest["idle_changes"] = []
        base.rebind_profiles()
        auth.before = {
            k: copy.deepcopy(base.evidence[k])
            for k in ("inventory", "saved", "secrets", "volumes")
        }
        auth.after = copy.deepcopy(auth.before)
        auth.after["saved"] = expected_auth_config(
            auth.before["saved"], auth.manifest["binding"]
        )
        auth.after["inventory"][0]["config"] = expected_auth_config(
            auth.before["inventory"][0]["config"], auth.manifest["binding"]
        )
        auth.after["inventory"][0]["config"]["image"] = base.image
        auth.after["inventory"][0]["image_ref"] = {
            "digest": base.image.split("@", 1)[1]
        }
        auth.setup_phases()
        raw_after, _ = render_auth_toml(
            base.raw.read_text(), auth.before["saved"], auth.manifest["binding"]
        )
        self.envelope = {
            "manifest": {
                "path": str(auth.write("deployment.json", base.manifest)),
                "sha256": digest(base.manifest),
            },
            "evidence": {
                "path": str(auth.write("deployment-evidence.json", base.evidence)),
                "sha256": digest(base.evidence),
            },
            "raw_config": {
                "path": str(base.raw),
                "before_sha256": hashlib.sha256(base.raw.read_bytes()).hexdigest(),
                "after_sha256": hashlib.sha256(raw_after.encode()).hexdigest(),
            },
        }
        self.pin_envelope()
        fly = base.bin / "fly"
        fly.write_text(
            fly.read_text().replace(
                "elif a[:3]==['config','show','--local']:",
                "elif a==['secrets','import','--help']: print('--stage --app')\nelif a[:3]==['config','show','--local']:",
            )
        )
        self.env = {
            **base.env,
            "PATH": str(auth.root) + os.pathsep + base.env["PATH"],
            "NEOTOMA_BASE_URL": f"http://localhost:{auth.server.server_port}",
            "NEOTOMA_BEARER_TOKEN": "synthetic-owned-bearer",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def pin_envelope(self):
        auth = self.auth
        packet = json.loads((self.root / "profiles.json").read_text())
        packet["deployment"] = self.envelope
        auth.write("profiles.json", packet)
        auth.record["profiles"]["sha256"] = digest(packet)
        auth.write("record.json", auth.record)

    def execute(self, phase, *, optimized=False):
        auth = self.auth
        auth.write("current.json", auth.phases[phase])
        output = self.root / "prepared"
        if output.exists():
            shutil.rmtree(output)
        result = subprocess.run(
            [
                sys.executable,
                *(["-O"] if optimized else []),
                str(Path(__file__).with_name("prepare_instance_auth_deployment.py")),
                "--manifest",
                str(self.root / "manifest.json"),
                "--evidence",
                str(self.root / "current.json"),
                "--action-record",
                str(self.root / "record.json"),
                "--action-record-sha256",
                digest(auth.record),
                "--context",
                str(self.base.context),
                "--phase",
                phase,
                "--output",
                str(output),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotIn("synthetic-private-canary", result.stdout + result.stderr)
        return result, json.loads(
            (output / "prepared.json").read_text()
        ) if output.exists() else None

    def test_actual_four_phase_argv_gates_parser_and_no_provider_action(self):
        for optimized in (False, True):
            for phase in (
                "auth_stage_before",
                "auth_stage_after",
                "auth_deploy_before",
                "auth_deploy_after",
            ):
                with self.subTest(phase=phase, optimized=optimized):
                    result, receipt = self.execute(phase, optimized=optimized)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(receipt["action_authorized"])
                    if phase == "auth_stage_before":
                        self.assertEqual(
                            receipt["argv"],
                            [
                                "fly",
                                "secrets",
                                "import",
                                "--app",
                                "owned-fixture",
                                "--stage",
                            ],
                        )
                    elif phase == "auth_deploy_before":
                        self.assertEqual(receipt["argv"][:2], ["fly", "deploy"])
                        self.assertEqual(
                            receipt["argv"][
                                receipt["argv"].index("--only-machines") + 1
                            ],
                            "source",
                        )
                        self.assertIn("--update-only", receipt["argv"])
                        self.assertIn("--no-public-ips", receipt["argv"])
                        self.assertEqual(
                            receipt["base_preparation"]["packaging_check"]["tests_run"],
                            1,
                        )
                        self.assertTrue(
                            receipt["machine_projection"]["complete_static_match"]
                        )
                        self.assertEqual(
                            receipt["raw_after_sha256"],
                            hashlib.sha256(
                                (self.root / "prepared" / "fly.toml").read_bytes()
                            ).hexdigest(),
                        )
                    else:
                        self.assertIsNone(receipt["argv"])
        calls = [
            json.loads(line)
            for line in (self.base.root / "calls").read_text().splitlines()
        ]
        self.assertTrue(calls)
        self.assertTrue(
            all(
                a
                in (["version"], ["deploy", "--help"], ["secrets", "import", "--help"])
                or a[:3] == ["config", "show", "--local"]
                for a in calls
            )
        )
        self.assertTrue((self.base.context / "gate-ran").exists())

    def test_wrong_private_deployment_and_raw_pins_never_emit(self):
        for section, key in (
            ("manifest", "sha256"),
            ("evidence", "sha256"),
            ("raw_config", "after_sha256"),
        ):
            original = copy.deepcopy(self.envelope)
            self.envelope[section][key] = "0" * 64
            self.pin_envelope()
            with self.subTest(section=section):
                result, receipt = self.execute("auth_deploy_before")
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(receipt)
            self.envelope = original
            self.pin_envelope()

    def test_wrong_protected_target_refuses_command_before_candidate_gates(self):
        values = dict(self.auth.synthetic_values)
        values["THEODORE_OIDC_TENANT_ID"] = "wrong-synthetic-tenant"
        self.auth.write("values.json", values)
        result, receipt = self.execute("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        self.assertFalse((self.base.context / "gate-ran").exists())

    def test_missing_pinned_deployment_envelope_never_uses_boolean_gate(self):
        packet = json.loads((self.root / "profiles.json").read_text())
        packet["deployment"] = {"approved": True, "packaging_passed": True}
        self.auth.write("profiles.json", packet)
        self.auth.record["profiles"]["sha256"] = digest(packet)
        self.auth.write("record.json", self.auth.record)
        result, receipt = self.execute("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)

    def test_dirty_candidate_and_parser_mutation_refuse_output(self):
        file = self.base.context / "Dockerfile"
        original = file.read_text()
        file.write_text("changed input")
        result, receipt = self.execute("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)
        file.write_text(original)
        self.env["OWNED_MUTATE"] = str(file)
        result, receipt = self.execute("auth_deploy_before")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(receipt)

    def test_raw_public_env_edits_preserve_every_unselected_byte_and_refuse_extra(self):
        raw = '# retained café comment\n[env]\nSCOPE="development" # keep\n[other]\nvalue=9\n'
        before = {"env": {"SCOPE": "development"}, "other": {"value": 9}}
        after, edits = render_auth_toml(raw, before, self.auth.manifest["binding"])
        self.assertEqual(len(edits), 4)
        self.assertIn("# retained café comment\n", after)
        self.assertIn('SCOPE="development" # keep\n', after)
        self.assertTrue(after.endswith("[other]\nvalue=9\n"))
        import tomllib

        self.assertEqual(
            tomllib.loads(after),
            expected_auth_config(before, self.auth.manifest["binding"]),
        )
        with self.assertRaises(Refused):
            render_auth_toml(
                "[other]\nvalue=9\n", before, self.auth.manifest["binding"]
            )


if __name__ == "__main__":
    unittest.main()
