"""Owned structural fixtures for the extracted preparation-only core."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from execution.lib.instance_profile_guard import (
    Refused,
    check_profiles,
    digest,
    stable_source,
)


def fixture():
    config = {
        "image": "old",
        "guest": {"memory_mb": 512},
        "env": {"SCOPE": "development"},
        "metadata": {
            "fly_release_id": "old",
            "fly_release_version": "1",
            "fly_process_group": "app",
        },
        "services": [{"autostop": True, "autostart": True, "min_machines_running": 0}],
    }
    inventory = [
        {
            "id": "source",
            "state": "started",
            "region": "fixture-region",
            "config": config,
        }
    ]
    saved = {
        "app": "owned-fixture",
        "http_service": {"auto_stop_machines": True, "min_machines_running": 0},
    }
    baseline = {
        "source_id": "source",
        "source_state": "started",
        "region": "fixture-region",
        "source_static_config_sha256": digest(stable_source(config)),
        "retained_machines": {},
        "saved_config_before_sha256": digest(saved),
        "saved_config_after_sha256": digest(saved),
        "image_repository": "registry.test/owned",
    }
    image = baseline["image_repository"] + "@sha256:" + "a" * 64
    return inventory, saved, baseline, image


class CompleteProfiles(unittest.TestCase):
    def test_positive_before_after_release_only(self):
        rows, saved, baseline, image = fixture()
        self.assertTrue(check_profiles(rows, saved, baseline, image))
        rows[0]["config"]["image"] = image
        rows[0]["config"]["metadata"]["fly_release_id"] = "new"
        rows[0]["config"]["metadata"]["fly_release_version"] = "2"
        rows[0]["image_ref"] = {"digest": image.split("@", 1)[1]}
        self.assertTrue(check_profiles(rows, saved, baseline, image, "after"))

    def test_each_static_layer_refuses_one_changed_field(self):
        for layer in ("source", "saved"):
            for field in ("environment", "budget", "idle", "extra"):
                rows, saved, baseline, image = fixture()
                target = rows[0]["config"] if layer == "source" else saved
                target[field] = "changed"
                with self.subTest(layer=layer, field=field):
                    with self.assertRaises(Refused):
                        check_profiles(rows, saved, baseline, image)

    def test_actual_idle_key_only_drift_refuses(self):
        rows, saved, baseline, image = fixture()
        rows[0]["config"]["services"][0]["min_machines_running"] = 1
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, image)
        rows, saved, baseline, image = fixture()
        saved["http_service"]["auto_stop_machines"] = False
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, image)

    def test_retained_clone_complete_and_stopped(self):
        rows, saved, baseline, image = fixture()
        clone = {
            "id": "retained",
            "state": "stopped",
            "region": "fixture-region",
            "config": {"services": [], "restart": "no"},
        }
        rows.append(clone)
        baseline["retained_machines"]["retained"] = {
            "state": "stopped",
            "config_sha256": digest(clone["config"]),
        }
        self.assertTrue(check_profiles(rows, saved, baseline, image))
        for key in ("state", "config"):
            bad = copy.deepcopy(rows)
            if key == "state":
                bad[1][key] = "started"
            else:
                bad[1][key]["image"] = "changed"
            with self.assertRaises(Refused):
                check_profiles(bad, saved, baseline, image)

    def test_inventory_phase_image_and_baseline_refusal(self):
        rows, saved, baseline, image = fixture()
        for bad in ([], rows + copy.deepcopy(rows), rows + [{"id": "extra"}]):
            with self.assertRaises(Refused):
                check_profiles(bad, saved, baseline, image)
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, image, "unknown")
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, "mutable:latest")
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, image, "after")
        baseline["extra"] = True
        with self.assertRaises(Refused):
            check_profiles(rows, saved, baseline, image)


def preparation_fixture():
    rows, saved, profile, image = fixture()
    saved["http_service"]["auto_stop_machines"] = "off"
    saved["http_service"]["min_machines_running"] = 1
    import copy

    normalized = copy.deepcopy(saved)
    normalized["http_service"]["auto_stop_machines"] = "stop"
    normalized["http_service"]["min_machines_running"] = 0
    profile["saved_config_before_sha256"] = digest(saved)
    profile["saved_config_after_sha256"] = digest(normalized)
    canonical = {
        "entity_id": "owned-binding",
        "snapshot": {
            "fly_app": saved["app"],
            "environment": "development",
            "region": profile["region"],
            "secret_names": ["BOUND_SECRET"],
        },
    }
    secrets = [{"name": "BOUND_SECRET", "status": "Deployed"}]
    volumes = [{"id": "owned-volume", "encrypted": True}]
    manifest = {
        "version": 1,
        "deployment_configuration": canonical["entity_id"],
        "canonical_sha256": digest(canonical["snapshot"]),
        "app": saved["app"],
        "environment": "development",
        "profile": profile,
        "idle_changes": [
            {
                "path": ["http_service", "auto_stop_machines"],
                "before": "off",
                "after": "stop",
            },
            {"path": ["http_service", "min_machines_running"], "before": 1, "after": 0},
        ],
        "secrets_sha256": digest(secrets),
        "volumes_sha256": digest(volumes),
        "tool_version": "owned-tool",
        "command": {"skip_release_command": False, "build_arguments": []},
        "packaging_gate": {
            "path": "owned_gate.py",
            "sha256": "b" * 64,
            "inputs": {"Dockerfile": "d" * 64},
        },
    }
    evidence = {
        "canonical": canonical,
        "inventory": rows,
        "saved": saved,
        "secrets": secrets,
        "volumes": volumes,
        "tool_version": "owned-tool",
    }
    return manifest, evidence, image


class Preparation(unittest.TestCase):
    def test_exact_two_keys_only_before_and_after(self):
        from execution.lib.instance_profile_guard import prepare, _deployment_argv

        manifest, evidence, image = preparation_fixture()
        before = copy.deepcopy(evidence)
        result = prepare(evidence, manifest, digest(manifest), image, "c" * 40)
        self.assertEqual(evidence, before)
        self.assertNotIn("--skip-release-command", _deployment_argv(manifest, image))
        self.assertIn("--only-machines", _deployment_argv(manifest, image))
        self.assertEqual(
            result["normalized"]["http_service"]["min_machines_running"], 0
        )
        evidence["saved"] = result["normalized"]
        evidence["inventory"][0]["image_ref"] = {"digest": image.split("@", 1)[1]}
        self.assertTrue(
            prepare(evidence, manifest, digest(manifest), image, "c" * 40, "after")
        )

    def test_metadata_target_normalization_drift(self):
        from execution.lib.instance_profile_guard import prepare

        for mutation in (
            "canonical",
            "secrets",
            "volumes",
            "tool",
            "manifest",
            "idle",
            "wrongapp",
            "shape",
        ):
            manifest, evidence, image = preparation_fixture()
            sha = digest(manifest)
            if mutation == "canonical":
                evidence["canonical"]["snapshot"]["environment"] = "production"
            if mutation == "secrets":
                evidence["secrets"].append({"name": "EXTRA", "status": "Staged"})
            if mutation == "volumes":
                evidence["volumes"][0]["encrypted"] = False
            if mutation == "tool":
                evidence["tool_version"] = "changed"
            if mutation == "manifest":
                manifest["command"]["skip_release_command"] = True
            if mutation == "idle":
                evidence["inventory"][0]["config"]["services"][0]["autostop"] = False
            if mutation == "wrongapp":
                evidence["saved"]["app"] = "other"
            if mutation == "shape":
                evidence["extra"] = True
            with self.subTest(mutation=mutation), self.assertRaises(Refused):
                prepare(evidence, manifest, sha, image, "c" * 40)

    def test_normalization_has_closed_paths_and_no_dynamic_learning(self):
        from execution.lib.instance_profile_guard import normalized_input

        manifest, evidence, _ = preparation_fixture()
        for changes in (
            [manifest["idle_changes"][0]],
            [
                *manifest["idle_changes"],
                {
                    "path": ["env", "SCOPE"],
                    "before": "development",
                    "after": "production",
                },
            ],
        ):
            bad = copy.deepcopy(manifest)
            bad["idle_changes"] = changes
            with self.assertRaises(Refused):
                normalized_input(evidence["saved"], bad)
        evidence["saved"]["http_service"]["min_machines_running"] = 2
        with self.assertRaises(Refused):
            normalized_input(evidence["saved"], manifest)

    def test_lossless_toml_two_scalar_roundtrip(self):
        from execution.lib.instance_profile_guard import (
            normalized_input,
            render_normalized_toml,
        )

        manifest, evidence, _ = preparation_fixture()
        saved = evidence["saved"]
        raw = 'app = "owned-fixture"\n\n[http_service]\nauto_stop_machines = "off"\nmin_machines_running = 1\n'
        normalized = normalized_input(saved, manifest)
        rendered = render_normalized_toml(
            raw, saved, normalized, manifest["idle_changes"]
        )
        self.assertEqual(rendered, raw.replace('"off"', '"stop"').replace("= 1", "= 0"))
        with self.assertRaises(Refused):
            render_normalized_toml(
                raw.replace("owned-fixture", "other"),
                saved,
                normalized,
                manifest["idle_changes"],
            )
        with self.assertRaises(Refused):
            render_normalized_toml(raw, saved, normalized, [])

    def test_production_skip_release_and_clone_selector_preserved(self):
        from execution.lib.instance_profile_guard import prepare, _deployment_argv

        manifest, evidence, image = preparation_fixture()
        clone = {
            "id": "retained",
            "state": "stopped",
            "region": manifest["profile"]["region"],
            "config": {"services": [], "restart": "no"},
        }
        evidence["inventory"].append(clone)
        manifest["profile"]["retained_machines"]["retained"] = {
            "state": "stopped",
            "config_sha256": digest(clone["config"]),
        }
        manifest["command"]["skip_release_command"] = True
        prepare(evidence, manifest, digest(manifest), image, "c" * 40)
        self.assertIn("--skip-release-command", _deployment_argv(manifest, image))
        self.assertEqual(
            _deployment_argv(manifest, image)[
                _deployment_argv(manifest, image).index("--exclude-machines") + 1
            ],
            "retained",
        )


class CanonicalIdleState(unittest.TestCase):
    def test_actual_auto_stop_state_is_dynamic_without_static_relaxation(self):
        from execution.lib.instance_profile_guard import prepare

        m, e, image = preparation_fixture()
        e["canonical"]["snapshot"]["always_on"] = False
        m["canonical_sha256"] = digest(e["canonical"]["snapshot"])
        m["profile"]["source_state"] = {
            "before": ["started", "stopped"],
            "after": ["started", "stopped"],
        }
        for state in ("started", "stopped"):
            e["inventory"][0]["state"] = state
            self.assertTrue(prepare(e, m, digest(m), image, "c" * 40))
        e["inventory"][0]["config"]["services"][0]["min_machines_running"] = 1
        with self.assertRaises(Refused):
            prepare(e, m, digest(m), image, "c" * 40)

    def test_dynamic_state_requires_canonical_policy_and_refuses_unknown(self):
        from execution.lib.instance_profile_guard import prepare

        m, e, image = preparation_fixture()
        m["profile"]["source_state"] = {
            "before": ["started", "stopped"],
            "after": ["started", "stopped"],
        }
        with self.assertRaises(Refused):
            prepare(e, m, digest(m), image, "c" * 40)
        e["canonical"]["snapshot"]["always_on"] = False
        m["canonical_sha256"] = digest(e["canonical"]["snapshot"])
        e["inventory"][0]["state"] = "suspended"
        with self.assertRaises(Refused):
            prepare(e, m, digest(m), image, "c" * 40)


if __name__ == "__main__":
    unittest.main()
