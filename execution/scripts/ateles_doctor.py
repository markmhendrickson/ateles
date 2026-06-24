#!/usr/bin/env python3
"""ateles doctor — onboarding preflight.

Reports which rung of the onboarding ladder the current environment can reach,
and what blocks the next one. Stdlib-only and fail-soft: any probe that errors is
treated as "not satisfied" rather than crashing. Checks reflect the documented
0->6 ladder in docs/README.md.

Usage:
    python3 execution/scripts/ateles_doctor.py        # human report
    python3 execution/scripts/ateles_doctor.py --json # machine-readable
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.path.expanduser("~"))


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _neotoma_reachable(base_url: str) -> bool:
    """Best-effort: any HTTP response (even 401/403) means the server is up."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            return 200 <= resp.status < 600
    except urllib.error.HTTPError:
        return True  # server answered, just not 2xx
    except Exception:
        return False


def _keys_dir() -> Path:
    cand = _env("ATELES_PRIVATE_KEYS_DIR")
    if cand:
        return Path(os.path.expanduser(cand))
    for p in (HOME / ".ateles-private" / "keys", REPO_ROOT.parent / "ateles-private" / "keys"):
        if p.exists():
            return p
    return HOME / ".ateles-private" / "keys"


def _has_keypair() -> bool:
    d = _keys_dir()
    try:
        return d.is_dir() and any(f.is_file() for f in d.iterdir())
    except Exception:
        return False


def _launchd_plists() -> bool:
    try:
        return any((HOME / "Library" / "LaunchAgents").glob("com.ateles.*.plist"))
    except Exception:
        return False


def _sops_key() -> bool:
    return bool(_env("SOPS_AGE_KEY")) or (HOME / ".config" / "sops" / "age" / "keys.txt").exists()


def _daemon_count() -> int:
    try:
        return sum(1 for p in (REPO_ROOT / "execution" / "daemons").iterdir() if p.is_dir())
    except Exception:
        return 0


def build_rungs() -> list[dict]:
    claude = shutil.which("claude")
    n_base = _env("NEOTOMA_BASE_URL")
    n_tok = _env("NEOTOMA_BEARER_TOKEN", "NEOTOMA_BEARER_TOKEN_PROD")
    tg = bool(_env("TELEGRAM_BOT_TOKEN")) and bool(_env("TELEGRAM_CHAT_ID"))
    daemons = _daemon_count()

    reach = _neotoma_reachable(n_base) if n_base else False

    return [
        {
            "rung": 0, "name": "Comprehend",
            "ok": (REPO_ROOT / "docs" / "architecture.md").exists(),
            "detail": f"repo checked out at {REPO_ROOT}",
            "fix": "Clone the repo and read docs/architecture.md.",
        },
        {
            "rung": 1, "name": "First agent (stub mode)",
            "ok": bool(claude),
            "detail": f"claude CLI: {claude or 'not found on PATH'}",
            "fix": "Install the Claude CLI (npm i -g @anthropic-ai/claude-code) so it is on PATH.",
        },
        {
            "rung": 2, "name": "Connect memory (Neotoma)",
            "ok": bool(n_base and n_tok),
            "detail": f"NEOTOMA_BASE_URL={'set' if n_base else 'missing'}, "
                      f"token={'set' if n_tok else 'missing'}, "
                      f"reachable={'yes' if reach else 'no/unknown'}",
            "fix": "Set NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN (see .env.example).",
        },
        {
            "rung": 3, "name": "First daemon, foreground",
            "ok": tg and daemons > 0,
            "detail": f"Telegram channel={'set' if tg else 'missing'}, daemons on disk={daemons}",
            "fix": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, then run e.g. "
                   "python3 execution/daemons/morning-brief/morning-brief.py.",
        },
        {
            "rung": 4, "name": "Attributed identity (AAuth)",
            "ok": _has_keypair(),
            "detail": f"keypair dir: {_keys_dir()} ({'present' if _has_keypair() else 'empty/missing'})",
            "fix": "Mint a keypair: python3 execution/scripts/mint_daemon_keypair.py <agent>, "
                   "and create its agent_grant.",
        },
        {
            "rung": 5, "name": "Persist & schedule",
            "ok": (bool(shutil.which("docker")) or _launchd_plists()) and _sops_key(),
            "detail": f"docker={'yes' if shutil.which('docker') else 'no'}, "
                      f"launchd plists={'yes' if _launchd_plists() else 'no'}, "
                      f"sops age key={'yes' if _sops_key() else 'no'}",
            "fix": "Install docker or load launchd plists, and provision the SOPS age key "
                   "(see docs/secrets_management.md).",
        },
        {
            "rung": 6, "name": "Expand & extend",
            "ok": True,
            "detail": "author your own agent_definition / workflow_definition entities",
            "fix": "See docs/swarm_orchestration.md.",
        },
    ]


def reachable_rung(rungs: list[dict]) -> int:
    """Highest rung N such that every rung 0..N is satisfied."""
    reached = -1
    for r in rungs:
        if r["ok"]:
            reached = r["rung"]
        else:
            break
    return reached


def main(argv: list[str]) -> int:
    rungs = build_rungs()
    reached = reachable_rung(rungs)

    if "--json" in argv:
        print(json.dumps({"reachable_rung": reached, "rungs": rungs}, indent=2))
        return 0

    print("\n  ateles doctor — onboarding preflight\n")
    for r in rungs:
        if r["rung"] <= reached:
            mark = "\033[32m✓\033[0m"
        elif r["rung"] == reached + 1:
            mark = "\033[31m✗\033[0m"
        else:
            mark = "\033[90m·\033[0m"
        print(f"  {mark} rung {r['rung']}  {r['name']}")
        print(f"        {r['detail']}")
    print()
    if reached >= 6:
        print("  \033[32mAll rungs reachable.\033[0m You can run and extend the full swarm.\n")
    else:
        nxt = rungs[reached + 1]
        print(f"  Next: rung {nxt['rung']} — {nxt['name']}")
        print(f"        {nxt['fix']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
