"""The single place this fixture touches the #1190 runtime boundary.

`docs/foundation/adapters.md#the-admission-contract` obligations are exercised against the "sixth"
reference adapter that `lib.adapters` (ateles#1190) is to expose. That runtime does not exist on this
checkout yet (#1190 is open, unmerged, no branch) — `import_sixth_adapter()` is therefore expected to
raise `RuntimeMissingError` today, and the runner turns that into `FailureCode.RUNTIME_MISSING` rather
than a soft skip (`docs/foundation/conformance_suite.md#adapter-admission`; UX AC: "do not skip green").

This module names the exact surface #1190 must expose for the fixture to pick it up with no change:
a `lib.adapters` package carrying a `SIXTH_ADAPTER_FACTORY` callable that returns a fresh
`ReferenceAdapter`-shaped object (see `reference.py` for the shape the instruments assert against)
each call, so reference and variant runs never share mutable state (arch: variant isolation).
"""

from __future__ import annotations

from typing import Callable

from execution.conformance.adapter_admission.reference import ReferenceAdapter


class RuntimeMissingError(RuntimeError):
    """Raised when `lib.adapters` (ateles#1190) is not importable, or lacks the expected factory."""


def import_sixth_adapter_factory() -> Callable[[], ReferenceAdapter]:
    """Return a zero-arg factory that builds a fresh sixth/reference adapter instance.

    Raises `RuntimeMissingError` if `lib.adapters` cannot be imported, or does not expose
    `SIXTH_ADAPTER_FACTORY`. Never returns a stand-in silently — the caller (runner.py) is the one
    place that decides what a missing runtime means for the exit code.
    """
    try:
        import lib.adapters as adapters_module  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeMissingError(
            "lib.adapters is not importable. The adapter runtime (ateles#1190) has not "
            "landed on this checkout."
        ) from exc

    factory = getattr(adapters_module, "SIXTH_ADAPTER_FACTORY", None)
    if factory is None or not callable(factory):
        raise RuntimeMissingError(
            "lib.adapters is importable but does not expose SIXTH_ADAPTER_FACTORY(). "
            "The adapter runtime (ateles#1190) has not shipped the reference adapter yet."
        )
    return factory
