"""Tests for the connector daemon.

The alerting rules carry the weight here. An alert that fires on every
transient blip gets muted, and a muted alert is the checkout-drift log all over
again — a correct signal nobody reads.
"""

from __future__ import annotations

import importlib.util
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

    def read_status(self, name):
        return self.statuses.get(name)


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


def test_no_connectors_registered_yet_is_a_clean_no_op():
    """Stage 1 ships the framework live but with no sources; that must not error."""
    assert daemon.build_connectors() == []


def test_run_once_without_connectors_returns_empty(monkeypatch):
    monkeypatch.setattr(daemon, "build_connectors", lambda: [])
    assert daemon.run_once() == {}


def test_env_filter_selects_which_connectors_may_run(monkeypatch):
    """The staging control: Fly ships live while GitHub stays dark, no code change."""
    from lib.connectors.runner import enabled_connector_names

    monkeypatch.setenv("ATELES_CONNECTORS", "fly, github")
    assert enabled_connector_names() == {"fly", "github"}

    # Unset means "all" — not "none", which would silently disable everything.
    monkeypatch.delenv("ATELES_CONNECTORS")
    assert enabled_connector_names() is None
