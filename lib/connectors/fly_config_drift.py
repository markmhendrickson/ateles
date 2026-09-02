"""
lib/connectors/fly_config_drift.py — compare committed Fly config to running machines.

Ports the comparison semantics from neotoma's ``check_fly_config_drift.sh`` into
pure Python so the Fly connector can persist drift without shelling out to a
bash pre-deploy gate.

Rules (matching the script):
  - shrink memory/cpus → drift
  - grow memory → note only
  - performance → shared at equal core count → drift
  - losing all health checks when machine had checks → drift
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VmWant:
    """Guest shape declared in a committed Fly config file."""

    memory_mb: int = 0
    cpus: int = 0
    cpu_kind: str = ""
    health_check_count: int = 0


@dataclass(frozen=True)
class MachineGuest:
    """Guest shape reported by ``flyctl machine list --json``."""

    memory_mb: int = 0
    cpus: int = 0
    cpu_kind: str = "unknown"
    health_check_count: int = 0


@dataclass
class MachineDriftResult:
    """Drift verdict for one machine against the committed config."""

    drift: bool = False
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def memory_to_mb(raw: str) -> int:
    """Normalize ``'8gb'`` / ``'8192'`` to megabytes."""
    text = (raw or "").strip().lower()
    match = re.match(r"^(\d+)", text)
    if not match:
        return 0
    amount = int(match.group(1))
    return amount * 1024 if text.endswith("gb") else amount


def parse_vm_want_from_config(config_text: str) -> VmWant:
    """Read only the ``[[vm]]`` block and count ``[[http_service.checks]]``."""
    vm_block = _extract_vm_block(config_text)
    want_memory = _first_match(
        r"memory\s*=\s*['\"]([^'\"]+)['\"]", vm_block, group=1
    ) or _first_match(r"memory\s*=\s*(\d+[a-z]*)", vm_block, group=1)
    want_cpus = _first_match(r"cpus\s*=\s*(\d+)", vm_block, group=1)
    want_kind = _first_match(
        r"cpu_kind\s*=\s*['\"]([^'\"]+)['\"]", vm_block, group=1
    )
    want_checks = len(
        re.findall(r"^\s*\[\[http_service\.checks\]\]", config_text, flags=re.MULTILINE)
    )
    return VmWant(
        memory_mb=memory_to_mb(want_memory or ""),
        cpus=int(want_cpus or 0),
        cpu_kind=(want_kind or "").strip(),
        health_check_count=want_checks,
    )


def parse_vm_want_from_path(config_path: Path) -> VmWant:
    return parse_vm_want_from_config(config_path.read_text(encoding="utf-8"))


def compare_machine_guest(want: VmWant, got: MachineGuest) -> MachineDriftResult:
    """Apply shrink-is-drift / grow-is-a-note semantics for one machine."""
    result = MachineDriftResult()

    if want.memory_mb and got.memory_mb > want.memory_mb:
        result.drift = True
        result.messages.append(
            f"deploying config would SHRINK memory {got.memory_mb}MB -> {want.memory_mb}MB"
        )
    elif want.memory_mb and got.memory_mb < want.memory_mb:
        result.notes.append(
            f"deploy would GROW memory {got.memory_mb}MB -> {want.memory_mb}MB"
        )

    if want.cpus and got.cpus > want.cpus:
        result.drift = True
        result.messages.append(
            f"deploying config would REDUCE cpus {got.cpus} -> {want.cpus}"
        )

    if (
        want.cpu_kind
        and got.cpu_kind == "performance"
        and want.cpu_kind != "performance"
    ):
        result.drift = True
        result.messages.append(
            f"deploying config would downgrade cpu_kind {got.cpu_kind} -> {want.cpu_kind}"
        )

    if got.health_check_count > 0 and want.health_check_count == 0:
        result.drift = True
        result.messages.append(
            f"deploying config would REMOVE all {got.health_check_count} health check(s)"
        )
    if got.health_check_count == 0:
        result.warnings.append("machine currently has NO health checks")

    return result


def compare_all_machines(
    want: VmWant, machines: list[MachineGuest]
) -> MachineDriftResult:
    """Merge drift results across every machine."""
    merged = MachineDriftResult()
    for got in machines:
        one = compare_machine_guest(want, got)
        if one.drift:
            merged.drift = True
        merged.messages.extend(one.messages)
        merged.notes.extend(one.notes)
        merged.warnings.extend(one.warnings)
    return merged


def _extract_vm_block(config_text: str) -> str:
    lines: list[str] = []
    in_vm = False
    for line in config_text.splitlines():
        if line.strip().startswith("[[vm]]"):
            in_vm = True
            continue
        if in_vm and line.strip().startswith("[") and not line.strip().startswith("[["):
            break
        if in_vm and re.match(r"^\[\[", line.strip()) and not line.strip().startswith(
            "[[vm"
        ):
            break
        if in_vm:
            lines.append(line)
    return "\n".join(lines)


def _first_match(pattern: str, text: str, *, group: int) -> str:
    match = re.search(pattern, text)
    return match.group(group) if match else ""
