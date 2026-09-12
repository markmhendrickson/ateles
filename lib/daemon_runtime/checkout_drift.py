"""
lib/daemon_runtime/checkout_drift.py — detect a daemon running stale/diverged code.

A daemon runs the working tree it was launched from, not whatever is on
``origin/main``. When that checkout drifts — sitting behind main, or carrying
local commits that were never pushed — the daemon silently executes code nobody
is reviewing, and a merged fix does nothing.

This has bitten the same deployment checkout three times:

  - ateles#339  "preserve(apis): harness-router dispatch balancing + Codex
                 dispatch fix (found unpushed in the daemon checkout)"
  - ateles#361  "fix(apis): recover harness routing + Codex dispatch work
                 stranded in the deploy checkout"
  - 2026-08-09  ``~/ateles-rc-src`` sat on a local merge commit from 2026-07-28.
                ateles#401 merged to main and changed nothing for the running
                daemon; `git pull --ff-only` refused (diverged) and left HEAD
                where it was, so a release stayed blocked with no error anywhere.

The first two were recovered by hand after the fact. This module exists so the
third class is caught at startup instead of discovered during an incident.

## Posture

Reporting is the point; blocking is opt-in.

``check_checkout_drift`` is pure detection and never raises. ``warn_on_drift``
logs and returns — it does NOT exit — because these daemons are the swarm's
release, payment, and dispatch path, and a guard that hard-stops 18 daemons on a
stale checkout would cause a larger outage than the drift it prevents. Set
``ATELES_ENFORCE_CHECKOUT_FRESHNESS=1`` to make a drifted checkout fatal for
daemons that would rather refuse than run unknown code.

## Where the report goes

An ERROR line in a daemon log is a write-only channel. Nobody tails eighteen
log files, so "advisory" in practice meant "silent" — the same failure mode as
ateles#583, and the reason ateles#573 (Anthus dead for two months) stayed
invisible: the fix merged and still did not reach the daemon until someone
pulled the checkout by hand.

So ``warn_on_drift`` ALSO writes an ``escalation`` entity to Neotoma, which
Anthus already surfaces to the operator. Drift becomes an item in the operator's
decision queue rather than a line in a file. The write is best-effort and
fail-open (no token, no network, no ``httpx`` → log and continue): a daemon must
never fail to start because the escalation could not be filed.

Escalations are deduplicated per (daemon, HEAD, state) via the idempotency key,
so a daemon restarting every two minutes on a stale checkout files ONE
escalation, not seven hundred a day. A new HEAD or a changed state files a new
one, because that is genuinely new information.

Network failure is never drift. If ``git fetch`` cannot reach the remote we
report ``UNKNOWN`` rather than guessing, so an offline laptop does not look
identical to a checkout carrying unpushed commits.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Make drift fatal instead of advisory (opt-in per daemon or globally).
ENFORCE_ENV = "ATELES_ENFORCE_CHECKOUT_FRESHNESS"

#: Skip the remote-ref refresh. For tests and for hosts that are deliberately
#: offline; the check then compares against whatever ref data is already local.
NO_FETCH_ENV = "ATELES_CHECKOUT_DRIFT_NO_FETCH"

#: Force the path that is inspected. Entrypoint tests need this because
#: ``prepare.py`` always passes ``Path(__file__).parent`` (the real package
#: tree), so setting the subprocess ``cwd`` alone never reaches the guard —
#: CI then inspects the Actions checkout (often no ``@{u}``) and the fixture
#: drift is invisible. Production leaves this unset.
CHECKOUT_ROOT_ENV = "ATELES_CHECKOUT_DRIFT_ROOT"

#: Suppress the Neotoma escalation write (keep the log line). For tests and for
#: hosts where filing an escalation is not wanted. Detection is unaffected.
NO_ESCALATE_ENV = "ATELES_CHECKOUT_DRIFT_NO_ESCALATE"

#: Where the escalation is filed. Matches the session-integrity proxy defaults
#: so both audit paths land on the same instance.
NEOTOMA_BASE_URL_ENV = "NEOTOMA_BASE_URL"
NEOTOMA_DEFAULT_BASE_URL = "https://neotoma.markmhendrickson.com"
NEOTOMA_TOKEN_ENV = "NEOTOMA_BEARER_TOKEN"


@dataclass(frozen=True)
class DriftReport:
    """What a checkout looks like relative to its upstream."""

    #: "clean" | "behind" | "diverged" | "dirty" | "unknown" | "not_a_repo"
    state: str
    behind: int = 0
    ahead: int = 0
    head: str = ""
    upstream: str = ""
    detail: str = ""

    @property
    def is_drifted(self) -> bool:
        """True when the daemon is running code that is not upstream `main`.

        ``unknown`` and ``not_a_repo`` are deliberately NOT drift: the first
        means we could not tell (usually no network), and the second means the
        daemon runs from something other than a git checkout. Neither is
        evidence of stale code, and treating them as drift would train operators
        to ignore the warning.
        """
        return self.state in ("behind", "diverged", "dirty")

    def summary(self) -> str:
        if self.state == "clean":
            return f"checkout is current with {self.upstream}"
        if self.state == "behind":
            return (
                f"checkout is {self.behind} commit(s) BEHIND {self.upstream} — "
                "this daemon is running code that has been superseded"
            )
        if self.state == "diverged":
            return (
                f"checkout has DIVERGED from {self.upstream}: {self.ahead} local "
                f"commit(s) not upstream, {self.behind} upstream commit(s) missing. "
                "A merged fix will NOT reach this daemon, and `git pull --ff-only` "
                "will refuse without changing HEAD"
            )
        if self.state == "dirty":
            return "checkout has uncommitted changes — the daemon is running unreviewed edits"
        if self.state == "unknown":
            return f"could not determine checkout state ({self.detail})"
        return f"not a git checkout ({self.detail})"


def _git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def check_checkout_drift(
    checkout: Path | str | None = None, *, fetch: bool | None = None
) -> DriftReport:
    """
    Report how ``checkout`` stands relative to its upstream branch.

    ``checkout`` defaults to the directory containing the calling daemon's
    package root; pass it explicitly when the daemon's file lives elsewhere.

    Never raises — a guard that can crash the process it guards is worse than
    no guard.
    """
    # Env override wins so entrypoint subprocess tests can point the guard at a
    # fixture repo without rewriting the daemon's Path(__file__) call site.
    override = os.environ.get(CHECKOUT_ROOT_ENV)
    if override:
        root = Path(override)
    elif checkout is not None:
        root = Path(checkout)
    else:
        root = Path(__file__).resolve().parent.parent.parent

    rc, top = _git(["rev-parse", "--show-toplevel"], root)
    if rc != 0:
        return DriftReport(state="not_a_repo", detail=top[:200])
    repo = Path(top)

    rc, head = _git(["rev-parse", "--short", "HEAD"], repo)
    if rc != 0:
        return DriftReport(state="unknown", detail=f"cannot read HEAD: {head[:120]}")

    rc, upstream = _git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo
    )
    if rc != 0 or not upstream:
        # A detached HEAD or an untracked branch has nothing to compare against.
        return DriftReport(
            state="unknown", head=head, detail="no upstream branch configured"
        )

    do_fetch = fetch if fetch is not None else os.environ.get(NO_FETCH_ENV) != "1"
    if do_fetch:
        # Refresh remote refs only. This writes to .git but never touches the
        # working tree, so it cannot disturb a daemon mid-run.
        rc, out = _git(["fetch", "--quiet", "--no-tags"], repo, timeout=60)
        if rc != 0:
            # Offline is not drift. Say so rather than inventing a verdict.
            return DriftReport(
                state="unknown",
                head=head,
                upstream=upstream,
                detail=f"fetch failed: {out[:120]}",
            )

    rc, counts = _git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], repo)
    if rc != 0:
        return DriftReport(
            state="unknown", head=head, upstream=upstream, detail=counts[:120]
        )
    try:
        ahead_s, behind_s = counts.split()
        ahead, behind = int(ahead_s), int(behind_s)
    except ValueError:
        return DriftReport(
            state="unknown",
            head=head,
            upstream=upstream,
            detail=f"unparseable rev-list output: {counts[:80]}",
        )

    if ahead and behind:
        state = "diverged"
    elif behind:
        state = "behind"
    elif ahead:
        # Ahead-only is the shape that stranded work in the deploy checkout
        # twice: commits that exist nowhere else, one power-cycle from being
        # lost, and invisible to anyone reviewing main.
        state = "diverged"
    else:
        rc, dirty = _git(["status", "--porcelain"], repo)
        # Ignore untracked files: a daemon checkout accumulates logs and state
        # files as a matter of course, and flagging those would bury the signal.
        tracked = [
            ln for ln in dirty.splitlines() if ln[:2] not in ("??",)
        ] if rc == 0 else []
        state = "dirty" if tracked else "clean"

    return DriftReport(
        state=state, behind=behind, ahead=ahead, head=head, upstream=upstream
    )


def escalate_drift(daemon_name: str, report: DriftReport) -> bool:
    """
    File a drift ``escalation`` in Neotoma so the operator actually sees it.

    Best-effort and fail-open by design: returns True when the write was
    attempted and accepted, False when it was skipped or failed. A daemon must
    never fail to start because its escalation could not be filed — the log line
    in ``warn_on_drift`` remains the backstop.

    Deduplicated on (daemon, HEAD, state): a daemon relaunched every two minutes
    on the same stale commit files one escalation, not one per start.
    """
    if os.environ.get(NO_ESCALATE_ENV) == "1":
        log.debug(f"[{daemon_name}] drift escalation suppressed by {NO_ESCALATE_ENV}")
        return False

    token = os.environ.get(NEOTOMA_TOKEN_ENV, "")
    if not token:
        log.debug(f"[{daemon_name}] no {NEOTOMA_TOKEN_ENV} — drift escalation not filed")
        return False

    base_url = os.environ.get(NEOTOMA_BASE_URL_ENV) or NEOTOMA_DEFAULT_BASE_URL
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    detail = (
        f"Daemon '{daemon_name}' is running a checkout that has drifted from its "
        f"upstream: {report.summary()} (HEAD={report.head}, upstream={report.upstream}). "
        "A daemon executes the working tree it was launched from, not origin/main, "
        "so any fix merged since this HEAD is NOT running here. This daemon may "
        "report healthy while executing code nobody is reviewing. "
        "Remedy: fast-forward the deploy checkout and restart the daemon. "
        "ADVISORY: the daemon was NOT stopped; this is an audit signal."
    )

    entity = {
        "entity_type": "escalation",
        "escalation_type": "checkout_drift",
        # 'behind' is routine deploy lag; 'diverged'/'ahead' means unpushed,
        # unreviewed commits live only on this host and are one power-cycle
        # from being lost — that is materially worse than being stale.
        "severity": "warning" if report.state == "behind" else "error",
        "source_agent": daemon_name,
        "summary": f"{daemon_name}: deploy checkout drifted ({report.state})",
        "detail": detail,
        "status": "open",
        "observed_at": now,
    }

    # Dedup key deliberately excludes a timestamp: the same daemon on the same
    # HEAD in the same state is the same finding, however often it restarts.
    idem = f"escalation-checkout-drift-{daemon_name}-{report.head}-{report.state}"

    try:
        import httpx  # noqa: PLC0415

        resp = httpx.post(
            f"{base_url}/store",
            json={
                "entities": [entity],
                "idempotency_key": idem,
                "observation_source": "workflow_state",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code >= 400:
            log.warning(
                f"[{daemon_name}] drift escalation rejected "
                f"(HTTP {resp.status_code}); log line stands as the record"
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{daemon_name}] drift escalation not filed (non-fatal): {exc}")
        return False


def warn_on_drift(
    daemon_name: str,
    checkout: Path | str | None = None,
    *,
    enforce: bool | None = None,
    escalate: bool = True,
) -> DriftReport:
    """
    Check the checkout, log the result, and file an escalation on drift.

    Advisory by default: logs at ERROR, files a Neotoma ``escalation`` so the
    drift reaches the operator's decision queue rather than only a log file, and
    continues. Pass ``enforce=True`` (or set
    ``ATELES_ENFORCE_CHECKOUT_FRESHNESS=1``) to raise ``CheckoutDriftError``
    instead. Pass ``escalate=False`` to keep the log line without the write.

    The escalation is filed BEFORE the enforcement check so the operator gets
    the signal even when the daemon is configured to abort on drift — otherwise
    the strictest daemons would be the quietest ones.
    """
    report = check_checkout_drift(checkout)

    if not report.is_drifted:
        if report.state == "clean":
            log.debug(f"[{daemon_name}] {report.summary()}")
        else:
            log.info(f"[{daemon_name}] checkout freshness: {report.summary()}")
        return report

    log.error(
        f"[{daemon_name}] CHECKOUT DRIFT — {report.summary()}. "
        f"HEAD={report.head} upstream={report.upstream}. "
        "Code merged to main is NOT running here until this checkout is updated."
    )

    if escalate:
        # Never let a failed escalation become the reason a daemon dies: this
        # helper already swallows its own errors and returns a bool.
        escalate_drift(daemon_name, report)

    should_enforce = (
        enforce if enforce is not None else os.environ.get(ENFORCE_ENV) == "1"
    )
    if should_enforce:
        raise CheckoutDriftError(report)
    return report


class CheckoutDriftError(RuntimeError):
    """Raised by ``warn_on_drift`` when enforcement is enabled and the checkout drifted."""

    def __init__(self, report: DriftReport) -> None:
        super().__init__(f"checkout drift: {report.summary()}")
        self.report = report
