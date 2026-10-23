"""Agentic eval of the operator command that clears a Fly delivery receipt.

Drives the REAL command entry (``main()`` with argv, in-process; plus real
subprocess runs of ``list`` and a dry-run ``clear``) against throwaway ledgers,
with faults injected at the audit append, the directory sync, the ledger write
and the read-back. The judge checks the exit code, whether the receipt is still
in the ledger, the audit events, the operator's messages, and that neither an
exception's text nor a credential-shaped value reaches the audit file or the
output. ``MUTATIONS`` are source changes to a copy of the command that must turn
scenarios red (the judge can go red).
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
APIS = HERE.parents[1] / "daemons" / "apis"
sys.path.insert(0, str(APIS))

import build_delivery as delivery  # noqa: E402
import fly_runner  # noqa: E402

KEY = "owner/repo#123"
OTHER = "owner/repo#124"
ERROR_TEXT = "EVAL-ERROR-TEXT-must-not-leak"
# A credential-shaped value planted in a receipt; built from fragments so no
# secret-shaped literal sits in the source.
CREDENTIAL = "gh" + "p_" + "EVALcredentialMustNeverBeShown" + "0123456789"
COMMAND = APIS / "fly_delivery_receipt.py"

# Source changes to a copy of the command; each must turn a scenario red.
MUTATIONS = {
    # a removal that raised but reads back gone is reported as complete
    "readback_gone_is_success": ('    stage = ("remove_failed" if not gone',
                                 '    problem = problem if not gone else ""\n'
                                 '    stage = ("remove_failed" if not gone'),
    # the audit file is synced but its directory is not
    "file_only_fsync": ("    _fsync_dir(path.parent)\n", ""),
}


def load_scenarios() -> list[dict]:
    return json.loads((HERE / "fixtures/scenarios.json").read_text())["scenarios"]


def load_command(mutation: str | None = None) -> types.ModuleType:
    """The command as a module; with *mutation*, a changed copy of its source."""
    source = COMMAND.read_text()
    if mutation:
        old, new = MUTATIONS[mutation]
        assert old in source, mutation
        source = source.replace(old, new, 1)
    module = types.ModuleType("fly_delivery_receipt_under_eval")
    module.__file__ = str(COMMAND)
    exec(compile(source, str(COMMAND), "exec"), module.__dict__)  # noqa: S102
    return module


def _boom() -> OSError:
    return OSError(ERROR_TEXT)


def inject(fault, cli, ledger, monkeypatch) -> None:
    real_append, real_save = cli._append_audit, ledger._save
    if fault == "first_append":
        def append(led, line):
            if line["stage"] == "removing":
                raise _boom()
            return real_append(led, line)
        monkeypatch.setattr(cli, "_append_audit", append)
    elif fault == "closing_append":
        def append(led, line):
            if line["stage"] != "removing":
                raise _boom()
            return real_append(led, line)
        monkeypatch.setattr(cli, "_append_audit", append)
    elif fault == "raise_after_replace":
        def save(data):
            real_save(data)
            raise _boom()
        monkeypatch.setattr(ledger, "_save", save)
    elif fault == "raise_before_replace":
        def save(data):
            raise _boom()
        monkeypatch.setattr(ledger, "_save", save)
    elif fault == "dir_sync_removing":
        def sync(directory):
            raise _boom()
        monkeypatch.setattr(cli, "_fsync_dir", sync)
    elif fault == "dir_sync_closing":
        calls = []

        def sync(directory):
            calls.append(directory)
            if len(calls) == 2:
                raise _boom()
        monkeypatch.setattr(cli, "_fsync_dir", sync)
    elif fault == "readback_unreadable":
        real_read, calls = ledger.delivery_receipts, []

        def read():
            calls.append(1)
            if len(calls) > 1:
                raise _boom()
            return real_read()
        monkeypatch.setattr(ledger, "delivery_receipts", read)
    else:
        assert fault is None, fault


def make_ledger(tmp_path, monkeypatch, *, malformed=False):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv(delivery.FLY_DELIVERY_REGISTRY_ENV, str(path))
    ledger = fly_runner.FlyLedger(path)
    entry = {"status": "not_delivered", "branch": "cicada/issue-123-eval", "run_id": "run-eval",
             "commit": "c" * 40, "recorded_at": 1_700_000_000.0, "token": "t-1",
             "reason": CREDENTIAL}
    assert ledger.reserve_delivery(KEY, entry)
    assert ledger.reserve_delivery(OTHER, {**entry, "status": "pr_failed", "token": "t-2"})
    if malformed:
        path.write_text("{not json")
    return ledger


def run_scenario(scenario, tmp_path, monkeypatch, *, mutation=None) -> dict:
    cli = load_command(mutation)
    ledger = make_ledger(tmp_path, monkeypatch, malformed=scenario.get("ledger") == "malformed")
    before = ledger.path.read_bytes()
    inject(scenario.get("fault"), cli, ledger, monkeypatch)
    argv = [a.replace("{KEY}", KEY) for a in scenario["argv"]]
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(argv, ledger=ledger, out=out, err=err)
    audit = cli.audit_path(ledger)
    audit_text = audit.read_text() if audit.exists() else ""
    try:
        present = KEY in fly_runner.FlyLedger(ledger.path).delivery_receipts()
    except (OSError, fly_runner.FlyLedgerError):
        present = None
    return {"rc": rc, "out": out.getvalue(), "err": err.getvalue(), "present": present,
            "events": [json.loads(x)["stage"] for x in audit_text.splitlines()],
            "audit": audit_text, "unchanged": ledger.path.read_bytes() == before,
            "other_kept": present is not None and OTHER in fly_runner.FlyLedger(
                ledger.path).delivery_receipts()}  # fmt: skip


def judge(scenario, got) -> list[str]:
    problems = []
    if got["rc"] != scenario["rc"]:
        problems.append(f"exit {got['rc']}, expected {scenario['rc']}")
    if "present" in scenario and got["present"] != scenario["present"]:
        problems.append(f"receipt present={got['present']}, expected {scenario['present']}")
    if got["events"] != scenario["events"]:
        problems.append(f"audit events {got['events']}, expected {scenario['events']}")
    if scenario.get("unchanged") and not got["unchanged"]:
        problems.append("the ledger changed")
    if got["present"] is not None and not got["other_kept"]:
        problems.append("another receipt was removed")
    for text in scenario.get("out", []):
        if text not in got["out"]:
            problems.append(f"output lacks {text!r}")
    for text in scenario.get("err", []):
        if text not in got["err"]:
            problems.append(f"message lacks {text!r}")
    for text in scenario.get("not_out", []):
        if text in got["out"]:
            problems.append(f"output contains {text!r}")
    for secret in (ERROR_TEXT, CREDENTIAL):
        for where in ("out", "err", "audit"):
            if secret in got[where]:
                problems.append(f"{secret[:10]}... reached the {where}")
    if scenario["rc"] == 0 and got["err"]:
        problems.append("a clean run printed a warning")
    return problems
