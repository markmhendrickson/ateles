"""Synthetic input custody at the existing packaging entrypoint."""

import json
import os
from pathlib import Path

import pytest

import package_rule_inventory_inputs as inputs


def canonical_source(root):
    root.mkdir()
    for relative in inputs.EXACT_INPUTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ordinary fixture\n")
    return root


def observe_reads(monkeypatch, target, *, before_leaf=None):
    identity = (target.stat().st_dev, target.stat().st_ino)
    reads = []
    path_read, fd_read, fd_open = Path.read_bytes, os.read, os.open

    def read_path(path):
        if before_leaf and path.name == "CLAUDE.md":
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
        if before_leaf and path == "CLAUDE.md" and kwargs.get("dir_fd") is not None:
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
