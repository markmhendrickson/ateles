"""Tests for cyphorhinus's poll-failure backoff.

The incident these guard against: a DNS/connection failure returns in ~2ms,
so without an explicit sleep between failed getUpdates calls the poll loop
spins at the speed of the error — the 2026-08-18 incident wrote ~500 log
lines/second for the length of the outage, filling a 926 GB disk. Log
rotation (test_logging_setup.py) bounds bytes on disk; this file locks the
throttle that keeps the loop itself from spinning in the first place.
"""

from __future__ import annotations

import pathlib
import sys
import urllib.error
from unittest.mock import MagicMock, patch

_CYPHORHINUS_DIR = pathlib.Path(__file__).resolve().parent
if str(_CYPHORHINUS_DIR) not in sys.path:
    sys.path.insert(0, str(_CYPHORHINUS_DIR))

import cyphorhinus  # noqa: E402


def setup_function(_fn):
    """Reset module-global failure count so tests don't leak state."""
    cyphorhinus._poll_failures = 0


def test_backoff_schedule_doubles_and_caps_at_poll_timeout():
    """delay = min(2**min(failures, 6), POLL_TIMEOUT) for consecutive failures."""
    with patch("cyphorhinus.time.sleep") as mock_sleep:
        expected = []
        for n in range(1, 9):
            cyphorhinus._sleep_after_failed_poll()
            expected.append(min(2 ** min(n, 6), cyphorhinus.POLL_TIMEOUT))
        actual = [call.args[0] for call in mock_sleep.call_args_list]
        assert actual == expected
        # The cap must actually bind — otherwise this test would pass even if
        # the exponent overflowed the timeout instead of being clamped to it.
        assert actual[-1] == cyphorhinus.POLL_TIMEOUT


def test_backoff_resets_to_zero_on_success():
    """A successful getUpdates call must reset _poll_failures, or a single
    transient blip would keep compounding the delay on every later error."""
    cyphorhinus._poll_failures = 5
    mock_response = MagicMock()
    mock_response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "result": []}'
    )
    with patch("cyphorhinus.urllib.request.urlopen", return_value=mock_response):
        cyphorhinus._tg_get_updates(offset=0)
    assert cyphorhinus._poll_failures == 0


def test_url_error_invokes_backoff():
    """The URLError branch (DNS/connection failure — the actual 2026-08-18
    trigger) must call the throttle, not just log and spin."""
    with patch(
        "cyphorhinus.urllib.request.urlopen",
        side_effect=urllib.error.URLError("no dns"),
    ):
        with patch("cyphorhinus._sleep_after_failed_poll") as mock_backoff:
            result = cyphorhinus._tg_get_updates(offset=0)
    assert result == []
    mock_backoff.assert_called_once()


def test_generic_exception_invokes_backoff():
    """The catch-all Exception branch must also throttle — this is the branch
    most likely to silently regress since it's easy to assume URLError alone
    covers all failure modes."""
    with patch(
        "cyphorhinus.urllib.request.urlopen",
        side_effect=ValueError("malformed response"),
    ):
        with patch("cyphorhinus._sleep_after_failed_poll") as mock_backoff:
            result = cyphorhinus._tg_get_updates(offset=0)
    assert result == []
    mock_backoff.assert_called_once()


def test_incident_shaped_repro_bounds_sleep_calls_under_sustained_failure():
    """Reproduces the incident shape: sustained failures must not busy-loop.
    Every failed poll sleeps, and the schedule is monotonically bounded by
    POLL_TIMEOUT — so a long outage degrades to one poll-interval cadence
    rather than a log-writing spin."""
    with patch(
        "cyphorhinus.urllib.request.urlopen",
        side_effect=urllib.error.URLError("outage"),
    ):
        with patch("cyphorhinus.time.sleep") as mock_sleep:
            for _ in range(20):
                cyphorhinus._tg_get_updates(offset=0)
    assert mock_sleep.call_count == 20
    assert all(
        0 < call.args[0] <= cyphorhinus.POLL_TIMEOUT
        for call in mock_sleep.call_args_list
    )
