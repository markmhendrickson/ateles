#!/usr/bin/env python3
"""render_hooks.py — render harness hook adapters from Neotoma `hook_policy`
entities + harness-agnostic `hooks/logic/*.py` modules.

Mirrors the pattern established by `render_agent_docs.py` for agents:

    Neotoma `hook_policy` entity   (source of truth: intent, enforces, wiring)
                +
    hooks/logic/<hook_name>.py     (source of truth: pure, harness-agnostic behavior)
                v
    .claude/hooks/<hook_name>.py   (GENERATED derived artifact — thin adapter)
    .claude/settings.json          (GENERATED matcher/event wiring, merged in-place)

Neither generated artifact is hand-edited. `--check` verifies disk matches the
policy+logic sources and exits nonzero on drift; `--write` regenerates them.

A hook with logic but NO matching `hook_policy` entity is a governance gap —
the generator refuses to produce or update its adapter (fail closed) rather
than let an ungoverned adapter exist. See docs/hooks/POLICY_TEMPLATE.md.

Usage:
    render_hooks.py --check                    # verify disk matches policy+logic; exit 1 on drift
    render_hooks.py --write                     # regenerate adapters + settings.json wiring
    render_hooks.py --check --harness openclaw   # report NOT_IMPLEMENTED gaps for another harness
    render_hooks.py --help

Exit codes (versioned contract — a change here is a breaking change to CI):
    0  OK / write succeeded
    1  DRIFT (--check found stale adapters) or missing-policy refusal (--write)
    2  WARN (an active adapter has no active hook_policy — ungoverned)
    3  NOT_IMPLEMENTED (--harness targets a harness with no adapter for a governed hook)

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env), matching render_agent_docs.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGIC_DIR = REPO_ROOT / "hooks" / "logic"
ADAPTERS_DIR = {
    "claude": REPO_ROOT / ".claude" / "hooks",
}
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
REPOSITORY = "markmhendrickson/ateles"
POLICY_TEMPLATE_DOC = "docs/hooks/POLICY_TEMPLATE.md"

ADAPTER_HEADER = (
    "# GENERATED FILE. Do not edit directly.\n"
    "# Source of truth: hooks/logic/{hook_name}.py (behavior) + Neotoma\n"
    "# hook_policy entity {policy_id} (wiring/intent). Regenerate with:\n"
    "#   python3 execution/scripts/render_hooks.py --write\n"
)


def _display_path(path: Path) -> str:
    """Path for human-readable output: relative to REPO_ROOT when possible
    (the normal case), else the absolute path (tests point ADAPTERS_DIR at a
    tmp dir outside the repo)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Neotoma retrieval
# --------------------------------------------------------------------------

def _load_env() -> tuple[str, str]:
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key == "NEOTOMA_BASE_URL" and not base_url:
                base_url = value
            elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                token = value
    if not base_url:
        sys.exit("NEOTOMA_BASE_URL not set (env or ~/.config/neotoma/.env)")
    return base_url.rstrip("/"), token


def _request(url: str, token: str, payload: dict | None = None, retries: int = 5) -> dict:
    """Same retry/auth discipline as render_agent_docs.py — reuses the invoking
    developer's or CI's existing Neotoma credentials; no separate credential
    path is introduced for this generator."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "ateles-neotoma-sync/1.0")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(payload).encode()
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionError) as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"Neotoma unreachable after {retries} tries: {last}")


def _unwrap(entity: dict) -> dict:
    s = entity.get("snapshot", entity)
    if isinstance(s.get("snapshot"), dict):
        s = s["snapshot"]
    return s


def fetch_hook_policies(base_url: str, token: str, repository: str = REPOSITORY) -> dict[tuple[str, str], list[dict]]:
    """Fetch active hook_policy entities scoped to this repository, keyed by
    (hook_name, harness). NOTE: this deployment is single-tenant-per-instance
    (one Neotoma instance per operator); the repository filter below is the
    only scoping boundary this generator applies today. A future multi-tenant
    Neotoma deployment MUST add tenant/workspace scoping to this query rather
    than relying on repository alone — do not assume repository scoping is
    sufficient tenant isolation.
    """
    data = _request(f"{base_url}/entities/query", token, {"entity_type": "hook_policy", "limit": 500})
    ents = data.get("entities") or data.get("results") or []
    by_key: dict[tuple[str, str], list[dict]] = {}
    for e in ents:
        s = _unwrap(e)
        if s.get("repository") != repository:
            continue
        if not s.get("hook_name") or not s.get("harness"):
            continue
        s["_entity_id"] = e.get("entity_id") or s.get("entity_id", "")
        key = (s["hook_name"], s["harness"])
        by_key.setdefault(key, []).append(s)
    return by_key


def _active_policy(candidates: list[dict], hook_name: str) -> dict | None:
    """Resolve one candidate to govern this hook. Ambiguous matches (more than
    one ACTIVE policy for the same hook_name+harness+repository) are an error
    — never 'pick the first result'."""
    active = [c for c in candidates if c.get("status") == "active"]
    if len(active) > 1:
        ids = ", ".join(c.get("_entity_id", "?") for c in active)
        raise SystemExit(
            f"ERROR: ambiguous hook_policy for '{hook_name}' — {len(active)} active "
            f"candidates ({ids}). Refusing to guess; archive all but one."
        )
    return active[0] if active else None


# --------------------------------------------------------------------------
# Logic discovery
# --------------------------------------------------------------------------

def discover_logic_modules() -> list[str]:
    """Hook names with a hooks/logic/<name>.py module (excludes __init__/tests)."""
    if not LOGIC_DIR.exists():
        return []
    names = []
    for p in LOGIC_DIR.glob("*.py"):
        if p.stem == "__init__":
            continue
        names.append(p.stem)
    return sorted(names)


# --------------------------------------------------------------------------
# Adapter generation (harness: claude)
# --------------------------------------------------------------------------

def claude_adapter_source(hook_name: str, policy: dict) -> str:
    """Thin Claude Code adapter: stdlib-only stdin/stdout wiring around the
    harness-agnostic hooks/logic/<hook_name>.py module. Catches any exception
    from the logic module and emits a one-line additionalContext naming the
    hook + exception — never a bare stack trace."""
    header = ADAPTER_HEADER.format(hook_name=hook_name, policy_id=policy.get("_entity_id", "?"))
    event = policy.get("event", "")
    return (
        header
        + "\n"
        + f'"""Adapter for the {hook_name} hook ({event}).\n\n'
        + f"Wiring/intent governed by Neotoma hook_policy `{policy.get('_entity_id','?')}`.\n"
        + f"Behavior lives in hooks/logic/{hook_name}.py (harness-agnostic).\n"
        + '"""\n'
        + "from __future__ import annotations\n\n"
        + "import json\n"
        + "import sys\n"
        + "from pathlib import Path\n\n"
        + "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
        + f"from hooks.logic.{hook_name} import handle  # noqa: E402\n\n\n"
        + "def main() -> int:\n"
        + "    try:\n"
        + "        raw = sys.stdin.read()\n"
        + "        event = json.loads(raw) if raw.strip() else {}\n"
        + "    except Exception:\n"
        + "        return 0  # fail open: malformed stdin never blocks the harness\n\n"
        + "    try:\n"
        + "        context = handle(event)\n"
        + "    except Exception as exc:  # noqa: BLE001 — never let a bare traceback leak\n"
        + "        print(json.dumps({\n"
        + f'            "hookSpecificOutput": {{\n'
        + f'                "hookEventName": "{event}",\n'
        + f'                "additionalContext": f"[{hook_name}] hook error: {{type(exc).__name__}}: {{exc}}",\n'
        + "            }\n"
        + "        }))\n"
        + "        return 0  # fail open\n\n"
        + "    if context:\n"
        + "        print(json.dumps({\n"
        + '            "hookSpecificOutput": {\n'
        + f'                "hookEventName": "{event}",\n'
        + '                "additionalContext": context,\n'
        + "            }\n"
        + "        }))\n"
        + "    return 0\n\n\n"
        + 'if __name__ == "__main__":\n'
        + "    sys.exit(main())\n"
    )


def _settings_hook_entry(hook_name: str, policy: dict) -> dict:
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{hook_name}.py"',
            }
        ]
    }
    matcher = policy.get("matcher") or ""
    if matcher:
        entry = {"matcher": matcher, **entry}
    return entry


def _hook_entry_matches(existing: dict, generated: dict) -> bool:
    return existing.get("matcher") == generated.get("matcher") and existing.get("hooks") == generated.get("hooks")


def merge_settings_wiring(settings: dict, managed: dict[str, dict]) -> tuple[dict, list[str]]:
    """Merge generated hook wiring into settings.json's hooks block, touching
    ONLY entries for hooks this generator manages (by exact command path
    match). Hand-authored hook entries (e.g. ateles-session-start.sh) are left
    untouched. Returns (new_settings, change_summary_lines)."""
    settings = json.loads(json.dumps(settings))  # deep copy
    hooks_block = settings.setdefault("hooks", {})
    changes: list[str] = []

    for hook_name, policy in managed.items():
        event = policy["event"]
        generated_entry = _settings_hook_entry(hook_name, policy)
        command_needle = f".claude/hooks/{hook_name}.py"
        event_list = hooks_block.setdefault(event, [])

        existing_idx = None
        for i, entry in enumerate(event_list):
            for h in entry.get("hooks", []):
                if command_needle in h.get("command", ""):
                    existing_idx = i
                    break
            if existing_idx is not None:
                break

        if existing_idx is None:
            event_list.append(generated_entry)
            changes.append(f"  + wired {hook_name} into {event} (new)")
        elif not _hook_entry_matches(event_list[existing_idx], generated_entry):
            old_matcher = event_list[existing_idx].get("matcher")
            new_matcher = generated_entry.get("matcher")
            if old_matcher != new_matcher:
                changes.append(
                    f"  ~ {hook_name}: matcher changed {old_matcher!r} -> {new_matcher!r} in {event}"
                )
            else:
                changes.append(f"  ~ {hook_name}: command wiring updated in {event}")
            event_list[existing_idx] = generated_entry
        # else: already matches, no change

    return settings, changes


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

class MissingPolicyError(SystemExit):
    def __init__(self, hook_name: str):
        super().__init__(
            f"ERROR: no execution_policy entity found for hook '{hook_name}'\n"
            f"  author one first: see {POLICY_TEMPLATE_DOC}, or run "
            f"`neotoma store --entity-type hook_policy ...`"
        )


def _adapter_targets(hook_names: list[str], policies: dict[tuple[str, str], list[dict]], harness: str) -> dict[Path, str]:
    """Map adapter output path -> generated content, for governed hooks only.
    Raises MissingPolicyError (fail closed) if a logic module has no active
    policy for this harness."""
    if harness not in ADAPTERS_DIR:
        return {}
    out: dict[Path, str] = {}
    for hook_name in hook_names:
        candidates = policies.get((hook_name, harness), [])
        policy = _active_policy(candidates, hook_name)
        if policy is None:
            raise MissingPolicyError(hook_name)
        out[ADAPTERS_DIR[harness] / f"{hook_name}.py"] = claude_adapter_source(hook_name, policy)
    return out


def _governed_hooks(hook_names: list[str], policies: dict[tuple[str, str], list[dict]], harness: str) -> dict[str, dict]:
    """hook_name -> active policy, for hooks that have BOTH logic and an active
    policy for this harness. Used for settings.json wiring (skips ungoverned
    hooks rather than raising, since --check separately reports WARN for
    those)."""
    governed = {}
    for hook_name in hook_names:
        policy = _active_policy(policies.get((hook_name, harness), []), hook_name)
        if policy is not None:
            governed[hook_name] = policy
    return governed


# --------------------------------------------------------------------------
# check / write
# --------------------------------------------------------------------------

def check(hook_names: list[str], policies: dict[tuple[str, str], list[dict]], harness: str) -> int:
    if harness not in ADAPTERS_DIR:
        # NOT_IMPLEMENTED reporting path for another harness: report gaps for
        # every hook that has policy+logic for at least one harness (i.e. it
        # is a governed hook in general, just not ported to this harness).
        any_governed = {h for h in hook_names if any(k[0] == h for k in policies)}
        reported = False
        for h in sorted(any_governed):
            print(f"NOT_IMPLEMENTED: {h} (policy exists, no {harness} adapter)")
            reported = True
        if reported:
            return 3
        print(f"OK: no hooks reference harness '{harness}'")
        return 0

    # Split hooks into governed (has an active policy for this harness -> can
    # compute drift) vs ungoverned (no active policy). A never-governed hook
    # with no adapter on disk yet is a hard refusal (missing_policy); a
    # previously-governed hook whose policy was since archived/deleted while
    # its adapter is STILL deployed is a WARN, not a refusal — the adapter is
    # running, just unaccountably so.
    missing_policy = []
    for hook_name in hook_names:
        candidates = policies.get((hook_name, harness), [])
        if _active_policy(candidates, hook_name) is None:
            missing_policy.append(hook_name)

    targets = _adapter_targets([h for h in hook_names if h not in missing_policy], policies, harness)

    failures = []
    for path, content in targets.items():
        on_disk = path.read_text() if path.exists() else ""
        if on_disk != content:
            failures.append(_display_path(path))

    # WARN: adapter deployed on disk but its policy is archived/missing for
    # this harness — running ungoverned.
    warnings = []
    refusals = []
    for hook_name in missing_policy:
        adapter_path = ADAPTERS_DIR[harness] / f"{hook_name}.py"
        if adapter_path.exists():
            warnings.append(hook_name)
        else:
            refusals.append(hook_name)

    if refusals:
        for hook_name in refusals:
            print(str(MissingPolicyError(hook_name)))
        return 1

    if failures:
        print(f"DRIFT: {len(failures)}/{len(targets)} hook adapter(s) do not match policy+logic")
        for f in failures:
            hook_name = Path(f).stem
            print(f"  DRIFT: .claude/hooks/{hook_name}.py does not match hooks/logic/{hook_name}.py")
            print(f"    run: python render_hooks.py --write")
        return 1

    if warnings:
        for hook_name in warnings:
            print(
                f"WARN: hooks/logic/{hook_name}.py has no active hook_policy for harness "
                f"'{harness}' — adapter is running ungoverned"
            )
        return 2

    print(f"OK: {len(targets)}/{len(targets)} hook adapters match policy+logic")
    return 0


def write(hook_names: list[str], policies: dict[tuple[str, str], list[dict]], harness: str) -> int:
    if harness not in ADAPTERS_DIR:
        sys.exit(f"ERROR: --write does not support harness '{harness}' yet (no adapter generator wired)")

    # Refuse per-hook, not as an all-or-nothing batch: one hook missing an
    # active policy (never authored, or archived) must not block --write from
    # regenerating every OTHER hook's already-governed adapter. This is
    # exactly the "logic added before policy is authored" case the workflow
    # expects to hit routinely as new hooks are added one at a time.
    missing_policy = [h for h in hook_names if _active_policy(policies.get((h, harness), []), h) is None]
    for hook_name in missing_policy:
        print(str(MissingPolicyError(hook_name)))

    governable = [h for h in hook_names if h not in missing_policy]
    targets = _adapter_targets(governable, policies, harness)

    written = []
    unchanged = []
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text() if path.exists() else None
        if before == content:
            unchanged.append(path)
            continue
        path.write_text(content)
        written.append(path)

    governed = _governed_hooks(hook_names, policies, harness)
    settings_before = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
    settings_after, wiring_changes = merge_settings_wiring(settings_before, governed)
    if settings_after != settings_before:
        SETTINGS_PATH.write_text(json.dumps(settings_after, indent=2) + "\n")

    print(f"rendered {len(targets)} hook adapter(s) for harness '{harness}':")
    for p in written:
        print(f"  wrote {_display_path(p)}")
    for p in unchanged:
        print(f"  unchanged {_display_path(p)}")
    if wiring_changes:
        print("settings.json wiring changes:")
        for line in wiring_changes:
            print(line)
    elif not written:
        print("no changes (idempotent no-op)")
    return 1 if missing_policy else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify disk matches policy+logic sources; exit 1 on drift, 2 on WARN, 3 on NOT_IMPLEMENTED")
    parser.add_argument("--write", action="store_true", help="regenerate adapters + settings.json wiring from policy+logic sources")
    parser.add_argument("--harness", default="claude", help="target harness (default: claude)")
    args = parser.parse_args()

    if not args.check and not args.write:
        parser.print_help()
        return 0

    base_url, token = _load_env()
    # Repo CI does not yet provision NEOTOMA_BEARER_TOKEN (same gap documented
    # in agent-config-validation.yml). --check must SKIP cleanly rather than
    # fail the lane on 401 — unit tests already assert the contract without a
    # live Neotoma call. --write still requires a token (mutate path).
    if not (token or "").strip():
        if args.write:
            sys.exit(
                "NEOTOMA_BEARER_TOKEN not set (env or ~/.config/neotoma/.env) — "
                "required for --write"
            )
        print(
            "SKIP: NEOTOMA_BEARER_TOKEN unset — cannot verify hook_policy drift "
            "against Neotoma (CI secret not yet provisioned)"
        )
        return 0

    hook_names = discover_logic_modules()
    policies = fetch_hook_policies(base_url, token)

    if args.write:
        return write(hook_names, policies, args.harness)
    return check(hook_names, policies, args.harness)


if __name__ == "__main__":
    sys.exit(main())
