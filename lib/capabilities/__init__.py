"""Host-side media-generation capability client (ateles#1189).

Public surface: ``lib.capabilities.generation``. Import from here::

    from lib.capabilities import generate, render_and_critique, GenerationRefused

Trust boundary: this package runs only where generation credentials are
allowed to exist. Never in a dispatched agent child. See
``docs/dev/generation_capability_client.md``.
"""

from .critique import (
    ConceptResult,
    CritiqueVerdict,
    ReviewInput,
    SelectionSummary,
    render_and_critique,
    summarize_concepts,
)
from .errors import CODES, CapabilityError, GenerationRefused
from .generation import CapabilityClient, GenerationResult, generate
from .slots import IMAGE_GENERATION, SLOTS, VECTOR_MARK_GENERATION, VIDEO_GENERATION

__all__ = [
    "CODES",
    "CapabilityClient",
    "CapabilityError",
    "ConceptResult",
    "CritiqueVerdict",
    "GenerationRefused",
    "GenerationResult",
    "IMAGE_GENERATION",
    "ReviewInput",
    "SLOTS",
    "SelectionSummary",
    "VECTOR_MARK_GENERATION",
    "VIDEO_GENERATION",
    "generate",
    "render_and_critique",
    "summarize_concepts",
]
