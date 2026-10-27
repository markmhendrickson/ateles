"""Provider health reads fail honest (2026-10-27).

All three lens providers were unusable at once: one exhausted, one without
authentication, one whose usage reading had been stale for six days. The
router showed the stale reading's last values as if they were current, named
only one provider's reason when it refused, and told nobody. These tests pin:

(a) a reading older than the configured age is UNKNOWN, never its last value;
(b) an outage names every provider's reason and raises ONE notification per
    outage (not per call), and a recovery re-arms it;
(c) a provider with no login inside the isolated harness home is reported as
    an authentication-setup failure that names what is missing, and the runner
    copies no credential.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "execution" / "scripts"
for _p in (str(_DAEMON_DIR), str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_router  # noqa: E402
import provider_outage  # noqa: E402
import skill_runner  # noqa: E402
from lib.notify import Notifier, Priority  # noqa: E402

WEEK = 7 * 24 * 3600.0
ALL = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": "/bin/cursor"}
NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}


@pytest.fixture(autouse=True)
def _router(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json"))
    for name in (
        "APIS_HARNESS_HEADROOM",
        "APIS_HARNESS_MIN_HEADROOM",
        "APIS_HARNESS_USAGE_MAX_AGE_SECONDS",
        "APIS_USAGE_STALE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
    # The conftest turns the usage gate off for suites that do not target it;
    # these do, and the live refresh probe stays off (no CLI is launched).
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _record(provider, *, age_seconds, used=30.0, resets_in=3 * 24 * 3600.0):
    now = time.time()
    harness_router.record_usage(
        provider,
        [{"name": "weekly", "used_percent": used,
          "resets_at": harness_router._iso_from_wall(now + resets_in)}],
        observed_at=now - age_seconds,
    )


# ── (a) a stale reading is unknown, never its last value ------------------------


def test_a_reading_older_than_the_configured_age_is_unknown_not_its_last_value():
    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)  # six days old

    reading = harness_router.usage_reading("claude")

    assert reading.state == harness_router.READING_UNKNOWN
    assert reading.age_seconds > 5 * 24 * 3600.0
    assert "not a current reading" in reading.reason
    assert "APIS_HARNESS_USAGE_MAX_AGE_SECONDS" in reading.reason
    # nothing in the reading carries the 97% it last said
    assert "97" not in reading.reason


def test_a_fresh_reading_is_fresh():
    _record("claude", age_seconds=60.0)
    assert harness_router.usage_reading("claude").state == harness_router.READING_FRESH


def test_the_age_is_the_configured_one(monkeypatch):
    _record("claude", age_seconds=2 * 3600.0)
    assert harness_router.usage_reading("claude").state == harness_router.READING_FRESH
    monkeypatch.setenv("APIS_HARNESS_USAGE_MAX_AGE_SECONDS", "3600")
    assert harness_router.usage_reading("claude").state == harness_router.READING_UNKNOWN


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5", "nan", "inf"])
def test_an_unusable_age_setting_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("APIS_HARNESS_USAGE_MAX_AGE_SECONDS", raw)
    assert harness_router.usage_max_age_seconds() == 21600.0


def test_a_provider_never_observed_is_none_not_fresh():
    assert harness_router.usage_reading("cursor").state == harness_router.READING_NONE


def test_a_recorded_exhaustion_with_a_future_reset_is_current_by_its_own_time():
    harness_router.record_exhausted("codex", time.time() + 3600)
    assert harness_router.usage_reading("codex").state == harness_router.READING_FRESH


def test_an_old_reading_whose_windows_have_all_reset_is_current():
    now = time.time()
    harness_router.record_usage(
        "claude",
        [{"name": "weekly", "used_percent": 80.0,
          "resets_at": harness_router._iso_from_wall(now - 3600)}],
        observed_at=now - 30 * 3600.0,
    )
    assert harness_router.usage_reading("claude").state == harness_router.READING_FRESH


def test_headroom_labels_a_stale_reading_unknown_rather_than_default():
    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)
    _record("codex", age_seconds=60.0, used=40.0)

    resolved = harness_router.headroom_resolution()

    assert resolved["claude"][1] == harness_router.HEADROOM_SOURCE_UNKNOWN
    # the stale 97% is not the value either
    assert resolved["claude"][0] != pytest.approx(0.03)
    assert resolved["codex"][1] == harness_router.HEADROOM_SOURCE_LIVE_USAGE
    assert resolved["codex"][0] == pytest.approx(0.6)
    # a provider with no reading at all stays "default"
    assert resolved["cursor"][1] == harness_router.HEADROOM_SOURCE_DEFAULT


def test_a_manual_override_still_wins_over_an_unknown_reading(tmp_path, monkeypatch):
    _record("claude", age_seconds=6 * 24 * 3600.0)
    path = tmp_path / "headroom.json"
    path.write_text(json.dumps({"claude": {"headroom": 0.0, "cooldown_reason": "manual"}}))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(path))
    value, source = harness_router.headroom_resolution()["claude"]
    assert (value, source) == (0.0, harness_router.HEADROOM_SOURCE_MANUAL_OVERRIDE)


def test_show_withholds_a_stale_readings_windows_and_headroom(capsys):
    import harness_usage

    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)
    _record("codex", age_seconds=60.0, used=40.0)

    assert harness_usage.main(["show"]) == 0
    shown = json.loads(capsys.readouterr().out)

    stale = shown["claude"]
    assert stale["reading"]["state"] == "unknown"
    assert stale["reading"]["age_seconds"] > 5 * 24 * 3600
    assert stale["headroom"] is None
    assert stale["headroom_source"] == harness_router.HEADROOM_SOURCE_UNKNOWN
    assert stale["windows"] == []
    assert stale["windows_withheld"] == 1
    assert "97" not in json.dumps(stale["reading"])
    # the neutral value the router would select with is named, not passed off as measured
    assert stale["headroom_used_for_selection"] == 1.0

    fresh = shown["codex"]
    assert fresh["reading"]["state"] == "fresh"
    assert fresh["headroom"] == pytest.approx(0.6)
    assert fresh["windows"][0]["used_percent"] == 40.0
    assert "windows_withheld" not in fresh


# ── (b) an outage names every provider and alerts once -----------------------


def _outage_world(monkeypatch=None):
    """claude: stale reading (gate refuses); codex: headroom 0; cursor: no binary."""
    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)
    harness_router.record_exhausted("codex", time.time() + 5 * 3600.0)
    return {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None}


def test_the_reasons_name_every_candidate_provider(monkeypatch):
    binaries = _outage_world(monkeypatch)

    reasons = harness_router.provider_exclusion_reasons(binaries)

    assert set(reasons) == {"claude", "codex", "cursor"}
    assert "stale" in reasons["claude"]
    assert "headroom=0.000" in reasons["codex"]
    assert reasons["cursor"] == "binary unavailable"


def test_a_usable_provider_is_absent_from_the_reasons(monkeypatch):
    binaries = _outage_world(monkeypatch)
    _record("claude", age_seconds=60.0, used=10.0)  # claude recovers
    assert "claude" not in harness_router.provider_exclusion_reasons(binaries)


class RecordingNotifier:
    """Notifier.send/clear_dedupe semantics in memory: first send per key delivers."""

    def __init__(self):
        self.delivered: list[dict] = []
        self.suppressed = 0
        self._open: set[str] = set()

    def send(self, message, priority=None, handler="", dedupe_key=None, **_):
        if dedupe_key is not None and dedupe_key in self._open:
            self.suppressed += 1
            return False
        if dedupe_key is not None:
            self._open.add(dedupe_key)
        self.delivered.append({"message": message, "priority": priority, "key": dedupe_key})
        return True

    def clear_dedupe(self, key):
        self._open.discard(key)


async def _never_attempt(selected):  # pragma: no cover - must not be reached
    raise AssertionError(f"dispatch reached provider {selected}")


def _dispatch(binaries, notifier, **kwargs):
    return asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada", _never_attempt, binaries=binaries, notifier=notifier, **kwargs
        )
    )


def test_the_dispatch_error_names_every_providers_reason(monkeypatch):
    binaries = _outage_world(monkeypatch)

    result = _dispatch(binaries, RecordingNotifier())

    assert not result.ok
    for needle in ("claude:", "codex:", "cursor:", "stale", "headroom=0.000",
                   "binary unavailable"):
        assert needle in result.error, (needle, result.error)


def test_one_notification_per_outage_not_per_call(monkeypatch):
    binaries = _outage_world(monkeypatch)
    notifier = RecordingNotifier()

    for _ in range(5):
        _dispatch(binaries, notifier)

    assert len(notifier.delivered) == 1
    assert notifier.suppressed == 4
    sent = notifier.delivered[0]
    assert sent["priority"] == Priority.BLOCKER
    assert sent["key"] == provider_outage.OUTAGE_DEDUPE_KEY
    for needle in ("claude:", "codex:", "cursor:", "stale"):
        assert needle in sent["message"]


def test_a_recovery_rearms_the_alert_so_a_recurrence_is_reported(monkeypatch):
    binaries = _outage_world(monkeypatch)
    notifier = RecordingNotifier()
    _dispatch(binaries, notifier)
    assert len(notifier.delivered) == 1

    # claude comes back: a dispatch finds a candidate and clears the open outage
    _record("claude", age_seconds=60.0, used=10.0)

    async def ok(selected):
        return skill_runner.SkillResult("cicada", True, 0, "done", "", provider=selected)

    asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada", ok, binaries=binaries, notifier=notifier
        )
    )
    assert notifier._open == set()

    # ...and when it goes stale again, the operator is told again
    harness_router.record_usage(
        "claude",
        [{"name": "weekly", "used_percent": 97.0,
          "resets_at": harness_router._iso_from_wall(time.time() + 3 * 24 * 3600)}],
        observed_at=time.time() - 6 * 24 * 3600.0,
    )
    _dispatch(binaries, notifier)
    assert len(notifier.delivered) == 2


def test_the_real_notifier_dedupes_the_outage_across_instances(tmp_path):
    """Through lib.notify.Notifier itself, including its persisted journal."""
    sent: list[str] = []

    def build():
        n = Notifier(rubric=NO_SILENCE)
        n._digest_path = tmp_path / "digest.json"
        n._dedupe_path = tmp_path / "dedupe.json"
        n._deliver = lambda m, force=False, email_eligible=True: (sent.append(m), True)[1]
        return n

    reasons = {"claude": "usage reading stale", "codex": "headroom=0.000"}
    assert provider_outage.report_outage(build(), reasons) is True
    assert provider_outage.report_outage(build(), reasons) is False  # new process
    assert len(sent) == 1
    provider_outage.clear_outage(build())
    assert provider_outage.report_outage(build(), reasons) is True
    assert len(sent) == 2


def test_a_self_clearing_wait_does_not_page_the_operator():
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    harness_router.record_cooling("codex", time.time() + 3600, reason="session_limit")
    harness_router.record_cooling("cursor", time.time() + 3600, reason="session_limit")
    notifier = RecordingNotifier()

    result = _dispatch(ALL, notifier)

    assert result.cooled_until
    assert notifier.delivered == []


def test_a_pinned_provider_being_ineligible_is_not_an_outage(monkeypatch):
    binaries = _outage_world(monkeypatch)
    notifier = RecordingNotifier()

    result = _dispatch(binaries, notifier, provider="codex")

    assert "ineligible" in result.error
    assert notifier.delivered == []


def test_no_notifier_degrades_to_the_message_alone(monkeypatch):
    binaries = _outage_world(monkeypatch)
    result = _dispatch(binaries, None)
    assert "claude:" in result.error and "codex:" in result.error


def test_a_notifier_that_raises_never_breaks_dispatch(monkeypatch):
    binaries = _outage_world(monkeypatch)

    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("telegram down")

        def clear_dedupe(self, key):
            raise RuntimeError("journal unreadable")

    result = _dispatch(binaries, Broken())
    assert not result.ok and "claude:" in result.error


# ── (c) authentication setup is named, not copied ---------------------------------


def test_cursor_without_a_binding_is_an_authentication_setup_failure():
    import harness_lens_runner as hlr

    text = hlr.authentication_setup_failure("cursor")

    assert text.startswith("authentication setup incomplete for provider 'cursor'")
    assert "no authentication available inside its isolated harness home" in text
    assert "named Cursor subscription capability" in text  # what is missing
    assert "does not copy Cursor credentials" in text


@pytest.mark.parametrize(
    "provider,missing",
    [("claude", "CLAUDE_CODE_OAUTH_TOKEN"), ("codex", "login file")],
)
def test_each_provider_names_what_it_is_missing(provider, missing):
    import harness_lens_runner as hlr

    assert missing in hlr.authentication_setup_failure(provider)


def test_the_refusal_is_what_the_guard_check_returns(tmp_path):
    import harness_lens_runner as hlr

    sandbox = hlr.HarnessSandbox(
        provider="cursor", root=tmp_path, env_extra={}, command_wrapper=[],
        credential_read_denied=True, credential_binding_protected=True,
        user_config_write_denied=True, git_stash_denied=True,
        authentication_ready=False, unavailable_guards=(), review_write_confined=True,
    )
    assert hlr.refuse_if_guard_required(sandbox) == hlr.authentication_setup_failure("cursor")


def test_the_cursor_home_is_not_given_a_credential(tmp_path, monkeypatch):
    """The sandbox build must leave cursor unauthenticated rather than copy anything in."""
    import harness_lens_runner as hlr

    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    fake_login = tmp_path / "operator-home" / ".cursor" / "auth.json"
    fake_login.parent.mkdir(parents=True)
    fake_login.write_text("operator-login-state-sentinel")

    sandbox = hlr.HarnessSandbox.build("cursor", tmp_path / "scratch")

    assert sandbox.authentication_ready is False
    leaked = [
        p for p in sandbox.root.rglob("*")
        if p.is_file() and "operator-login-state-sentinel" in p.read_text(errors="ignore")
    ]
    assert leaked == []


# ── round 1 review: the incident's own shape, and the branches it named ----------


def _stale_then_failing_probe(provider="claude"):
    """Stale since day N, then every refresh probe fails: the 2026-10-27 state."""
    _record(provider, age_seconds=6 * 24 * 3600.0, used=97.0)
    harness_router.record_probe_unknown(
        provider, source="claude_rate_limit_event", detail="Not logged in",
        observed_at=time.time(),
    )


def test_a_failed_refresh_probe_does_not_make_stale_windows_current():
    """Red on the first head: the probe's NEW time made the six-day-old windows fresh."""
    _stale_then_failing_probe()

    reading = harness_router.usage_reading("claude")

    assert reading.state == harness_router.READING_UNKNOWN
    assert harness_router.live_headroom("claude") is None  # the two agree
    _value, source = harness_router.headroom_resolution()["claude"]
    assert source == harness_router.HEADROOM_SOURCE_UNKNOWN


def test_show_still_withholds_the_windows_when_the_probe_is_failing(capsys):
    import harness_usage

    _stale_then_failing_probe()
    assert harness_usage.main(["show"]) == 0
    row = json.loads(capsys.readouterr().out)["claude"]

    assert row["reading"]["state"] == "unknown"
    assert row["windows"] == [] and row["windows_withheld"] == 1
    assert row["headroom"] is None
    # the gate names the login failure; show carries it
    assert "Not logged in" in row["usage_gate"]["reason"]


def test_a_windowless_paced_provider_with_an_availability_probe_is_unknown():
    """live_headroom takes no opinion there either: availability is not budget."""
    now = time.time()
    harness_router._usage_path().write_text(json.dumps({"claude": {
        "observed_at": harness_router._iso_from_wall(now),
        "windows": [],
        "probe": {"status": "available", "observed_at": harness_router._iso_from_wall(now),
                  "source": "probe", "detail": None},
    }}))
    assert harness_router.live_headroom("claude") is None
    assert harness_router.usage_reading("claude").state == harness_router.READING_UNKNOWN


def test_ungated_show_does_not_present_the_neutral_value_as_measured(capsys, monkeypatch):
    import harness_usage

    monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "codex")  # claude not gated
    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)
    assert harness_usage.main(["show"]) == 0
    summary = json.loads(capsys.readouterr().out)["claude"]["usage_gate"]["summary"]
    assert "UNKNOWN headroom" in summary
    assert "on headroom 1.00" not in summary


def test_the_notification_is_one_provider_per_line_and_points_at_the_runbook():
    text = provider_outage.outage_notification(
        {"claude": "usage reading stale", "codex": "headroom=0.000",
         "cursor": "binary unavailable"}
    )
    lines = text.splitlines()
    assert lines[0].startswith("Dispatch is stopped")
    assert lines[1:4] == [
        "- claude: usage reading stale",
        "- codex: headroom=0.000",
        "- cursor: binary unavailable",
    ]
    assert "harness-headroom-restore.md" in lines[-1]


def test_the_codex_login_path_is_rendered_without_the_username(monkeypatch, tmp_path):
    import harness_lens_runner as hlr

    monkeypatch.setattr(hlr.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: tmp_path / ".codex" / "auth.json")
    text = hlr.authentication_setup_failure("codex")
    assert "~/.codex/auth.json" in text
    assert str(tmp_path) not in text


# the branches the first review found unwatched


def test_the_final_branch_names_all_three_and_alerts_once(monkeypatch):
    """No candidate, not cooled, not gated: the generic branch."""
    monkeypatch.setenv("APIS_USAGE_GATE", "off")
    harness_router.record_exhausted("codex", time.time() + 5 * 3600.0)
    binaries = {"claude": None, "codex": "/bin/codex", "cursor": None}
    notifier = RecordingNotifier()

    first = _dispatch(binaries, notifier)
    second = _dispatch(binaries, notifier)

    for result in (first, second):
        assert "claude: binary unavailable" in result.error
        assert "codex: headroom=0.000" in result.error
        assert "cursor: binary unavailable" in result.error
    assert len(notifier.delivered) == 1 and notifier.suppressed == 1


def test_a_cooling_window_beside_providers_that_will_not_clear_pages(monkeypatch):
    monkeypatch.setenv("APIS_USAGE_GATE", "off")
    harness_router.record_cooling("claude", time.time() + 3600, reason="session_limit")
    harness_router.record_exhausted("codex", time.time() + 5 * 3600.0)
    binaries = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None}
    notifier = RecordingNotifier()

    result = _dispatch(binaries, notifier)

    assert result.cooled_until  # still the self-clearing shape for the panel
    assert "other providers:" in result.error
    assert "cursor: binary unavailable" in result.error
    assert len(notifier.delivered) == 1


def test_a_pinned_provider_that_the_gate_refuses_is_not_an_outage():
    """Pin claude with a stale reading: it reaches the gate branch, and must not page."""
    _record("claude", age_seconds=6 * 24 * 3600.0, used=97.0)
    notifier = RecordingNotifier()

    result = _dispatch(ALL, notifier, provider="claude")

    assert "stale" in result.error
    assert "other providers:" not in result.error
    assert notifier.delivered == []


def test_the_weekly_pace_line_is_a_self_clearing_wait():
    now = time.time()
    harness_router.record_usage(
        "claude",
        [{"name": "weekly", "used_percent": 59.0,
          "resets_at": harness_router._iso_from_wall(now + 6 * 24 * 3600.0)}],
        observed_at=now,
    )
    gate = harness_router.usage_gate("claude")
    assert gate is not None and gate.code == harness_router.GATE_PACED
    assert provider_outage.is_self_clearing(["claude"]) is True
    assert provider_outage.is_self_clearing(["claude", "cursor"]) is False  # cursor is not waiting


def test_being_a_candidate_does_not_rearm_the_alert_only_completed_work_does(monkeypatch):
    """The router admits a provider on a neutral headroom that a login problem
    later refuses: such a run must not reset the outage and cause a second page."""
    binaries = _outage_world(monkeypatch)
    notifier = RecordingNotifier()
    _dispatch(binaries, notifier)
    assert len(notifier.delivered) == 1

    _record("claude", age_seconds=60.0, used=10.0)  # claude is a candidate again

    async def fails(selected):
        return skill_runner.SkillResult(
            "cicada", False, 1, "", "authentication required", provider=selected
        )

    asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada", fails, binaries=binaries, notifier=notifier
        )
    )
    assert notifier._open == {provider_outage.OUTAGE_DEDUPE_KEY}  # still open


def test_a_broken_clear_never_breaks_a_successful_dispatch(monkeypatch):
    binaries = _outage_world(monkeypatch)
    _record("claude", age_seconds=60.0, used=10.0)

    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("down")

        def clear_dedupe(self, key):
            raise RuntimeError("journal unreadable")

    async def ok(selected):
        return skill_runner.SkillResult("cicada", True, 0, "done", "", provider=selected)

    result = asyncio.run(
        skill_runner._run_provider_attempts(
            "cicada", ok, binaries=binaries, notifier=Broken()
        )
    )
    assert result.ok


def test_gathering_the_reasons_failing_keeps_the_original_refusal(monkeypatch):
    binaries = _outage_world(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("snapshot unreadable")

    monkeypatch.setattr(skill_runner, "provider_exclusion_reasons", boom)
    result = _dispatch(binaries, RecordingNotifier())
    assert not result.ok and result.error  # still a result, not an exception
