"""Capability slot names for media generation, defined once.

Each slot is the exact ``capability`` string of a ``vendor_binding`` entity
(foundation decision 35). There are no aliases: ``svg_gen``, ``veo`` and the
like are rejected, not mapped, so a typo can never silently resolve to a
different (paid) binding.
"""

from __future__ import annotations

VECTOR_MARK_GENERATION = "vector_mark_generation"
IMAGE_GENERATION = "image_generation"
VIDEO_GENERATION = "video_generation"

SLOTS: tuple[str, ...] = (
    VECTOR_MARK_GENERATION,
    IMAGE_GENERATION,
    VIDEO_GENERATION,
)


def is_slot(value: object) -> bool:
    """True only for one of the exact slot strings."""
    return isinstance(value, str) and value in SLOTS
