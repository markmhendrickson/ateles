"""A payment task parked in the backlog is not listed as due or overdue."""

from __future__ import annotations


def test_due_payment_scan_skips_backlog(monkeypatch):
    import monedula

    tasks = {
        "ent_pay_backlog": {"snapshot": {"status": "backlog", "due_date": "2026-01-01"}},
        "ent_pay_pending": {"snapshot": {"status": "pending", "due_date": "2026-01-01"}},
    }
    monkeypatch.setattr(monedula, "NEOTOMA_BASE_URL", "http://synthetic.invalid")
    monkeypatch.setattr(monedula, "_fetch_entity_by_id", lambda tid: tasks[tid])
    handlers = [type("H", (), {"profile": type("P", (), {"neotoma_task_id": tid})()})()
                for tid in tasks]
    due = monedula.fetch_due_payment_tasks(handlers)
    assert due == [tasks["ent_pay_pending"]]
