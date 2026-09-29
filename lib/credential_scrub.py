"""Names of generation vendor credentials, defined once.

Two consumers must agree on this list, so it lives in one import-light module
outside the ``lib.capabilities`` package (principle 9: one source, defined
once). It is deliberately NOT inside the package: importing a package member
runs the package ``__init__``, which loads the whole client, and the Apis
dispatch process (``skill_runner``) must not depend on the client to scrub an
environment. Nothing here imports anything.

* vendor adapters, which read ONLY the names they are allowed to read;
* ``skill_runner._subscription_only_env``, which strips these names, and any
  name starting with a listed prefix, from every dispatched agent's child
  environment.

Adding a generation vendor means adding its key name here. The prefix list
exists so a sibling key the operator adds tomorrow (for example
``GEMINI_API_KEY_2``) is still stripped before anyone remembers to name it.
"""

from __future__ import annotations

GOOGLE_KEY_NAMES: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
RECRAFT_KEY_NAMES: tuple[str, ...] = ("RECRAFT_API_KEY",)

# Every generation credential name the swarm knows about.
GENERATION_CREDENTIAL_NAMES: tuple[str, ...] = (
    *GOOGLE_KEY_NAMES,
    *RECRAFT_KEY_NAMES,
    "GOOGLE_AI_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "VEO_API_KEY",
)

# Any name with one of these prefixes is treated as a generation credential.
GENERATION_CREDENTIAL_PREFIXES: tuple[str, ...] = (
    "GEMINI_",
    "GOOGLE_GENERATIVE_AI_",
    "GOOGLE_GENAI_",
    "RECRAFT_",
    "VEO_",
)


def is_generation_credential(name: str) -> bool:
    """True when ``name`` is a generation credential that must not reach an agent."""
    upper = (name or "").upper()
    return upper in GENERATION_CREDENTIAL_NAMES or upper.startswith(
        GENERATION_CREDENTIAL_PREFIXES
    )

# skill_runner sets this in every dispatched agent's child environment. The
# capability client refuses to run when it is present: generation is a
# host-side operation and never runs inside a scrubbed agent child.
AGENT_CHILD_MARKER_ENV = "ATELES_DISPATCHED_AGENT"
