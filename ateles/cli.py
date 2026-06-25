"""The ``ateles`` command-line interface.

Spine of the install-by-package epic (issue #18 / ``docs/installability.md``).
Each verb maps to a workstream; this module is the W0 skeleton, so the verbs are
stubs that announce their workstream and exit non-zero until that workstream
lands. Keeping the surface stable now means later workstreams fill in behaviour
without changing the operator-facing contract.

Deliberately stdlib-only (argparse) so ``python -m ateles`` runs before any
third-party dependency is installed.
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
        "interactive wizard: collect operator domain/channels/locale, write a "
        "validated config + .env",
    ),
    "doctor": (
        "W1",
        "preflight: Neotoma reachable, keys valid, CLIs present, schemas "
        "registered, context entities seeded",
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

#: exit code for a not-yet-implemented verb (distinct from argparse's usage error 2...
#: argparse uses 2 for usage errors, so use 3 here to disambiguate).
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
        subparsers.add_parser(verb, help=f"[{workstream}] {intent}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    return _stub(args.command)


if __name__ == "__main__":
    sys.exit(main())
