"""Synthetic input custody at the existing packaging entrypoint."""

import json
import os
import sys
from pathlib import Path

import pytest

import package_rule_inventory_inputs as inputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "credential_containment"))
import credential_containment_runner as packet_eval  # noqa: E402


def canonical_source(root):
    root.mkdir()
    for relative in inputs.EXACT_INPUTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ordinary fixture\n")
    return root


def observe_reads(monkeypatch, target, *, before_leaf=None, leaf_name="CLAUDE.md"):
    identity = (target.stat().st_dev, target.stat().st_ino)
    reads = []
    path_read, fd_read, fd_open = Path.read_bytes, os.read, os.open

    def read_path(path):
        if before_leaf and path.name == leaf_name:
            before_leaf()
        info = path.stat()
        if (info.st_dev, info.st_ino) == identity:
            reads.append("path")
        return path_read(path)

    def read_fd(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            reads.append("descriptor")
        return fd_read(fd, size)

    def open_fd(path, flags, *args, **kwargs):
        if before_leaf and path == leaf_name and kwargs.get("dir_fd") is not None:
            before_leaf()
        return fd_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_path)
    monkeypatch.setattr(os, "read", read_fd)
    monkeypatch.setattr(os, "open", open_fd)
    return reads


def test_ordinary_packaging_remains_usable(tmp_path):
    source = canonical_source(tmp_path / "source")
    destination = tmp_path / "output"
    inputs.package_inputs(source, destination)
    inputs.validate_inputs(destination)
    assert json.loads((destination / inputs.MANIFEST).read_text())["files"]


@pytest.mark.parametrize("plant", ["hardlink", "parent", "swap", "oversized"])
def test_packaging_refuses_before_disallowed_content_read(tmp_path, monkeypatch, plant):
    source = canonical_source(tmp_path / "source")
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic ordinary sentinel\n")
    leaf = source / "CLAUDE.md"
    trigger = None
    if plant == "hardlink":
        leaf.unlink()
        os.link(outside, leaf)
    elif plant == "parent":
        (source / "docs").rename(source / "local-docs")
        (source / "docs").symlink_to(source / "local-docs", target_is_directory=True)
        outside = source / "local-docs/foundation/rule_inventory.md"
    elif plant == "swap":
        def trigger():
            if not leaf.is_symlink():
                leaf.unlink()
                leaf.symlink_to(outside)
    else:
        leaf.write_text("x" * 32)
        outside = leaf
        monkeypatch.setattr(inputs, "MAX_FILE_BYTES", 8)
    reads = observe_reads(monkeypatch, outside, before_leaf=trigger)
    refused = False
    try:
        inputs.package_inputs(source, tmp_path / "output")
    except inputs.InputBoundaryError:
        refused = True
    assert reads == [], "disallowed content was read before refusal"
    assert refused, "disallowed input was accepted"


@pytest.fixture
def packet(tmp_path):
    root = tmp_path / "packet"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "a.txt").write_text("first fixture")
    (other / "b.txt").write_text("second fixture")
    inventory = {"roots": {"packet": str(root), "source": str(other)},
                 "files": [{"root": "packet", "path": "a.txt"},
                           {"root": "source", "path": "b.txt"}]}
    (root / "inputs.json").write_text(json.dumps(inventory))
    return root, other, inventory


def packet_cli(monkeypatch, root, operation):
    monkeypatch.setattr(sys, "argv", ["package_rule_inventory_inputs.py",
        "--inventory-root", str(root), "--inventory", "inputs.json",
        "--" + operation + "-manifest", "manifest.json"])
    return inputs.main()


def test_explicit_packet_collection_and_verification_share_trusted_roots(packet, monkeypatch, capsys):
    root, other, _ = packet
    assert packet_cli(monkeypatch, root, "collect") == 0
    result = json.loads((root / "manifest.json").read_text())
    assert [row["root"] for row in result["files"]] == ["packet", "source"]
    assert all(len(row["sha256"]) == 64 for row in result["files"])
    assert packet_cli(monkeypatch, root, "verify") == 0
    output = capsys.readouterr().out
    assert output.count("explicit input boundary verified") == 2
    assert "canonical rule inventory" not in output
    (other / "b.txt").write_text("changed")
    assert packet_cli(monkeypatch, root, "verify") == 2


@pytest.mark.parametrize("operation", ["collect", "verify"])
@pytest.mark.parametrize("plant", ["leaf", "dangling", "parent", "root", "root_dangling", "absolute", "escape",
                                  "fifo", "hardlink", "swap", "size"])
def test_packet_cli_refuses_without_reading_disallowed_inputs(
    packet, tmp_path, monkeypatch, capsys, operation, plant,
):
    root, other, inventory = packet
    if operation == "verify":
        assert packet_cli(monkeypatch, root, "collect") == 0
        capsys.readouterr()
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary outside sentinel")
    leaf = root / "a.txt"
    trigger = None
    if plant in {"leaf", "dangling"}:
        leaf.unlink()
        leaf.symlink_to(outside if plant == "leaf" else tmp_path / "missing")
    elif plant == "parent":
        (other / "a.txt").write_text("outside directory fixture")
        outside = other / "a.txt"
        (root / "nested").symlink_to(other, target_is_directory=True)
        inventory["files"][0]["path"] = "nested/a.txt"
    elif plant in {"root", "root_dangling"}:
        link = tmp_path / "root-link"
        link.symlink_to(root if plant == "root" else tmp_path / "missing", target_is_directory=True)
        inventory["roots"]["packet"] = str(link)
    elif plant in {"absolute", "escape"}:
        inventory["files"][0]["path"] = str(outside) if plant == "absolute" else "../outside.txt"
    elif plant == "fifo":
        leaf.unlink()
        os.mkfifo(leaf)
        outside = leaf
    elif plant == "hardlink":
        leaf.unlink()
        os.link(outside, leaf)
    elif plant == "swap":
        def trigger():
            if not leaf.is_symlink():
                leaf.unlink()
                leaf.symlink_to(outside)
    else:
        monkeypatch.setattr(inputs, "MAX_FILE_BYTES", 8)
        outside = leaf
    (root / "inputs.json").write_text(json.dumps(inventory))
    reads = observe_reads(monkeypatch, outside, before_leaf=trigger, leaf_name="a.txt")
    result = packet_cli(monkeypatch, root, operation)
    assert reads == []
    assert result == 2
    output = capsys.readouterr()
    assert "verified" not in output.out
    assert str(root) not in output.err and "sentinel" not in output.err
    if operation == "collect":
        assert not (root / "manifest.json").exists()


@pytest.mark.parametrize("operation", ["collect", "verify"])
def test_packet_rejects_replaced_entry_after_open(packet, tmp_path, monkeypatch, operation):
    root, _, _ = packet
    if operation == "verify":
        assert packet_cli(monkeypatch, root, "collect") == 0
    leaf = root / "a.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary outside sentinel")
    leaf_identity = (leaf.stat().st_dev, leaf.stat().st_ino)
    reads = observe_reads(monkeypatch, outside)
    read = os.read
    swapped = []

    def replace_then_read(fd, count):
        info = os.fstat(fd)
        if not swapped and (info.st_dev, info.st_ino) == leaf_identity:
            leaf.unlink()
            leaf.symlink_to(outside)
            swapped.append(True)
        return read(fd, count)

    monkeypatch.setattr(os, "read", replace_then_read)
    assert packet_cli(monkeypatch, root, operation) == 2
    assert swapped == [True] and reads == []


def test_manifest_cannot_supply_new_input_authority(packet, tmp_path, monkeypatch):
    root, _, _ = packet
    assert packet_cli(monkeypatch, root, "collect") == 0
    manifest = root / "manifest.json"
    data = json.loads(manifest.read_text())
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary outside sentinel")
    data["files"][0]["path"] = str(outside)
    manifest.write_text(json.dumps(data))
    reads = observe_reads(monkeypatch, outside)
    result = packet_cli(monkeypatch, root, "verify")
    assert reads == []
    assert result == 2


@pytest.mark.parametrize("document", ["inputs.json", "manifest.json"])
@pytest.mark.parametrize("plant", ["leaf", "hardlink", "fifo"])
def test_bootstrap_documents_use_the_same_boundary(packet, tmp_path, monkeypatch, document, plant):
    root, _, _ = packet
    assert packet_cli(monkeypatch, root, "collect") == 0
    target = root / document
    outside = tmp_path / "outside.txt"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    if plant == "leaf":
        target.symlink_to(outside)
    elif plant == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
        outside = target
    reads = observe_reads(monkeypatch, outside)
    result = packet_cli(monkeypatch, root, "verify")
    assert reads == []
    assert result == 2


@pytest.mark.parametrize("limit", ["count", "roots", "total", "owner"])
def test_packet_limits_and_ownership_bind_before_content(packet, monkeypatch, limit):
    root, _, _ = packet
    if limit == "count":
        monkeypatch.setattr(inputs, "MAX_FILES", 1)
    elif limit == "roots":
        monkeypatch.setattr(inputs, "MAX_ROOTS", 1)
    elif limit == "total":
        monkeypatch.setattr(inputs, "MAX_TOTAL_BYTES", 4)
    else:
        monkeypatch.setattr(os, "geteuid", lambda: (root.stat().st_uid + 1))
    reads = observe_reads(monkeypatch, root / "a.txt")
    assert packet_cli(monkeypatch, root, "collect") == 2
    assert reads == []
    assert not (root / "manifest.json").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_boundary_closes_owned_descriptors(packet, monkeypatch, missing):
    root, _, _ = packet
    if missing:
        (root / "a.txt").unlink()
    opened = set()
    original_open, original_close = os.open, os.close

    def open_fd(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def close_fd(fd):
        opened.discard(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", open_fd)
    monkeypatch.setattr(os, "close", close_fd)
    assert packet_cli(monkeypatch, root, "collect") == (2 if missing else 0)
    assert opened == set()


@pytest.mark.parametrize("operation", ["collect", "verify"])
@pytest.mark.parametrize("swap_parent", [False, True])
def test_declared_parent_custody_is_checked_at_open(packet, monkeypatch, operation, swap_parent):
    root, other, inventory = packet
    (root / "nested").mkdir()
    (root / "a.txt").rename(root / "nested/a.txt")
    inventory["files"][0]["path"] = "nested/a.txt"
    (root / "inputs.json").write_text(json.dumps(inventory))
    if operation == "verify":
        assert packet_cli(monkeypatch, root, "collect") == 0
    outside = other / "a.txt"
    outside.write_text("synthetic outside sentinel")
    reads = observe_reads(monkeypatch, outside)
    changed = []

    def replace_parent():
        if not changed:
            (root / "nested").rename(root / "saved")
            (root / "nested").symlink_to(other, target_is_directory=True)
            changed.append(True)

    if swap_parent:
        original = os.open

        def open_fd(path, flags, *args, **kwargs):
            if path in {"nested", "nested/a.txt"} and kwargs.get("dir_fd") is not None:
                replace_parent()
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_fd)
    else:
        replace_parent()
    result = packet_cli(monkeypatch, root, operation)
    assert changed == [True] and reads == []
    assert result == 2


@pytest.mark.parametrize("document", ["inputs.json", "manifest.json"])
def test_metadata_size_cap_precedes_document_read(packet, monkeypatch, document):
    root, _, _ = packet
    assert packet_cli(monkeypatch, root, "collect") == 0
    target = root / document
    cap = max((root / "inputs.json").stat().st_size,
              (root / "manifest.json").stat().st_size) + 16
    target.write_text("x" * (cap + 1))
    monkeypatch.setattr(inputs, "MAX_MANIFEST_BYTES", cap)
    reads = observe_reads(monkeypatch, target)
    result = packet_cli(monkeypatch, root, "verify")
    assert reads == []
    assert result == 2


@pytest.mark.parametrize("entry", [
    {"root": "missing", "path": "a.txt"},
    {"root": "packet", "path": "manifest.json"},
    {"root": "packet", "path": "nested/./a.txt"},
])
def test_declared_inventory_rejects_ambiguous_scope_before_inputs(packet, monkeypatch, entry):
    root, _, inventory = packet
    inventory["files"][0] = entry
    (root / "inputs.json").write_text(json.dumps(inventory))
    assert packet_cli(monkeypatch, root, "collect") == 2
    assert not (root / "manifest.json").exists()


def test_shared_reader_checks_parent_of_canonical_manifest(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    (root / inputs.MANIFEST).write_text('{"schema":2,"files":[]}')
    with pytest.raises(inputs.InputBoundaryError):
        inputs.validate_inputs(alias)
    assert inputs.recorded_commit_dates(alias) == {}


@pytest.mark.parametrize("change", ["duplicate", "schema", "digest", "size"])
def test_manifest_shape_cannot_change_the_verification_contract(packet, monkeypatch, change):
    root, _, inventory = packet
    assert packet_cli(monkeypatch, root, "collect") == 0
    if change == "duplicate":
        inventory["files"].append(inventory["files"][0])
        (root / "inputs.json").write_text(json.dumps(inventory))
    else:
        manifest = root / "manifest.json"
        data = json.loads(manifest.read_text())
        if change == "schema":
            data["schema"] = True
        else:
            data["files"][0]["sha256" if change == "digest" else "size"] = "invalid"
        manifest.write_text(json.dumps(data))
    reads = observe_reads(monkeypatch, root / "a.txt")
    assert packet_cli(monkeypatch, root, "verify") == 2
    assert reads == []


@pytest.mark.parametrize("change", ["relative_unicode", "root_unicode", "unsupported_flags"])
def test_unusable_inputs_refuse_before_artifact_reads(packet, monkeypatch, change):
    root, _, inventory = packet
    if change == "relative_unicode":
        inventory["files"][0]["path"] = "\ud800"
    elif change == "root_unicode":
        inventory["roots"]["packet"] = "/\ud800"
    else:
        monkeypatch.delattr(os, "O_NOFOLLOW")
    (root / "inputs.json").write_text(json.dumps(inventory))
    reads = observe_reads(monkeypatch, root / "a.txt")
    assert packet_cli(monkeypatch, root, "collect") == 2
    assert reads == []


def test_existing_temporary_entry_is_never_deleted(packet, monkeypatch):
    from types import SimpleNamespace

    root, _, _ = packet
    target = root / ".input-fixed"
    target.write_text("unrelated fixture")
    monkeypatch.setattr(inputs.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    assert packet_cli(monkeypatch, root, "collect") == 2
    assert target.read_text() == "unrelated fixture"


def test_replaced_output_entry_cannot_report_success(packet, tmp_path, monkeypatch):
    root, _, _ = packet
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary synthetic sentinel")
    replace = os.replace

    def replaced(source, destination, **kwargs):
        replace(source, destination, **kwargs)
        (root / destination).unlink()
        (root / destination).symlink_to(outside)

    monkeypatch.setattr(os, "replace", replaced)
    reads = observe_reads(monkeypatch, outside)
    assert packet_cli(monkeypatch, root, "collect") == 2
    assert reads == []


def test_discovered_directory_disappearance_is_not_silently_optional(tmp_path, monkeypatch):
    source = canonical_source(tmp_path / "source")
    role = source / ".claude/skills/example"
    role.mkdir(parents=True)
    (role / "SKILL.md").write_text("ordinary role fixture")
    original = os.open
    disappeared = []

    def open_fd(path, flags, *args, **kwargs):
        if path == "example" and not disappeared:
            (role / "SKILL.md").unlink()
            role.rmdir()
            disappeared.append(True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_fd)
    with pytest.raises(inputs.InputBoundaryError):
        inputs.package_inputs(source, tmp_path / "output")
    assert disappeared == [True]
    assert not (tmp_path / "output" / inputs.MANIFEST).exists()


@pytest.mark.parametrize("limit", ["count", "total"])
def test_canonical_packager_budgets_precede_content(tmp_path, monkeypatch, limit):
    source = canonical_source(tmp_path / "source")
    if limit == "count":
        monkeypatch.setattr(inputs, "MAX_FILES", 0)
    else:
        monkeypatch.setattr(inputs, "MAX_TOTAL_BYTES", 4)
    reads = observe_reads(monkeypatch, source / "CLAUDE.md")
    with pytest.raises(inputs.InputBoundaryError):
        inputs.package_inputs(source, tmp_path / "output")
    assert reads == []


def observed_cli_process(root, tmp_path, outside, operation, *, swap=False, wrong_owner=False):
    result, evidence = packet_eval.run_packet_cli(
        root, tmp_path, outside, operation, swap=swap, wrong_owner=wrong_owner,
        script=Path(inputs.__file__).resolve(),
    )
    assert evidence["external_reads"] == []
    assert evidence["owned_fds_remaining"] == 0
    return result, evidence


def test_actual_cli_process_collects_and_verifies_declared_inputs(packet, tmp_path):
    root, _, _ = packet
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary synthetic sentinel")
    for operation in ["collect", "verify"]:
        result, _ = observed_cli_process(root, tmp_path, outside, operation)
        assert result.returncode == 0, result.stderr
        assert "explicit input boundary verified" in result.stdout


@pytest.mark.parametrize("operation", ["collect", "verify"])
@pytest.mark.parametrize("case", ["leaf", "ownership", "swap"])
def test_actual_cli_process_refuses_before_external_reads(packet, tmp_path, operation, case):
    root, _, _ = packet
    outside = tmp_path / "outside.txt"
    outside.write_text("ordinary synthetic sentinel")
    if operation == "verify":
        assert observed_cli_process(root, tmp_path, outside, "collect")[0].returncode == 0
    if case == "leaf":
        (root / "a.txt").unlink()
        (root / "a.txt").symlink_to(outside)
    result, evidence = observed_cli_process(root, tmp_path, outside, operation,
                                          swap=case == "swap", wrong_owner=case == "ownership")
    assert result.returncode == 2
    assert "verified" not in result.stdout
    assert str(root) not in result.stderr
    if case == "swap":
        assert evidence["swaps"] == [True]
