"""Operator alerts when a provider's usage window crosses 50% and 75%.

Provider spend governance, task ent_e5259618cae6cafd4be120e0. On 2026-11-15 a
provider's monthly allowance burned in about seven hours with no warning. This
module warns while there is still room: when a usage window recorded in
``harness-usage.json`` (written by ``execution/scripts/harness_usage.py
refresh``, read through ``harness_router.usage_windows``) first reaches 50%
used, and again at 75%, one alert per threshold per window goes to the
operator through the existing notifier (``lib.notify.Notifier``).

Called from ``skill_runner._run_skill_once`` for every dispatch attempt — the
one path daemon dispatches and interactive ``dispatch_role`` dispatches share —
with the provider that attempt selected.

Once per threshold per window. The notifier's persistent dedupe journal
(``Notifier.send(dedupe_key=...)``, ateles#1127) already records "this condition
was reported"; the key names the provider, the window and that window's reset
time, so the next window (a new ``resets_at``) alerts afresh. The journal is
checked before anything else is read, so a dispatch inside an already-reported
window costs one small file read. No separate state store is added.

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
import re
from datetime import datetime, timedelta, timezone
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


def build_message(
    *,
    provider: str,
    window: str,
    used_percent: float,
    threshold: int,
    resets_at: datetime | None,
    callers: list[tuple[str | None, str | None, int]],
    span_known: bool,
) -> str:
    reset_text = resets_at.isoformat(timespec="minutes") if resets_at else "unknown"
    span_note = "" if span_known else " (window length unknown; last 7 days)"
    return (
        f"Provider usage: {provider} {window} window is {used_percent:g}% used "
        f"(crossed {threshold}%). Resets {reset_text}. "
        f"Top callers by dispatch count{span_note}: {_describe_callers(callers)}."
    )


def _default_journal_factory():
    from lib.notify import Notifier

    return Notifier()


def _default_notifier_factory():
    from lib.notify import Notifier

    return Notifier.from_neotoma()


def check_usage_thresholds(
    provider: str,
    *,
    notifier=None,
    notifier_factory: Callable[[], object] | None = None,
    journal_factory: Callable[[], object] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Alert once per crossed threshold per window for ``provider``.

    Returns the dedupe keys of alerts sent by this call (empty when nothing new
    crossed or anything failed). ``notifier`` is the dispatching daemon's own;
    when it is ``None`` (an interactive ``dispatch_role`` run), the dedupe
    journal is checked through a plain ``Notifier()`` (``journal_factory``; no
    network), and the sending notifier is built by ``notifier_factory``
    (default ``Notifier.from_neotoma``) only once a threshold has newly
    crossed. Never raises.
    """
    sent: list[str] = []
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
            if not crossed:
                continue
            # One alert per call per window: the highest newly crossed
            # threshold. Lower thresholds crossed at the same moment are
            # recorded as reported too, so a jump from 40% to 80% says 75%
            # once rather than sending 50% and 75% together.
            keys = {t: dedupe_key(provider, name, reset_key, t) for t in crossed}
            if notifier is None and journal is None:
                journal = (journal_factory or _default_journal_factory)()
            probe = notifier if notifier is not None else journal
            pending = [t for t in crossed if not _already_reported(probe, keys[t])]
            if not pending:
                continue
            if notifier is None:
                notifier = (notifier_factory or _default_notifier_factory)()
            threshold = max(pending)
            span, span_known = window_span(name)
            window_start = (resets_at - span) if resets_at else (current - span)
            try:
                callers = model_tiering.top_callers(
                    since=window_start, provider=provider, limit=3
                )
            except Exception as exc:  # noqa: BLE001 — the alert still goes out
                log.warning(f"[apis] usage alert: ledger unreadable ({exc}); sending without callers")
                callers = []
            message = build_message(
                provider=provider, window=name, used_percent=used,
                threshold=threshold, resets_at=resets_at, callers=callers,
                span_known=span_known,
            )
            from lib.notify import Priority

            notifier.send(
                message, priority=Priority.WARN, handler="apis",
                dedupe_key=keys[threshold],
            )
            for lower in pending:
                if lower != threshold:
                    _mark_reported(notifier, keys[lower])
            sent.append(keys[threshold])
            log.info(f"[apis] usage alert sent: {message}")
    except Exception as exc:  # noqa: BLE001 — never block or fail a dispatch
        log.warning(f"[apis] usage threshold check failed (non-fatal): {exc}")
    return sent


def _already_reported(notifier, key: str) -> bool:
    """True when the notifier's dedupe journal holds ``key``; False if unknown."""
    if notifier is None:
        return False
    try:
        return bool(notifier.is_dedupe_open(key))
    except Exception:  # noqa: BLE001
        return False


def _mark_reported(notifier, key: str) -> None:
    marker = getattr(notifier, "_mark_dedupe_notified", None)
    if callable(marker):
        try:
            marker(key, "apis")
        except Exception:  # noqa: BLE001
            pass
