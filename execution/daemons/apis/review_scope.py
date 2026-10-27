"""execution/daemons/apis/review_scope.py — a repo's own declaration of which
changes need which review lenses.

Operator ruling, 2026-10-27 (agent_policy ``ent_d0f1a840e549b3b299f62397``,
bootstrap rule):

* Lens selection is by changed path. The full five-role panel reviews changes
  that touch external sending, payment, sign-in or authorization, data writes,
  schema, or the gate, approval and review machinery itself. Any other change
  requires green CI plus the one lens whose scope it touches (UX for
  user-facing copy and UI, architecture otherwise); a reviewer may still
  request another lens with a stated reason.
* A lens's approval carries to a new head when the diff since its approved head
  does not touch its scope; it is re-run only when its scope changed.

This module holds the pure decisions and the one file format, shared by the
approval tool (``approve_pr_as_app``) and the carry rule (``review_carry``). It
imports no daemon, no network client and no Neotoma: a session on any machine
can use it for any repository.

The scope file lives IN the repository it governs, at ``.github/review_scope.json``,
and is read from the PR's BASE branch, never from its head. A PR therefore
cannot edit the file to downgrade its own panel; a change to the file itself is
reviewed under the previous version (and the file lists itself in
``full_panel_paths`` so that review is the full panel).

Format (``version`` 1)::

    {
      "version": 1,
      "full_panel_paths": ["globs", ...],      # required, non-empty
      "ux_paths": ["globs", ...],              # optional
      "lens_scopes": {"security": ["globs"], "arch": [...], ...}   # optional
    }

Globs match the whole repo-relative path with ``/`` separators: ``*`` is any run
of characters except ``/``, ``?`` one such character, ``**`` any run including
``/`` (``a/**`` is everything under ``a/``, ``**/x.py`` is ``x.py`` at any
depth), and a trailing ``/`` means everything under that directory. There is no
negation and no basename-only matching: a pattern without ``/`` matches only a
file at the repo root.

Fail closed on every input (docs/foundation/principles.md, "fail closed on the
field that carries the safety meaning"): a missing, unreadable, oversized,
malformed, unrecognised-key or empty-``full_panel_paths`` file, and an empty
changed-file list, select the FULL panel, and the reason is reported. For carry
the same failures mean "no scope declared", which carries LESS (the pre-existing
rule: every non-allowlisted file touches every lens), never more.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

SCOPE_PATH = ".github/review_scope.json"
SCOPE_VERSION = 1
MAX_SCOPE_BYTES = 256 * 1024

# Lens keys a scope file may name. Mirrors `approve_pr_as_app.LENS_AGENTS` and
# `review_panel.LENSES`; `test_review_scope.py` pins the equality.
KNOWN_LENSES: frozenset[str] = frozenset(
    {"pm", "arch", "ux", "legal", "qa", "security", "content"}
)

_TOP_LEVEL_KEYS = frozenset({"version", "full_panel_paths", "ux_paths", "lens_scopes"})


class ScopeError(ValueError):
    """The scope file is not a usable declaration."""


# ── globs ───────────────────────────────────────────────────────────────────


def _glob_regex(pattern: str) -> re.Pattern[str]:
    if not pattern or pattern.startswith("/") or "\\" in pattern or "\x00" in pattern:
        raise ScopeError(f"unusable glob {pattern!r}")
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")  # `**/` also matches zero directories
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def compile_globs(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(_glob_regex(p) for p in patterns)


def first_match(path: str, globs: Sequence[re.Pattern[str]]) -> int | None:
    """Index of the first glob *path* matches, or None."""
    norm = path[2:] if path.startswith("./") else path
    for i, g in enumerate(globs):
        if g.match(norm):
            return i
    return None


# ── the file ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReviewScope:
    full_panel_paths: tuple[str, ...]
    ux_paths: tuple[str, ...] = ()
    lens_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _globs(self, patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
        return compile_globs(patterns)

    def full_panel_trigger(self, files: Iterable[str]) -> tuple[str, str] | None:
        """(file, matching glob) for the first changed file on a full-panel path."""
        globs = self._globs(self.full_panel_paths)
        for f in files:
            idx = first_match(f, globs)
            if idx is not None:
                return f, self.full_panel_paths[idx]
        return None

    def ux_trigger(self, files: Iterable[str]) -> tuple[str, str] | None:
        globs = self._globs(self.ux_paths)
        for f in files:
            idx = first_match(f, globs)
            if idx is not None:
                return f, self.ux_paths[idx]
        return None

    def declares(self, lens: str) -> bool:
        return lens in self.lens_scopes

    def touches_lens(self, lens: str, files: Iterable[str]) -> str | None:
        """The first file in *files* that falls in *lens*'s declared scope, or
        None. Only meaningful when ``declares(lens)``; an undeclared lens has no
        scope here and the caller must use the default rule."""
        globs = self._globs(tuple(self.lens_scopes.get(lens, ())))
        for f in files:
            if first_match(f, globs) is not None:
                return f
        return None


def _globs_field(raw: object, name: str, *, required: bool) -> tuple[str, ...]:
    if raw is None:
        if required:
            raise ScopeError(f"{name} is missing")
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ScopeError(f"{name} must be a list of strings")
    if required and not raw:
        raise ScopeError(f"{name} is empty")
    compile_globs(raw)  # raises ScopeError on an unusable glob
    return tuple(raw)


def parse_scope(text: str) -> ReviewScope:
    """The declaration in *text*, or ``ScopeError`` saying why it is unusable."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ScopeError(f"not valid JSON ({exc.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise ScopeError("top level is not an object")
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ScopeError(f"unrecognised key(s): {', '.join(map(str, unknown))}")
    version = data.get("version")
    if isinstance(version, bool) or version != SCOPE_VERSION:
        raise ScopeError(f"version must be {SCOPE_VERSION}")
    full = _globs_field(data.get("full_panel_paths"), "full_panel_paths", required=True)
    ux = _globs_field(data.get("ux_paths"), "ux_paths", required=False)
    raw_lens = data.get("lens_scopes")
    lens_scopes: dict[str, tuple[str, ...]] = {}
    if raw_lens is not None:
        if not isinstance(raw_lens, dict):
            raise ScopeError("lens_scopes must be an object")
        for lens, globs in raw_lens.items():
            if lens not in KNOWN_LENSES:
                raise ScopeError(f"lens_scopes names unknown lens {lens!r}")
            lens_scopes[lens] = _globs_field(globs, f"lens_scopes.{lens}", required=True)
    return ReviewScope(full, ux, lens_scopes)


@dataclass(frozen=True)
class ScopeLoad:
    """A scope file read, or why none could be used."""

    scope: ReviewScope | None
    source: str  # where it was read from, or where it was looked for
    error: str = ""


def load_from_contents_payload(payload: object, *, source: str) -> ScopeLoad:
    """A ``ScopeLoad`` from a GitHub contents-API response body."""
    if not isinstance(payload, dict) or payload.get("type") not in (None, "file"):
        return ScopeLoad(None, source, "the path is not a file")
    content = payload.get("content")
    if not isinstance(content, str):
        return ScopeLoad(None, source, "the file has no readable content")
    if payload.get("encoding") not in (None, "base64"):
        return ScopeLoad(None, source, "the file content is not base64")
    try:
        raw = base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError):
        return ScopeLoad(None, source, "the file content is not valid base64")
    if len(raw) > MAX_SCOPE_BYTES:
        return ScopeLoad(None, source, f"the file is larger than {MAX_SCOPE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ScopeLoad(None, source, "the file is not UTF-8")
    try:
        return ScopeLoad(parse_scope(text), source)
    except ScopeError as exc:
        return ScopeLoad(None, source, str(exc))


# ── the panel decision ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PanelDecision:
    """Which review panel a change needs, and why (for the approval body)."""

    full: bool
    lens: str  # the single required lens; "" when `full`
    reason: str
    source: str

    def describe(self) -> str:
        if self.full:
            return f"panel mode: path -> FULL five-lens panel. {self.reason} Scope file: {self.source}."
        return (
            f"panel mode: path -> one lens ({self.lens}). {self.reason} "
            f"Scope file: {self.source}."
        )


def select_path_panel(changed_files: Sequence[str], load: ScopeLoad) -> PanelDecision:
    """The panel by changed path. Fail closed: anything unknown is the full panel."""
    source = load.source
    if load.scope is None:
        return PanelDecision(
            True, "", f"The scope file could not be used ({load.error or 'unknown reason'}), so the full panel applies.", source
        )
    files = [f for f in changed_files if f]
    if not files:
        return PanelDecision(
            True, "", "The changed-file list is empty or unreadable, so the full panel applies.", source
        )
    hit = load.scope.full_panel_trigger(files)
    if hit is not None:
        return PanelDecision(
            True, "", f"`{hit[0]}` matches full_panel_paths `{hit[1]}`.", source
        )
    ux = load.scope.ux_trigger(files)
    if ux is not None:
        return PanelDecision(
            False, "ux", f"`{ux[0]}` matches ux_paths `{ux[1]}`, and no changed file is on a full-panel path.", source
        )
    return PanelDecision(
        False, "arch", "No changed file is on a full-panel or ux path, so architecture is the one lens.", source
    )
