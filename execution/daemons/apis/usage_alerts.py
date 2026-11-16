"""Operator alerts when a provider's usage window crosses 50% and 75%.

Provider spend governance, task ent_e5259618cae6cafd4be120e0. On 2026-11-15 a
provider's monthly allowance burned in about seven hours with no warning. This
module warns while there is still room: when a usage window recorded in
``harness-usage.json`` (written by ``execution/scripts/harness_usage.py
refresh``, read through ``harness_router.usage_windows``) first reaches 50%
used, and again at 75%, one alert per threshold per window goes to the
operator through the existing notifier (``lib.notify.Notifier``).

Called from two places with the provider concerned: ``skill_runner.
_run_skill_once`` for every dispatch attempt (the one path daemon dispatches and
interactive ``dispatch_role`` dispatches share), and wherever a usage reading
is written (``usage_probe.refresh_usage_if_stale``, which ``harness_usage.py
refresh`` and the dispatcher's automatic refresh both use, and
``harness_usage.py usage``), so a crossing is alerted even when no dispatch
follows it.

Once per threshold per window, and only once it reached the operator. The
notifier's persistent dedupe journal (ateles#1127) records "this condition was
reported"; the key names the provider, the window and that window's reset time,
so the next window (a new ``resets_at``) alerts afresh. The journal is read
first, so a check inside an already-reported window costs one small file read.
No separate state store is added.

* Every crossed threshold the window has not yet delivered is sent, in one
  alert naming all of them: a first reading of 80% says "crossed 50% and 75%".
* A threshold counts as delivered only when ``Notifier.send`` reports the
  transport delivered it. A held notice (silence window: not sent at all, so
  it is not queued twice) or a failed or transport-less send leaves it eligible
  on the next check; ``send``'s own pre-delivery journal entry is released
  (``Notifier.release_dedupe``) so the retry is not suppressed. The same
  process waits ``RETRY_BACKOFF_SECONDS`` before retrying it.
* The re-check and the send run under a non-blocking ``flock`` on a sidecar of
  the dedupe journal (``held_view.file_lock``, the notifier's own cross-process
  lock pattern). A second process that finds it held skips this check: the
  holder is alerting, and if it fails the key stays eligible. If ``flock``
  itself errors the lock fails open, as every notifier lock does.

Fail-open, always: a missing or malformed usage file, an unreadable ledger, a
notifier that cannot be built or a send that raises is logged and swallowed.
``check_usage_thresholds`` never raises and never blocks a dispatch.

Top callers come from the tier ledger's attribution fields
(``model_tiering.top_callers``) over the window's span. The span is known for
the window names the usage probe writes (``five_hour``, ``weekly``/
``weekly_all``/``seven_day``, ``window_<N>m``, ``monthly``); for any other name
the last seven days are used and the message says so.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import harness_router
import model_tiering

log = logging.getLogger("apis.usage_alerts")

THRESHOLDS: tuple[int, ...] = (50, 75)
DEDUPE_PREFIX = "provider-usage-threshold"
_DEFAULT_SPAN = timedelta(days=7)
_NAMED_SPANS: dict[str, timedelta] = {
    "five_hour": timedelta(hours=5),
    "session": timedelta(hours=5),
    "weekly": timedelta(days=7),
    "weekly_all": timedelta(days=7),
    "seven_day": timedelta(days=7),
    "monthly": timedelta(days=30),
}
_MINUTES_NAME = re.compile(r"^window_(\d+)m$")

# After a send that did not deliver, this process waits this long before
# retrying the same alert, so a host with no working transport does not repeat
# the whole send path on every dispatch. Process-local on purpose: nothing new
# is persisted, a restarted or separate process retries at once, and the
# dedupe journal stays the only record of what was delivered.
RETRY_BACKOFF_SECONDS = 600.0
_retry_after: dict[str, float] = {}


def window_span(name: str) -> tuple[timedelta, bool]:
    """``(span, known)`` for a usage window name."""
    key = (name or "").strip().lower()
    if key in _NAMED_SPANS:
        return _NAMED_SPANS[key], True
    match = _MINUTES_NAME.match(key)
    if match and int(match.group(1)) > 0:
        return timedelta(minutes=int(match.group(1))), True
    return _DEFAULT_SPAN, False


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percent(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value == value and value >= 0 else None  # drop NaN/negatives


def dedupe_key(provider: str, window: str, resets_at: str | None, threshold: int) -> str:
    return f"{DEDUPE_PREFIX}:{provider}:{window}:{resets_at or 'no-reset'}:{threshold}"


def _describe_callers(callers: list[tuple[str | None, str | None, int]]) -> str:
    if not callers:
        return "no attributed dispatches in the ledger"
    parts = []
    for kind, name, count in callers:
        label = f"{kind or 'unknown kind'} {name or '(caller not recorded)'}"
        parts.append(f"{label} ({count})")
    return ", ".join(parts)


def _threshold_text(thresholds: list[int]) -> str:
    labels = [f"{t}%" for t in sorted(thresholds)]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def build_message(
    *,
    provider: str,
    window: str,
    used_percent: float,
    thresholds: list[int],
    resets_at: datetime | None,
    callers: list[tuple[str | None, str | None, int]],
    span_known: bool,
) -> str:
    reset_text = resets_at.isoformat(timespec="minutes") if resets_at else "unknown"
    span_note = "" if span_known else " (window length unknown; last 7 days)"
    return (
        f"Provider usage: {provider} {window} window is {used_percent:g}% used "
        f"(crossed {_threshold_text(thresholds)}). Resets {reset_text}. "
        f"Top callers by dispatch count{span_note}: {_describe_callers(callers)}."
    )


def _default_journal_factory():
    from lib.notify import Notifier

    return Notifier()


def _default_notifier_factory():
    from lib.notify import Notifier

    return Notifier.from_neotoma()


def _journal_lock_path(notifier) -> Path:
    """The sidecar lock beside the notifier's dedupe journal."""
    journal = None
    getter = getattr(notifier, "dedupe_journal_path", None)
    if callable(getter):
        try:
            journal = Path(getter())
        except Exception:  # noqa: BLE001
            journal = None
    if journal is None:
        # Same default the notifier itself uses.
        journal = Path(
            os.environ.get("ATELES_DEDUPE_JOURNAL_PATH", "").strip()
            or Path(tempfile.gettempdir()) / "ateles-notify-dedupe.json"
        )
    return journal.with_name(journal.name + ".usage-alerts.lock")


def _silenced(notifier) -> bool:
    probe = getattr(notifier, "in_silence_window", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001
        return False


def check_usage_thresholds(
    provider: str,
    *,
    notifier=None,
    notifier_factory: Callable[[], object] | None = None,
    journal_factory: Callable[[], object] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Alert every crossed, undelivered threshold per window for ``provider``.

    Returns the dedupe keys this call recorded as delivered (empty when nothing
    new crossed, the alert was held or failed, or anything failed).
    ``notifier`` is the dispatching daemon's own; when it is ``None`` (an
    interactive ``dispatch_role`` run or a usage refresh), the dedupe journal is
    checked through a plain ``Notifier()`` (``journal_factory``; no network),
    and the sending notifier is built by ``notifier_factory`` (default
    ``Notifier.from_neotoma``) only once a threshold is pending. Never raises.
    """
    from lib.notify import held_view

    delivered_keys: list[str] = []
    journal = None
    try:
        current = now or datetime.now(timezone.utc)
        windows = harness_router.usage_windows(provider)
        for window in windows:
            name = str(window.get("name") or "").strip() or "unnamed"
            used = _percent(window.get("used_percent"))
            if used is None:
                continue
            resets_raw = window.get("resets_at")
            resets_at = _parse_iso(resets_raw)
            if resets_at is not None and resets_at <= current:
                continue  # that window has reset; its reading no longer applies
            reset_key = resets_at.isoformat() if resets_at else None
            crossed = [t for t in THRESHOLDS if used >= t]
            if notifier is None and journal is None:
                journal = (journal_factory or _default_journal_factory)()
            probe = notifier if notifier is not None else journal
            if resets_at is None:
                # No reset time means no per-window key: the key is reused
                # across windows, so a threshold usage has fallen back under
                # (the window rolled over) is cleared and can alert again.
                for below in (t for t in THRESHOLDS if used < t):
                    _clear_if_reported(probe, dedupe_key(provider, name, None, below))
            if not crossed:
                continue
            keys = {t: dedupe_key(provider, name, reset_key, t) for t in crossed}
            if not [t for t in crossed if not _already_reported(probe, keys[t])]:
                continue
            retry_key = keys[max(crossed)]
            if _retry_after.get(retry_key, 0.0) > time.monotonic():
                continue  # this process just failed to deliver it; back off
            with held_view.file_lock(_journal_lock_path(probe), blocking=False) as held:
                if not held:
                    log.info(
                        f"[apis] usage alert for {provider} {name}: another process "
                        "is alerting it now; skipping this check"
                    )
                    continue
                # Re-read under the lock: another process may have delivered.
                pending = [t for t in crossed if not _already_reported(probe, keys[t])]
                if not pending:
                    continue
                if notifier is None:
                    notifier = (notifier_factory or _default_notifier_factory)()
                if _silenced(notifier):
                    log.info(
                        f"[apis] usage alert for {provider} {name} held (silence "
                        "window); it stays eligible on the next check"
                    )
                    continue
                span, span_known = window_span(name)
                window_start = (resets_at - span) if resets_at else (current - span)
                try:
                    callers = model_tiering.top_callers(
                        since=window_start, provider=provider, limit=3
                    )
                except Exception as exc:  # noqa: BLE001 — the alert still goes out
                    log.warning(
                        f"[apis] usage alert: ledger unreadable ({exc}); sending without callers"
                    )
                    callers = []
                message = build_message(
                    provider=provider, window=name, used_percent=used,
                    thresholds=pending, resets_at=resets_at, callers=callers,
                    span_known=span_known,
                )
                send_key = keys[max(pending)]
                delivered = False
                try:
                    from lib.notify import Priority

                    delivered = bool(notifier.send(
                        message, priority=Priority.WARN, handler="apis",
                        dedupe_key=send_key,
                    ))
                except Exception as exc:  # noqa: BLE001 — retried next check
                    log.warning(f"[apis] usage alert send raised (non-fatal): {exc}")
                if delivered:
                    _retry_after.pop(retry_key, None)
                    for t in pending:
                        _mark_reported(notifier, keys[t])
                    delivered_keys.extend(keys[t] for t in pending)
                else:
                    # send() journals the key before routing; undo that so an
                    # undelivered alert is retried, not suppressed.
                    _release(notifier, send_key)
                    _retry_after[retry_key] = time.monotonic() + RETRY_BACKOFF_SECONDS
                log.info(
                    "[apis] usage alert "
                    + ("delivered" if delivered
                       else "NOT delivered (failed or no transport); retried on the next check")
                    + f": {message}"
                )
    except Exception as exc:  # noqa: BLE001 — never block or fail a dispatch
        log.warning(f"[apis] usage threshold check failed (non-fatal): {exc}")
    return delivered_keys


def check_after_usage_write(provider: str) -> None:
    """Hook for code that has just written a usage reading. Never raises."""
    try:
        check_usage_thresholds(provider)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[apis] usage threshold check after refresh failed (non-fatal): {exc}")


def _already_reported(notifier, key: str) -> bool:
    """True when the notifier's dedupe journal holds ``key``; False if unknown."""
    if notifier is None:
        return False
    try:
        return bool(notifier.is_dedupe_open(key))
    except Exception:  # noqa: BLE001
        return False


def _clear_if_reported(notifier, key: str) -> None:
    if notifier is None or not _already_reported(notifier, key):
        return
    try:
        notifier.clear_dedupe(key)
    except Exception:  # noqa: BLE001
        pass


def _mark_reported(notifier, key: str) -> None:
    marker = getattr(notifier, "_mark_dedupe_notified", None)
    if callable(marker):
        try:
            marker(key, "apis")
        except Exception:  # noqa: BLE001
            pass


def _release(notifier, key: str) -> None:
    releaser = getattr(notifier, "release_dedupe", None)
    if callable(releaser):
        try:
            releaser(key)
        except Exception:  # noqa: BLE001
            pass
