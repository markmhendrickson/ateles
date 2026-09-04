"""
lib/daemon_runtime/plist_drift.py — detect a daemon running config nobody reviewed.

The sibling of :mod:`checkout_drift`. That module answers "is this daemon running
stale CODE?"; this one answers "is it running stale CONFIG?" — the launchd
environment it was actually booted with, versus the plist checked into the repo.

Config that lives only in a deployment artifact drifts invisibly. There is no
diff to notice, no error to see, and the flag that governs how much autonomy the
swarm has can differ from the file that claims to describe it — indefinitely.

The motivating case, found 2026-08-25:

  ``ATELES_SWARM_AUTO_BUILD`` was absent from the repo plist entirely while the
  running daemon carried ``=0``. It had been enabled twice and rolled back twice
  during incidents (ateles#359, ateles#460); both fixes landed and neither
  re-enable happened, because the intent lived only in sessions that ended. For
  four days after PR #482 fixed the blocking bug, the swarm was configured to
  stop before every build and nothing anywhere said so.

  The same audit found the repo plist ALSO pointed ``ProgramArguments``,
  ``VIRTUAL_ENV`` and ``PYTHONPATH`` at the shared session clone rather than the
  deployment checkout, and omitted four more live keys. It was not a stale copy
  of the truth — it was wrong, and copying it over the live plist would have
  disabled auto-rereview, dropped two of three watched repositories, and pointed
  the daemon's Python at a tree that is dirty most of the time.

## Posture

Advisory, matching :mod:`checkout_drift`. ``warn_on_plist_drift`` logs and
returns; it does not exit. These daemons are the swarm's release, payment, and
dispatch path, and refusing to boot over a config difference would cause a
larger outage than the drift it reports. Set ``ATELES_ENFORCE_PLIST_CONFIG=1``
to make divergence fatal for a daemon that would rather refuse than run
unreviewed config.

A missing plist is NOT drift — daemons launched by hand, by tests, or on a host
with no launchd have nothing to compare against, and reporting that as drift
would train operators to ignore the warning. The same reasoning as
``checkout_drift``'s UNKNOWN state.
"""

from __future__ import annotations

import logging
import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Make config drift fatal instead of advisory (opt-in per daemon or globally).
ENFORCE_ENV = "ATELES_ENFORCE_PLIST_CONFIG"

#: Force the repo plist path that is inspected. Tests need this; production
#: derives the path from the module location.
REPO_PLIST_ENV = "ATELES_PLIST_DRIFT_REPO_FILE"

#: Force the live plist path that is inspected. Tests need this; production
#: reads ``~/Library/LaunchAgents/<label>.plist``.
LIVE_PLIST_ENV = "ATELES_PLIST_DRIFT_LIVE_FILE"

#: Keys whose value legitimately differs per machine or per checkout, and which
#: therefore must not be reported as drift. Everything else — every autonomy
#: flag in particular — is expected to match the reviewed file exactly.
DEFAULT_IGNORED_KEYS: frozenset[str] = frozenset({"HOME", "PATH"})


class PlistConfigDriftError(RuntimeError):
    """Raised only when ``ATELES_ENFORCE_PLIST_CONFIG=1`` and config diverges."""


@dataclass(frozen=True)
class PlistDriftReport:
    """How the live launchd environment differs from the reviewed plist."""

    #: "clean" | "drifted" | "unknown"
    state: str
    #: key -> (live_value, repo_value) for keys present in both but differing.
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Keys the live daemon carries that the reviewed plist does not declare.
    live_only: tuple[str, ...] = ()
    #: Keys the reviewed plist declares that the live daemon does not carry.
    repo_only: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_drifted(self) -> bool:
        """True when live config differs from the reviewed file.

        ``unknown`` is deliberately NOT drift: it means we could not compare
        (no live plist, unreadable file), not that the config is wrong.
        """
        return self.state == "drifted"

    def summary(self) -> str:
        if self.state == "clean":
            return "launchd config matches the reviewed plist"
        if self.state == "unknown":
            return f"could not compare launchd config ({self.detail})"
        parts: list[str] = []
        for key, (live_value, repo_value) in sorted(self.changed.items()):
            parts.append(f"{key}: running={live_value!r} reviewed={repo_value!r}")
        if self.live_only:
            parts.append(
                "running but UNDECLARED in the repo plist: "
                + ", ".join(sorted(self.live_only))
            )
        if self.repo_only:
            parts.append(
                "declared in the repo plist but NOT running: "
                + ", ".join(sorted(self.repo_only))
            )
        return (
            "launchd config has DRIFTED from the reviewed plist — the daemon is "
            "running settings nobody reviewed: " + "; ".join(parts)
        )


def _read_env(path: Path) -> dict[str, str] | None:
    """Return a plist's EnvironmentVariables, or None when it cannot be read."""
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    env = data.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def check_plist_drift(
    label: str,
    repo_plist: Path,
    *,
    live_plist: Path | None = None,
    ignored_keys: frozenset[str] = DEFAULT_IGNORED_KEYS,
) -> PlistDriftReport:
    """Compare the live launchd environment against the reviewed repo plist.

    Pure detection: never raises, never exits. ``label`` names the launchd job
    (e.g. ``com.ateles.apis``) and is used to locate the live plist when
    ``live_plist`` is not given.
    """
    repo_override = os.environ.get(REPO_PLIST_ENV, "").strip()
    if repo_override:
        repo_plist = Path(repo_override).expanduser()

    live_override = os.environ.get(LIVE_PLIST_ENV, "").strip()
    if live_override:
        live_plist = Path(live_override).expanduser()
    elif live_plist is None:
        live_plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    if not live_plist.is_file():
        # Launched by hand, by tests, or on a host without launchd. Nothing to
        # compare against — not evidence of bad config.
        return PlistDriftReport("unknown", detail=f"no live plist at {live_plist}")

    live_env = _read_env(live_plist)
    if live_env is None:
        return PlistDriftReport("unknown", detail=f"could not parse {live_plist}")

    repo_env = _read_env(repo_plist)
    if repo_env is None:
        return PlistDriftReport("unknown", detail=f"could not parse {repo_plist}")

    live_keys = set(live_env) - ignored_keys
    repo_keys = set(repo_env) - ignored_keys

    changed = {
        key: (live_env[key], repo_env[key])
        for key in live_keys & repo_keys
        if live_env[key] != repo_env[key]
    }
    live_only = tuple(sorted(live_keys - repo_keys))
    repo_only = tuple(sorted(repo_keys - live_keys))

    if not changed and not live_only and not repo_only:
        return PlistDriftReport("clean")
    return PlistDriftReport(
        "drifted", changed=changed, live_only=live_only, repo_only=repo_only
    )


def warn_on_plist_drift(
    label: str,
    repo_plist: Path,
    *,
    live_plist: Path | None = None,
    ignored_keys: frozenset[str] = DEFAULT_IGNORED_KEYS,
    enforce: bool | None = None,
) -> PlistDriftReport:
    """Log config drift at startup. Advisory unless enforcement is opted into.

    Returns the report so a caller can make its own decision; raises
    ``PlistConfigDriftError`` when ``enforce=True`` or when
    ``ATELES_ENFORCE_PLIST_CONFIG=1``.
    """
    report = check_plist_drift(
        label, repo_plist, live_plist=live_plist, ignored_keys=ignored_keys
    )
    if not report.is_drifted:
        log.debug("[plist-drift] %s: %s", label, report.summary())
        return report

    log.error("[plist-drift] %s: %s", label, report.summary())
    log.error(
        "[plist-drift] %s: reconcile %s with the live plist, or update the live "
        "plist and restart with `launchctl bootout gui/$UID/%s` then "
        "`launchctl bootstrap gui/$UID <plist>` — `kickstart -k` reuses the "
        "cached service definition and will NOT pick up an edited plist.",
        label,
        repo_plist,
        label,
    )
    should_enforce = (
        enforce if enforce is not None else os.environ.get(ENFORCE_ENV, "0") == "1"
    )
    if should_enforce:
        raise PlistConfigDriftError(report.summary())
    return report
