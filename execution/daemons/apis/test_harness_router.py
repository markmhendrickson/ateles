"""Focused tests for quota-aware bundled-plan harness selection."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch, tmp_path):
    monkeypatch.delenv("APIS_HARNESS_PROVIDERS", raising=False)
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_MIN_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json")
    )
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _available() -> dict[str, str]:
    return {
        "claude": "/bin/claude",
        "codex": "/bin/codex",
        "cursor": "/bin/cursor-agent",
    }


def test_equal_headroom_round_robins_across_three_providers() -> None:
    first_choices = [
        harness_router.provider_candidates(_available(), now=100.0)[0]
        for _ in range(3)
    ]
    assert first_choices == ["claude", "codex", "cursor"]


def test_highest_headroom_receives_first_dispatch(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
    )
    candidates = harness_router.provider_candidates(_available(), now=100.0)
    assert candidates == ["codex", "cursor", "claude"]


def test_provider_at_or_below_minimum_is_held_out(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.05, "codex": 0.8, "cursor": 0.0}',
    )
    assert harness_router.provider_candidates(_available(), now=100.0) == ["codex"]


def test_usable_names_and_candidates_share_eligibility(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.05, "codex": 0.8, "cursor": 0.0}',
    )

    assert harness_router.usable_provider_names(_available(), now=100.0) == {
        "codex"
    }
    assert harness_router.provider_candidates(_available(), now=100.0) == ["codex"]


def test_provider_exclusion_reason_names_headroom_floor(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 1.0, "codex": 0.0, "cursor": 1.0}',
    )

    assert harness_router.provider_exclusion_reason(
        "codex", _available(), now=100.0
    ) == "headroom=0.000 is at or below minimum=0.050"


def test_provider_exclusion_reason_names_missing_binary() -> None:
    available = _available()
    available["codex"] = None

    assert harness_router.provider_exclusion_reason(
        "codex", available, now=100.0
    ) == "binary unavailable"


def test_provider_exclusion_reason_names_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("codex", now=100.0)

    # cool_down() always persists cooldown metadata (ateles#1257), so the
    # exclusion reason is now the enriched form, not the bare "cooling down"
    # string. The bare-message fallback is covered separately for the case
    # where no metadata exists at all (e.g. a legacy flat-float headroom
    # entry with no cooldown recorded).
    reason = harness_router.provider_exclusion_reason(
        "codex", _available(), now=101.0
    )
    assert reason is not None
    assert reason.startswith("cooling down (cooldown_reason=quota_error_default")


def test_capacity_cooldown_removes_provider_until_expiry(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("claude", now=100.0)
    assert "claude" not in harness_router.provider_candidates(
        _available(), now=129.9
    )
    assert "claude" in harness_router.provider_candidates(
        _available(), now=130.0
    )


def test_missing_binary_is_not_eligible() -> None:
    available = _available()
    available["codex"] = None
    assert "codex" not in harness_router.provider_candidates(available, now=100.0)


def test_operator_order_is_respected_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_PROVIDERS", "cursor,claude,cursor,unknown"
    )
    assert harness_router.configured_providers() == ["cursor", "claude"]
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "cursor"


def test_invalid_headroom_json_fails_open_to_equal_weights(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", "not-json")
    assert harness_router.configured_headroom() == {
        "claude": 1.0,
        "codex": 1.0,
        "cursor": 1.0,
    }


def test_headroom_file_can_be_refreshed_without_restart(
    monkeypatch, tmp_path
) -> None:
    headroom_file = tmp_path / "headroom.json"
    headroom_file.write_text(
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "codex"

    headroom_file.write_text(
        '{"claude": 0.9, "codex": 0.1, "cursor": 0.4}',
        encoding="utf-8",
    )
    harness_router.reset_state()
    assert harness_router.provider_candidates(_available(), now=101.0)[0] == "claude"


def test_malformed_headroom_file_falls_back_to_env(monkeypatch, tmp_path) -> None:
    headroom_file = tmp_path / "headroom.json"
    headroom_file.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
    )
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "codex"


# ── ateles#1257: provider cooldown honors the provider's own reset time ───────


def test_parsed_reset_time_survives_past_fixed_cooldown_default(
    monkeypatch, tmp_path
) -> None:
    """The actual reported bug: a fixed hourly cooldown resurrects a provider
    that quoted a multi-week reset. MUST be verified red against unmodified
    cool_down() before committing (git stash the implementation, run pytest,
    confirm this fails, restore) — see PR body for the verification note."""
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "3600")
    harness_router.cool_down(
        "claude",
        reason_text="usage limit reached, resets in 3 weeks",
        now=100.0,
    )
    # Two hours later the fixed 3600s (1h) cooldown would have expired, but
    # the parsed 3-week reset must still exclude the provider.
    assert "claude" not in harness_router.provider_candidates(
        _available(), now=100.0 + 2 * 3600
    )


class TestParseResetHint:
    """Pure-function unit tests for parse_reset_hint()."""

    def test_iso8601_absolute_date(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "quota exceeded, resets at 2026-10-16T12:00:00Z", now_wall=now
        )
        assert result is not None
        assert result > now

    def test_yyyy_mm_dd_form(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "usage limit reached, resets on 2026-10-01", now_wall=now
        )
        assert result is not None
        assert result > now

    def test_relative_duration_hour(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "rate limit reached, resets in 5 hours", now_wall=now
        )
        assert result == now + 5 * 3600

    def test_relative_duration_day(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "quota exceeded, resets in 2 days", now_wall=now
        )
        assert result == now + 2 * 86400

    def test_relative_duration_week(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "usage limit reached, resets in 3 weeks", now_wall=now
        )
        assert result == now + 3 * 604800

    def test_singular_boundary_one_week(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "resets in 1 week", now_wall=now
        )
        assert result == now + 604800

    def test_plural_boundary_two_weeks(self) -> None:
        now = 1_700_000_000.0
        result = harness_router.parse_reset_hint(
            "resets in 2 weeks", now_wall=now
        )
        assert result == now + 2 * 604800

    def test_unparseable_garbage_returns_none(self) -> None:
        assert (
            harness_router.parse_reset_hint(
                "something went wrong, try again later", now_wall=1_700_000_000.0
            )
            is None
        )

    def test_malformed_date_in_recognized_clause_returns_none_no_exception(
        self,
    ) -> None:
        # "resets at" is matched but the date is not a valid calendar date.
        result = harness_router.parse_reset_hint(
            "quota exceeded, resets at 2026-99-99T99:99:99Z",
            now_wall=1_700_000_000.0,
        )
        assert result is None

    def test_past_dated_match_returns_none(self) -> None:
        now = 1_700_000_000.0
        past_iso = harness_router._iso_from_wall(now - 3600)
        result = harness_router.parse_reset_hint(
            f"resets at {past_iso}", now_wall=now
        )
        assert result is None

    def test_exact_boundary_equal_to_now_returns_none(self) -> None:
        now = 1_700_000_000.0
        now_iso = harness_router._iso_from_wall(now)
        result = harness_router.parse_reset_hint(f"resets at {now_iso}", now_wall=now)
        assert result is None

    def test_empty_text_returns_none(self) -> None:
        assert harness_router.parse_reset_hint("", now_wall=1_700_000_000.0) is None

    def test_absurdly_large_relative_duration_returns_none_no_overflow(self) -> None:
        """Regression: an unbounded \\d+ in the "resets in N weeks" regex let
        a huge or adversarial N reach datetime.fromtimestamp() downstream in
        cool_down()/_iso_from_wall() and raise OverflowError/ValueError
        instead of degrading to the fixed-duration fallback. No real quota
        reset is ever a year+ out, so this must return None, never raise."""
        now = 1_700_000_000.0
        assert (
            harness_router.parse_reset_hint(
                "resets in 999999999999999999999 weeks", now_wall=now
            )
            is None
        )
        # A smaller-looking but still year+-out value that would overflow
        # Python's datetime year range (max 9999) if it reached
        # datetime.fromtimestamp() unchecked.
        assert (
            harness_router.parse_reset_hint(
                "resets in 600000 weeks", now_wall=now
            )
            is None
        )

    def test_absurdly_large_relative_duration_cool_down_does_not_raise(
        self, monkeypatch, tmp_path
    ) -> None:
        """Same regression, exercised through cool_down(): must fall back to
        the fixed-duration default rather than crashing the caller — never
        fail open (no cooldown at all) and never crash instead of cooling
        down."""
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "3600")
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 999999999999999999999 weeks",
            now=100.0,
        )
        assert "claude" in harness_router.cooling_providers(now=100.0 + 3599)
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_default"


class TestCoolDownBranches:
    """cool_down() branch coverage: parsed-success, parse-failure fallback,
    backward-compat no-reason_text, and distinct log-line assertions."""

    def test_parsed_success_updates_memory_and_file(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        assert "claude" in harness_router.cooling_providers(now=100.0 + 3600)
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_parsed"
        assert on_disk["claude"]["cooldown_until"] is not None

    def test_reset_clause_found_regardless_of_which_joined_segment_carries_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """Regression: skill_runner.py's call site joins result.error,
        result.stderr, and (conditionally) result.stdout into one reason_text
        blob — mirroring how _provider_failure_kind scans them jointly — so
        the reset-time clause is found no matter which one of the three
        segments carries it. An earlier `or`-chained version silently
        dropped whichever segments didn't win the `or`, so this pins the
        join contract at the cool_down() call boundary."""
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        error_text = "claude launch failed: usage limit reached, resets in 3 weeks"
        stderr_text = "unrelated deprecation warning, nothing to do with quota"
        joined = " ".join(t for t in (error_text, stderr_text, "") if t)
        harness_router.cool_down("claude", reason_text=joined, now=100.0)
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_parsed"

    def test_parse_failure_falls_back_to_default_duration(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
        # Recognized capacity error (skill_runner would classify this as
        # "capacity"), but no parseable reset time.
        harness_router.cool_down(
            "claude", reason_text="quota exceeded, try again later", now=100.0
        )
        assert "claude" not in harness_router.provider_candidates(
            _available(), now=129.9
        )
        assert "claude" in harness_router.provider_candidates(
            _available(), now=130.0
        )
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_default"

    def test_no_reason_text_is_backward_compatible(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
        harness_router.cool_down("claude", now=100.0)
        assert "claude" not in harness_router.provider_candidates(
            _available(), now=129.9
        )
        assert "claude" in harness_router.provider_candidates(
            _available(), now=130.0
        )
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_default"

    def test_parsed_success_and_parse_failure_log_distinct_messages(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        with caplog.at_level("INFO", logger="apis.harness_router"):
            harness_router.cool_down(
                "claude",
                reason_text="usage limit reached, resets in 3 weeks",
                now=100.0,
            )
        parsed_messages = [
            r.message for r in caplog.records if "parsed quota reset time" in r.message
        ]
        assert len(parsed_messages) == 1
        caplog.clear()

        with caplog.at_level("INFO", logger="apis.harness_router"):
            harness_router.cool_down(
                "codex", reason_text="quota exceeded, unparseable", now=100.0
            )
        fallback_messages = [
            r.message
            for r in caplog.records
            if "no parseable reset time" in r.message
        ]
        assert len(fallback_messages) == 1
        # The two log lines are distinct and greppable.
        assert parsed_messages[0] != fallback_messages[0]

    def test_log_lines_do_not_echo_unbounded_raw_text(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        huge_secret_marker = "SECRET_TOKEN_" + ("x" * 5000)
        raw_text = f"quota exceeded, unparseable garbage {huge_secret_marker}"
        with caplog.at_level("INFO", logger="apis.harness_router"):
            harness_router.cool_down("claude", reason_text=raw_text, now=100.0)
        for record in caplog.records:
            assert len(record.message) < len(raw_text)
            assert huge_secret_marker not in record.message


class TestHeadroomFilePersistenceContract:
    """Read-merge-write preserves other providers; atomic write; write
    failure does not raise."""

    def test_merge_preserves_other_providers_flat_and_object(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps(
                {
                    "codex": 0.42,
                    "cursor": {
                        "headroom": 0.7,
                        "cooldown_until": None,
                        "cooldown_reason": "manual",
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        # Untouched providers preserved byte-for-shape.
        assert on_disk["codex"] == 0.42
        assert on_disk["cursor"] == {
            "headroom": 0.7,
            "cooldown_until": None,
            "cooldown_reason": "manual",
        }
        assert on_disk["claude"]["cooldown_reason"] == "quota_error_parsed"

    def test_direct_file_read_after_cool_down_matches_documented_shape(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        entry = on_disk["claude"]
        assert set(entry.keys()) == {"headroom", "cooldown_until", "cooldown_reason"}
        assert entry["cooldown_reason"] == "quota_error_parsed"
        assert isinstance(entry["cooldown_until"], str)

    def test_atomic_write_mid_op_failure_leaves_no_torn_json(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps({"codex": 0.5}), encoding="utf-8"
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))

        def _boom(*_args, **_kwargs):
            raise OSError("simulated os.replace failure")

        monkeypatch.setattr(harness_router.os, "replace", _boom)
        # Must not raise — write failure is caught and logged.
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        # Original file is untouched (still valid JSON, still the old value).
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk == {"codex": 0.5}
        # In-memory cooldown still applied for this process.
        assert "claude" in harness_router.cooling_providers(now=100.0 + 3600)

    def test_write_oserror_does_not_raise_and_memory_cooldown_holds(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))

        def _boom(*_args, **_kwargs):
            raise OSError("simulated mkstemp failure")

        monkeypatch.setattr(harness_router.tempfile, "mkstemp", _boom)
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        assert "claude" in harness_router.cooling_providers(now=100.0 + 3600)


class TestConfiguredHeadroomReadPath:
    def test_legacy_flat_float_regression_guard(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 0.15}')
        result = harness_router.configured_headroom()
        assert result["claude"] == 0.15
        assert harness_router.configured_cooldown_metadata() == {}

    def test_object_shape_parses_headroom_and_metadata(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {
                        "headroom": 0.3,
                        "cooldown_until": "2099-01-01T00:00:00+00:00",
                        "cooldown_reason": "quota_error_parsed",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        result = harness_router.configured_headroom()
        assert result["claude"] == 0.3
        metadata = harness_router.configured_cooldown_metadata()
        assert metadata["claude"]["cooldown_reason"] == "quota_error_parsed"
        assert metadata["claude"]["cooldown_until"] == "2099-01-01T00:00:00+00:00"

    def test_malformed_object_entry_falls_back_to_1_for_that_key_only(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {"headroom": "not-a-number"},
                    "codex": 0.6,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        result = harness_router.configured_headroom()
        assert result["claude"] == 1.0
        assert result["codex"] == 0.6

    def test_auto_restoration_on_read_both_value_and_on_disk(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        past_iso = harness_router._iso_from_wall(time.time() - 3600)
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {
                        "headroom": 1.0,
                        "cooldown_until": past_iso,
                        "cooldown_reason": "quota_error_parsed",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        result = harness_router.configured_headroom()
        assert result["claude"] == 1.0
        assert "claude" not in harness_router.configured_cooldown_metadata()
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_until"] is None
        assert on_disk["claude"]["cooldown_reason"] is None

    def test_restoration_log_line_fires_exactly_once_across_repeated_reads(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        past_iso = harness_router._iso_from_wall(time.time() - 3600)
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {
                        "headroom": 1.0,
                        "cooldown_until": past_iso,
                        "cooldown_reason": "quota_error_parsed",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        with caplog.at_level("INFO", logger="apis.harness_router"):
            harness_router.configured_headroom()
            harness_router.configured_headroom()
            harness_router.configured_headroom()
        restore_messages = [
            r.message for r in caplog.records if "restored to headroom" in r.message
        ]
        assert len(restore_messages) == 1

    def test_manual_cooldown_never_auto_restored(self, monkeypatch, tmp_path) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {
                        "headroom": 0.0,
                        "cooldown_until": None,
                        "cooldown_reason": "manual",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        result = harness_router.configured_headroom()
        assert result["claude"] == 0.0
        metadata = harness_router.configured_cooldown_metadata()
        assert metadata["claude"]["cooldown_reason"] == "manual"
        # File is untouched.
        on_disk = json.loads(headroom_file.read_text(encoding="utf-8"))
        assert on_disk["claude"]["cooldown_reason"] == "manual"


class TestProviderExclusionReasonEnrichment:
    def test_enriched_message_present_when_metadata_exists(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        harness_router.cool_down(
            "claude",
            reason_text="usage limit reached, resets in 3 weeks",
            now=100.0,
        )
        reason = harness_router.provider_exclusion_reason(
            "claude", _available(), now=100.0 + 3600
        )
        assert reason is not None
        assert "cooldown_reason=quota_error_parsed" in reason
        assert "cooldown_until=" in reason
        assert "remaining" in reason

    def test_bare_message_fallback_when_no_metadata(self, monkeypatch) -> None:
        # Seed the in-memory monotonic cooldown directly, bypassing
        # cool_down() entirely, so no cooldown metadata is ever persisted to
        # the headroom file. This is the "no metadata exists" case the bare
        # "cooling down" message must still cover (e.g. process state from
        # before this feature existed, or a headroom file with no cooldown
        # fields at all).
        harness_router._cooldown_until["codex"] = 130.0
        reason = harness_router.provider_exclusion_reason(
            "codex", _available(), now=101.0
        )
        assert reason == "cooling down"

    def test_bare_message_with_manual_reason_and_no_cooldown_until(
        self, monkeypatch, tmp_path
    ) -> None:
        headroom_file = tmp_path / "headroom.json"
        headroom_file.write_text(
            json.dumps(
                {
                    "claude": {
                        "headroom": 1.0,
                        "cooldown_until": None,
                        "cooldown_reason": "manual",
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
        # Force the in-memory cooldown clock without going through cool_down()
        # (manual cooldowns are set by an operator editing the file directly).
        harness_router._cooldown_until["claude"] = 200.0
        harness_router.configured_headroom()  # populate metadata cache
        reason = harness_router.provider_exclusion_reason(
            "claude", _available(), now=100.0
        )
        assert reason == "cooling down"


def test_effect_level_selection_excludes_stale_headroom_with_parsed_cooldown(
    monkeypatch, tmp_path
) -> None:
    """The actual reported bug, at the selection layer: the headroom file
    shows the exhausted provider at stale headroom=1.0 (as it would be
    written by an out-of-date monitor) plus a 3-week parsed cooldown. The
    real candidate/selection function — not just _provider_exclusion_reason
    in isolation — must exclude it."""
    headroom_file = tmp_path / "headroom.json"
    future_iso = harness_router._iso_from_wall(time.time() + 3 * 604800)
    headroom_file.write_text(
        json.dumps(
            {
                "claude": {
                    "headroom": 1.0,
                    "cooldown_until": future_iso,
                    "cooldown_reason": "quota_error_parsed",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
    # Seed the in-memory monotonic cooldown too, as cool_down() would have
    # when the failure originally happened (the file alone does not drive
    # _cooldown_until — that is process-local by design; this simulates the
    # same process that wrote the file).
    harness_router.cool_down(
        "claude",
        reason_text="usage limit reached, resets in 3 weeks",
        now=100.0,
    )
    candidates = harness_router.provider_candidates(_available(), now=100.0 + 2 * 3600)
    assert "claude" not in candidates
    assert harness_router.usable_provider_names(
        _available(), now=100.0 + 2 * 3600
    ) == {"codex", "cursor"}
