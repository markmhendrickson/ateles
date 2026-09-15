"""
lib/daemon_runtime/checkout_identity.py — fail-closed "right tree?" guard.

Checkout *identity* answers whether a daemon is running from its documented
deploy checkout (``~/ateles-rc-src``). Checkout *drift* (``checkout_drift.py``)
answers whether that tree is current with upstream. The two must stay distinct:
a wrong tree that happens to be "fresh" relative to its own branch is still
fatal (ateles#515; incidents #339, #361, #412).

Deploy-bound daemons call ``enforce_deploy_checkout`` at the top of ``main()``.
Enforcement is always fatal for those daemons — there is no production bypass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Override the *expected* deploy root (default ``~/ateles-rc-src``).
DEPLOY_CHECKOUT_ENV = "ATELES_DEPLOY_CHECKOUT"

#: Force the *actual* root the guard inspects. Entrypoint tests only;
#: production leaves this unset. Not a skip — still compared to expected.
IDENTITY_ROOT_ENV = "ATELES_CHECKOUT_IDENTITY_ROOT"

EXIT_WRONG_TREE = 78
EXIT_CANNOT_VERIFY = 79
EXIT_EXPECTED_MISSING = 80
EXIT_CHECK_ERROR = 81

FATAL_WRONG_TREE = "FATAL: wrong checkout — refusing to start."
FATAL_CANNOT_VERIFY = "FATAL: cannot determine checkout identity"
FATAL_EXPECTED_MISSING = "FATAL: deploy checkout not provisioned"
FATAL_CHECK_ERROR = "FATAL: checkout identity check failed"

EXIT_CODES = {
    "wrong_tree": EXIT_WRONG_TREE,
    "cannot_verify": EXIT_CANNOT_VERIFY,
    "expected_missing": EXIT_EXPECTED_MISSING,
    "check_error": EXIT_CHECK_ERROR,
}

FATAL_PREFIXES = {
    "wrong_tree": FATAL_WRONG_TREE,
    "cannot_verify": FATAL_CANNOT_VERIFY,
    "expected_missing": FATAL_EXPECTED_MISSING,
    "check_error": FATAL_CHECK_ERROR,
}

#: Identity states — deliberately disjoint from checkout_drift DriftReport.state.
IDENTITY_STATES = frozenset(
    {"ok", "wrong_tree", "cannot_verify", "expected_missing", "check_error"}
)

DRIFT_STATES = frozenset(
    {"clean", "behind", "diverged", "dirty", "unknown", "not_a_repo"}
)

FIX_BLOCK = (
    "Fix:\n"
    "  bash ~/ateles-rc-src/execution/scripts/isolate_daemons_to_rc_src.sh --apply\n"
    "Docs: docs/daemon_rc_autodeploy.md"
)

#: Per-daemon FATAL "why" lines and plist labels (consumed by enforce + docs).
DAEMON_REGISTRY: dict[str, dict[str, str]] = {
    "cotinga": {
        "plist_label": "com.ateles.cotinga",
        "why": "Daily briefing runs from whatever tree launchd points at.",
    },
    "cyphorhinus": {
        "plist_label": "com.ateles.cyphorhinus",
        "why": "Reply-router stores operator follow-ups to Neotoma.",
    },
    "piculet": {
        "plist_label": "com.ateles.piculet",
        "why": "Watch loop transcribes audio and writes entities.",
    },
    "sylvia": {
        "plist_label": "com.ateles.sylvia",
        "why": "Recurring-task rollforward mutates Neotoma tasks.",
    },
    "phoenicurus-prepare": {
        "plist_label": "com.ateles.phoenicurus-prepare",
        "why": (
            "This daemon drives releases. Running from a session clone risks "
            "silent wrong-code execution (see ateles#339, #361, #412, #515)."
        ),
    },
}


@dataclass(frozen=True)
class IdentityReport:
    """Result of a checkout-identity check (pure; never raises)."""

    state: str  # ok | wrong_tree | cannot_verify | expected_missing | check_error
    actual_root: str
    expected_root: str
    branch: str = ""
    detail: str = ""


class CheckoutIdentityError(SystemExit):
    """Optional typed abort; ``enforce_deploy_checkout`` prefers ``sys.exit``."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


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


def default_deploy_root() -> Path:
    override = os.environ.get(DEPLOY_CHECKOUT_ENV)
    if override:
        return Path(override)
    return Path.home() / "ateles-rc-src"


def resolve_expected_root(expected_root: Path | None = None) -> Path:
    if expected_root is not None:
        return Path(expected_root)
    return default_deploy_root()


def resolve_repo_root(from_path: Path) -> IdentityReport:
    """
    Resolve git toplevel and branch for ``from_path``.

    Returns an IdentityReport with state ``ok`` when git metadata is readable
    (actual_root / branch filled; expected_root left empty — caller compares).
    Failure states use ``cannot_verify`` or ``check_error``.
    """
    start = Path(from_path).resolve()
    cwd = start if start.is_dir() else start.parent
    try:
        rc, top = _git(["rev-parse", "--show-toplevel"], cwd)
    except Exception as exc:  # noqa: BLE001 — unexpected check failure
        return IdentityReport(
            state="check_error",
            actual_root=str(cwd),
            expected_root="",
            detail=str(exc)[:200],
        )
    if rc != 0 or not top:
        return IdentityReport(
            state="cannot_verify",
            actual_root=str(cwd),
            expected_root="",
            detail=f"no .git found at {cwd}" if "not a git" in top.lower() or rc != 0 else top[:200],
        )
    root = Path(top)
    rc_b, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if rc_b != 0:
        return IdentityReport(
            state="cannot_verify",
            actual_root=str(root),
            expected_root="",
            detail=f"unreadable HEAD: {branch[:120]}",
        )
    if branch == "HEAD":
        branch = "(detached)"
    return IdentityReport(
        state="ok",
        actual_root=str(root),
        expected_root="",
        branch=branch,
    )


def check_checkout_identity(
    *,
    script_path: Path,
    expected_root: Path | None = None,
) -> IdentityReport:
    """Pure detection — never raises, never exits."""
    expected = resolve_expected_root(expected_root)

    if not expected.exists():
        return IdentityReport(
            state="expected_missing",
            actual_root="",
            expected_root=str(expected),
            detail="expected deploy root does not exist on disk",
        )

    override = os.environ.get(IDENTITY_ROOT_ENV)
    if override:
        actual_path = Path(override)
        # Still need branch / git readability from the override path.
        resolved = resolve_repo_root(actual_path)
        if resolved.state != "ok":
            return IdentityReport(
                state=resolved.state,
                actual_root=resolved.actual_root or str(actual_path),
                expected_root=str(expected),
                branch=resolved.branch,
                detail=resolved.detail,
            )
        actual = Path(resolved.actual_root).resolve()
        branch = resolved.branch
    else:
        resolved = resolve_repo_root(Path(script_path))
        if resolved.state != "ok":
            return IdentityReport(
                state=resolved.state,
                actual_root=resolved.actual_root,
                expected_root=str(expected),
                branch=resolved.branch,
                detail=resolved.detail,
            )
        actual = Path(resolved.actual_root).resolve()
        branch = resolved.branch

    try:
        expected_r = expected.resolve()
    except OSError as exc:
        return IdentityReport(
            state="check_error",
            actual_root=str(actual),
            expected_root=str(expected),
            branch=branch,
            detail=str(exc)[:200],
        )

    if actual == expected_r:
        return IdentityReport(
            state="ok",
            actual_root=str(actual),
            expected_root=str(expected_r),
            branch=branch,
        )
    return IdentityReport(
        state="wrong_tree",
        actual_root=str(actual),
        expected_root=str(expected_r),
        branch=branch,
    )


def format_fatal_message(
    daemon_name: str,
    report: IdentityReport,
    *,
    why: str,
    plist_label: str,
) -> str:
    """Accipiter contract: what's wrong → where → why → exact fix. No ANSI."""
    state = report.state
    if state == "ok":
        return ""

    if state == "wrong_tree":
        line1 = FATAL_WRONG_TREE
    elif state == "expected_missing":
        line1 = FATAL_EXPECTED_MISSING
    elif state == "check_error":
        line1 = FATAL_CHECK_ERROR
    else:
        line1 = FATAL_CANNOT_VERIFY
        if report.detail and "no .git" in report.detail:
            line1 = (
                f"{FATAL_CANNOT_VERIFY} (no .git found at "
                f"{report.actual_root or '<unknown>'}) — refusing to start "
                "rather than risk wrong-tree execution."
            )
        elif report.detail:
            line1 = f"{FATAL_CANNOT_VERIFY} — {report.detail}"

    branch = report.branch or "(unknown)"
    running = report.actual_root or "(unknown)"
    expected = report.expected_root or str(default_deploy_root())

    parts = [
        line1,
        "",
        f"  daemon:       {daemon_name}",
        f"  running from: {running}  (branch: {branch})",
        f"  expected:     {expected}  (must track origin/main)",
        "",
        why,
        "",
        FIX_BLOCK,
    ]
    if plist_label:
        # Keep label discoverable without inventing unshipped tooling.
        parts.insert(-2, f"  plist:        {plist_label}")
        parts.insert(-2, "")
    msg = "\n".join(parts)
    # Operability: never emit ANSI.
    return msg.replace("\x1b", "")


def enforce_deploy_checkout(
    daemon_name: str,
    script_path: Path,
    *,
    why: str | None = None,
    plist_label: str | None = None,
    expected_root: Path | None = None,
) -> None:
    """
    Fail closed: print FATAL to stderr and ``sys.exit`` with a dedicated code.

    Never returns on failure. Happy path is silent (optional DEBUG only — none).
    """
    registry = DAEMON_REGISTRY.get(daemon_name, {})
    why_line = why if why is not None else registry.get("why", "Deploy-bound daemon.")
    label = (
        plist_label
        if plist_label is not None
        else registry.get("plist_label", f"com.ateles.{daemon_name}")
    )

    try:
        report = check_checkout_identity(
            script_path=Path(script_path), expected_root=expected_root
        )
    except Exception as exc:  # noqa: BLE001
        report = IdentityReport(
            state="check_error",
            actual_root=str(Path(script_path).resolve()),
            expected_root=str(resolve_expected_root(expected_root)),
            detail=str(exc)[:200],
        )

    if report.state == "ok":
        return

    msg = format_fatal_message(daemon_name, report, why=why_line, plist_label=label)
    print(msg, file=sys.stderr)
    code = EXIT_CODES.get(report.state, EXIT_CHECK_ERROR)
    sys.exit(code)


def verify_daemon_checkout(
    daemon_name: str,
    script_path: Path,
    *,
    why: str | None = None,
    plist_label: str | None = None,
    check_freshness: bool = False,
) -> None:
    """Thin orchestrator: identity (always fatal) then optional freshness warn."""
    enforce_deploy_checkout(
        daemon_name, script_path, why=why, plist_label=plist_label
    )
    if check_freshness:
        from checkout_drift import warn_on_drift  # noqa: PLC0415

        warn_on_drift(daemon_name, Path(script_path).resolve().parent)
