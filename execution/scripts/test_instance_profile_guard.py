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
        "command": {
            "skip_release_command": False,
            "build_arguments": [],
            "release": {
                "present": False,
                "command": None,
                "disposition": "absent",
                "gate": None,
            },
        },
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
        evidence["saved"]["deploy"] = {"release_command": "node dist/owned_schema.js"}
        manifest["command"]["release"] = {
            "present": True,
            "command": "node dist/owned_schema.js",
            "disposition": "existing_skip",
            "gate": None,
        }
        manifest["profile"]["saved_config_before_sha256"] = digest(evidence["saved"])
        normalized = copy.deepcopy(evidence["saved"])
        normalized["http_service"].update(
            auto_stop_machines="stop", min_machines_running=0
        )
        manifest["profile"]["saved_config_after_sha256"] = digest(normalized)
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


def reconciliation_fixture():
    import tomllib

    raw = '\r\n'.join([
        '# owned input; retain spacing and comments',
        'app = "owned-fixture"',
        '[env]',
        'SCOPE = "development" # unchanged',
        '[http_service]',
        'auto_stop_machines = "off" # idle',
        'min_machines_running = 1',
        '[[http_service.checks]]',
        'path = "/ready" # check',
        'interval = "30s"',
        'timeout = "30s"',
        'grace_period = "1m0s"',
        '[[vm]]',
        'memory = "2gb" # budget',
        'cpus = 2',
        'cpu_kind = "shared"',
        '[[restart]]',
        'policy = "always" # keep comment',
        '[deploy]',
        'release_command = "owned required command"',
        '',
    ])
    saved = tomllib.loads(raw)
    selected = [
        (['http_service', 'auto_stop_machines'], 'stop'),
        (['http_service', 'min_machines_running'], 0),
        (['vm', 0, 'memory'], '1gb'),
        (['vm', 0, 'cpus'], 1),
        (['restart', 0, 'policy'], 'on-failure'),
        (['restart', 0, 'retries'], 10),
        (['http_service', 'checks', 0, 'path'], '/health'),
        (['http_service', 'checks', 0, 'interval'], '15s'),
        (['http_service', 'checks', 0, 'timeout'], '10s'),
        (['http_service', 'checks', 0, 'grace_period'], '30s'),
    ]
    after = copy.deepcopy(saved)
    changes = []
    for path, value in selected:
        before_parent, after_parent = saved, after
        for key in path[:-1]:
            before_parent, after_parent = before_parent[key], after_parent[key]
        present = path[-1] in before_parent
        changes.append({
            'path': path, 'before_present': present,
            'before': before_parent.get(path[-1]),
            'after_present': True, 'after': value,
        })
        after_parent[path[-1]] = value
    manifest = {'version': 2, 'input_changes': changes,
                'profile': {'saved_config_after_sha256': digest(after)}}
    return raw, saved, after, manifest


class Reconciliation(unittest.TestCase):
    def normalized(self, saved, manifest):
        from execution.lib.instance_profile_guard import normalized_input
        try:
            return normalized_input(saved, manifest)
        except (Refused, KeyError, TypeError) as exc:
            self.fail(f'admitted closed reconciliation refused: {type(exc).__name__}')

    def test_ten_selected_changes_and_input_immutable(self):
        _, saved, after, manifest = reconciliation_fixture()
        before = copy.deepcopy(saved)
        self.assertEqual(self.normalized(saved, manifest), after)
        self.assertEqual(saved, before)

    def test_lossless_crlf_comments_and_exact_absent_insertion(self):
        from execution.lib.instance_profile_guard import render_normalized_toml
        raw, saved, after, manifest = reconciliation_fixture()
        normalized = self.normalized(saved, manifest)
        rendered = render_normalized_toml(raw, saved, normalized, manifest['input_changes'])
        expected = raw.replace('"off"', '"stop"').replace('running = 1', 'running = 0')
        expected = expected.replace('"2gb"', '"1gb"').replace('cpus = 2', 'cpus = 1')
        expected = expected.replace('"always"', '"on-failure"').replace('[deploy]', 'retries = 10\r\n[deploy]')
        expected = expected.replace('"/ready"', '"/health"').replace('"30s"', '"15s"', 1)
        expected = expected.replace('timeout = "30s"', 'timeout = "10s"').replace('"1m0s"', '"30s"')
        self.assertEqual(rendered, expected)
        self.assertEqual(__import__('tomllib').loads(rendered), after)

    def test_each_omission_extra_path_presence_and_shape_refuses(self):
        from execution.lib.instance_profile_guard import normalized_input
        _, saved, _, manifest = reconciliation_fixture()
        self.normalized(saved, manifest)  # known positive before refusal controls
        for index in range(10):
            with self.subTest(omitted=index):
                bad = copy.deepcopy(manifest)
                bad['input_changes'].pop(index)
                with self.assertRaises(Refused):
                    normalized_input(saved, bad)
        for path in (['env', 'SCOPE'], ['vm', 1, 'memory'], ['restart', 0, 'max_retries']):
            bad = copy.deepcopy(manifest)
            bad['input_changes'][0]['path'] = path
            with self.assertRaises(Refused):
                normalized_input(saved, bad)
        for key, value in (('before_present', True), ('after_present', False), ('before', 0)):
            bad = copy.deepcopy(manifest)
            bad['input_changes'][5][key] = value
            with self.assertRaises(Refused):
                normalized_input(saved, bad)
        for section in ('vm', 'restart'):
            bad = copy.deepcopy(saved)
            bad[section].append(copy.deepcopy(bad[section][0]))
            with self.assertRaises(Refused):
                normalized_input(bad, manifest)

    def test_web_zero_changes_byte_identity(self):
        from execution.lib.instance_profile_guard import render_normalized_toml
        raw, saved, _, _ = reconciliation_fixture()
        manifest = {'version': 2, 'input_changes': [], 'profile': {'saved_config_after_sha256': digest(saved)}}
        self.assertEqual(self.normalized(saved, manifest), saved)
        self.assertEqual(render_normalized_toml(raw, saved, saved, []), raw)


if __name__ == "__main__":
    unittest.main()
