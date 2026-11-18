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
reported"; the key names the provider, the window and that window's reset time.
A reset correction while the old window is active migrates that key; a window
whose previous reset has passed alerts afresh. The journal is read
first, so a check inside an already-reported window costs one small file read.
No separate state store is added.

* Every crossed threshold the window has not yet delivered is sent, in one
  alert naming all of them: a first reading of 80% says "crossed 50% and 75%".
* A threshold counts as delivered only when the transport delivers it. During
  silence, ``Notifier.send`` persists the notice in its existing held queue;
  the journal records every threshold it covers as held, then delivered when
  that queued notice drains. Failed sends remain eligible for a later check.
* The handoff thread owns a non-blocking ``flock`` on a sidecar of the dedupe
  journal through transport completion and threshold marking. A second check
  that cannot acquire it skips the send; an unreadable lock also skips it.

Fail-open, always: a missing usage file, an unreadable ledger, a notifier that
cannot be built or a send that raises is logged and swallowed. An existing
malformed usage file raises one keyed diagnostic notice per source per day.
``check_usage_thresholds`` never raises and never blocks a dispatch.

Top callers come from the tier ledger's attribution fields
(``model_tiering.top_callers``) over the window's span. The span is known for
the window names the usage probe writes (``five_hour``, ``weekly``/
``weekly_all``/``seven_day``, ``window_<N>m``, ``monthly``); for any other name
the last seven days are used and the message says so.
"""

from __future__ import annotations

import logging
import hashlib
import math
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import harness_router
import model_tiering
from lib.notify import held_view

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
RETRY_BACKOFF_SECONDS = 5.0
ALERT_SEND_TIMEOUT_SECONDS = held_view.ALERT_SEND_TIMEOUT_SECONDS
_retry_after: dict[str, float] = {}


def _canonical_window_name(name: object) -> str:
    if not isinstance(name, str):
        return "unavailable"
    cleaned = name.strip().casefold()
    if not 0 < len(cleaned) <= 64:
        return "unavailable"
    if cleaned in ("weekly", "seven_day"):
        return "weekly_all"
    if cleaned == "session":
        return "five_hour"
    match = _MINUTES_NAME.match(cleaned)
    if match and len(match.group(1)) <= 6:
        minutes = int(match.group(1))
        if abs(minutes - 10080) <= 60:
            return "weekly_all"
        if abs(minutes - 300) <= 15:
            return "five_hour"
    return cleaned


def window_span(name: str) -> tuple[timedelta, bool]:
    """``(span, known)`` for a usage window name."""
    key = _canonical_window_name(name)
    if key in _NAMED_SPANS:
        return _NAMED_SPANS[key], True
    match = _MINUTES_NAME.match(key)
    if match and len(match.group(1)) <= 6:
        minutes = int(match.group(1))
        if 1 <= minutes <= 525600:
            return timedelta(minutes=minutes), True
    return _DEFAULT_SPAN, False


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    try:
        normalized = aware.astimezone(timezone.utc)
        return normalized if 2000 <= normalized.year <= 2100 else None
    except (ValueError, OverflowError, OSError):
        return None


def _percent(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def dedupe_key(provider: str, window: str, resets_at: str | None, threshold: int) -> str:
    reset = _parse_iso(resets_at)
    normalized = reset.isoformat(timespec="seconds") if reset else "no-reset"
    return f"{DEDUPE_PREFIX}:{provider}:{_canonical_window_name(window)}:{normalized}:{threshold}"


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
    attribution_unavailable: bool = False,
    observed_at: datetime | None = None,
    reading_fresh: bool = True,
) -> str:
    reset_text = resets_at.isoformat(timespec="minutes") if resets_at else "unknown"
    span_note = "" if span_known else " (window length unknown; last 7 days)"
    caller_text = (
        "attribution unavailable (ledger could not be read)"
        if attribution_unavailable else _describe_callers(callers)
    )
    usage_text = (
        f"is {used_percent:g}% used" if reading_fresh else
        f"was last observed at {observed_at.isoformat(timespec='minutes') if observed_at else 'unknown time'} as {used_percent:g}% used"
    )
    return (
        f"Provider usage: {provider} {window} window {usage_text} "
        f"(crossed {_threshold_text(thresholds)}). Resets {reset_text}. "
        f"Top callers by dispatch count{span_note}: "
        f"{caller_text}."
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

    delivered_keys: list[str] = []
    journal = None
    try:
        current = now or datetime.now(timezone.utc)
        source = harness_router._usage_path()
        problem = harness_router.usage_snapshot_problem(source)
        if problem is not None:
            _notice_unreadable_snapshot(
                source, problem, current, notifier=notifier,
                notifier_factory=notifier_factory,
                journal_factory=journal_factory,
            )
            if harness_router.usage_snapshot_problem(source, provider=provider) is not None:
                return []
        try:
            reading = harness_router.usage_reading(provider, now_wall=current.timestamp())
        except (ValueError, OverflowError, OSError) as exc:
            log.warning("[apis] usage reading freshness unavailable: %s", exc)
            reading = None
        reading_fresh = reading is not None and reading.state == harness_router.READING_FRESH
        observed_at = None
        if reading is not None and reading.observed_at is not None:
            try:
                observed_at = datetime.fromtimestamp(reading.observed_at, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        windows = harness_router.usage_windows(provider)
        for window in windows:
            name = _canonical_window_name(window.get("name"))
            used = _percent(window.get("used_percent"))
            if used is None:
                continue
            resets_raw = window.get("resets_at")
            resets_at = _parse_iso(resets_raw)
            if resets_at is not None and not current - timedelta(days=366) <= resets_at <= current + timedelta(days=366):
                resets_at = None
            if resets_at is not None and resets_at <= current:
                continue  # that window has reset; its reading no longer applies
            reset_key = resets_at.isoformat(timespec="seconds") if resets_at else None
            current_instant = _parse_iso(reset_key)
            crossed = [t for t in THRESHOLDS if used >= t]
            if notifier is None and journal is None:
                journal = (journal_factory or _default_journal_factory)()
            probe = notifier if notifier is not None else journal
            if resets_at is None:
                # No reset time means no per-window key: the key is reused
                # across windows, so a threshold usage has fallen back under
                # (the window rolled over) is cleared and can alert again.
                with held_view.file_lock(
                    _journal_lock_path(probe), blocking=False, fail_open=False,
                ) as held:
                    if not held:
                        continue
                    for below in (t for t in THRESHOLDS if used < t):
                        _clear_if_reported(probe, dedupe_key(provider, name, None, below))
            if not crossed:
                continue
            keys = {t: dedupe_key(provider, name, reset_key, t) for t in crossed}
            # Even a reported destination must reconcile an older spelling:
            # a reset-time correction can arrive after a previous check.
            retry_key = keys[max(crossed)]
            if _retry_after.get(retry_key, 0.0) > time.monotonic():
                continue  # this process just failed to deliver it; back off
            result_keys: list[str] = []

            def handoff(
                *, probe=probe, keys=keys, crossed=crossed, name=name,
                resets_at=resets_at, reset_key=reset_key,
                current_instant=current_instant, used=used, current=current,
                retry_key=retry_key, notifier=notifier,
            ) -> None:
                # This thread owns the journal-sidecar lock until every key
                # covered by the transport is marked. The caller's join may
                # time out without releasing ownership of an in-flight send.
                with held_view.file_lock(
                    _journal_lock_path(probe), blocking=False, fail_open=False,
                ) as held:
                    if not held:
                        return
                    try:
                        provider_prefix = f"{DEDUPE_PREFIX}:{provider}:"
                        for old_key in probe.dedupe_keys_with_prefix(provider_prefix):
                            old_name, name_separator, remainder = old_key[len(provider_prefix):].partition(":")
                            if not name_separator or _canonical_window_name(old_name) != name:
                                continue
                            old_reset, separator, threshold_text = remainder.rpartition(":")
                            if not separator or not threshold_text.isdigit():
                                continue
                            old_instant = _parse_iso(old_reset)
                            canonical_key = dedupe_key(provider, name, reset_key, int(threshold_text))
                            if old_key == canonical_key:
                                continue
                            if old_instant is not None and current >= old_instant:
                                probe.clear_dedupe(old_key)
                            else:
                                migrator = getattr(probe, "migrate_dedupe", None)
                                if not callable(migrator) or not migrator(old_key, canonical_key):
                                    return  # keep the old key for a later check
                        pending = [t for t in crossed if not _already_reported(probe, keys[t])]
                        if not pending:
                            return
                        sender_notifier = notifier or (notifier_factory or _default_notifier_factory)()
                        span, span_known = window_span(name)
                        try:
                            window_start = (resets_at - span) if resets_at else (current - span)
                        except (ValueError, OverflowError):
                            window_start = current - _DEFAULT_SPAN
                            span_known = False
                        try:
                            callers = model_tiering.top_callers(
                                since=window_start, provider=provider, limit=3
                            )
                            attribution_unavailable = False
                        except Exception as exc:  # noqa: BLE001
                            log.warning("[apis] usage alert: ledger unreadable (%s)", exc)
                            callers = []
                            attribution_unavailable = True
                        message = build_message(
                            provider=provider, window=name, used_percent=used,
                            thresholds=pending, resets_at=resets_at, callers=callers,
                            span_known=span_known,
                            attribution_unavailable=attribution_unavailable,
                            observed_at=observed_at, reading_fresh=reading_fresh,
                        )
                        from lib.notify import Priority

                        send_result = sender_notifier.send(
                            message, priority=Priority.WARN, handler="apis",
                            dedupe_key=keys[max(pending)], retryable=True,
                            email_eligible=False,
                            related_dedupe_keys=tuple(
                                keys[t] for t in pending if t != max(pending)
                            ),
                        )
                        delivered = bool(send_result)
                        if delivered:
                            if getattr(send_result, "value", None) != "delivered_receipt_unknown":
                                for threshold in pending:
                                    _mark_reported(sender_notifier, keys[threshold])
                            _retry_after.pop(retry_key, None)
                            result_keys.extend(keys[threshold] for threshold in pending)
                            outcome = "delivered, receipt unknown" if getattr(send_result, "value", None) == "delivered_receipt_unknown" else "delivered"
                        else:
                            entry = sender_notifier._dedupe_journal.get(keys[max(pending)], {})
                            if isinstance(entry, dict) and entry.get("state") == "held":
                                outcome = "held"
                            else:
                                outcome = "NOT delivered"
                                _retry_after[retry_key] = time.monotonic() + RETRY_BACKOFF_SECONDS
                        log.info("[apis] usage alert %s: %s", outcome, message)
                    except Exception as exc:  # noqa: BLE001
                        _retry_after[retry_key] = time.monotonic() + RETRY_BACKOFF_SECONDS
                        log.warning("[apis] usage alert handoff failed: %s", exc)

            sender = threading.Thread(target=handoff, daemon=True)
            sender.start()
            sender.join(ALERT_SEND_TIMEOUT_SECONDS)
            delivered_keys.extend(result_keys)
    except Exception as exc:  # noqa: BLE001 — never block or fail a dispatch
        log.warning(f"[apis] usage threshold check failed (non-fatal): {exc}")
    return delivered_keys


def _notice_unreadable_snapshot(
    source: Path, problem: str, current: datetime, *,
    notifier, notifier_factory, journal_factory,
) -> None:
    """Use the same journal and held queue as threshold notices."""
    from lib.notify import Priority, held_view

    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    key = f"{held_view.UNREADABLE_USAGE_KEY_PREFIX}{digest}:{current.date().isoformat()}"
    source_label = str(source)
    if len(source_label) > 200:
        source_label = source_label[:197] + "... (APIS_HARNESS_USAGE_FILE)"
    try:
        probe = notifier or (journal_factory or _default_journal_factory)()
        if _already_reported(probe, key):
            return
        sender = notifier or (notifier_factory or _default_notifier_factory)()
        sender.send(
            f"Usage snapshot unreadable at {source_label}: {problem}. Inspect and repair "
            "the source, then run `python3 execution/scripts/harness_usage.py refresh`.",
            priority=Priority.WARN, handler="apis", dedupe_key=key,
            retryable=True, email_eligible=False,
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics cannot block dispatch
        log.warning("[apis] unreadable usage snapshot notice failed: %s", exc)


def schedule_dispatch_check(provider: str, *, notifier=None) -> threading.Thread:
    """Keep notifier configuration and transport I/O off the dispatch path.

    The worker uses the same journal and cross-process lock as synchronous
    usage-refresh checks. Notifier.send records its dedupe entry before handing
    the alert to a transport; a failed send releases it for retry.
    """
    def run() -> None:
        try:
            check_usage_thresholds(provider, notifier=notifier)
        except Exception as exc:  # noqa: BLE001 — alerts cannot fail a dispatch
            log.warning("[apis] usage threshold worker failed (non-fatal): %s", exc)

    worker = threading.Thread(
        target=run,
        name=f"usage-alert-{provider}",
        daemon=True,
    )
    worker.start()
    return worker


def check_after_usage_write(provider: str) -> None:
    """Hook for code that has just written a usage reading. Never raises."""
    try:
        check_usage_thresholds(provider)
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[apis] ERROR: usage threshold check after refresh failed: {exc}", file=sys.stderr)
        log.error("[apis] usage threshold check after refresh failed: %s", exc)


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
    marker = getattr(notifier, "_mark_dedupe_delivered", None)
    if callable(marker):
        try:
            marker(key, "apis")
        except Exception:  # noqa: BLE001
            pass
