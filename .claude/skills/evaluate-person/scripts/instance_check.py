#!/usr/bin/env python3
"""Behavioural write-instance assertion for `evaluate-person` (step 0.1).

SKILL.md 0.1 used to say the MCP tool prefix distinguishes Neotoma instances.
It does not. The prefix is a NAME, and a name is not a behaviour.

Motivating failure, dogfood run 2026-09-22. `mcp__mcpsrv_neotoma__*` WAS the
operator's prefix, so the nominal check passed. `get_authenticated_user` on that
prefix returned:

    {"storage": {"storage_backend": "local",
                 "sqlite_db": "/Users/.../data/neotoma.db"}}

-- a LOCAL SQLite database holding 6 rendered_page rows, while the operator's
hosted prod instance held 285. The tool prefix, the MCP server name, the wrapper
filename and NEOTOMA_ENV=production ALL said "hosted prod", and the running
process was nonetheless local. Root cause since confirmed: the running
`neotoma mcp proxy` processes were launched without `--downstream-url`, so they
fell back to DEFAULT_BASE_URL = http://localhost:3080. Nothing about the name
could have revealed that.

A run can therefore pass the old 0.1 test and still write the evaluation
somewhere the operator will never look -- and it did: `publish_rendered_page`
returned "rendered_page not found" for an entity the hosted REST API served
correctly, because the two were different stores.

This is the same "prove the instrument" discipline SKILL.md 3.1 already demands
for counts, applied to the single most consequential decision in the skill. So
this script does for the WRITE TARGET what instrument_log.py does for a COUNT:
it asks the live endpoint what it actually is and compares the ANSWER, not the
label, against what the run intends.

A failed assertion is a HARD STOP. The run aborts before writing anywhere --
not "writes to the wrong place and reports it afterwards", because by then the
evaluation exists on a client or a scratch store and must be deleted.

Usage
-----
    # assert the live answer, however you obtained it, against the intent
    instance_check.py assert --path I \\
        --prefix mcp__mcpsrv_neotoma__ \\
        --intended-origin https://neotoma.markmhendrickson.com \\
        --probe-json response.json

    # or feed the probe on stdin
    get_authenticated_user_output | instance_check.py assert --path I \\
        --prefix mcp__mcpsrv_neotoma__ \\
        --intended-origin https://neotoma.markmhendrickson.com --probe-json -

    instance_check.py summary --path I

Exit codes: 0 asserted, 1 MISMATCH (abort the run), 2 usage error.

Stdlib only. Makes no network calls of its own: the probe is performed by the
agent through whatever channel it will actually write through, which is the
point -- a probe through a different channel proves nothing about this one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Keys a Neotoma-shaped probe may carry its origin under, most specific first.
ORIGIN_KEYS = ("base_url", "origin", "endpoint", "host", "server_url", "url")
BACKEND_KEYS = ("storage_backend", "backend", "mode")
LOCAL_MARKERS = ("local", "sqlite", "localhost", "127.0.0.1", "0.0.0.0", "::1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _walk(obj, keys: tuple[str, ...]) -> str | None:
    """First value found under any of `keys`, searched depth-first."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            found = _walk(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk(v, keys)
            if found:
                return found
    return None


def _find_sqlite_path(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "sqlite" in k.lower() and isinstance(v, str) and v.strip():
                return v.strip()
            found = _find_sqlite_path(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_sqlite_path(v)
            if found:
                return found
    return None


def _norm_origin(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if "://" not in v:
        v = "https://" + v
    parts = urlsplit(v)
    if not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".rstrip("/").lower()


def evaluate(probe: dict, intended_origin: str) -> tuple[bool, str, dict]:
    """Compare what the endpoint SAYS IT IS against what the run intends.

    Returns (ok, message, observed). Anything unreadable is a MISMATCH: an
    unparseable answer is not a passing answer. Failing open here would restore
    the exact defect -- a check that cannot tell hosted from local.
    """
    observed_origin = _norm_origin(_walk(probe, ORIGIN_KEYS))
    backend = _walk(probe, BACKEND_KEYS)
    sqlite_path = _find_sqlite_path(probe)
    observed = {
        "origin": observed_origin,
        "storage_backend": backend,
        "sqlite_db": sqlite_path,
    }
    want = _norm_origin(intended_origin)
    if want is None:
        return False, f"--intended-origin {intended_origin!r} is not a usable origin", observed

    if sqlite_path:
        return (
            False,
            "MISMATCH: the endpoint reports a local SQLite database at "
            f"{sqlite_path!r}, not {want}. This is the dogfood failure exactly -- "
            "the prefix, the server name and NEOTOMA_ENV all said hosted prod "
            "while the proxy had fallen back to localhost. Do not write.",
            observed,
        )
    if backend and any(m in backend.lower() for m in LOCAL_MARKERS):
        return (
            False,
            f"MISMATCH: storage backend reports {backend!r}, which is not the "
            f"intended hosted instance {want}. Do not write.",
            observed,
        )
    if observed_origin is None:
        return (
            False,
            "MISMATCH: the probe carries no readable origin or storage backend, so "
            "it cannot establish which store the writes will land in. An "
            "unreadable answer is not a passing answer -- re-probe the endpoint "
            "you are about to write through.",
            observed,
        )
    if any(m in observed_origin for m in LOCAL_MARKERS):
        return (
            False,
            f"MISMATCH: the endpoint resolves to {observed_origin}, a local store, "
            f"not the intended {want}. Do not write.",
            observed,
        )
    if observed_origin != want:
        return (
            False,
            f"MISMATCH: the endpoint resolves to {observed_origin}, the run intends "
            f"{want}. A write now lands where the operator will never look. "
            "Do not write.",
            observed,
        )
    return True, f"WRITE INSTANCE CONFIRMED: {observed_origin} (probed, not assumed).", observed


def cmd_assert(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.probe_json == "-" else Path(args.probe_json).read_text()
    try:
        probe = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"MISMATCH: probe output is not JSON ({exc}). A probe that cannot be "
            "read cannot confirm the write target. Do not write.",
            file=sys.stderr,
        )
        return 1

    ok, message, observed = evaluate(probe, args.intended_origin)
    record = {
        "prefix": args.prefix,
        "intended_origin": args.intended_origin,
        "observed": observed,
        "result": "confirmed" if ok else "mismatch",
        "message": message,
        "checked_at": _now(),
    }
    if args.path:
        Path(args.path).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    if not ok:
        print(message, file=sys.stderr)
        print(
            "\nHARD STOP (SKILL.md 0.1). Abort the run before the first write. "
            "Name the OBSERVED store in the run summary, not the tool prefix -- "
            "the prefix is what passed while the writes went to the wrong store.",
            file=sys.stderr,
        )
        return 1
    print(message)
    print(f"  probed through prefix: {args.prefix}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(
            "NO INSTANCE ASSERTION RECORDED -- SKILL.md 0.1 requires the write "
            "target to be probed behaviourally before the first write.",
            file=sys.stderr,
        )
        return 1
    rec = json.loads(path.read_text())
    if rec.get("result") != "confirmed":
        print(f"INSTANCE ASSERTION FAILED: {rec.get('message')}", file=sys.stderr)
        return 1
    obs = rec.get("observed", {})
    print(
        "Write instance (probed): "
        f"{obs.get('origin')} via prefix {rec.get('prefix')}, "
        f"backend {obs.get('storage_backend') or 'unreported'}, "
        f"checked {rec.get('checked_at')}"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("assert")
    a.add_argument("--path", default=None, help="where to record the assertion")
    a.add_argument("--prefix", required=True, help="the MCP tool prefix the writes will use")
    a.add_argument("--intended-origin", required=True,
                   help="the instance this run intends to write to, e.g. https://neotoma.example.com")
    a.add_argument("--probe-json", required=True,
                   help="file holding get_authenticated_user output, or '-' for stdin")
    a.set_defaults(func=cmd_assert)

    s = sub.add_parser("summary")
    s.add_argument("--path", required=True)
    s.set_defaults(func=cmd_summary)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
