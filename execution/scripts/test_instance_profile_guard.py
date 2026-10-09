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


if __name__ == "__main__":
    unittest.main()
