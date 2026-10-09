"""Owned metadata and protected-consumer controls; no external credential reads."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.lib.instance_auth_profile_adapter import (
    op_field,
    private_json,
    resolve_action,
    verify_protected_fields,
)
from execution.lib.instance_profile_guard import Refused, digest
from execution.scripts import test_instance_auth_profile_guard as auth_tests
from lib.capabilities.credentials import Secret


class ProtectedMetadataAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture = auth_tests.ActionManifestBindings()
        fixture.setUp()
        self.manifest = copy.deepcopy(fixture.manifest)
        self.canonical_id = "ent_" + "a" * 24
        self.canonical = {
            "entity_id": self.canonical_id,
            "entity_type": "deployment_configuration",
            "merged_to_entity_id": None,
            "snapshot": {
                "environment": "development",
                "public_domain": "queue.example.test",
            },
        }
        self.manifest["references"].update(
            canonical_ref=self.canonical_id,
            canonical_sha256=digest(self.canonical["snapshot"]),
        )
        self.registration = self.manifest["registration"]
        self.selected = {
            **fixture.selected,
            "manifest_sha256": digest(self.manifest),
            "canonical_sha256": digest(self.canonical["snapshot"]),
        }
        self.registration_file = self.write("registration.json", self.registration)
        self.record = {
            "version": 1,
            "selected": self.selected,
            "canonical": {
                "entity_id": self.canonical_id,
                "snapshot_sha256": digest(self.canonical["snapshot"]),
                "environment_mapping": {"canonical": "development", "runtime": "dev"},
            },
            "registration": {
                "path": str(self.registration_file),
                "sha256": digest(self.registration),
            },
        }
        self.refs = {
            name: "op://Private/" + "a" * 26 + "/" + name
            for name in self.manifest["protected_fields"]
        }
        self.values = {
            self.refs["THEODORE_OIDC_TENANT_ID"]: "owned-tenant",
            self.refs["THEODORE_OIDC_CLIENT_ID"]: "owned-signin-client",
            self.refs["THEODORE_OIDC_CLIENT_SECRET"]: "synthetic-only-value",
        }

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def resolve(self, record=None, current=None):
        record = self.record if record is None else record
        current = self.canonical if current is None else current
        return resolve_action(
            self.write("record.json", record),
            digest(record),
            self.manifest,
            canonical_get=lambda _: current,
        )

    def reader(self, locator):
        return Secret(self.values[locator])

    def test_actual_pinned_record_and_fresh_canonical_metadata_positive(self):
        result = self.resolve()
        self.assertEqual(result["canonical"], self.canonical)
        self.assertEqual(result["registration"], self.registration)
        self.assertNotIn("action_authorized", result)

    def test_record_pin_registration_pin_and_canonical_drift_refuse(self):
        path = self.write("record.json", self.record)
        with self.assertRaises(Refused):
            resolve_action(
                path, "0" * 64, self.manifest, canonical_get=lambda _: self.canonical
            )
        self.write("registration.json", {**self.registration, "client_id": "foreign"})
        with self.assertRaises(Refused):
            self.resolve()
        self.write("registration.json", self.registration)
        for key, value in (
            ("entity_id", "ent_" + "b" * 24),
            ("entity_type", "task"),
            ("merged_to_entity_id", "ent_" + "b" * 24),
            ("snapshot", {**self.canonical["snapshot"], "environment": "production"}),
        ):
            with self.subTest(key=key), self.assertRaises(Refused):
                self.resolve(current={**self.canonical, key: value})

    def test_no_boolean_or_alternate_field_selection_or_missing_mapping(self):
        for key, value in (("approved", True), ("selected", None), ("version", True)):
            bad = {**self.record, key: value}
            with self.subTest(key=key), self.assertRaises(Refused):
                self.resolve(record=bad)
        bad = copy.deepcopy(self.record)
        bad["canonical"].pop("environment_mapping")
        with self.assertRaises(Refused):
            self.resolve(record=bad)

    def test_private_json_duplication_nonfinite_permissions_and_symlink_refuse(self):
        path = self.write("private.json", {})
        for text in ('{"x":1,"x":2}', '{"x":NaN}', "{"):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(Refused):
                private_json(path)
        path.write_text("{}")
        path.chmod(0o644)
        with self.assertRaises(Refused):
            private_json(path)
        path.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(Refused):
            private_json(link)

    def test_protected_exact_target_and_existing_value_continuity_no_output(self):
        receipt = verify_protected_fields(
            self.refs,
            self.registration,
            reader=self.reader,
            before_refs={
                "THEODORE_OIDC_CLIENT_SECRET": self.refs["THEODORE_OIDC_CLIENT_SECRET"]
            },
        )
        self.assertTrue(receipt["protected_target_matches"])
        self.assertFalse(receipt["credential_values_recorded"])
        self.assertNotIn("synthetic-only-value", json.dumps(receipt))
        self.assertEqual(str(Secret("synthetic-only-value")), "Secret(<redacted>)")
        before = "op://Private/" + "b" * 26 + "/credential"
        self.values[before] = "different-synthetic-value"
        with self.assertRaises(Refused):
            verify_protected_fields(
                self.refs,
                self.registration,
                reader=self.reader,
                before_refs={"THEODORE_OIDC_CLIENT_SECRET": before},
            )
        self.values[self.refs["THEODORE_OIDC_CLIENT_ID"]] = "foreign"
        with self.assertRaises(Refused):
            verify_protected_fields(self.refs, self.registration, reader=self.reader)

    def test_protected_failure_is_sanitized_and_no_ambient_fallback(self):
        def broken(_):
            raise RuntimeError("synthetic-private-canary")

        with self.assertRaises(Refused) as caught:
            verify_protected_fields(self.refs, self.registration, reader=broken)
        self.assertNotIn("synthetic-private-canary", str(caught.exception))
        with self.assertRaises(Refused):
            verify_protected_fields(
                self.refs, self.registration, reader=lambda _: "plaintext"
            )

    def test_actual_op_consumer_argv_never_contains_value_and_failure_suppresses_stderr(
        self,
    ):
        response = type(
            "Response",
            (),
            {"returncode": 0, "stdout": "synthetic-only-value", "stderr": ""},
        )()
        with patch(
            "execution.lib.instance_auth_profile_adapter.subprocess.run",
            return_value=response,
        ) as run:
            value = op_field(self.refs["THEODORE_OIDC_CLIENT_SECRET"])
            self.assertEqual(value.reveal(), "synthetic-only-value")
            self.assertNotIn("synthetic-only-value", repr(run.call_args))
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
        response.returncode = 1
        response.stderr = "synthetic-private-canary"
        with patch(
            "execution.lib.instance_auth_profile_adapter.subprocess.run",
            return_value=response,
        ):
            with self.assertRaises(Refused) as caught:
                op_field(self.refs["THEODORE_OIDC_CLIENT_SECRET"])
        self.assertNotIn("synthetic-private-canary", str(caught.exception))
        with patch("execution.lib.instance_auth_profile_adapter.subprocess.run") as run:
            with self.assertRaises(Refused):
                op_field("op://Private/named-item/credential")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
