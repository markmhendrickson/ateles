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
import stat
import time
from dataclasses import dataclass
from pathlib import Path

PAUSE_FILE_NAME = "fly-runner-pause.json"
DEFAULT_PAUSE_SECONDS = 3600
MAX_PAUSE_SECONDS = 12 * 3600
MAX_REASON_CHARS = 200
CLEAR_COMMAND = "python3 execution/scripts/fly_pause.py clear"
# A record ``set_pause`` writes is a few hundred bytes; anything larger is
# not one, and is not read into memory.
MAX_RECORD_BYTES = 4096
# Clock slack allowed on top of ``MAX_PAUSE_SECONDS`` for a record's expiry.
UNTIL_SLACK_SECONDS = 300
UNKNOWN_TIME = "an unknown time"


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
    """UTC text for *ts*; never raises (an unrenderable time is named so)."""
    try:
        return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:  # noqa: BLE001 - total by contract: a message must never raise
        return UNKNOWN_TIME


@dataclass(frozen=True)
class PauseState:
    """``paused`` is the only field a caller decides on. ``until`` is None
    for an unreadable record (its expiry is unknown)."""

    paused: bool
    until: float | None = None
    reason: str = ""
    unreadable: bool = False

    def message(self) -> str:
        """The refusal text. Never raises, whatever the fields hold."""
        try:
            reason = " ".join(str(self.reason).split())[:MAX_REASON_CHARS]
            if self.unreadable or self.until is None:
                return (
                    f"Fly paused: the pause record cannot be read ({reason}); "
                    f"treated as paused until it is cleared with `{CLEAR_COMMAND}`"
                )
            return f"Fly paused until {_render(self.until)}: {reason}"
        except Exception:  # noqa: BLE001 - total by contract
            return (
                "Fly paused: the pause record cannot be read; treated as paused "
                f"until it is cleared with `{CLEAR_COMMAND}`"
            )

    def until_text(self) -> str:
        """The pause's end as UTC text; "" when unknown. Never raises."""
        if self.unreadable or self.until is None:
            return ""
        text = _render(self.until)
        return "" if text == UNKNOWN_TIME else text


NOT_PAUSED = PauseState(paused=False)


def _unreadable(why: str) -> PauseState:
    return PauseState(True, None, why, unreadable=True)


def _read_record(path: Path) -> bytes | PauseState | None:
    """The record's bytes; None when there is no record; an unreadable
    ``PauseState`` for anything at the path that is not a small regular file
    (a directory, a symlink, dangling or not, a FIFO, an oversized file)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return _unreadable(type(exc).__name__)
    if not stat.S_ISREG(st.st_mode):
        return _unreadable("not a regular file")
    if st.st_size > MAX_RECORD_BYTES:
        return _unreadable("record too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None  # removed between the two looks: cleared
    except (OSError, ValueError) as exc:
        return _unreadable(type(exc).__name__)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _unreadable("not a regular file")
        raw = os.read(fd, MAX_RECORD_BYTES + 1)
    except (OSError, ValueError) as exc:
        return _unreadable(type(exc).__name__)
    finally:
        os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        return _unreadable("record too large")
    return raw


def _parse(raw: bytes, now: float) -> PauseState:
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        until = data["until"]
        reason = data["reason"]
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise ValueError("until is not a number")
        until = float(until)  # an over-long integer raises OverflowError
        if until != until or until in (float("inf"), float("-inf")):
            raise ValueError("until is not finite")
        if not isinstance(reason, str):
            raise ValueError("reason is not text")
        reason = " ".join(reason.split())
        reason.encode("utf-8")  # a lone surrogate cannot be shown
        if not reason or len(reason) > MAX_REASON_CHARS:
            raise ValueError("reason is empty or too long")
    except Exception as exc:  # noqa: BLE001 - any invalid record is a pause
        return _unreadable(f"malformed record: {type(exc).__name__}")
    if until <= now:
        return NOT_PAUSED
    if (
        until > now + MAX_PAUSE_SECONDS + UNTIL_SLACK_SECONDS
        or _render(until) == UNKNOWN_TIME
    ):
        # No record set_pause writes ends this late; its expiry is not trusted.
        return _unreadable("malformed record: until is beyond the 12-hour limit")
    return PauseState(True, until, reason)


def read_pause(path: Path, now: float | None = None) -> PauseState:
    """The pause in force at *path*. No file: not paused. An expired record:
    not paused. Anything else that is not a valid, current record (a file
    that cannot be read, decoded or parsed, wrong types, an expiry that is
    past the 12-hour limit or cannot be shown, a directory, a symlink, an
    oversized file): paused, unreadable (fail closed). Never raises."""
    try:
        now = time.time() if now is None else float(now)
        raw = _read_record(Path(path))
        if raw is None or isinstance(raw, PauseState):
            return NOT_PAUSED if raw is None else raw
        return _parse(raw, now)
    except Exception as exc:  # noqa: BLE001 - fail closed, never raise
        return _unreadable(type(exc).__name__)


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
    """Remove the pause record (a symlink there is removed, not followed).
    True when one was removed. Raises ``OSError`` when something is at the
    path that cannot be removed, such as a directory."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True
