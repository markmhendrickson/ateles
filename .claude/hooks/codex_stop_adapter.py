#!/usr/bin/env python3
"""Trusted Codex adapter for the shared turn-ending gate evaluators.

The installed command supplies ``--gate`` and ``--event``.  Those immutable
arguments select the evaluator and expected lifecycle event; fields inside the
event payload cannot select the harness response contract.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import NamedTuple

from stop_gate_contract import GateResult, read_event_strict, respond_codex


class AdapterConfig(NamedTuple):
    gate: str
    event: str


VALID_CONFIGS = frozenset(
    {
        AdapterConfig("decision-shape", "Stop"),
        AdapterConfig("decision-shape", "SubagentStop"),
        AdapterConfig("report-quality", "Stop"),
    }
)


def parse_config(argv: Sequence[str]) -> AdapterConfig | GateResult:
    if len(argv) != 4 or argv[0] != "--gate" or argv[2] != "--event":
        return GateResult.indeterminate(
            "Codex adapter configuration must be --gate <gate> --event <event>"
        )
    config = AdapterConfig(argv[1], argv[3])
    if config not in VALID_CONFIGS:
        return GateResult.indeterminate(
            f"unrecognized Codex adapter configuration {config.gate!r}/{config.event!r}"
        )
    return config


def validate_event(event: dict, expected_event: str) -> GateResult | None:
    if event.get("stop_hook_active") is True:
        return GateResult.allow()
    event_name = event.get("hook_event_name")
    if not isinstance(event_name, str) or event_name != expected_event:
        return GateResult.indeterminate(
            f"hook_event_name must be exactly {expected_event!r}"
        )
    model = event.get("model")
    if not isinstance(model, str) or not model.strip():
        return GateResult.indeterminate("model must be a non-empty string")
    last_message = event.get("last_assistant_message")
    if not isinstance(last_message, str) or not last_message.strip():
        return GateResult.indeterminate(
            "last_assistant_message must be a non-empty string"
        )
    return None


def _evaluator(gate: str) -> Callable[[dict], GateResult]:
    if gate == "decision-shape":
        from decision_shape_gate import evaluate_event

        return lambda event: evaluate_event(event, enforced=True)
    if gate == "report-quality":
        from report_quality_gate import evaluate_event

        return evaluate_event
    raise ValueError(f"unknown gate {gate!r}")


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_config(list(sys.argv[1:] if argv is None else argv))
    if isinstance(config, GateResult):
        return respond_codex(config)

    event = read_event_strict(sys.stdin)
    if isinstance(event, GateResult):
        return respond_codex(event)
    validation = validate_event(event, config.event)
    if validation is not None:
        return respond_codex(validation)

    try:
        result = _evaluator(config.gate)(event)
    except Exception as exc:  # noqa: BLE001 - Codex must receive continuation JSON
        result = GateResult.indeterminate(
            f"{config.gate} evaluator raised {type(exc).__name__}"
        )
    return respond_codex(result)


if __name__ == "__main__":
    raise SystemExit(main())
