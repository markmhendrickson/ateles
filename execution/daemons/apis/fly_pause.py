"""One host-wide switch that pauses every Fly Machines API call from this host.

The pause record is a small JSON file next to the Fly runner ledger
(``fly-runner-pause.json`` beside ``APIS_FLY_LEDGER_FILE``). It holds an
expiry time and a short reason. While it is in force:

* ``FlyMachinesClient._call`` (the one place every Machines API request
  goes through) refuses before any request is built, so Apis's periodic
  settlement, the reaper, launches, polls, clean-up and the status probe all
  make no Fly call;
* new launches refuse up front, minting no token and reserving nothing.

Fail closed: a record that exists but cannot be read or parsed counts as
paused. An expired record is not a pause. Set, clear and show it with
``execution/scripts/fly_pause.py``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

PAUSE_FILE_NAME = "fly-runner-pause.json"
DEFAULT_PAUSE_SECONDS = 3600
MAX_PAUSE_SECONDS = 12 * 3600
MAX_REASON_CHARS = 200
CLEAR_COMMAND = "python3 execution/scripts/fly_pause.py clear"


def ledger_path(env: dict | None = None) -> Path:
    """The Fly runner ledger path, resolved exactly as ``FlyConfig.from_env``."""
    env = os.environ if env is None else env
    override = env.get("APIS_FLY_LEDGER_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ateles" / "fly-runner-ledger.json"


def pause_path(ledger_path: Path) -> Path:
    """The pause record for the ledger at *ledger_path*."""
    return Path(ledger_path).with_name(PAUSE_FILE_NAME)


def _render(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass(frozen=True)
class PauseState:
    """``paused`` is the only field a caller decides on. ``until`` is None
    for an unreadable record (its expiry is unknown)."""

    paused: bool
    until: float | None = None
    reason: str = ""
    unreadable: bool = False

    def message(self) -> str:
        if self.unreadable:
            return (
                f"Fly paused: the pause record cannot be read ({self.reason}); "
                f"treated as paused until it is cleared with `{CLEAR_COMMAND}`"
            )
        return f"Fly paused until {_render(self.until or 0.0)}: {self.reason}"

    def until_text(self) -> str:
        return _render(self.until) if self.until is not None else ""


NOT_PAUSED = PauseState(paused=False)


def read_pause(path: Path, now: float | None = None) -> PauseState:
    """The pause in force at *path*. No file: not paused. A file that cannot
    be read or parsed: paused (fail closed). An expired record: not paused."""
    now = time.time() if now is None else now
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return NOT_PAUSED
    except OSError as exc:
        return PauseState(True, None, f"{type(exc).__name__}", unreadable=True)
    try:
        data = json.loads(raw)
        until = data["until"]
        reason = data["reason"]
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise ValueError("until is not a number")
        if until != until or until in (float("inf"), float("-inf")):
            raise ValueError("until is not finite")
        if not isinstance(reason, str):
            raise ValueError("reason is not text")
    except (ValueError, KeyError, TypeError) as exc:
        return PauseState(True, None, f"malformed record: {type(exc).__name__}",
                          unreadable=True)  # fmt: skip
    if until <= now:
        return NOT_PAUSED
    return PauseState(True, float(until), reason)


def set_pause(path: Path, *, seconds: float = DEFAULT_PAUSE_SECONDS, reason: str,
              now: float | None = None) -> PauseState:  # fmt: skip
    """Write the pause record atomically. Refuses a duration that is not
    positive or is over ``MAX_PAUSE_SECONDS``, and an empty reason."""
    now = time.time() if now is None else now
    if not (0 < seconds <= MAX_PAUSE_SECONDS):
        raise ValueError(
            f"pause duration must be over 0 and at most {MAX_PAUSE_SECONDS}s "
            f"(12 hours); got {seconds:g}s"
        )
    reason = " ".join(reason.split())
    if not reason:
        raise ValueError("a pause needs a short reason")
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"reason is over {MAX_REASON_CHARS} characters")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    until = now + seconds
    record = {
        "until": until,
        "until_utc": _render(until),
        "reason": reason,
        "set_at": now,
    }
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    state = read_pause(path, now)
    if not state.paused or state.unreadable:
        raise OSError(f"pause record at {path} did not read back as a pause")
    return state


def clear_pause(path: Path) -> bool:
    """Remove the pause record. True when one was removed."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True
