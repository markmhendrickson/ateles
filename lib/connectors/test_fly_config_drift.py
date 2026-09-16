"""Unit tests for Fly config drift comparison semantics."""

from __future__ import annotations

from lib.connectors.fly_config_drift import (
    MachineGuest,
    VmWant,
    compare_machine_guest,
    memory_to_mb,
    parse_vm_want_from_config,
)


SAMPLE_CONFIG = """
[http_service]
  internal_port = 3180

[[http_service.checks]]
  path = "/ready"
  method = "GET"

[[vm]]
  memory = '1gb'
  cpu_kind = 'shared'
  cpus = 1
"""


def test_memory_to_mb_normalizes_gb():
    assert memory_to_mb("8gb") == 8192
    assert memory_to_mb("512") == 512


def test_parse_vm_want_reads_vm_block_only():
    want = parse_vm_want_from_config(SAMPLE_CONFIG)
    assert want.memory_mb == 1024
    assert want.cpus == 1
    assert want.cpu_kind == "shared"
    assert want.health_check_count == 1


def test_shrink_memory_is_drift():
    want = VmWant(memory_mb=1024, cpus=1, cpu_kind="shared", health_check_count=1)
    got = MachineGuest(memory_mb=8192, cpus=2, cpu_kind="performance", health_check_count=1)
    result = compare_machine_guest(want, got)
    assert result.drift
    assert any("SHRINK memory" in msg for msg in result.messages)


def test_grow_memory_is_note_not_drift():
    want = VmWant(memory_mb=8192, cpus=2, cpu_kind="performance", health_check_count=1)
    got = MachineGuest(memory_mb=1024, cpus=1, cpu_kind="shared", health_check_count=1)
    result = compare_machine_guest(want, got)
    assert not result.drift
    assert any("GROW memory" in note for note in result.notes)


def test_performance_to_shared_is_drift_even_at_equal_cpus():
    want = VmWant(memory_mb=8192, cpus=2, cpu_kind="shared", health_check_count=1)
    got = MachineGuest(memory_mb=8192, cpus=2, cpu_kind="performance", health_check_count=1)
    result = compare_machine_guest(want, got)
    assert result.drift
    assert any("downgrade cpu_kind" in msg for msg in result.messages)


def test_losing_health_checks_is_drift():
    want = VmWant(memory_mb=8192, cpus=2, cpu_kind="performance", health_check_count=0)
    got = MachineGuest(memory_mb=8192, cpus=2, cpu_kind="performance", health_check_count=2)
    result = compare_machine_guest(want, got)
    assert result.drift
    assert any("REMOVE all" in msg for msg in result.messages)
