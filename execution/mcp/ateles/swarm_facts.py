#!/usr/bin/env python3
"""
swarm_facts — answer operational questions about the swarm from LIVE state.

WHY THIS EXISTS
---------------
Agents working on this swarm repeatedly assert operational facts they inferred
rather than checked, and the assertions are wrong. Six instances in a single
session, each cheap to verify and each corrected only after wasted effort:

  1. "Publishing a release is the only way to deploy" — read from ONE `on:`
     key of a workflow file; `workflow_dispatch` was right there.
  2. Scaled the retired `neotoma` Fly app instead of the live
     `neotoma-markmhendrickson`. One DNS lookup distinguishes them.
  3. Assumed streaming inherited a hallucination filter. A grep found zero.
  4. Assumed more reader-pool workers would help. Measurement said otherwise.
  5. Assumed a sibling checkout was current. It was three behind.
  6. Assumed the tailer passed scoped credentials. It passed all 35 keys.

The information was never missing. What was missing was a PROMPT to look and an
obvious PLACE to look. A doc cannot supply either: `docs/` already describes a
checkout-drift guard as though daemons generally call it, when exactly 1 of 18
does. A stale doc is worse than no doc, because it is confidently wrong.

So every answer here is COMPUTED AT CALL TIME from the system of record —
the workflow YAML on disk, DNS, the Fly API, launchd, git. Nothing is
transcribed into prose that can drift out of sync with reality.

FAIL-CLOSED
-----------
Every check returns an explicit `status`. A check that cannot reach its source
returns "unknown" WITH a reason — never a silent empty result that an agent
would read as an all-clear. This mirrors the convention the observability tools
in server.py already follow: an unreachable source must not look like a clean
one, because "I checked and it's fine" and "I couldn't check" lead to opposite
decisions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Bounded so a hung DNS server or Fly API cannot stall an agent's turn.
CMD_TIMEOUT = 20


def _run(cmd: list[str], timeout: int = CMD_TIMEOUT) -> tuple[int, str, str]:
    """Run *cmd*, returning (returncode, stdout, stderr).

    A missing binary is reported as returncode 127 rather than raising, so a
    check degrades to "unknown" instead of taking down the whole call.
    """
    if not shutil.which(cmd[0]):
        return 127, "", f"{cmd[0]} not found on PATH"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", f"{type(exc).__name__}: {exc}"


def _repo_root(explicit: str | None = None) -> Path:
    """The repo to inspect: explicit arg, env override, or this file's repo."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ATELES_REPO_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3]


# ── deploy triggers ──────────────────────────────────────────────────────────


def _parse_triggers(text: str) -> tuple[list[str], dict[str, Any]]:
    """Extract the `on:` trigger names from workflow YAML.

    Parsed with PyYAML when available, else a line-scanner fallback, because
    this check must work even where the MCP server's deps are thin. Note the
    `on:` key: PyYAML resolves a bare `on` to boolean True (YAML 1.1), so the
    key can arrive as either the string "on" or True — reading only one of
    them is its own silent-miss bug.
    """
    detail: dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text) or {}
        on = doc.get("on", doc.get(True))
        if isinstance(on, dict):
            for name, cfg in on.items():
                if isinstance(cfg, dict):
                    detail[str(name)] = cfg
            return [str(k) for k in on.keys()], detail
        if isinstance(on, list):
            return [str(x) for x in on], detail
        if isinstance(on, str):
            return [on], detail
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: find the `on:` block and take its top-level child keys.
    names: list[str] = []
    lines = text.splitlines()
    in_on = False
    on_indent = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if not in_on:
            m = re.match(r"^(on|True|\"on\"|'on'):\s*(.*)$", stripped)
            if m and indent == 0:
                in_on = True
                on_indent = indent
                inline = m.group(2).strip()
                if inline and not inline.startswith("#"):
                    if inline.startswith("["):
                        names += [
                            t.strip().strip("'\"")
                            for t in inline.strip("[]").split(",")
                            if t.strip()
                        ]
                    else:
                        names.append(inline)
            continue
        if indent <= on_indent and stripped:
            break
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", stripped)
        if km and indent == on_indent + 2:
            names.append(km.group(1))
        elif stripped.startswith("- "):
            names.append(stripped[2:].strip().strip("'\""))
    return names, detail


def check_deploy_triggers(
    workflow: str | None = None, repo_root: str | None = None
) -> dict:
    """Every way each GitHub Actions workflow can be started.

    Catches wrong-assumption #1. Reading one `on:` key and concluding "you must
    publish a release" cost a full day when `workflow_dispatch` was available
    the whole time. This lists ALL triggers per workflow, so the complete set is
    visible rather than whichever one an agent's eye landed on first.
    """
    root = _repo_root(repo_root)
    wf_dir = root / ".github" / "workflows"
    result: dict[str, Any] = {
        "question": "What can trigger each workflow (i.e. what deploys/ships)?",
        "source": str(wf_dir),
        "computed_from": "the workflow YAML on disk, all `on:` keys",
    }
    if not wf_dir.is_dir():
        result["status"] = "unknown"
        result["reason"] = f"no workflows directory at {wf_dir}"
        return result

    files = sorted(
        p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml") and p.is_file()
    )
    if workflow:
        want = workflow.lower()
        files = [p for p in files if want in p.name.lower()]
        if not files:
            result["status"] = "unknown"
            result["reason"] = f"no workflow matching {workflow!r} in {wf_dir}"
            return result

    workflows = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            workflows.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        names, detail = _parse_triggers(text)
        entry: dict[str, Any] = {"file": path.name, "triggers": names}
        if "workflow_dispatch" in names:
            cfg = detail.get("workflow_dispatch") or {}
            inputs = list((cfg.get("inputs") or {}).keys()) if isinstance(cfg, dict) else []
            entry["manually_dispatchable"] = True
            entry["dispatch_inputs"] = inputs
            entry["dispatch_command"] = (
                f"gh workflow run {path.name} --ref <branch>"
                + "".join(f" -f {i}=<value>" for i in inputs)
            )
        else:
            entry["manually_dispatchable"] = False
        workflows.append(entry)

    result["status"] = "ok"
    result["workflows"] = workflows
    manual = [w["file"] for w in workflows if w.get("manually_dispatchable")]
    result["interpretation"] = (
        f"{len(workflows)} workflow(s). "
        + (
            f"{len(manual)} can be run manually with `gh workflow run` — "
            "a release is NOT required for these: " + ", ".join(manual)
            if manual
            else "None accept workflow_dispatch; all are event-driven."
        )
    )
    return result


# ── which app serves a domain ────────────────────────────────────────────────


def check_serving_app(domain: str) -> dict:
    """Which Fly app actually serves *domain*, and when it last deployed.

    Catches wrong-assumption #2. Scaling `neotoma` (retired, last deployed
    2026-05-12) instead of `neotoma-markmhendrickson` wasted a debugging cycle;
    DNS answers this in one lookup. Deploy times for every candidate app are
    included because "the app named after the product" is exactly the wrong
    heuristic once a rename has happened.
    """
    result: dict[str, Any] = {
        "question": f"Which app serves {domain}?",
        "domain": domain,
        "computed_from": "live DNS resolution, then the Fly app list",
    }

    chain: list[str] = []
    target = domain
    for _ in range(5):
        rc, out, err = _run(["dig", "+short", target, "CNAME"])
        if rc != 0:
            result["dns_error"] = err or f"dig exited {rc}"
            break
        line = out.splitlines()[0].strip().rstrip(".") if out.strip() else ""
        if not line:
            break
        chain.append(line)
        target = line
    result["cname_chain"] = chain

    if not chain:
        rc, out, _ = _run(["dig", "+short", domain, "A"])
        result["a_records"] = out.splitlines() if rc == 0 and out else []

    fly_app = None
    for hop in chain:
        m = re.match(r"^(?P<app>[a-z0-9][a-z0-9-]*)\.fly\.dev$", hop)
        if m:
            fly_app = m.group("app")
            break
    result["serving_fly_app"] = fly_app

    rc, out, err = _run(["flyctl", "apps", "list", "--json"])
    if rc == 0 and out:
        try:
            apps = json.loads(out)
            listed = []
            for a in apps if isinstance(apps, list) else []:
                name = a.get("Name") or a.get("name")
                listed.append(
                    {
                        "name": name,
                        "status": a.get("Status") or a.get("status"),
                        "last_deploy": a.get("LatestDeploy") or a.get("latestDeploy"),
                        "is_serving_this_domain": name == fly_app,
                    }
                )
            result["fly_apps"] = listed
            match = next((a for a in listed if a["is_serving_this_domain"]), None)
            if match:
                result["serving_app_status"] = match.get("status")
                result["serving_app_last_deploy"] = match.get("last_deploy")
            decoys = [
                a["name"]
                for a in listed
                if a["name"] and fly_app and a["name"] != fly_app
                and (a["name"] in fly_app or fly_app.startswith(a["name"]))
            ]
            if decoys:
                result["similarly_named_apps_NOT_serving_this_domain"] = decoys
        except Exception as exc:
            result["fly_list_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["fly_list_error"] = err or f"flyctl exited {rc}"

    if fly_app:
        result["status"] = "ok"
        note = f"{domain} is served by Fly app '{fly_app}'"
        if result.get("serving_app_last_deploy"):
            note += f" (last deploy {result['serving_app_last_deploy']})"
        if result.get("similarly_named_apps_NOT_serving_this_domain"):
            note += (
                ". Do NOT act on these similarly-named apps, they do not serve it: "
                + ", ".join(result["similarly_named_apps_NOT_serving_this_domain"])
            )
        result["interpretation"] = note + "."
    else:
        result["status"] = "unknown"
        result["reason"] = (
            result.get("dns_error")
            or f"{domain} does not resolve to a *.fly.dev host; it may be served elsewhere"
        )
    return result


# ── checkout freshness ───────────────────────────────────────────────────────


def check_checkout_freshness(path: str | None = None) -> dict:
    """Is a checkout current with its upstream, or is it citing stale code?

    Catches wrong-assumption #5. An agent cited line numbers from
    `~/repos/neotoma` that had moved three checkouts ago. Ahead-only counts as
    drift here for the same reason the daemon guard treats it so: unpushed
    commits in a checkout are invisible to review and one power-cycle from
    being lost.
    """
    root = _repo_root(path)
    result: dict[str, Any] = {
        "question": f"Is {root} current with its upstream?",
        "path": str(root),
        "computed_from": "git rev-list against the tracking ref, after a fetch",
    }
    if not (root / ".git").exists() and not root.is_dir():
        result["status"] = "unknown"
        result["reason"] = f"{root} is not a directory"
        return result

    rc, branch, err = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        result["status"] = "unknown"
        result["reason"] = err or "not a git repository"
        return result
    result["branch"] = branch

    if os.environ.get("ATELES_CHECKOUT_DRIFT_NO_FETCH") != "1":
        frc, _, ferr = _run(["git", "-C", str(root), "fetch", "--quiet"], timeout=30)
        if frc != 0:
            # A failed fetch is NOT drift. Offline must not look identical to
            # unpushed commits, or the warning gets ignored.
            result["fetch_failed"] = ferr or f"git fetch exited {frc}"

    rc, upstream, _ = _run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    if rc != 0 or not upstream:
        upstream = "origin/main"
    result["upstream"] = upstream

    rc, counts, err = _run(
        ["git", "-C", str(root), "rev-list", "--left-right", "--count", f"{upstream}...HEAD"]
    )
    if rc != 0:
        result["status"] = "unknown"
        result["reason"] = err or "could not compare against upstream"
        return result
    parts = counts.split()
    behind = int(parts[0]) if len(parts) > 0 else 0
    ahead = int(parts[1]) if len(parts) > 1 else 0
    result["behind"] = behind
    result["ahead"] = ahead

    rc, porcelain, _ = _run(["git", "-C", str(root), "status", "--porcelain"])
    # Untracked files are not drift — deployment checkouts accumulate logs.
    tracked_dirty = [
        ln for ln in porcelain.splitlines() if ln and not ln.startswith("??")
    ]
    result["dirty_tracked_files"] = len(tracked_dirty)

    rc, head, _ = _run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
    result["head"] = head

    problems = []
    if behind:
        problems.append(f"{behind} commit(s) BEHIND {upstream} — code here is stale")
    if ahead:
        problems.append(f"{ahead} unpushed commit(s) — invisible to review")
    if tracked_dirty:
        problems.append(f"{len(tracked_dirty)} modified tracked file(s)")
    if result.get("fetch_failed"):
        result["status"] = "unknown"
        result["interpretation"] = (
            f"Could not refresh remote refs ({result['fetch_failed']}); "
            "the comparison below may be against stale remote state."
        )
    elif problems:
        result["status"] = "drifted"
        result["interpretation"] = (
            f"{root} has DRIFTED: " + "; ".join(problems)
            + ". Do not cite line numbers or behaviour from this checkout as current."
        )
    else:
        result["status"] = "ok"
        result["interpretation"] = f"{root} is current with {upstream} at {head}."
    return result


# ── daemon reality ───────────────────────────────────────────────────────────


def check_daemons(repo_root: str | None = None) -> dict:
    """Which daemons exist on disk, which are actually loaded, which are neither.

    Catches the class of error behind the stale-doc problem the operator named:
    CLAUDE.md describes daemons that are dead. This compares the daemon
    directories in the repo against what launchd has actually loaded, so
    "described", "installed", and "running" are three separate columns instead
    of one assumption.
    """
    root = _repo_root(repo_root)
    daemon_dir = root / "execution" / "daemons"
    result: dict[str, Any] = {
        "question": "Which daemons exist, and which are actually running?",
        "computed_from": "daemon dirs on disk vs `launchctl list` vs LaunchAgents plists",
        "daemon_source_dir": str(daemon_dir),
    }

    on_disk = (
        sorted(p.name for p in daemon_dir.iterdir() if p.is_dir())
        if daemon_dir.is_dir()
        else []
    )
    result["daemons_in_repo"] = on_disk

    prefix = os.environ.get("ATELES_LAUNCHD_PREFIX", "com.ateles.")
    rc, out, err = _run(["launchctl", "list"])
    loaded: dict[str, dict] = {}
    if rc == 0:
        for line in out.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            pid, status, label = cols[0], cols[1], cols[2]
            if not label.startswith(prefix):
                continue
            loaded[label[len(prefix):]] = {
                "label": label,
                "pid": None if pid == "-" else pid,
                "last_exit_status": status,
                "running": pid != "-",
            }
    else:
        result["launchctl_error"] = err or f"launchctl exited {rc}"

    result["loaded_launchd_jobs"] = loaded

    running = sorted(k for k, v in loaded.items() if v["running"])
    loaded_not_running = sorted(k for k, v in loaded.items() if not v["running"])
    in_repo_not_loaded = sorted(set(on_disk) - set(loaded))
    loaded_not_in_repo = sorted(set(loaded) - set(on_disk))

    result["running"] = running
    result["loaded_but_not_running"] = loaded_not_running
    result["in_repo_but_never_loaded"] = in_repo_not_loaded
    result["loaded_but_no_repo_dir"] = loaded_not_in_repo

    if "launchctl_error" in result:
        result["status"] = "unknown"
        result["interpretation"] = (
            "Could not read launchd state; daemon liveness is UNKNOWN, not clear."
        )
    else:
        result["status"] = "ok"
        result["interpretation"] = (
            f"{len(on_disk)} daemon(s) in the repo, {len(loaded)} loaded in launchd, "
            f"{len(running)} actually running. "
            f"In repo but never loaded ({len(in_repo_not_loaded)}): "
            f"{', '.join(in_repo_not_loaded) or 'none'}. "
            "A daemon present in the repo or named in a doc is NOT evidence it runs."
        )
    return result


# ── does this code path exist ────────────────────────────────────────────────


def check_code_path(pattern: str, repo_root: str | None = None) -> dict:
    """Does a named mechanism exist in the code, and what actually calls it?

    Catches wrong-assumptions #3 and #6 — the "surely it inherits X" class.
    Reports the callers separately from the definition, because the stale-doc
    failure in this repo is exactly that shape: `checkout_drift` is DEFINED and
    documented as though daemons call it, and precisely one of eighteen does.
    Existence and use are different questions, so they get different fields.
    """
    root = _repo_root(repo_root)
    result: dict[str, Any] = {
        "question": f"Does {pattern!r} exist in the code, and what calls it?",
        "pattern": pattern,
        "repo_root": str(root),
        "computed_from": "ripgrep/grep over the working tree at call time",
    }

    tool = "rg" if shutil.which("rg") else "grep"
    if tool == "rg":
        cmd = ["rg", "-n", "--no-heading", "-S", pattern, str(root)]
    else:
        cmd = ["grep", "-rn", "-I", pattern, str(root)]
    rc, out, err = _run(cmd, timeout=30)
    if rc not in (0, 1):
        result["status"] = "unknown"
        result["reason"] = err or f"{tool} exited {rc}"
        return result

    hits = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath = parts[0]
        if "/.git/" in fpath or "/node_modules/" in fpath:
            continue
        try:
            rel = str(Path(fpath).relative_to(root))
        except ValueError:
            rel = fpath
        hits.append({"file": rel, "line": int(parts[1]) if parts[1].isdigit() else None,
                     "text": parts[2].strip()[:200]})

    result["match_count"] = len(hits)
    result["matches"] = hits[:60]
    if len(hits) > 60:
        result["truncated"] = f"showing 60 of {len(hits)} matches"

    tests = [h for h in hits if "test" in h["file"].lower()]
    non_test = [h for h in hits if "test" not in h["file"].lower()]
    result["non_test_files"] = sorted({h["file"] for h in non_test})
    result["test_files"] = sorted({h["file"] for h in tests})

    result["status"] = "ok"
    if not hits:
        result["interpretation"] = (
            f"ZERO matches for {pattern!r}. This mechanism does NOT exist here — "
            "do not assume it is inherited from elsewhere."
        )
    else:
        result["interpretation"] = (
            f"{len(hits)} match(es) across {len(result['non_test_files'])} non-test "
            f"file(s) and {len(result['test_files'])} test file(s). "
            "Existence is not use: check the non-test callers before assuming "
            "this fires on any given path."
        )
    return result


# ── registry ─────────────────────────────────────────────────────────────────

CHECKS = {
    "deploy_triggers": check_deploy_triggers,
    "serving_app": check_serving_app,
    "checkout_freshness": check_checkout_freshness,
    "daemons": check_daemons,
    "code_path": check_code_path,
}


def check_swarm_fact(check: str, **kwargs: Any) -> dict:
    """Dispatch to a named check. Unknown names list what IS available."""
    fn = CHECKS.get(check)
    if not fn:
        return {
            "status": "unknown",
            "error": f"unknown check: {check!r}",
            "available_checks": sorted(CHECKS),
        }
    accepted = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    filtered = {k: v for k, v in kwargs.items() if k in accepted and v is not None}
    try:
        return fn(**filtered)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "status": "unknown",
            "check": check,
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "The check failed. This is NOT an all-clear — the fact is unverified.",
        }
