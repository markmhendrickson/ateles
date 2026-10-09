"""Owned profile effects only: no identity provider, credential or deploy calls."""

import copy
import unittest

from execution.lib.instance_auth_profile_guard import (
    AUTH_SECRET_NAMES,
    check_action_manifest,
    check_auth_phase,
    expected_auth_config,
)
from execution.lib.instance_profile_guard import Refused, digest


class ActionManifestBindings(unittest.TestCase):
    def setUp(self):
        self.registration = {
            "tenant_id": "owned-tenant",
            "client_id": "owned-signin-client",
            "service_principal_id": "owned-sp",
            "role_id": "owned-role",
            "role_value": "queue_operator",
            "callback": "https://queue.example.test/auth/callback",
            "single_tenant": True,
            "assignment_required": True,
            "api_permissions": [],
            "scopes": ["openid", "profile"],
            "assignments": {"owned-staff": "owned-role", "owned-verifier": None},
            "verification_sequence": {
                "principal_id": "owned-verifier",
                "phase": "no_role_pending",
                "no_role_evidence_sha256": None,
            },
            "security_defaults": {"enabled": True, "evidence_sha256": "a" * 64},
            "secret_expiry": {
                "starts_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-07-01T00:00:00Z",
            },
        }
        self.manifest = {
            "version": 1,
            "mode": "auth_activation",
            "action_ref": "owned-action",
            "references": {
                "task": "owned-task",
                "source_sha256": "b" * 64,
                "canonical_ref": "owned-config",
                "canonical_sha256": "c" * 64,
            },
            "binding": {
                "origin": "https://queue.example.test",
                "environment": "dev",
                "machine_id": "owned-machine",
            },
            "candidate": {
                "guard_source_sha256": "d" * 64,
                "commit": "e" * 40,
                "image": "registry.example.test/owned@sha256:" + "f" * 64,
            },
            "profiles": {
                key: "1" * 64
                for key in (
                    "before_config_sha256",
                    "after_config_sha256",
                    "before_static_sha256",
                    "after_static_sha256",
                    "before_secret_metadata_sha256",
                )
            },
            "registration": self.registration,
            "protected_fields": {
                name: "op://Private/owned-item/" + name for name in AUTH_SECRET_NAMES
            },
        }
        self.selected = {
            "action_ref": "owned-action",
            "manifest_sha256": digest(self.manifest),
            "environment": "dev",
            "source_sha256": "b" * 64,
            "canonical_sha256": "c" * 64,
            "registration_sha256": digest(self.registration),
        }

    def check(self, manifest=None, selected=None, registration=None):
        return check_action_manifest(
            self.manifest if manifest is None else manifest,
            self.selected if selected is None else selected,
            self.registration if registration is None else registration,
        )

    def repin_fixture(self, manifest):
        # Synthetic independently selected metadata, never an authority claim.
        return {
            **self.selected,
            "manifest_sha256": digest(manifest),
            "registration_sha256": digest(manifest["registration"]),
        }

    def test_exact_metadata_positive_does_not_authorize_action(self):
        original = copy.deepcopy(self.manifest)
        result = self.check()
        self.assertTrue(result["action_manifest_matches"])
        self.assertTrue(result["registration_metadata_matches"])
        self.assertFalse(result["action_authorized"])
        self.assertEqual(self.manifest, original)

    def test_well_formed_independent_digest_cannot_be_replaced_by_template_digest(self):
        selected = {**self.selected, "manifest_sha256": "0" * 64}
        with self.assertRaises(Refused):
            self.check(selected=selected)

    def test_selected_digest_source_canonical_environment_and_actual_registration_refuse(
        self,
    ):
        for key in self.selected:
            selected = {**self.selected, key: "wrong"}
            with self.subTest(key=key), self.assertRaises(Refused):
                self.check(selected=selected)
        actual = {**self.registration, "client_id": "foreign-mail-client"}
        with self.assertRaises(Refused):
            self.check(registration=actual)

    def test_self_approval_unknown_fields_null_and_unfilled_template_refuse(self):
        for value in (None, {}, {**self.manifest, "approved": True}):
            with self.subTest(value=value), self.assertRaises(Refused):
                check_action_manifest(value, self.selected, self.registration)
        for path in ("tenant_id", "client_id", "service_principal_id", "role_id"):
            for value in (None, "", [], True):
                bad = copy.deepcopy(self.manifest)
                bad["registration"][path] = value
                with self.subTest(path=path, value=value), self.assertRaises(Refused):
                    self.check(bad, self.repin_fixture(bad), bad["registration"])

    def test_registration_protocol_role_scope_and_callback_are_closed(self):
        for key, value in (
            ("role_value", "mail_reader"),
            ("callback", "https://other.example.test/auth/callback"),
            ("single_tenant", False),
            ("assignment_required", False),
            ("api_permissions", ["Mail.Read"]),
            ("scopes", ["openid", "profile", "offline_access"]),
        ):
            bad = copy.deepcopy(self.manifest)
            bad["registration"][key] = value
            with self.subTest(key=key), self.assertRaises(Refused):
                self.check(bad, self.repin_fixture(bad), bad["registration"])

    def test_protected_locators_are_exact_distinct_and_never_values(self):
        for mutation in ("missing", "extra", "duplicate", "null", "value", "malformed"):
            bad = copy.deepcopy(self.manifest)
            fields = bad["protected_fields"]
            name = sorted(AUTH_SECRET_NAMES)[0]
            if mutation == "missing":
                fields.pop(name)
            elif mutation == "extra":
                fields["UNSELECTED"] = "op://Private/owned/extra"
            elif mutation == "duplicate":
                fields[name] = fields[sorted(AUTH_SECRET_NAMES)[1]]
            elif mutation == "null":
                fields[name] = None
            elif mutation == "value":
                fields[name] = {"secret_value": "synthetic-only"}
            else:
                fields[name] = "op://Private/item"
            with self.subTest(mutation=mutation), self.assertRaises(Refused):
                self.check(bad, self.repin_fixture(bad))

    def test_no_role_sequence_preserves_existing_verifier_then_exact_final_assignment(
        self,
    ):
        self.check()
        bad = copy.deepcopy(self.manifest)
        bad["registration"]["assignments"]["owned-verifier"] = "owned-role"
        with self.assertRaises(Refused):
            self.check(bad, self.repin_fixture(bad), bad["registration"])
        good = copy.deepcopy(bad)
        good["registration"]["verification_sequence"].update(
            phase="completed", no_role_evidence_sha256="2" * 64
        )
        self.check(good, self.repin_fixture(good), good["registration"])
        good["registration"]["verification_sequence"]["no_role_evidence_sha256"] = None
        with self.assertRaises(Refused):
            self.check(good, self.repin_fixture(good), good["registration"])

    def test_all_profiles_candidate_and_expiry_metadata_must_be_filled(self):
        for key in self.manifest["profiles"]:
            bad = copy.deepcopy(self.manifest)
            bad["profiles"][key] = None
            with self.subTest(key=key), self.assertRaises(Refused):
                self.check(bad, self.repin_fixture(bad))
        for key in self.manifest["candidate"]:
            bad = copy.deepcopy(self.manifest)
            bad["candidate"][key] = "unfilled"
            with self.subTest(key=key), self.assertRaises(Refused):
                self.check(bad, self.repin_fixture(bad))
        for end in (
            None,
            "",
            "2025-12-31T00:00:00Z",
            "2026-07-01T00:00:00",
            "2026-07-01T00:00:00+02:00",
        ):
            bad = copy.deepcopy(self.manifest)
            bad["registration"]["secret_expiry"]["expires_at"] = end
            with self.subTest(end=end), self.assertRaises(Refused):
                self.check(bad, self.repin_fixture(bad), bad["registration"])


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
