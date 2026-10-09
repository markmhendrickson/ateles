"""Owned profile effects only: no identity provider, credential or deploy calls."""

import copy
import unittest

from execution.lib.instance_auth_profile_guard import (
    AUTH_SECRET_NAMES,
    check_auth_phase,
    expected_auth_config,
)
from execution.lib.instance_profile_guard import Refused


class AuthPhaseProfiles(unittest.TestCase):
    def setUp(self):
        self.binding = {
            "origin": "https://queue.example.test",
            "environment": "dev",
            "machine_id": "owned-machine",
        }
        self.before = {
            "env": {"UNCHANGED": "preserved"},
            "guest": {"memory_mb": 512},
            "services": [{"internal_port": 8080}],
            "image": "owned-before",
            "metadata": {},
        }
        self.after = expected_auth_config(self.before, self.binding)
        self.secrets = [{"name": "THEODORE_PASSWORD", "status": "Deployed"}]

    def phase_secrets(self, phase):
        if phase == "auth_stage_before":
            return copy.deepcopy(self.secrets)
        return copy.deepcopy(self.secrets) + [
            {
                "name": name,
                "status": "Deployed" if phase == "auth_deploy_after" else "Staged",
            }
            for name in AUTH_SECRET_NAMES
        ]

    def check(self, phase, config=None, secrets=None, after=None, binding=None):
        return check_auth_phase(
            self.before,
            self.after if after is None else after,
            (
                (self.after if phase == "auth_deploy_after" else self.before)
                if config is None
                else config
            ),
            self.secrets,
            self.phase_secrets(phase) if secrets is None else secrets,
            self.binding if binding is None else binding,
            phase,
        )

    def test_four_real_phase_profiles_preserve_every_other_field(self):
        original = copy.deepcopy(self.before)
        for phase in (
            "auth_stage_before",
            "auth_stage_after",
            "auth_deploy_before",
            "auth_deploy_after",
        ):
            with self.subTest(phase=phase):
                result = self.check(phase)
                self.assertTrue(result["profile_matches"])
                self.assertTrue(result["secret_metadata_matches"])
                self.assertFalse(result["action_authorized"])
        self.assertEqual(self.before, original)
        self.assertEqual(self.after["guest"], self.before["guest"])

    def test_each_auth_value_and_unselected_profile_field_refuses(self):
        for key in (
            "THEODORE_AUTH_MODE",
            "THEODORE_OIDC_ORIGIN",
            "THEODORE_OIDC_ENVIRONMENT",
            "THEODORE_OIDC_MACHINE_ID",
            "UNSELECTED",
        ):
            config = copy.deepcopy(self.after)
            config["env"][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(Refused):
                self.check("auth_deploy_after", config=config)
        config = copy.deepcopy(self.after)
        config["guest"]["memory_mb"] = 8192
        with self.assertRaises(Refused):
            self.check("auth_deploy_after", config=config)

    def test_secret_additions_status_and_original_metadata_are_exact(self):
        for phase in (
            "auth_stage_before",
            "auth_stage_after",
            "auth_deploy_before",
            "auth_deploy_after",
        ):
            for mutation in (
                "missing",
                "extra",
                "old_status",
                "wrong_phase",
                "duplicate",
            ):
                rows = self.phase_secrets(phase)
                if mutation == "missing":
                    rows.pop()
                elif mutation == "extra":
                    rows.append({"name": "UNSELECTED", "status": "Staged"})
                elif mutation == "old_status":
                    rows[0]["status"] = "Changed"
                elif mutation == "wrong_phase":
                    rows.append(
                        {"name": "THEODORE_OIDC_CLIENT_SECRET", "status": "Wrong"}
                    )
                else:
                    rows.append(copy.deepcopy(rows[0]))
                with (
                    self.subTest(phase=phase, mutation=mutation),
                    self.assertRaises(Refused),
                ):
                    self.check(phase, secrets=rows)

    def test_unknown_phase_binding_and_half_applied_profile_refuse(self):
        with self.assertRaises(Refused):
            self.check("unknown")
        for origin in (
            "http://queue.example.test",
            "https://queue.example.test/path",
            "https://queue.example.test?x=1",
            "https://user@queue.example.test",
        ):
            with self.subTest(origin=origin), self.assertRaises(Refused):
                self.check(
                    "auth_stage_before", binding={**self.binding, "origin": origin}
                )
        with self.assertRaises(Refused):
            self.check("auth_stage_after", config=self.after)
        with self.assertRaises(Refused):
            self.check("auth_deploy_after", config=self.before)
        with self.assertRaises(Refused):
            self.check("auth_stage_before", after={**self.after, "extra": 1})


if __name__ == "__main__":
    unittest.main()
