"""Tests for the connector daemon.

The alerting rules carry the weight here. An alert that fires on every
transient blip gets muted, and a muted alert is the checkout-drift log all over
again — a correct signal nobody reads.
"""

from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.connectors.base import ConnectorResult, ConnectorStatus  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "connectors_daemon", Path(__file__).parent / "connectors_daemon.py"
)
daemon = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(daemon)


class FakeStore:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.writes: list[ConnectorStatus] = []
        self.configured = True

    def read_status(self, name):
        return self.statuses.get(name)

    def write_status(self, status):
        self.writes.append(status)
        self.statuses[status.connector_name] = status


def _reports(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(
        daemon,
        "emit_daemon_report",
        lambda severity, message, details=None: sent.append((severity, message, details)),
    )
    return sent


# ── alert thresholds ────────────────────────────────────────────────────────


def test_no_alert_on_a_single_failure(monkeypatch):
    """One failed run is routine — a laptop sleeps, a fetch times out."""
    sent = _reports(monkeypatch)
    store = FakeStore({"fly": ConnectorStatus(connector_name="fly", consecutive_failures=1)})

    daemon.alert_on_failures(store, {"fly": ConnectorResult.failure("timeout")})

    assert sent == []


def test_alert_once_the_threshold_is_reached(monkeypatch):
    sent = _reports(monkeypatch)
    store = FakeStore(
        {
            "fly": ConnectorStatus(
                connector_name="fly",
                consecutive_failures=3,
                last_success_at="2026-08-29T09:00:00+00:00",
            )
        }
    )

    daemon.alert_on_failures(store, {"fly": ConnectorResult.failure("HTTP 502")})

    assert len(sent) == 1
    severity, message, details = sent[0]
    # Anthus routes error/critical to the operator.
    assert severity == "error"
    assert "fly" in message and "3 consecutive" in message
    assert details["last_success_at"] == "2026-08-29T09:00:00+00:00"


def test_successful_connectors_never_alert(monkeypatch):
    sent = _reports(monkeypatch)
    daemon.alert_on_failures(FakeStore(), {"fly": ConnectorResult.success(16)})
    assert sent == []


def test_skipped_connectors_never_alert_even_after_many_passes(monkeypatch):
    """Unbound skip is ok=True — soft idle must not page after N passes."""
    sent = _reports(monkeypatch)
    store = FakeStore(
        {
            "fly": ConnectorStatus(
                connector_name="fly",
                consecutive_failures=0,
                status="never_run",
            )
        }
    )
    skipped = ConnectorResult.skipped("set FLY_APP or DEPLOYMENT_CONFIGURATION_ID")
    for _ in range(5):
        daemon.alert_on_failures(store, {"fly": skipped})
    assert sent == []


def test_alert_names_the_last_success_as_never_when_there_was_none(monkeypatch):
    """'Never worked' and 'worked last Tuesday' are different operator situations."""
    sent = _reports(monkeypatch)
    store = FakeStore(
        {"gh": ConnectorStatus(connector_name="gh", consecutive_failures=5)}
    )

    daemon.alert_on_failures(store, {"gh": ConnectorResult.failure("boom")})

    assert sent[0][2]["last_success_at"] == "never"


def test_alert_is_about_connector_health_not_observed_values(monkeypatch):
    """An alarm from a stale observation asserts a present it cannot see.

    The daemon deliberately alarms on 'this connector stopped working', which
    is certain, rather than on what a stale reading appears to show.
    """
    sent = _reports(monkeypatch)
    store = FakeStore(
        {"fly": ConnectorStatus(connector_name="fly", consecutive_failures=4)}
    )

    daemon.alert_on_failures(store, {"fly": ConnectorResult.failure("unreachable")})

    _, message, _ = sent[0]
    assert "going stale" in message
    # Not a claim about versions, config, or anything the connector failed to read.
    assert "behind" not in message


# ── registration ────────────────────────────────────────────────────────────


def test_fly_connector_registered_by_default():
    """Stage 2 registers Fly; ATELES_CONNECTORS can still filter it out."""
    names = [getattr(c, "name", "") for c in daemon.build_connectors()]
    assert "fly" in names


def test_build_connectors_respects_env_filter(monkeypatch):
    monkeypatch.setenv("ATELES_CONNECTORS", "github")
    assert daemon.build_connectors() == []


def test_run_once_without_connectors_writes_heartbeat(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(daemon, "build_connectors", lambda: [])
    monkeypatch.setattr(daemon, "ConnectorStore", lambda: store)

    results = daemon.run_once()

    assert results["connectors"].ok
    assert len(store.writes) == 1
    heartbeat = store.writes[0]
    assert heartbeat.connector_name == "connectors"
    assert heartbeat.status == "ok"
    assert heartbeat.records_written == 0


def test_run_once_with_fly_enabled_runs_connector(monkeypatch):
    """When Fly is registered, run_once drives it instead of the framework heartbeat."""
    from lib.connectors.fly import FlyConnector

    store = FakeStore()
    monkeypatch.setattr(daemon, "build_connectors", lambda: [FlyConnector()])
    monkeypatch.setattr(daemon, "ConnectorStore", lambda: store)
    monkeypatch.setattr(
        FlyConnector,
        "observe",
        lambda self: __import__(
            "lib.connectors.base", fromlist=["ConnectorResult"]
        ).ConnectorResult.success(records_written=0),
    )

    results = daemon.run_once()

    assert "fly" in results
    assert results["fly"].ok


def test_run_once_without_connectors_fails_when_heartbeat_unconfigured(monkeypatch):
    store = FakeStore()
    store.configured = False
    monkeypatch.setattr(daemon, "build_connectors", lambda: [])
    monkeypatch.setattr(daemon, "ConnectorStore", lambda: store)

    results = daemon.run_once()

    assert not results["connectors"].ok
    assert store.writes == []


def test_env_filter_selects_which_connectors_may_run(monkeypatch):
    """The staging control: Fly ships live while GitHub stays dark, no code change."""
    from lib.connectors.runner import enabled_connector_names

    monkeypatch.setenv("ATELES_CONNECTORS", "fly, github")
    assert enabled_connector_names() == {"fly", "github"}

    # Unset means "all" — not "none", which would silently disable everything.
    monkeypatch.delenv("ATELES_CONNECTORS")
    assert enabled_connector_names() is None


# ── install contract ────────────────────────────────────────────────────────


def test_plist_committed_alongside_installer():
    daemon_dir = Path(__file__).resolve().parent
    assert (daemon_dir / "com.ateles.connectors.plist").is_file()


def test_install_script_preflights_missing_plist():
    src = (Path(__file__).resolve().parent / "install.sh").read_text()
    assert "PLIST_SRC=" in src
    assert "missing launchd plist" in src
    assert "REPO_ROOT=" in src
    assert 'launchctl bootstrap "$DOMAIN" "$DEST"' in src
    assert "com.ateles.connectors is not listed" in src
    assert 'cp "$TMP_PLIST" "$DEST"' in src


def test_plist_matches_resident_daemon_contract():
    plist_path = Path(__file__).resolve().parent / "com.ateles.connectors.plist"
    plist = plistlib.loads(plist_path.read_bytes())

    assert plist["Label"] == "com.ateles.connectors"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert "StartInterval" not in plist
    assert plist["StandardOutPath"].endswith("/Library/Logs/ateles/connectors.log")
    assert plist["StandardErrorPath"].endswith("/Library/Logs/ateles/connectors.log")
    args = plist["ProgramArguments"]
    assert args[0].endswith("/.venv/bin/python3")
    assert args[1].endswith("/execution/daemons/connectors/connectors_daemon.py")
    assert "--once" not in args
    env = plist["EnvironmentVariables"]
    assert env["NEOTOMA_BASE_URL"] == "https://neotoma.markmhendrickson.com"
    assert env["CONNECTOR_POLL_SECONDS"] == "900"
    assert "NEOTOMA_BEARER_TOKEN" not in env
