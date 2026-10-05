"""Normalize harness tool calls into shared action capabilities.

The policy layer should not need to know whether a shell call arrived as
Claude's legacy ``Bash`` tool or Codex's current ``exec_command`` tool, nor
whether a Neotoma write used the current or legacy MCP server prefix.  This
module owns that translation.  It deliberately retains ``raw_tool_id`` so
audit records can stay bounded to the actual recipient-path entrance.

This module classifies actions only.  It neither grants authority nor decides
whether a capability is allowed; injection and enforcement remain separate
consumers of the normalized result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_SHELL_ALIASES = frozenset({"Bash", "exec_command", "functions.exec_command"})
_READ_ALIASES = frozenset({"Read", "read_file"})
_GREP_ALIASES = frozenset({"Grep", "grep"})
_GLOB_ALIASES = frozenset({"Glob", "glob"})
_TERMINATION_EVENTS = frozenset({"Stop", "SubagentStop"})

_NEOTOMA_PREFIXES = ("mcp__neotoma__", "mcp__mcpsrv_neotoma__")
_NEOTOMA_MUTATIONS = frozenset(
    {
        "store",
        "store_structured",
        "correct",
        "create_relationship",
        "create_relationships",
        "delete_entity",
        "delete_relationship",
        "restore_entity",
        "restore_relationship",
        "publish_rendered_page",
        "register_relationship_type",
        "update_schema_incremental",
        "unsubscribe",
    }
)

_HARNESS_CONFIG_PATH_RE = re.compile(
    r"(\.cursor/mcp\.json|\.claude/settings(\.local)?\.json|\.neotoma/aauth)"
)
_SEGMENT_SPLIT = re.compile(r"&&|[;\n|]")
_READ_ONLY_LEADERS = re.compile(
    r"^(?:"
    r"git\s+(?:diff|show|log|blame|grep|status|cat-file|commit|tag|notes)\b"
    r"|cat|less|more|head|tail|wc|file"
    r"|rg|grep|ag|ack"
    r"|ls|find\s+.*-name"
    r"|gh\s+(?:pr|issue)\s+(?:comment|view|diff|list)"
    r"|echo|printf"
    r")\b"
)
_REDIRECT_TO_PATH_RE = re.compile(
    r">>?\s*[\"']?[^|;&\n]*" + _HARNESS_CONFIG_PATH_RE.pattern
)
_OTHER_MUTATION_SHAPE_RE = re.compile(
    r"(?:"
    r"\bsed\b.*-i\b"
    r"|\btee\b"
    r"|\b(?:cp|mv|install|rsync)\b"
    r"|\bgit\s+(?:checkout|restore|apply|stash\s+pop)\b"
    r"|\brm\b"
    r"|\btruncate\b"
    r"|\bchmod\b"
    r")"
)
_GH_API_LEADER_RE = re.compile(r"^gh\s+api\b")
_ADVISORY_PATH_RE = re.compile(r"/security-advisories\b", re.IGNORECASE)
_GRAPHQL_ADVISORY_FIELD_RE = re.compile(
    r"securityAdvisor(y|ies)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class ActionCapabilities:
    raw_tool_id: str
    canonical_tool_id: str
    action_classes: tuple[str, ...]
    entity_types: tuple[str, ...] = ()
    mutated_paths: tuple[str, ...] = ()

    def has(self, action_class: str) -> bool:
        return action_class in self.action_classes


def _join_line_continuations(command: str) -> str:
    return re.sub(r"\\[ \t]*\n", " ", command)


def bash_touches_harness_config(command: str) -> bool:
    for segment in _SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized or not _HARNESS_CONFIG_PATH_RE.search(normalized):
            continue
        if _REDIRECT_TO_PATH_RE.search(normalized):
            return True
        if _READ_ONLY_LEADERS.match(normalized):
            continue
        if _OTHER_MUTATION_SHAPE_RE.search(normalized):
            return True
    return False


def bash_touches_advisory(command: str) -> bool:
    for segment in _SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized or not _GH_API_LEADER_RE.match(normalized):
            continue
        if _ADVISORY_PATH_RE.search(normalized):
            return True
        if "graphql" in normalized and _GRAPHQL_ADVISORY_FIELD_RE.search(normalized):
            return True
    return False


def _apply_patch_paths(command: str) -> tuple[str, ...]:
    try:
        from sibling_repo_worktree_guard import (  # noqa: PLC0415
            _apply_patch_paths as shared_apply_patch_paths,
        )
    except Exception:  # noqa: BLE001 - classification fails closed to no paths
        return ()
    try:
        return tuple(str(path) for path in shared_apply_patch_paths(command))
    except Exception:  # noqa: BLE001 - one malformed patch must not break a hook
        return ()


def _entity_types(tool_input: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    direct = tool_input.get("entity_type")
    if isinstance(direct, str) and direct:
        values.append(direct)
    entities = tool_input.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, Mapping):
                continue
            value = entity.get("entity_type")
            if isinstance(value, str) and value:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _neotoma_operation(raw_tool_id: str) -> str | None:
    for prefix in _NEOTOMA_PREFIXES:
        if raw_tool_id.startswith(prefix):
            return raw_tool_id.removeprefix(prefix)
    return None


def _canonical_tool_id(
    raw_tool_id: str, *, hook_event_name: str | None = None
) -> str:
    if hook_event_name in _TERMINATION_EVENTS or raw_tool_id in _TERMINATION_EVENTS:
        return "termination"
    if raw_tool_id in _SHELL_ALIASES:
        return "shell"
    if raw_tool_id in _READ_ALIASES:
        return "read"
    if raw_tool_id in _GREP_ALIASES:
        return "grep"
    if raw_tool_id in _GLOB_ALIASES:
        return "glob"
    if raw_tool_id == "apply_patch":
        return "apply_patch"
    operation = _neotoma_operation(raw_tool_id)
    if operation is not None:
        return f"neotoma.{operation}"
    if raw_tool_id.startswith("mcp__cua_repl__") or raw_tool_id == "computer":
        return "browser_ui"
    return raw_tool_id


def classify_tool_call(
    raw_tool_id: str,
    tool_input: Mapping[str, Any] | None,
    *,
    hook_event_name: str | None = None,
) -> ActionCapabilities:
    """Return stable action classes for one raw harness entrance."""
    raw_tool_id = str(raw_tool_id or "")
    payload: Mapping[str, Any] = tool_input if isinstance(tool_input, Mapping) else {}
    canonical = _canonical_tool_id(raw_tool_id, hook_event_name=hook_event_name)
    classes: list[str] = []
    entity_types = _entity_types(payload)
    mutated_paths: tuple[str, ...] = ()

    if canonical == "termination":
        classes.append("termination")
    if canonical in {"shell", "read", "grep", "glob"}:
        classes.append("credential_read_candidate")

    operation = _neotoma_operation(raw_tool_id)
    if operation in _NEOTOMA_MUTATIONS:
        classes.append("neotoma_mutation")
    if "agent_grant" in entity_types and "neotoma_mutation" in classes:
        classes.append("grant_write")
    if {"agent_policy", "relationship_type"} & set(entity_types) and (
        "neotoma_mutation" in classes
    ):
        classes.append("policy_write")
    if operation == "publish_rendered_page":
        classes.extend(("publication", "rendered_page_publication"))

    if canonical == "browser_ui":
        classes.append("browser_ui_effect")

    path = payload.get("file_path") or payload.get("notebook_path")
    if isinstance(path, str) and _HARNESS_CONFIG_PATH_RE.search(path):
        classes.append("harness_config")

    command = payload.get("command") or payload.get("cmd")
    if not isinstance(command, str):
        command = ""
    if canonical == "shell" and command:
        if bash_touches_harness_config(command):
            classes.append("harness_config")
        if bash_touches_advisory(command):
            classes.append("advisory")
    if canonical == "apply_patch" and command:
        mutated_paths = _apply_patch_paths(command)
        if any(_HARNESS_CONFIG_PATH_RE.search(path) for path in mutated_paths):
            classes.append("harness_config")

    return ActionCapabilities(
        raw_tool_id=raw_tool_id,
        canonical_tool_id=canonical,
        action_classes=tuple(dict.fromkeys(classes)),
        entity_types=entity_types,
        mutated_paths=mutated_paths,
    )


__all__ = [
    "ActionCapabilities",
    "bash_touches_advisory",
    "bash_touches_harness_config",
    "classify_tool_call",
]
