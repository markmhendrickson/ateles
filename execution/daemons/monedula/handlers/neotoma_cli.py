"""
handlers/neotoma_cli.py — one place that knows how to write a field to Neotoma.

The CLI has NO `entities update` verb; the correct form is:

    neotoma --api-only corrections create <id> --entity-type <type> \
        --field-name <field> --corrected-value <value>

Three call sites had each hand-rolled `entities update`, so every one of them
failed silently at runtime ("error: unknown command 'update'") while logging an
optimistic success. Fixing one did not fix the others — hence this single
helper.

Every write returns True/False and logs the real return code, so a caller can
never report success for a write that did not land.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


def correct_field(
    entity_id: str,
    field: str,
    value: str,
    *,
    entity_type: str = "task",
    label: str = "monedula",
    timeout: int = 30,
) -> bool:
    """Write one field to a Neotoma entity via `corrections create`.

    Returns True only when the CLI exits 0. Fail-open: never raises.
    """
    if not entity_id or not field:
        return False

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning(f"[{label}] neotoma CLI not found — cannot write {field}")
        return False

    try:
        res = subprocess.run(
            [
                neotoma, "--api-only", "corrections", "create", entity_id,
                "--entity-type", entity_type,
                "--field-name", field,
                "--corrected-value", str(value),
            ],
            capture_output=True, text=True, timeout=timeout, env=os.environ,
        )
        if res.returncode != 0:
            log.warning(
                f"[{label}] neotoma {field} write failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:200]}"
            )
            return False
        log.info(f"[{label}] Neotoma {field} updated on {entity_id}.")
        return True
    except Exception as exc:  # noqa: BLE001 — never crash a payment run
        log.warning(f"[{label}] neotoma {field} write error: {exc}")
        return False
