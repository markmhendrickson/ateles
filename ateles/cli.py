"""The ``ateles`` command-line interface.

Spine of the install-by-package epic (issue #18 / ``docs/installability.md``).
Each verb maps to a workstream. ``init`` and ``doctor`` are implemented (W1);
the remaining verbs are stubs that announce their workstream and exit 3 until
their workstream lands. Keeping the surface stable means later workstreams fill
in behaviour without changing the operator-facing contract.

The top-level import graph is deliberately stdlib-only (argparse) so
``python -m ateles`` runs before any third-party dependency is installed;
implemented verbs import their (also stdlib-only) modules lazily.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

#: verb -> (workstream, one-line intent). The roadmap is the source of truth;
#: this table is what ``--help`` renders.
VERBS: dict[str, tuple[str, str]] = {
    "init": (
        "W1",
        "interactive wizard: collect operator domain/identity/locale, write a "
        "validated config",
    ),
    "doctor": (
        "W1",
        "diagnose missing setup steps (config, keys, CLIs, Neotoma) and report "
        "the next rung",
    ),
    "provision": (
        "W2",
        "register schemas, seed operator context entities, mint keypairs, create "
        "agent_grants (keystone)",
    ),
    "run": (
        "W6",
        "run a daemon (or all) in the foreground from the daemon registry",
    ),
    "deploy": (
        "W6",
        "render + install scheduler units (launchd / systemd / compose) from the "
        "daemon registry",
    ),
    "mirror": (
        "W7",
        "regenerate SKILL.md from Neotoma on demand (pull-mode, no tunnel/webhook)",
    ),
}

#: Verbs with real behaviour. `provision` is a dry-run planner (W2); the rest
#: of its execution (and run/deploy/mirror) land in later workstreams.
IMPLEMENTED = frozenset({"init", "doctor", "provision"})

#: Exit code for a not-yet-implemented verb — distinct from argparse's usage
#: error (2) so callers can tell "unknown flag" from "not built yet".
NOT_IMPLEMENTED_EXIT = 3

ROADMAP = (
    "roadmap: docs/installability.md · "
    "tracking: github.com/markmhendrickson/ateles/issues/18"
)


def _stub(verb: str) -> int:
    workstream, intent = VERBS[verb]
    print(f"ateles {verb}: planned under workstream {workstream} — not yet implemented.")
    print(f"  intent: {intent}")
    print(f"  {ROADMAP}")
    return NOT_IMPLEMENTED_EXIT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ateles",
        description=(
            "Install-by-package CLI for the Ateles agent swarm. Verbs are "
            "implemented across workstreams W1–W7 (see docs/installability.md)."
        ),
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"ateles {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for verb, (workstream, intent) in VERBS.items():
        sub = subparsers.add_parser(verb, help=f"[{workstream}] {intent}")
        if verb == "doctor":
            sub.add_argument(
                "--no-network",
                action="store_true",
                help="skip the Neotoma reachability probe",
            )
        elif verb == "init":
            sub.add_argument(
                "--non-interactive",
                action="store_true",
                help="don't prompt; seed config from env/existing values",
            )
        elif verb == "provision":
            sub.add_argument(
                "--commit",
                action="store_true",
                help="execute the plan (gated — needs W3/W4 + a live Neotoma; "
                "dry-run by default)",
            )
    return parser


def _run_doctor(args: argparse.Namespace) -> int:
    from .doctor import next_rung, render, run_checks

    checks = run_checks(check_network=not args.no_network)
    print(render(checks))
    return 0 if next_rung(checks) is None else 1


def _run_init(args: argparse.Namespace) -> int:
    from .initialize import run_init

    run_init(non_interactive=args.non_interactive)
    return 0


def _run_provision(args: argparse.Namespace) -> int:
    from .provision import run_provision

    return run_provision(commit=args.commit)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "init":
        return _run_init(args)
    if args.command == "provision":
        return _run_provision(args)
    return _stub(args.command)


if __name__ == "__main__":
    sys.exit(main())
