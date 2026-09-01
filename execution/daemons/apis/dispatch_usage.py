"""
execution/daemons/apis/dispatch_usage.py — per-dispatch model + token attribution.

Why this exists
---------------
Nothing in the swarm recorded which model a dispatch actually used, or what it
cost. The only usage figures available were the provider dashboard's, which
aggregate by model and day — enough to see that one model burned five days of
budget in one day, and useless for saying WHICH role, task, or stage did it.

This module extracts, from what a harness CLI already emits, the three things
that make a spend attributable:

* ``model``     — the model the harness reports it actually used
* ``provider``  — which plan the spend landed on
* ``tokens``    — input / output / cache counts, when reported

and ``skill_runner`` writes them onto the ``harness_event`` it already emits per
dispatch. Recording only: nothing here caps, throttles, or reroutes anything.

What each harness actually reports (verified empirically 2026-09-01, by running
each CLI and reading its real output — not from documentation)
-------------------------------------------------------------------------------
``claude``  ``--output-format json`` emits a trailing object carrying ``usage``
            (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
            ``cache_creation_input_tokens``), ``total_cost_usd``, and a
            ``modelUsage`` map keyed BY MODEL NAME. That map is the only place
            any harness names the model it actually used, so it is the strongest
            attribution signal available.

``codex``   ``--json`` emits JSONL; the final ``turn.completed`` event carries
            ``usage`` (``input_tokens``, ``cached_input_tokens``,
            ``output_tokens``, ``reasoning_output_tokens``). It reports NO model
            name anywhere in that stream.

``cursor``  ``--output-format json`` emits a ``result`` object carrying ``usage``
            with camelCase keys (``inputTokens``, ``outputTokens``,
            ``cacheReadTokens``, ``cacheWriteTokens``). ``stream-json`` adds a
            ``system``/``init`` event with a ``model`` field, but on an
            unpinned dispatch that field reads literally ``"Auto"`` — Cursor's
            server-side selector — NOT the model it resolved to. Cursor never
            reports the resolved model in either format.

The honest consequence, stated plainly
--------------------------------------
Token counts are only available when a harness is invoked in a JSON output mode.
The swarm's dispatch path deliberately runs all three in TEXT mode, because
downstream consumers parse the agent's prose out of stdout (gate verdicts, PR
URLs, review bodies). Switching those invocations to JSON would break that
contract, which is well outside "record what a dispatch spent".

So this module is written to be *parse-what-is-there*: it reads usage when the
output happens to carry it and reports honest absence when it does not. Every
field is optional and nothing is ever estimated, inferred, or back-filled from
a request. A recorded token count that is actually a guess is worse than a
missing field, so an unparseable output yields ``None``, never a zero.

``model_source`` records HOW the model was determined, so a reader can tell a
measured value from a merely-requested one:

* ``reported``  — the harness named the model it used (claude ``modelUsage``)
* ``requested`` — no harness report; this is the model the router ASKED for,
                  which is not proof of what ran. Recorded because per-model
                  fallback (ateles#667) can pick a different model than
                  requested, and a reader must never mistake one for the other.
* ``default``   — nothing was pinned and nothing was reported; the harness used
                  its own ambient default, whose identity is genuinely unknown.

That last case is not a gap to paper over: it is the finding. A dispatch that
cannot say what model it ran is exactly the condition that let one expensive
model absorb a whole plan's quota unnoticed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Providers whose CLI never reports the model it resolved to. Kept explicit so
# a reader of a `model_source="default"` event knows the omission is a known
# property of the harness, not a parse failure here.
PROVIDERS_WITHOUT_MODEL_REPORTING = ("codex", "cursor")


@dataclass
class DispatchUsage:
    """One dispatch's model + token attribution.

    Every field is optional by design. A harness that reports nothing yields an
    all-``None`` instance, which records honestly as "usage not reported by this
    harness" rather than as zero tokens.
    """

    provider: str = ""
    model: str | None = None
    # How `model` was determined — see the module docstring. Never claims a
    # requested model was the one actually used.
    model_source: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_cost_usd: float | None = None
    # Models named by the harness itself, when it reports more than one (a
    # single dispatch can span models, e.g. a cheap summarizer plus a main
    # model). Ordered as the harness listed them.
    reported_models: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_tokens(self) -> bool:
        """True when at least one token count was actually reported."""
        return any(
            v is not None
            for v in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
            )
        )

    @property
    def total_tokens(self) -> int | None:
        """Input + output, when both are known.

        Cache reads are deliberately excluded: they are billed differently
        across providers, and summing them into one headline number would make
        two providers' totals look comparable when they are not.
        """
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def as_event_fields(self) -> dict:
        """Render the fields to merge into a ``harness_event`` entity.

        Only keys with real values are emitted, so a harness that reports no
        usage adds no token keys at all — an absent field reads as "not
        reported", where a zero would read as "measured zero".
        """
        out: dict = {}
        if self.provider:
            out["provider"] = self.provider
        if self.model:
            out["model"] = self.model
        if self.model_source:
            out["model_source"] = self.model_source
        if self.input_tokens is not None:
            out["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            out["output_tokens"] = self.output_tokens
        if self.cache_read_tokens is not None:
            out["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens is not None:
            out["cache_write_tokens"] = self.cache_write_tokens
        if self.reasoning_tokens is not None:
            out["reasoning_tokens"] = self.reasoning_tokens
        if self.total_tokens is not None:
            out["total_tokens"] = self.total_tokens
        if self.total_cost_usd is not None:
            out["total_cost_usd"] = self.total_cost_usd
        return out

    def summary(self) -> str:
        """One-line human summary for logs and ``output_summary``.

        Says "usage not reported" explicitly rather than printing zeros, so a
        log reader is never misled into thinking a dispatch was free.
        """
        parts = [f"provider={self.provider or 'unknown'}"]
        if self.model:
            parts.append(f"model={self.model}({self.model_source or 'unknown'})")
        else:
            parts.append("model=unreported")
        if self.has_tokens:
            parts.append(
                f"tokens in={self.input_tokens if self.input_tokens is not None else '?'}"
                f" out={self.output_tokens if self.output_tokens is not None else '?'}"
            )
            if self.cache_read_tokens:
                parts.append(f"cache_read={self.cache_read_tokens}")
        else:
            parts.append("tokens=unreported")
        if self.total_cost_usd is not None:
            parts.append(f"cost_usd={self.total_cost_usd}")
        return " ".join(parts)


def _as_int(value: object) -> int | None:
    """Coerce a reported count to int, or None when it is not a real number.

    Booleans are rejected explicitly: ``isinstance(True, int)`` is True in
    Python, and a stray boolean silently becoming ``1`` token is exactly the
    kind of fake-but-plausible figure this module exists to avoid.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _iter_json_objects(text: str):
    """Yield top-level JSON objects from harness output.

    Handles both shapes seen in practice: one whole JSON document (claude,
    cursor ``json``) and JSONL event streams (codex ``--json``, cursor
    ``stream-json``). Non-JSON lines — banners, warnings, an agent's prose —
    are skipped rather than treated as an error, because every one of these
    CLIs interleaves human-readable noise with its machine output.
    """
    if not text:
        return
    stripped = text.strip()
    if not stripped:
        return
    # Whole-document first: the common case for --output-format json.
    try:
        obj = json.loads(stripped)
    except ValueError:
        pass
    else:
        if isinstance(obj, dict):
            yield obj
        return
    # Otherwise treat it as JSONL, tolerating interleaved non-JSON lines.
    for line in stripped.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _usage_from_claude(objects: list[dict]) -> DispatchUsage:
    """Parse claude's ``--output-format json`` result object.

    claude is the only harness that names the model it actually used, via the
    ``modelUsage`` map keyed by model name.
    """
    usage = DispatchUsage(provider="claude")
    for obj in objects:
        raw = obj.get("usage")
        if isinstance(raw, dict):
            usage.input_tokens = _as_int(raw.get("input_tokens"))
            usage.output_tokens = _as_int(raw.get("output_tokens"))
            usage.cache_read_tokens = _as_int(raw.get("cache_read_input_tokens"))
            usage.cache_write_tokens = _as_int(raw.get("cache_creation_input_tokens"))
            details = raw.get("output_tokens_details")
            if isinstance(details, dict):
                usage.reasoning_tokens = _as_int(details.get("thinking_tokens"))
        cost = _as_float(obj.get("total_cost_usd"))
        if cost is not None:
            usage.total_cost_usd = cost
        model_usage = obj.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            usage.reported_models = tuple(str(k) for k in model_usage)
            # The dispatch's headline model is the one that did the most output
            # work, so a small auxiliary model never masquerades as the main one.
            def _output_of(name: str) -> int:
                entry = model_usage.get(name)
                if isinstance(entry, dict):
                    return _as_int(entry.get("outputTokens")) or _as_int(
                        entry.get("output_tokens")
                    ) or 0
                return 0

            usage.model = max(usage.reported_models, key=_output_of)
            usage.model_source = "reported"
    return usage


def _usage_from_codex(objects: list[dict]) -> DispatchUsage:
    """Parse codex ``--json`` JSONL; usage rides the ``turn.completed`` event.

    codex reports no model name, so ``model`` stays unset here and is filled in
    by the caller only from what was REQUESTED, marked as such.
    """
    usage = DispatchUsage(provider="codex")
    for obj in objects:
        raw = obj.get("usage")
        if not isinstance(raw, dict):
            continue
        # Later turns supersede earlier ones; a resumed thread emits several.
        usage.input_tokens = _as_int(raw.get("input_tokens"))
        usage.output_tokens = _as_int(raw.get("output_tokens"))
        usage.cache_read_tokens = _as_int(raw.get("cached_input_tokens"))
        usage.cache_write_tokens = _as_int(raw.get("cache_write_input_tokens"))
        usage.reasoning_tokens = _as_int(raw.get("reasoning_output_tokens"))
    return usage


def _usage_from_cursor(objects: list[dict]) -> DispatchUsage:
    """Parse cursor's ``json`` / ``stream-json`` output.

    Cursor uses camelCase token keys. Its ``system``/``init`` event carries a
    ``model`` field, but on an unpinned dispatch that reads literally "Auto" —
    the selector's name, not the model it chose. Treating "Auto" as a model
    would record a value that looks measured and identifies nothing, which is
    precisely the failure this module is meant to prevent, so it is discarded.
    """
    usage = DispatchUsage(provider="cursor")
    for obj in objects:
        raw = obj.get("usage")
        if isinstance(raw, dict):
            usage.input_tokens = _as_int(raw.get("inputTokens"))
            usage.output_tokens = _as_int(raw.get("outputTokens"))
            usage.cache_read_tokens = _as_int(raw.get("cacheReadTokens"))
            usage.cache_write_tokens = _as_int(raw.get("cacheWriteTokens"))
        model = obj.get("model")
        if isinstance(model, str) and model.strip():
            if model.strip().lower() != "auto":
                usage.model = model.strip()
                usage.model_source = "reported"
                usage.reported_models = (model.strip(),)
    return usage


_PARSERS = {
    "claude": _usage_from_claude,
    "codex": _usage_from_codex,
    "cursor": _usage_from_cursor,
}


def parse_dispatch_usage(
    provider: str,
    stdout: str,
    *,
    requested_model: str | None = None,
) -> DispatchUsage:
    """Extract model + token attribution for one dispatch.

    ``stdout`` is whatever the harness produced. When it carries no machine
    usage — the case for the swarm's current text-mode invocations — the result
    holds no token counts, and ``model`` falls back to ``requested_model``
    marked ``model_source="requested"`` so a reader can never mistake the
    router's intent for a measurement.

    Never raises: usage recording must not be able to fail a dispatch.
    """
    provider = (provider or "").strip().lower()
    parser = _PARSERS.get(provider)
    if parser is None:
        usage = DispatchUsage(provider=provider)
    else:
        try:
            usage = parser(list(_iter_json_objects(stdout or "")))
        except Exception:
            # A malformed/truncated stream must degrade to "unreported", never
            # to a partial figure that reads as measured.
            usage = DispatchUsage(provider=provider)

    if usage.model is None:
        requested = (requested_model or "").strip()
        if requested and requested.lower() != "auto":
            usage.model = requested
            usage.model_source = "requested"
        else:
            usage.model_source = "default"
    return usage
