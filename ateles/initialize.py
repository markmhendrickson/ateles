"""``ateles init`` — configuration wizard (W1).

Collects the operator-specific settings and writes ``ateles.config.json``
(non-secret keys only — secrets stay in the environment / the W4 secret
backend). Supports ``--non-interactive`` for CI and scripted setup, and is
written against injectable ``input``/``output`` callables so it is testable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import (
    CONFIG_FILENAME,
    SECRET_KEYS,
    SETTINGS,
    AtelesConfig,
    config_path,
    load,
)


def _prompt(setting, current: str, input_fn, output_fn) -> str:
    suffix = f" [{current}]" if current else ""
    output_fn(f"\n{setting.key}\n  {setting.description}")
    answer = input_fn(f"  {setting.env}{suffix}: ").strip()
    return answer or current


def run_init(
    *,
    non_interactive: bool = False,
    environ: dict | None = None,
    input_fn=input,
    output_fn=print,
    start: Path | None = None,
) -> Path:
    """Write ateles.config.json from the current env/file plus any prompted
    answers, and return the path written. Secrets are never written to the file.
    """
    environ = os.environ if environ is None else environ
    cfg: AtelesConfig = load(start=start, environ=environ)
    path = config_path(start)

    collected = dict(cfg.values)
    if non_interactive:
        output_fn(f"[init] non-interactive: seeding {CONFIG_FILENAME} from env/existing values.")
    else:
        output_fn("ateles init — configure this operator's swarm.")
        output_fn("Secrets are NOT written to the config file; keep them in your environment.")
        for s in SETTINGS:
            if s.secret:
                continue  # never prompt secrets to a file
            collected[s.key] = _prompt(s, collected.get(s.key, ""), input_fn, output_fn)

    to_write = {k: v for k, v in collected.items() if k not in SECRET_KEYS and v}
    path.write_text(json.dumps(to_write, indent=2, sort_keys=True) + "\n")
    output_fn(f"\nWrote {len(to_write)} settings to {path}")
    output_fn("Run `ateles doctor` to verify and see the next rung.")
    return path
