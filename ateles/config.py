"""Typed configuration surface for Ateles (W1).

Replaces the ad-hoc ~40 environment variables (an `.env.example` with no
validation) with a *declared* schema: each setting names its env var, whether
it is required, and whether it is a secret. Config resolves from
``ateles.config.json`` (non-secret settings) overlaid by environment variables
— environment always wins (12-factor), and secrets are read from the
environment only, never persisted to the JSON file.

Operator-specific identifiers are read here, never defaulted in code: that is
the portability invariant the installability epic rests on (see W5). A fork
supplies its own values via ``ateles init`` / the environment; nothing operator-
specific is baked into the package.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = "ateles.config.json"


class ConfigError(Exception):
    """Raised when configuration cannot be read or parsed."""


@dataclass(frozen=True)
class Setting:
    key: str  # config/JSON key (snake_case)
    env: str  # environment variable name
    required: bool = False
    secret: bool = False
    description: str = ""


# The declared configuration surface. Operator-specific values intentionally
# have NO baked default — they must be supplied via ateles.config.json or the
# environment. (The legacy operator defaults still living in daemon code are
# removed in W5.)
SETTINGS: tuple[Setting, ...] = (
    Setting("operator_domain", "ATELES_OPERATOR_DOMAIN", required=True,
            description="Operator domain; AAuth issuer + JWKS host (e.g. example.com)."),
    Setting("operator_name", "OPERATOR_NAME", required=True,
            description="Operator full name (used in briefings/prompts)."),
    Setting("operator_email", "OPERATOR_EMAIL", required=True,
            description="Operator primary email / Google Calendar primary id."),
    Setting("neotoma_base_url", "NEOTOMA_BASE_URL", required=True,
            description="Neotoma API base URL for this operator's instance."),
    Setting("neotoma_bearer_token", "NEOTOMA_BEARER_TOKEN", required=True, secret=True,
            description="Bearer token for the Neotoma API (all daemons need it)."),
    Setting("anthropic_api_key", "ANTHROPIC_API_KEY", secret=True,
            description="Claude API key for agent dispatch + GHA reviewers."),
    Setting("secrets_dir", "ATELES_SECRETS_DIR",
            description="Directory the secret backend materializes from (W4)."),
    Setting("data_dir", "DATA_DIR",
            description="Root directory for parquet data files."),
    Setting("ateles_private_keys_dir", "ATELES_PRIVATE_KEYS_DIR",
            description="Directory holding AAuth keypairs (W3)."),
    Setting("telegram_bot_token", "TELEGRAM_BOT_TOKEN", secret=True,
            description="Telegram bot token for operator notifications."),
    Setting("telegram_chat_id", "TELEGRAM_CHAT_ID",
            description="Telegram chat id the bot posts to."),
)

SECRET_KEYS = frozenset(s.key for s in SETTINGS if s.secret)


@dataclass
class AtelesConfig:
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.values.get(key) or None

    def validate(self) -> list[str]:
        """Return human-readable problems; an empty list means valid."""
        problems: list[str] = []
        for s in SETTINGS:
            if s.required and not self.values.get(s.key):
                problems.append(
                    f"missing required setting '{s.key}' "
                    f"(set {s.env}, or add it to {CONFIG_FILENAME})"
                )
        url = self.values.get("neotoma_base_url")
        if url and not url.startswith(("http://", "https://")):
            problems.append(f"neotoma_base_url must be an http(s) URL, got {url!r}")
        return problems

    def redacted(self) -> dict[str, str]:
        """Config mapping safe to print — secret values masked."""
        return {
            k: ("***" if k in SECRET_KEYS and v else v)
            for k, v in self.values.items()
        }


def config_path(start: Path | None = None) -> Path:
    """Resolve the ateles.config.json path (``ATELES_CONFIG`` override, else CWD)."""
    override = os.environ.get("ATELES_CONFIG")
    if override:
        return Path(override)
    return (start or Path.cwd()) / CONFIG_FILENAME


def load(start: Path | None = None, environ: dict | None = None) -> AtelesConfig:
    """Load config: ``ateles.config.json`` as the base, environment overrides.

    Only declared settings are read. The environment always wins, and secret
    settings are read from the environment only — never from the JSON file.
    """
    environ = os.environ if environ is None else environ
    values: dict[str, str] = {}

    path = config_path(start)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ConfigError(f"could not read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        for s in SETTINGS:
            if s.secret:
                continue  # secrets are read from the environment only
            val = data.get(s.key)
            if val not in (None, ""):
                values[s.key] = str(val)

    for s in SETTINGS:
        env_val = environ.get(s.env)
        if env_val:
            values[s.key] = env_val

    return AtelesConfig(values=values)


def to_json_dict(cfg: AtelesConfig) -> dict[str, str]:
    """Non-secret settings suitable for persisting to ateles.config.json."""
    return {k: v for k, v in cfg.values.items() if k not in SECRET_KEYS and v}
