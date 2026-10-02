#!/usr/bin/env python3
"""Shared result and response contract for turn-ending gates.

The evaluator decides whether the event is allowed, blocked, or indeterminate.
The harness adapter decides how that result is returned.  Keeping those two
decisions separate prevents event-controlled fields from selecting the exit
semantics of the harness that invoked the gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, TextIO


GateDisposition = Literal["allow", "block", "indeterminate"]


@dataclass(frozen=True)
class GateResult:
    disposition: GateDisposition
    reason: str = ""

    @classmethod
    def allow(cls) -> "GateResult":
        return cls("allow")

    @classmethod
    def block(cls, reason: str) -> "GateResult":
        return cls("block", reason)

    @classmethod
    def indeterminate(cls, reason: str) -> "GateResult":
        return cls("indeterminate", reason)


def read_event_strict(stream: TextIO) -> GateResult | dict:
    """Read one hook event without collapsing malformed input to an allow."""
    try:
        raw = stream.read()
    except Exception as exc:  # noqa: BLE001 - converted into a restrictive result
        return GateResult.indeterminate(
            f"could not read hook input ({type(exc).__name__})"
        )
    if not raw.strip():
        return GateResult.indeterminate("hook input is empty")
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return GateResult.indeterminate(
            f"hook input is malformed JSON ({type(exc).__name__})"
        )
    if not isinstance(event, dict):
        return GateResult.indeterminate("hook input must be one JSON object")
    return event


def block_payload(result: GateResult) -> str:
    reason = result.reason or "turn-ending gate could not determine a safe result"
    if result.disposition == "indeterminate":
        reason = "Turn-ending gate indeterminate: " + reason + "."
    return json.dumps({"decision": "block", "reason": reason})


def respond_claude(result: GateResult) -> int:
    """Claude Code blocks Stop with exit 2."""
    if result.disposition == "allow":
        return 0
    print(block_payload(result))
    return 2


def respond_codex(result: GateResult) -> int:
    """Codex consumes structured continuation only from a successful hook."""
    if result.disposition == "allow":
        return 0
    print(block_payload(result))
    return 0
