"""Contract eval for the Fly connector.

Eval id: ateles-fly-connector-observe

Schemas for ``connector_status`` / ``deployment_observation`` are registered
via ``register_schemas.py`` (read-back gated). Unit tests stub
``schemas_match_expected`` so observe paths exercise store/runner behaviour
without requiring a live registry.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.connectors.fly import FlyConnector, resolve_fly_binding
from lib.connectors.runner import run_connector
from lib.connectors.store import ConnectorStore
from lib.connectors.test_connector_contract_eval import StubNeotoma

EVAL_ID = "ateles-fly-connector-observe"
FIXTURE = Path(__file__).parent / "fixtures" / "fly_releases_v15_v16.json"
FLY_MODULE = Path(__file__).parent / "fly.py"


class FlyStubStore(ConnectorStore):
    """ConnectorStore backed by StubNeotoma for integration tests."""

    def __init__(self, stub: StubNeotoma) -> None:
        super().__init__(base_url=stub.url, token="token")
        self._stub = stub


@pytest.fixture
def stub_neotoma():
    with StubNeotoma() as server:
        yield server


@pytest.fixture(autouse=True)
def _schemas_ready(monkeypatch):
    monkeypatch.setattr(
        "lib.connectors.fly.schemas_match_expected",
        lambda store: (True, []),
    )


def test_eval_id_is_stable():
    assert EVAL_ID == "ateles-fly-connector-observe"


def test_no_binding_skips_with_both_remediation_paths(monkeypatch, stub_neotoma):
    monkeypatch.delenv("FLY_APP", raising=False)
    monkeypatch.delenv("DEPLOYMENT_CONFIGURATION_ID", raising=False)
    store = FlyStubStore(stub_neotoma)
    result = FlyConnector(store=store).observe()
    assert result.ok
    assert result.detail.get("skipped") is True
    assert "FLY_APP" in result.detail["skip_reason"]
    assert "DEPLOYMENT_CONFIGURATION_ID" in result.detail["skip_reason"]
    assert stub_neotoma.entities_of_type("deployment_observation") == []


def test_existence_query_refusal_is_not_idempotent_success(
    monkeypatch, stub_neotoma, tmp_path
):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [
            {
                "config": {
                    "guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"},
                    "checks": {},
                }
            }
        ]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    connector = FlyConnector(store=store)
    monkeypatch.setattr(connector, "_release_already_stored", lambda fields: None)
    result = connector.observe()
    assert not result.ok
    assert "idempotent_note" not in result.detail
    assert result.detail.get("refused_releases", 0) > 0
    assert stub_neotoma.entities_of_type("deployment_observation") == []


def test_triggered_by_is_opaque_not_email(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = [
        {
            "ID": "rel_privacy",
            "Version": 99,
            "ImageRef": "deployment-privacy",
            "Status": "complete",
            "CreatedAt": "2026-09-01T00:00:00Z",
            "User": {"email": "deployer@example.com", "name": "Deployer"},
        }
    ]

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [
            {
                "config": {
                    "guest": {"memory_mb": 2048, "cpus": 2, "cpu_kind": "shared"},
                    "checks": {},
                }
            }
        ]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    FlyConnector(store=store).observe()
    obs = stub_neotoma.entities_of_type("deployment_observation")
    assert len(obs) == 1
    triggered = obs[0]["fields"]["triggered_by"]
    assert triggered.startswith("actor:")
    assert "@" not in triggered
    assert "example.com" not in triggered


def test_binding_from_fly_app_env(monkeypatch):
    monkeypatch.setenv("FLY_APP", "example-app")
    binding = resolve_fly_binding(ConnectorStore(base_url="https://x.invalid", token=""))
    assert not isinstance(binding, type(resolve_fly_binding.__annotations__.get("return")))
    from lib.connectors.fly import FlyBinding

    assert isinstance(binding, FlyBinding)
    assert binding.fly_app == "example-app"
    assert binding.instance_ref.startswith("env:")


def test_observe_never_raises(monkeypatch, stub_neotoma):
    monkeypatch.setenv("FLY_APP", "example-app")
    store = FlyStubStore(stub_neotoma)

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", boom)
    result = FlyConnector(store=store).observe()
    assert not result.ok
    assert "kaboom" in result.error


def test_one_entity_per_release_fields_complete(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text(
        "[[vm]]\nmemory = '2gb'\ncpu_kind = 'performance'\ncpus = 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))

    releases = json.loads(FIXTURE.read_text())
    machines = [
        {
            "config": {
                "guest": {
                    "memory_mb": 8192,
                    "cpus": 2,
                    "cpu_kind": "performance",
                },
                "checks": {"ready": {}},
            }
        }
    ]

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        if args == ["machine", "list"]:
            return machines
        raise AssertionError(f"unexpected flyctl args: {args}")

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    result = FlyConnector(store=store).observe()

    assert result.ok
    observations = stub_neotoma.entities_of_type("deployment_observation")
    assert len(observations) == len(releases)
    for entity in observations:
        fields = entity["fields"]
        for key in (
            "version",
            "image_ref",
            "deployed_at",
            "status",
            "triggered_by",
            "observed_at",
            "instance_ref",
            "source",
            "release_version",
        ):
            assert key in fields
        assert fields["source"] == "fly"


def test_v15_v16_same_version_different_image(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))

    releases = json.loads(FIXTURE.read_text())
    v15 = next(r for r in releases if r["Version"] == 15)
    v16 = next(r for r in releases if r["Version"] == 16)
    v15["version_label"] = "0.17.0"
    v16["version_label"] = "0.17.0"

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            patched = []
            for row in releases:
                copy = dict(row)
                if copy["Version"] in (15, 16):
                    copy["Version"] = copy["version_label"]
                patched.append(copy)
            return patched
        return [{"config": {"guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"}, "checks": {}}}]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    FlyConnector(store=store).observe()

    observations = stub_neotoma.entities_of_type("deployment_observation")
    by_version = {e["fields"]["version"]: e["fields"] for e in observations}
    assert by_version["0.17.0"]["image_ref"] in {
        "deployment-01M11TXSC",
        "deployment-01M1EBTEB",
    }
    images = {e["fields"]["image_ref"] for e in observations if e["fields"]["version"] == "0.17.0"}
    assert len(images) == 2


def test_image_changed_from_previous_release_flags(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))

    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [{"config": {"guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"}, "checks": {}}}]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    FlyConnector(store=store).observe()

    observations = sorted(
        stub_neotoma.entities_of_type("deployment_observation"),
        key=lambda e: int(e["fields"]["version"]),
    )
    v11 = next(e for e in observations if e["fields"]["version"] == "11")
    assert v11["fields"]["image_changed_from_previous_release"] is False
    v15 = next(e for e in observations if e["fields"]["version"] == "15")
    assert v15["fields"]["image_changed_from_previous_release"] is True


def test_idempotent_rerun_zero_new_writes(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [{"config": {"guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"}, "checks": {}}}]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    connector = FlyConnector(store=store)
    first = connector.observe()
    second = connector.observe()
    assert first.records_written == len(releases)
    assert second.records_written == 0
    assert second.detail.get("idempotent_note") == "0 new releases"


def test_config_drift_detected_on_shrink(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text(
        "[[vm]]\nmemory = '1gb'\ncpu_kind = 'shared'\ncpus = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [
            {
                "config": {
                    "guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"},
                    "checks": {"ready": {}},
                }
            }
        ]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    result = FlyConnector(store=store).observe()
    assert result.ok
    assert result.detail["config_drift_detected"] is True
    status_rows = stub_neotoma.entities_of_type("connector_status")
    assert any(r["fields"].get("config_drift_detected") for r in status_rows)


def test_fly_registered_in_build_connectors():
    import importlib.util
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    path = repo / "execution" / "daemons" / "connectors" / "connectors_daemon.py"
    spec = importlib.util.spec_from_file_location("connectors_daemon_fly", path)
    daemon = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    spec.loader.exec_module(daemon)
    names = [getattr(c, "name", "") for c in daemon.build_connectors()]
    assert "fly" in names


def test_fly_module_contains_no_literal_infrastructure_ids():
    text = FLY_MODULE.read_text(encoding="utf-8")
    banned = [
        r"\.fly\.dev",
        r"\.fly\.io",
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
    ]
    for pattern in banned:
        assert not re.search(pattern, text), f"banned pattern {pattern!r} in fly.py"


def test_partial_failure_machine_api_down_release_history_ok(
    monkeypatch, stub_neotoma, tmp_path
):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        raise RuntimeError("machine API 502")

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    result = FlyConnector(store=store).observe()
    assert result.ok
    assert "machine_api" in str(result.detail.get("partial_failures"))
    assert len(stub_neotoma.entities_of_type("deployment_observation")) == len(releases)


def test_run_connector_records_fly_status(monkeypatch, stub_neotoma, tmp_path):
    monkeypatch.setenv("FLY_APP", "example-app")
    config = tmp_path / "fly.toml"
    config.write_text("[[vm]]\nmemory = '2gb'\ncpus = 2\n", encoding="utf-8")
    monkeypatch.setenv("FLY_CONFIG_PATH", str(config))
    releases = json.loads(FIXTURE.read_text())

    def fake_flyctl(args, *, app, timeout=60):
        if args == ["releases"]:
            return releases
        return [{"config": {"guest": {"memory_mb": 8192, "cpus": 2, "cpu_kind": "performance"}, "checks": {}}}]

    monkeypatch.setattr("lib.connectors.fly.run_flyctl_json", fake_flyctl)
    store = FlyStubStore(stub_neotoma)
    result = run_connector(FlyConnector(store=store), store)
    assert result.ok
    status = stub_neotoma.entity_fields("connector_status", "fly")
    assert status["poll_interval_seconds"] == 900 or status.get("connector_name") == "fly"
