"""Every lens and carry-forward dispatch runs read-only on Fly.

A lens reviews and never delivers, so each ``run_skill`` call whose
``action_class`` is a lens review or a carry-forward check must pass
``github_read_only=True``. This is a static check over ``swarm_dispatch.py``
so it runs with plain pytest and no daemon imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

SWARM_DISPATCH = Path(__file__).with_name("swarm_dispatch.py")


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _classify(action_class: ast.AST) -> str | None:
    if (
        isinstance(action_class, ast.Call)
        and isinstance(action_class.func, ast.Attribute)
        and action_class.func.attr == "lens_review_class"
    ):
        return "lens"
    if (
        isinstance(action_class, ast.Attribute)
        and action_class.attr == "ACTION_CARRY_FORWARD_CHECK"
    ):
        return "carry_forward"
    return None


def _review_call_sites() -> list[tuple[str, ast.Call]]:
    tree = ast.parse(SWARM_DISPATCH.read_text(encoding="utf-8"))
    sites: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _func_name(node.func) != "run_skill":
            continue
        for kw in node.keywords:
            if kw.arg == "action_class":
                kind = _classify(kw.value)
                if kind:
                    sites.append((kind, node))
    return sites


def test_every_lens_and_carry_forward_dispatch_is_read_only() -> None:
    missing = []
    for kind, call in _review_call_sites():
        flag = next((kw for kw in call.keywords if kw.arg == "github_read_only"), None)
        if not (
            flag is not None
            and isinstance(flag.value, ast.Constant)
            and flag.value.value is True
        ):
            missing.append(f"{kind} run_skill at line {call.lineno}")
    assert not missing, "run_skill calls without github_read_only=True: " + ", ".join(
        missing
    )


def test_the_scan_found_the_known_call_sites() -> None:
    kinds = [kind for kind, _ in _review_call_sites()]
    assert kinds.count("lens") >= 5, kinds
    assert kinds.count("carry_forward") >= 2, kinds
