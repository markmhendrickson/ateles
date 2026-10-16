"""Shared fixtures for the capability client tests.

Hermetic: no network, no real credentials, no writes outside ``tmp_path``.
The vendor is a ``StubVendor`` (or an adapter with a fake transport), so no
test can make a paid call.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.capabilities import slots  # noqa: E402
from lib.capabilities.generation import CapabilityClient  # noqa: E402
from lib.capabilities.spend import SpendLedger  # noqa: E402
from lib.capabilities.vendors import StubVendor  # noqa: E402

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def binding_row(
    slot,
    vendor="stub",
    *,
    constraints="default",
    fallback="None by design",
    credential_location="STUB_KEY",
    entity_id=None,
    tool_namespace="stub-model",
):
    if constraints == "default":
        constraints = json.dumps({"model_tier": "stub-model", "monthly_cap_usd": 10})
    elif isinstance(constraints, dict):
        constraints = json.dumps(constraints)
    snap = {
        "capability": slot,
        "vendor": vendor,
        "tool_namespace": tool_namespace,
        "credential_location": credential_location,
        "fallback": fallback,
        "visibility": "private",
    }
    if constraints is not None:
        snap["constraints"] = constraints
    return {
        "entity_id": entity_id or f"ent_{slot}",
        "entity_type": "vendor_binding",
        "snapshot": {"snapshot": snap},
    }


class Spy:
    """A fetcher over a fixed row list that counts calls."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def __call__(self, entity_type):
        self.calls += 1
        assert entity_type == "vendor_binding"
        return list(self.rows)


class MemorySink:
    def __init__(self):
        self.stored = []

    def store(self, record, idempotency_key):
        self.stored.append((record, idempotency_key))
        return f"ent_rec_{len(self.stored)}", None


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    """Factory: cred_file(name, value) -> path of a 0600 file under a temp
    credential directory. Built at runtime so no secret-shaped literal sits in
    the source."""
    from lib.capabilities import credentials

    directory = tmp_path / "ateles-credentials"
    directory.mkdir()
    monkeypatch.setenv(credentials.CREDENTIAL_DIR_ENV, str(directory))

    def make(name, value):
        path = directory / "generation.env"
        path.write_text(name + "=" + value + "\n")
        path.chmod(0o600)
        return str(path)

    return make


@pytest.fixture
def now():
    return FIXED_NOW


@pytest.fixture
def ledger(tmp_path, now):
    return SpendLedger(tmp_path / "spend", clock=lambda: now, lock_timeout_s=2)


@pytest.fixture
def sink():
    return MemorySink()


@pytest.fixture
def make_client(tmp_path, ledger, sink):
    """Factory: make_client(rows, adapters=None, **overrides) -> CapabilityClient."""

    def _make(rows, adapters=None, **overrides):
        stub = StubVendor()
        kwargs = dict(
            fetch=Spy(rows),
            ledger=ledger,
            adapters=adapters if adapters is not None else {"stub": stub},
            sink=sink,
            artifact_root=tmp_path / "artifacts",
            process_values={},
        )
        kwargs.update(overrides)
        return CapabilityClient(**kwargs)

    return _make


@pytest.fixture
def slot_names():
    return slots.SLOTS
