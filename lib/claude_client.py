"""
lib/claude_client.py — Shared stdlib-only Claude API client for Ateles agents.

Used by Turdus (triage), Buteo (legal review), Pavo (commercial framing),
and the email-routing driver script. Mirrors the lightweight pattern from
execution/scripts/loxia_review.py so we don't pull in the anthropic SDK.

Degrades to a structured stub response when ANTHROPIC_API_KEY is absent,
which keeps dry-runs reproducible in environments without a live key.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"


@dataclass
class ClaudeResponse:
    text: str
    model: str
    stop_reason: str = ""
    stub: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def parse_json(self) -> dict[str, Any] | None:
        """Best-effort JSON parse of `text`. Strips ```json fences if present."""
        body = self.text.strip()
        if body.startswith("```"):
            body = body.strip("`")
            if body.lower().startswith("json"):
                body = body[4:]
            body = body.strip()
            if body.endswith("```"):
                body = body[:-3].strip()
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            log.debug("claude_client: JSON parse failed: %s", exc)
            return None


def call_claude(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> ClaudeResponse:
    """
    Call the Anthropic Messages API with a single user turn and a system prompt.

    Returns a ClaudeResponse. When ANTHROPIC_API_KEY is missing, returns a
    `stub=True` response carrying the prompt that would have been sent — the
    caller can render that in dry-run artifacts.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        stub_text = (
            "[STUB — ANTHROPIC_API_KEY not set in this environment]\n"
            f"Model: {model}\n"
            f"System prompt ({len(system)} chars):\n{system}\n\n"
            f"User prompt ({len(user)} chars):\n{user}"
        )
        return ClaudeResponse(text=stub_text, model=model, stop_reason="stub", stub=True)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if hasattr(exc, "read") else ""
        log.error("claude_client: HTTP %s — %s", exc.code, body[:500])
        return ClaudeResponse(
            text=f"(Claude API HTTP {exc.code}: {body[:300]})",
            model=model,
            stop_reason="error",
        )
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        log.error("claude_client: transport error: %s", exc)
        return ClaudeResponse(
            text=f"(Claude API error: {exc})", model=model, stop_reason="error"
        )

    text_blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in text_blocks if b.get("type") == "text")
    return ClaudeResponse(
        text=text,
        model=data.get("model", model),
        stop_reason=data.get("stop_reason", ""),
        raw=data,
    )
