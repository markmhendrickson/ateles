"""Owned natural CLI saved-command metadata effects; never deploys."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_prepare_instance_deployment as fixtures
from execution.lib.instance_profile_guard import (
    digest,
    installed_config_projection,
    stable_source,
)


class SavedStrategyCLI(unittest.TestCase):
    setUp = fixtures.LocalCLI.setUp
    rebind_profiles = fixtures.LocalCLI.rebind_profiles
    execute = fixtures.LocalCLI.execute

    def select(self):
        self.manifest["version"] = 3
        self.manifest.pop("idle_changes")
        self.manifest["input_changes"] = []
        raw_hash = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.manifest["input_toml"] = {
            "before_sha256": raw_hash,
            "after_sha256": raw_hash,
        }
        self.manifest["saved_command_metadata"] = {
            "path": "/deploy",
            "before_present": False,
            "before": None,
            "after_present": True,
            "after": {"strategy": "rolling"},
        }
        current = self.evidence["inventory"][0]["config"]
        current["services"][0].update({"autostop": False, "min_machines_running": 1})
        self.manifest["profile"]["source_static_config_sha256"] = digest(
            stable_source(current)
        )
        self.before = copy.deepcopy(self.evidence["saved"])
        self.after = {**copy.deepcopy(self.before), "deploy": {"strategy": "rolling"}}
        self.manifest["profile"]["saved_config_after_sha256"] = digest(self.after)
        self.provider_evidence(self.before)

    def provider_evidence(self, saved):
        self.evidence["saved"] = copy.deepcopy(saved)
        self.evidence["provider_saved_json"] = installed_config_projection(saved)
        raw = self.raw.read_text()
        if "deploy" in saved:
            raw += (
                "\n[deploy]\n"
                + "\n".join(k + "=" + json.dumps(v) for k, v in saved["deploy"].items())
                + "\n"
            )
        self.evidence["provider_saved_toml"] = (
            "# independent exporter timestamp\n" + raw
        )

    def after_phase(self):
        self.provider_evidence(self.after)
        current = self.evidence["inventory"][0]
        current["config"]["image"] = self.image
        current["image_ref"] = {"digest": self.image.split("@", 1)[1]}

    def clear_output(self):
        if self.output.exists():
            shutil.rmtree(self.output)
        (self.context / "gate-ran").unlink(missing_ok=True)

    def test_natural_before_after_preserves_seal_and_complete_provider_artifact(self):
        self.select()
        raw = self.raw.read_bytes()
        for phase in ("before", "after"):
            if phase == "after":
                self.after_phase()
            result = self.execute(phase=phase)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((self.output / "prepared.json").read_text())
            self.assertEqual(receipt["provider_saved"], self.evidence["saved"])
            self.assertEqual(receipt["normalized"], self.before)
            self.assertEqual(
                receipt["saved_command_metadata"],
                self.manifest["saved_command_metadata"],
            )
            self.assertEqual(receipt["config_edits"], [])
            self.assertEqual(receipt["packaging_check"]["tests_run"], 1)
            self.assertEqual(receipt["packaging_check"]["skips"], 0)
            self.assertEqual((self.output / "fly.toml").read_bytes(), raw)
            self.assertEqual(self.raw.read_bytes(), raw)
            self.assertEqual(
                receipt["argv"][receipt["argv"].index("--strategy") + 1], "rolling"
            )
            calls = [
                json.loads(x) for x in (self.root / "calls").read_text().splitlines()
            ]
            self.assertFalse(
                any(x[:1] == ["deploy"] and x != ["deploy", "--help"] for x in calls)
            )
            self.clear_output()

    def test_extra_descriptor_key_cannot_emit(self):
        self.select()
        self.manifest["saved_command_metadata"]["extra"] = "unadmitted"
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.context / "gate-ran").exists())

    def test_version_two_saved_after_remains_a_refusal(self):
        self.select()
        self.after_phase()
        self.manifest["version"] = 2
        self.manifest.pop("saved_command_metadata")
        self.evidence.pop("provider_saved_json")
        self.evidence.pop("provider_saved_toml")
        result = self.execute(phase="after")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_descriptor_variants_refuse_before_gate_or_emission(self):
        self.select()
        original = copy.deepcopy(self.manifest)
        variants = [None, [], {}, {"path": "/else"}]
        for key, value in (
            ("path", "/release"),
            ("before_present", 0),
            ("before_present", True),
            ("before", {}),
            ("after_present", 1),
            ("after_present", False),
            ("after", None),
            ("after", {}),
            ("after", "rolling"),
            ("after", {"strategy": "immediate"}),
            ("after", {"strategy": "rolling", "release_command": "unadmitted"}),
        ):
            variants.append({**original["saved_command_metadata"], key: value})
        for descriptor in variants:
            with self.subTest(descriptor=descriptor):
                bad = copy.deepcopy(original)
                bad["saved_command_metadata"] = descriptor
                self.assertEqual(self.execute(manifest=bad).returncode, 2)
                self.assertFalse(self.output.exists())
                self.assertFalse((self.context / "gate-ran").exists())

    def test_provider_json_binding_cannot_be_omitted(self):
        self.select()
        self.after_phase()
        self.evidence["provider_saved_json"]["env"]["SCOPE"] = "unadmitted"
        self.assertEqual(self.execute(phase="after").returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.context / "gate-ran").exists())

    def test_malformed_provider_export_refuses_safely(self):
        self.select()
        for value in (None, {}, [], True, 1, "[invalid"):
            with self.subTest(value=value):
                bad = copy.deepcopy(self.evidence)
                bad["provider_saved_toml"] = value
                result = self.execute(evidence=bad)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.output.exists())

    def test_sealed_input_integer_float_alias_is_not_equality(self):
        self.select()
        self.raw.write_text(self.raw.read_text().replace("cpus=1", "cpus=1.0"))
        raw_hash = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.manifest["input_toml"] = {
            "before_sha256": raw_hash,
            "after_sha256": raw_hash,
        }
        self.assertEqual(self.execute().returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.context / "gate-ran").exists())

    def test_local_parser_cannot_change_original_seal_then_emit(self):
        self.select()
        self.env["OWNED_MUTATE"] = str(self.raw)
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_local_parser_cannot_change_emitted_config_after_parsing(self):
        self.select()
        fly = self.bin / "fly"
        script = fly.read_text()
        script = script.replace(
            " print(json.dumps(c))",
            " Path(a[-1]).write_text('app=\\\"changed-after-parse\\\"\\n')\n print(json.dumps(c))",
        )
        fly.write_text(script)
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_independent_representations_and_sealed_comments_refuse_drift(self):
        self.select()
        self.after_phase()
        original = copy.deepcopy(self.evidence)
        for kind in (
            "toml",
            "json_boolean_type",
            "other_saved",
            "extra_deploy",
            "physical",
            "secret",
            "volume",
            "target",
        ):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(original)
                if kind == "toml":
                    bad["provider_saved_toml"] += "\n[extra]\nunadmitted=true\n"
                elif kind == "json_boolean_type":
                    bad["provider_saved_json"]["http_service"][
                        "auto_start_machines"
                    ] = 1
                elif kind == "other_saved":
                    bad["saved"]["env"]["SCOPE"] = "unadmitted"
                elif kind == "extra_deploy":
                    bad["saved"]["deploy"]["release_command"] = "unadmitted"
                elif kind == "physical":
                    bad["inventory"][0]["config"]["guest"]["memory_mb"] = 1024
                elif kind == "secret":
                    bad["secrets"].append({"name": "EXTRA", "status": "Deployed"})
                elif kind == "volume":
                    bad["volumes"].append({"id": "unadmitted"})
                else:
                    bad["canonical"]["snapshot"]["fly_app"] = "unadmitted"
                self.assertEqual(
                    self.execute(evidence=bad, phase="after").returncode, 2
                )
                self.assertFalse(self.output.exists())
        self.raw.write_bytes(self.raw.read_bytes() + b"# semantic-equal raw drift\n")
        self.assertEqual(self.execute(phase="after").returncode, 2)
        self.assertFalse(self.output.exists())

    def test_fixed_expected_transition_refuses_rebaseline_or_input_edit(self):
        self.select()
        for kind in (
            "before_present",
            "after_hash",
            "raw_after_hash",
            "input_edit",
            "release",
        ):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(self.manifest)
                evidence = copy.deepcopy(self.evidence)
                if kind == "before_present":
                    evidence["saved"]["deploy"] = {"strategy": "rolling"}
                    bad["profile"]["saved_config_before_sha256"] = digest(
                        evidence["saved"]
                    )
                elif kind == "after_hash":
                    bad["profile"]["saved_config_after_sha256"] = digest(
                        evidence["saved"]
                    )
                elif kind == "raw_after_hash":
                    bad["input_toml"]["after_sha256"] = "a" * 64
                elif kind == "input_edit":
                    bad["input_changes"] = [
                        {"path": ["env", "SCOPE"], "after": "unadmitted"}
                    ]
                else:
                    bad["command"]["release"]["present"] = True
                    bad["command"]["release"]["command"] = "unadmitted"
                self.assertEqual(
                    self.execute(manifest=bad, evidence=evidence).returncode, 2
                )
                self.assertFalse(self.output.exists())
                self.assertFalse((self.context / "gate-ran").exists())

    def test_optimized_cli_and_candidate_skip_guard_stay_binding(self):
        self.select()
        self.after_phase()
        result = self.execute(phase="after", optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.clear_output()
        self.gate.write_text(
            'import unittest\nclass Gate(unittest.TestCase):\n def test_bound(self): self.skipTest("owned omission")\n'
        )
        self.manifest["packaging_gate"]["sha256"] = hashlib.sha256(
            self.gate.read_bytes()
        ).hexdigest()
        import subprocess

        subprocess.run(
            ["git", "add", self.gate.name],
            cwd=self.context,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Owned Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "owned skipped gate",
            ],
            cwd=self.context,
            check=True,
            capture_output=True,
        )
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.context, text=True
        ).strip()
        self.assertEqual(self.execute(phase="after").returncode, 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
