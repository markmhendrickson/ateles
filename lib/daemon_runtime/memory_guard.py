"""
Memory guard wrapper for OWASP Agent Memory Guard integration.

Screens untrusted input from external sources before writing to Neotoma,
defending against memory poisoning (ASI06).

Integration for ateles#40.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from agent_memory_guard import (
        Action,
        MemoryGuard,
        Policy,
        PolicyViolation,
        SourceClass,
    )
    AMG_AVAILABLE = True
except ImportError:
    AMG_AVAILABLE = False
    log.warning(
        "agent-memory-guard not installed — memory poisoning defenses disabled. "
        "Install with: pip install agent-memory-guard>=0.3.3,<0.5"
    )


class NeotomaGuard:
    """
    Wraps Neotoma writes through OWASP Agent Memory Guard to screen untrusted input.

    Usage:
        guard = NeotomaGuard()
        action = guard.screen_entity(entity, source_class=SourceClass.EXTERNAL_TOOL, daemon="turdus")
        if action in (Action.ALLOW, Action.REDACT):
            store_to_neotoma(entity)
        elif action == Action.QUARANTINE:
            log_quarantined(entity)
    """

    def __init__(self, policy: Policy | None = None):
        """
        Initialize the guard.

        Args:
            policy: AMG Policy to enforce. Defaults to Policy.tiered().

        Raises:
            RuntimeError: If agent-memory-guard is not available (fail closed).
        """
        if not AMG_AVAILABLE:
            raise RuntimeError(
                "agent-memory-guard not available. "
                "Install with: pip install agent-memory-guard>=0.3.3,<0.5"
            )
        self._guard = MemoryGuard(policy=policy or Policy.tiered())

    def screen_entity(
        self,
        entity: dict[str, Any],
        *,
        source_class: SourceClass | str = SourceClass.EXTERNAL_TOOL,
        daemon: str = "unknown",
    ) -> Action:
        """
        Screen an entity dict before storing to Neotoma.

        Args:
            entity: The entity dict to screen (modified in place if redacted).
            source_class: Provenance of this write (EXTERNAL_TOOL for untrusted input).
            daemon: Name of the daemon performing the write.

        Returns:
            Action taken: ALLOW, REDACT, QUARANTINE, or BLOCK.

        Raises:
            PolicyViolation: If the entity is blocked by policy.
        """
        actions: list[Action] = []
        blocked_fields: list[str] = []
        quarantined_fields: list[str] = []

        if isinstance(source_class, str):
            source_class = SourceClass(source_class)

        for field, value in list(entity.items()):
            if not isinstance(value, str):
                continue

            entity_type = entity.get("entity_type", "entity")
            entity_name = entity.get("canonical_name", entity.get("title", "unnamed"))
            key = f"{daemon}:{entity_type}:{entity_name}:{field}"

            try:
                action = self._guard.write(
                    key,
                    value,
                    source=daemon,
                    source_class=source_class,
                )
                actions.append(action)

                if action == Action.REDACT:
                    entity[field] = self._guard.read(key, default=value)
                    log.info(f"[{daemon}] Redacted {entity_type}.{field}")
                elif action == Action.QUARANTINE:
                    quarantined_fields.append(field)
                elif action == Action.BLOCK:
                    blocked_fields.append(field)

            except PolicyViolation as exc:
                blocked_fields.append(field)
                log.warning(f"[{daemon}] Blocked {entity_type}.{field}: {exc}")
                raise

        if blocked_fields:
            raise PolicyViolation(
                f"Entity blocked: fields {blocked_fields} violated policy",
                key=f"{daemon}:{entity.get('entity_type')}",
            )

        if quarantined_fields:
            log.warning(f"[{daemon}] Entity quarantined: {quarantined_fields}")
            return Action.QUARANTINE

        if Action.REDACT in actions:
            return Action.REDACT

        return Action.ALLOW

    @property
    def guard(self) -> MemoryGuard | None:
        """Access the underlying MemoryGuard instance."""
        return self._guard

    @property
    def events(self) -> list:
        """Security events logged by the guard."""
        return self._guard.events

    @property
    def quarantine(self) -> dict:
        """Quarantined values held for review."""
        return self._guard.quarantine
