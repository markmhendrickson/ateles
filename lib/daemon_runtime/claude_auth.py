"""
lib/daemon_runtime/claude_auth.py — shared Anthropic-auth env for spawned
`claude --print` agents.

Every daemon that spawns a `claude` subprocess must prefer the operator's
Claude subscription (Max plan) over metered pay-as-you-go API credits.
`claude setup-token` mints a long-lived subscription token exposed as
CLAUDE_CODE_OAUTH_TOKEN. When it is present we hand it to the child and REMOVE
ANTHROPIC_API_KEY from the child's env — if both are set the API key wins and
the child bills metered credits (the failure that exhausted the panel's
balance and produced "credit balance is too low" / 401 errors on dispatched
agents).

When the OAuth token is absent this is a no-op: ANTHROPIC_API_KEY (if any) is
left in place so the child still authenticates, just on metered credits. This
keeps the change safe on hosts that haven't provisioned the subscription token.
"""

from __future__ import annotations

import os


def claude_subprocess_env(extra: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Return an environment dict for a spawned `claude --print` subprocess.

    Starts from os.environ, applies ``extra`` overrides, then enforces the
    OAuth-over-API-key preference: when CLAUDE_CODE_OAUTH_TOKEN is set,
    ANTHROPIC_API_KEY is removed so the subscription token is used.
    """
    env = {**os.environ, **(extra or {})}
    if env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        env.pop("ANTHROPIC_API_KEY", None)
    return env
