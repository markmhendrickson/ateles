"""Natural CLI effects for the explicitly selected unchanged rolling contract."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_prepare_saved_strategy as saved
from execution.lib.instance_profile_guard import digest


class UnchangedSavedStrategyCLI(unittest.TestCase):
    setUp = saved.SavedStrategyCLI.setUp
    rebind_profiles = saved.SavedStrategyCLI.rebind_profiles
    execute = saved.SavedStrategyCLI.execute
    clear_output = saved.SavedStrategyCLI.clear_output
    after_phase = saved.SavedStrategyCLI.after_phase

    def provider_evidence(self, value):
        self.evidence["saved"] = copy.deepcopy(value)
        self.evidence["provider_saved_json"] = saved.installed_config_projection(value)
        raw = self.raw.read_text().split("\n[deploy]\n", 1)[0]
        if "deploy" in value:
            raw += (
                "\n[deploy]\n"
                + "\n".join(k + "=" + json.dumps(v) for k, v in value["deploy"].items())
                + "\n"
            )
        self.evidence["provider_saved_toml"] = "# fresh exporter timestamp\n" + raw

    def select(self):
        saved.SavedStrategyCLI.select(self)
        self.manifest["version"] = 4
        self.raw.write_text(self.raw.read_text() + '\n[deploy]\nstrategy="rolling"\n')
        raw_hash = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.manifest["input_toml"] = {
            "before_sha256": raw_hash,
            "after_sha256": raw_hash,
        }
        self.manifest["saved_command_metadata"].update(
            {"before_present": True, "before": {"strategy": "rolling"}}
        )
        self.before = copy.deepcopy(self.after)
        self.manifest["profile"]["saved_config_before_sha256"] = digest(self.before)
        self.manifest["profile"]["saved_config_after_sha256"] = digest(self.before)
        self.provider_evidence(self.before)

    def test_before_after_preserves_complete_present_profile_and_raw_bytes(self):
        self.select()
        original = self.raw.read_bytes()
        for phase in ("before", "after"):
            for optimized in (False, True):
                with self.subTest(phase=phase, optimized=optimized):
                    if phase == "after":
                        self.after_phase()
                    result = self.execute(phase=phase, optimized=optimized)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    receipt = json.loads((self.output / "prepared.json").read_text())
                    self.assertEqual(receipt["provider_saved"], self.before)
                    self.assertEqual(receipt["normalized"], self.before)
                    self.assertEqual(
                        receipt["saved_command_metadata"],
                        self.manifest["saved_command_metadata"],
                    )
                    self.assertEqual(
                        receipt["machine_projection"],
                        receipt["provider_machine_projection"],
                    )
                    self.assertEqual(receipt["saved_toml_input_phase"], phase)
                    self.assertEqual(
                        receipt["saved_config_actual_sha256"], digest(self.before)
                    )
                    self.assertEqual(
                        receipt["saved_config_before_sha256"],
                        receipt["saved_config_after_sha256"],
                    )
                    self.assertEqual(receipt["config_edits"], [])
                    self.assertEqual(receipt["packaging_check"]["tests_run"], 1)
                    self.assertEqual(receipt["packaging_check"]["skips"], 0)
                    self.assertEqual(receipt["release_check"]["disposition"], "absent")
                    self.assertEqual((self.output / "fly.toml").read_bytes(), original)
                    self.assertEqual(self.raw.read_bytes(), original)
                    self.assertEqual(
                        receipt["saved_toml_before_sha256"],
                        receipt["saved_toml_after_sha256"],
                    )
                    self.assertEqual(
                        receipt["argv"][receipt["argv"].index("--strategy") + 1],
                        "rolling",
                    )
                    calls = [
                        json.loads(x)
                        for x in (self.root / "calls").read_text().splitlines()
                    ]
                    self.assertFalse(
                        any(
                            x[:1] == ["deploy"] and x != ["deploy", "--help"]
                            for x in calls
                        )
                    )
                    self.clear_output()

    def refused(self, *, manifest=None, evidence=None, phase="before", pre_gate=True):
        result = self.execute(manifest=manifest, evidence=evidence, phase=phase)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())
        if pre_gate:
            self.assertFalse((self.context / "gate-ran").exists())

    def test_descriptor_and_complete_state_are_both_bound(self):
        self.select()
        descriptor = copy.deepcopy(self.manifest["saved_command_metadata"])
        variants = [None, {}, [], {**descriptor, "extra": True}]
        for key, value in (
            ("path", "/else"),
            ("before_present", 1),
            ("before_present", False),
            ("before", None),
            ("before", {}),
            ("before", {"strategy": "immediate"}),
            ("after_present", 1),
            ("after_present", False),
            ("after", {"strategy": "rolling", "release_command": "unselected"}),
            ("after", {"strategy": "immediate"}),
        ):
            variants.append({**descriptor, key: value})
        for variant in variants:
            with self.subTest(descriptor=variant):
                bad = copy.deepcopy(self.manifest)
                bad["saved_command_metadata"] = variant
                self.refused(manifest=bad)
        for key in ("saved_config_before_sha256", "saved_config_after_sha256"):
            with self.subTest(digest=key):
                bad = copy.deepcopy(self.manifest)
                bad["profile"][key] = "0" * 64
                self.refused(manifest=bad)
        bad = copy.deepcopy(self.manifest)
        bad["input_changes"] = [{"path": ["http_service", "min_machines_running"]}]
        self.refused(manifest=bad)
        bad = copy.deepcopy(self.manifest)
        bad["input_toml"]["after_sha256"] = "0" * 64
        self.refused(manifest=bad)

    def test_provider_json_is_independently_bound(self):
        self.select()
        self.after_phase()
        self.evidence["provider_saved_json"]["env"]["SCOPE"] = "unselected"
        self.refused(phase="after")

    def test_unsupported_present_shapes_never_select_another_version(self):
        self.select()
        for value in (
            None,
            {},
            [],
            "rolling",
            {"strategy": "immediate"},
            {"strategy": "rolling", "release_command": "unselected"},
        ):
            with self.subTest(deploy=value):
                bad = copy.deepcopy(self.evidence)
                bad["saved"]["deploy"] = value
                self.refused(evidence=bad)
        bad = copy.deepcopy(self.evidence)
        del bad["saved"]["deploy"]
        self.refused(evidence=bad)
        for version in (True, 4.0, 5, 3):
            with self.subTest(version=version):
                bad = copy.deepcopy(self.manifest)
                bad["version"] = version
                self.refused(manifest=bad)
        for change in ("missing", "extra"):
            bad = copy.deepcopy(self.manifest)
            if change == "missing":
                del bad["saved_command_metadata"]
            else:
                bad["unselected"] = True
            self.refused(manifest=bad)

    def test_all_actual_profile_layers_and_authority_refuse_drift(self):
        self.select()
        self.after_phase()
        for kind in (
            "toml",
            "json_type",
            "saved",
            "static",
            "secret",
            "volume",
            "canonical",
            "tool",
        ):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(self.evidence)
                if kind == "toml":
                    bad["provider_saved_toml"] += "\n[extra]\nvalue=true\n"
                elif kind == "json_type":
                    bad["provider_saved_json"]["http_service"][
                        "auto_start_machines"
                    ] = 1
                elif kind == "saved":
                    bad["saved"]["env"]["SCOPE"] = "unselected"
                elif kind == "static":
                    bad["inventory"][0]["config"]["guest"]["memory_mb"] = 1024
                elif kind == "secret":
                    bad["secrets"].append({"name": "EXTRA", "status": "Deployed"})
                elif kind == "volume":
                    bad["volumes"].append({"id": "unselected"})
                elif kind == "canonical":
                    bad["canonical"]["snapshot"]["fly_app"] = "unselected"
                else:
                    bad["tool_version"] = "unselected"
                self.refused(evidence=bad, phase="after")

    def test_raw_and_candidate_gate_bindings_refuse_before_emission(self):
        self.select()
        original = self.raw.read_bytes()
        self.raw.write_bytes(original + b"# comment drift\n")
        self.refused()
        self.raw.write_bytes(original)
        bad = copy.deepcopy(self.manifest)
        bad["packaging_gate"]["sha256"] = "0" * 64
        self.refused(manifest=bad)
        bad = copy.deepcopy(self.manifest)
        bad["command"]["release"]["present"] = True
        self.refused(manifest=bad)

    test_local_parser_cannot_change_original_seal_then_emit = (
        saved.SavedStrategyCLI.test_local_parser_cannot_change_original_seal_then_emit
    )
    test_local_parser_cannot_change_emitted_config_after_parsing = saved.SavedStrategyCLI.test_local_parser_cannot_change_emitted_config_after_parsing


if __name__ == "__main__":
    unittest.main()
