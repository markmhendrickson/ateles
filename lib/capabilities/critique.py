"""Render-and-critique loop for concept work.

``render_and_critique`` generates a concept, renders it for review, asks an
injectable critic (an image-capable model in production) whether it meets the
brief and criteria, and revises the prompt, up to a hard round limit. It is a
distinct function from ``generate``: it never silently wraps or auto-promotes.

The one rule that matters: ``ready_for_operator_selection`` is set to ``True``
in exactly one place, the branch where the critic passes the concept. A failed
critique, the round limit, a refusal mid-loop, a critic error, an empty
artifact, or a missing critic all leave it ``False``. Failures are returned
with their status and notes; they are never omitted.

Example: the round-limit path (a critic that never passes)::

    from lib.capabilities import render_and_critique, CritiqueVerdict

    result = render_and_critique(
        "vector_mark_generation", "A monogram mark",
        criteria="<the visual-authorship checklist, injected by the caller>",
        critique_fn=lambda review: CritiqueVerdict(False, "stroke weight uneven"),
        max_rounds=3,
    )
    assert result.status == "round_limit"
    assert result.ready_for_operator_selection is False   # never promoted
    print(result.refusal_code)   # "CRITIQUE_ROUND_LIMIT"
    print(result.notes)          # one critique note per round

An empty selection is explicit, never a missing key: ``summarize_concepts``
returns ``ready=[]`` plus reason counts that sum to the number of concepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .errors import CRITIQUE_ROUND_LIMIT, GenerationRefused
from .generation import GenerationResult, generate as _generate

MAX_ROUNDS_HARD_LIMIT = 10
DEFAULT_MAX_ROUNDS = 3

STATUS_PASSED = "passed"
STATUS_ROUND_LIMIT = "round_limit"
STATUS_REFUSED = "refused"
STATUS_CRITIC_ERROR = "critic_error"
STATUS_NO_CRITIC = "no_critic"


@dataclass(frozen=True)
class CritiqueVerdict:
    passed: bool
    notes: str = ""
    revised_prompt: str | None = None


@dataclass(frozen=True)
class ReviewInput:
    """What a critic sees for one round."""

    round: int
    brief: str
    criteria: str
    prompt: str
    artifact_ref: str
    raster_preview_ref: str | None
    generation: GenerationResult


@dataclass
class ConceptResult:
    slot: str
    brief: str
    status: str
    ready_for_operator_selection: bool = False
    rounds: int = 0
    final: GenerationResult | None = None
    notes: list[str] = field(default_factory=list)
    refusal_code: str | None = None
    generations: list[GenerationResult] = field(default_factory=list)


def render_and_critique(
    slot: str,
    brief: str,
    *,
    criteria: str = "",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    critique_fn: Callable[[ReviewInput], CritiqueVerdict] | None = None,
    generate_fn: Callable[..., GenerationResult] | None = None,
    **generate_kwargs: Any,
) -> ConceptResult:
    """Generate, review, and revise one concept. See the module docstring."""
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not (
        1 <= max_rounds <= MAX_ROUNDS_HARD_LIMIT
    ):
        raise ValueError(f"max_rounds must be an integer from 1 to {MAX_ROUNDS_HARD_LIMIT}")
    result = ConceptResult(slot=slot, brief=brief, status=STATUS_NO_CRITIC)
    if critique_fn is None:
        # Fail closed: without a critic nothing can be judged, so nothing is
        # generated (no spend) and nothing is promoted.
        result.notes.append("no critique_fn supplied; concept not generated and not ready")
        return result
    gen = generate_fn or _generate
    prompt = brief
    for round_no in range(1, max_rounds + 1):
        result.rounds = round_no
        try:
            generation = gen(slot, prompt, **generate_kwargs)
        except GenerationRefused as refused:
            result.status = STATUS_REFUSED
            result.refusal_code = refused.code
            result.notes.append(f"round {round_no}: generation refused [{refused.code}] {refused.hint}")
            return result
        result.generations.append(generation)
        result.final = generation
        try:
            verdict = critique_fn(
                ReviewInput(
                    round=round_no, brief=brief, criteria=criteria, prompt=prompt,
                    artifact_ref=generation.artifact_ref,
                    raster_preview_ref=generation.raster_preview_ref,
                    generation=generation,
                )
            )
            if not isinstance(verdict, CritiqueVerdict):
                raise TypeError("critique_fn must return a CritiqueVerdict")
        except Exception as exc:  # noqa: BLE001 - a broken critic never promotes
            result.status = STATUS_CRITIC_ERROR
            result.notes.append(f"round {round_no}: critic failed ({type(exc).__name__})")
            return result
        result.notes.append(f"round {round_no}: {'pass' if verdict.passed else 'fail'}: {verdict.notes}")
        if verdict.passed is True:
            result.status = STATUS_PASSED
            result.ready_for_operator_selection = True  # the only place this is set
            return result
        prompt = verdict.revised_prompt or (
            f"{brief}\n\nRevise to address: {verdict.notes}" if verdict.notes else brief
        )
    result.status = STATUS_ROUND_LIMIT
    result.refusal_code = CRITIQUE_ROUND_LIMIT
    return result


@dataclass(frozen=True)
class SelectionSummary:
    ready: list[ConceptResult]
    not_ready: list[ConceptResult]
    reason_counts: dict[str, int]


def summarize_concepts(concepts: Sequence[ConceptResult]) -> SelectionSummary:
    """Split concepts into ready and not, with counts by status.

    ``ready`` is always a list (empty when nothing passed) and ``reason_counts``
    always accounts for every input concept.
    """
    ready = [c for c in concepts if c.ready_for_operator_selection]
    not_ready = [c for c in concepts if not c.ready_for_operator_selection]
    counts: dict[str, int] = {}
    for concept in not_ready:
        counts[concept.status] = counts.get(concept.status, 0) + 1
    if ready:
        counts[STATUS_PASSED] = len(ready)
    return SelectionSummary(ready=ready, not_ready=not_ready, reason_counts=counts)
