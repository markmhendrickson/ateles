"""
lib/gate_names.py — the one spelling of a gate name, and of its absence.

Gate names cross four boundaries: `workflow_definition` entities in Neotoma
(hand-edited), an issue entity's `gate_status` map (written by several agents),
GitHub labels, and the dispatcher's own comparisons. Each of those is a place a
name can pick up a mechanical variation that changes nothing a human reads and
everything a comparison does.

This module exists rather than an inline `.strip().lower()` at each read site
because the variations are not all handled by `lower()`, and because the sites
had already drifted: `lib/issue_labels.py` lowercased gate names while
`lib/daemon_runtime/workflow_resolver.py` only stripped them, so a gate stored
as ``"Impl"`` was a label-matching ``impl`` and a resolver-matching ``Impl`` at
the same time.

It lives in `lib/` rather than in `workflow_resolver` so that `issue_labels`,
which is a pure projection with no I/O, can import it without taking on
`httpx` and a Neotoma base URL. The normalization is pure; the resolution is
not, and they are separated accordingly.

Why the safety meaning lands here
---------------------------------

`impl` is not just another gate name. `ResolvedWorkflow.pre_impl_gate_names`
derives the whole pre-impl sequence by finding the phase of the gate named
``impl`` and taking everything earlier. A name that fails to match ``impl``
does not produce a wrong gate — it produces NO impl phase, hence an EMPTY
pre-impl sequence, hence a vacuous "no gate is unsigned". So a casing or
homoglyph difference in exactly one field silently converts a gated workflow
into an ungated one, which is why normalization is applied at the parse
boundary and not at each comparison.
"""

from __future__ import annotations

import unicodedata
from typing import Any

# The gate whose phase separates "before implementation" from "after". Every
# workflow that implements anything declares it.
IMPL_GATE_NAME = "impl"


def normalize_gate_name(raw: Any) -> str:
    """Reduce a gate name to the one form every comparison uses.

    Three mechanical variations collapse here, because each silently changes
    which gates count as pre-impl:

      * **Case.** A gate stored as ``"Impl"`` never equals ``"impl"``, so
        `pre_impl_gate_names` finds no impl phase and returns ``()`` — every
        pre-impl gate vanishes and the issue reads as fully signed.
      * **Unicode lookalikes.** NFKC folding maps the fullwidth and
        mathematical-Latin ranges onto ASCII, so ``"ｉｍｐｌ"`` and
        ``"\U0001d422\U0001d426\U0001d429\U0001d425"`` reduce to ``"impl"``
        rather than passing as a distinct gate no phase comparison matches.
      * **Surrounding whitespace**, including the non-breaking space that
        survives a copy-paste into an entity field.

    NFKC runs BEFORE casefold because the fold is defined on the composed form;
    running it after would leave fullwidth capitals unmatched. `casefold` is
    used rather than `lower` so non-ASCII case pairs reduce completely.

    This normalizes FORM only. A name that is not a gate the swarm knows is
    returned reduced and unchanged — inventing a gate is how a bad value
    becomes a silent waive. Returns ``""`` for absence, so absence has exactly
    one spelling (the `SENTINEL_ASSIGNEES` shape in
    `execution/daemons/apis/routing.py`).

    NOT handled deliberately: Cyrillic and Greek homoglyphs (``і``, ``ο``) are
    distinct characters that NFKC does not fold, and mapping them would mean
    guessing that a name in another script was meant to be Latin. They reduce
    to a name that matches no gate, which fails closed — an unmatched gate name
    now refuses rather than shortening the sequence, so the safe outcome does
    not depend on catching every possible lookalike.
    """
    if raw is None:
        return ""
    folded = unicodedata.normalize("NFKC", str(raw)).casefold()
    # NFKC leaves NBSP (U+00A0) intact; `str.strip()` with no argument removes
    # it along with ASCII whitespace, but only at the edges — an interior one
    # is a different name and is left alone.
    return folded.strip()
