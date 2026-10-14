"""Which files are placeholder templates, defined once.

A placeholder template (``.env.example`` and its siblings) shows the shape of a
credential file with no values in it. It is tracked in repositories on purpose
and is the one safe way to see which names a credential file defines, so every
credential control treats it the same way:

* the credential read hook (``.claude/hooks/credential_read_guard.py``) never
  counts it as a credential path,
* the review launch check (``refuse_if_credential_files_in_worktree`` in
  ``execution/scripts/harness_lens_runner.py``) does not refuse a worktree for
  holding one,
* the review sandbox profile (``build_sandbox_exec_profile`` in the same
  runner) re-allows reading one after its credential read denies.

A template is judged by its own name. A symbolic link named like a template is
judged by what it points at as well: the hook follows links, and the sandbox
matches the resolved path, so a template-looking name cannot launder a
credential target.

This module is stdlib-only with no package ``__init__`` dependency, so the hook
can import it by path. A caller that cannot import it must fail closed (treat
no name as a template), never substitute its own copy of the list.
"""

from __future__ import annotations

PLACEHOLDER_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist")


def is_placeholder_template(name_or_path: str) -> bool:
    """True when the file name (or path) ends in a placeholder-template suffix,
    compared without regard to case."""
    return bool(name_or_path) and name_or_path.lower().endswith(PLACEHOLDER_TEMPLATE_SUFFIXES)


def _case_insensitive(text: str) -> str:
    out = []
    for char in text:
        if char.isalpha():
            out.append(f"[{char.lower()}{char.upper()}]")
        elif char == ".":
            out.append("\\.")
        else:
            out.append(char)
    return "".join(out)


def placeholder_template_sandbox_regexes() -> tuple[str, ...]:
    """The same rule as Seatbelt regexes (matched against the full absolute
    path; Seatbelt has no case-insensitive flag, so each letter is a class)."""
    return tuple(
        f"{_case_insensitive(suffix)}$" for suffix in PLACEHOLDER_TEMPLATE_SUFFIXES
    )
