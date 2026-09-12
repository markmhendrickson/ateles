#!/usr/bin/env python3
"""
Compare NEOTOMA_SSE_SUBSCRIPTION_ID* declarations between repo-tracked launchd
plists and installed LaunchAgents copies.

Read-only: never writes plists or Neotoma entities. Exit 0 when every compared
pair matches; non-zero on value mismatch, repo-only key, installed-only key, or
invalid POSIX env-var names (hyphens in the suffix).
"""

from __future__ import annotations

import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path

SUBSCRIPTION_PREFIX = "NEOTOMA_SSE_SUBSCRIPTION_ID"
LABEL_PREFIX = "com.ateles."


@dataclass(frozen=True)
class ParityFailure:
    daemon: str
    kind: str
    detail: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_repo_daemons_dir() -> Path:
    return repo_root() / "execution" / "daemons"


def default_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def posix_safe_env_suffix(label_suffix: str) -> str:
    return label_suffix.upper().replace("-", "_")


def expected_subscription_env_key(label: str) -> str:
    if not label.startswith(LABEL_PREFIX):
        raise ValueError(f"unexpected launchd label: {label!r}")
    suffix = label[len(LABEL_PREFIX) :]
    return f"{SUBSCRIPTION_PREFIX}_{posix_safe_env_suffix(suffix)}"


def _load_plist(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())


def _env_vars(plist: dict) -> dict[str, str]:
    raw = plist.get("EnvironmentVariables") or {}
    return {str(k): str(v) for k, v in raw.items()}


def subscription_entries(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key == SUBSCRIPTION_PREFIX or key.startswith(f"{SUBSCRIPTION_PREFIX}_")
    }


def invalid_hyphen_subscription_keys(keys: list[str]) -> list[str]:
    return [key for key in keys if key.startswith(SUBSCRIPTION_PREFIX) and "-" in key]


def iter_repo_plists(repo_daemons_dir: Path) -> list[Path]:
    return sorted(repo_daemons_dir.glob("**/com.ateles.*.plist"))


def compare_daemon(
    *,
    label: str,
    repo_env: dict[str, str],
    installed_env: dict[str, str] | None,
) -> list[ParityFailure]:
    failures: list[ParityFailure] = []
    repo_subs = subscription_entries(repo_env)
    installed_subs = subscription_entries(installed_env) if installed_env is not None else {}

    for surface, keys in (("repo", sorted(repo_subs)), ("installed", sorted(installed_subs))):
        for bad_key in invalid_hyphen_subscription_keys(keys):
            failures.append(
                ParityFailure(
                    daemon=label,
                    kind="invalid_posix_env_key",
                    detail=(
                        f"{surface} plist uses invalid env key {bad_key!r}; "
                        f"expected POSIX-safe {expected_subscription_env_key(label)!r}"
                    ),
                )
            )

    if installed_env is None:
        if repo_subs:
            failures.append(
                ParityFailure(
                    daemon=label,
                    kind="installed_plist_missing",
                    detail=f"repo declares {repo_subs!r} but no installed plist at LaunchAgents/{label}.plist",
                )
            )
        return failures

    all_keys = sorted(set(repo_subs) | set(installed_subs))
    for key in all_keys:
        repo_val = repo_subs.get(key)
        installed_val = installed_subs.get(key)
        if repo_val is None:
            failures.append(
                ParityFailure(
                    daemon=label,
                    kind="installed_only_subscription_key",
                    detail=(
                        f"installed-only key {key!r}: installed={installed_val!r}, repo=(missing)"
                    ),
                )
            )
        elif installed_val is None:
            failures.append(
                ParityFailure(
                    daemon=label,
                    kind="repo_only_subscription_key",
                    detail=f"repo-only key {key!r}: repo={repo_val!r}, installed=(missing)",
                )
            )
        elif repo_val != installed_val:
            failures.append(
                ParityFailure(
                    daemon=label,
                    kind="subscription_id_mismatch",
                    detail=(
                        f"key {key!r}: repo={repo_val!r}, installed={installed_val!r}"
                    ),
                )
            )
    return failures


def check_parity(
    *,
    repo_daemons_dir: Path,
    launch_agents_dir: Path,
) -> tuple[list[str], list[ParityFailure]]:
    successes: list[str] = []
    failures: list[ParityFailure] = []

    for repo_path in iter_repo_plists(repo_daemons_dir):
        plist = _load_plist(repo_path)
        label = str(plist.get("Label") or "")
        if not label:
            failures.append(
                ParityFailure(
                    daemon=repo_path.name,
                    kind="missing_label",
                    detail=f"repo plist {repo_path} has no Label key",
                )
            )
            continue

        repo_env = _env_vars(plist)
        installed_path = launch_agents_dir / f"{label}.plist"
        installed_env = _env_vars(_load_plist(installed_path)) if installed_path.is_file() else None

        daemon_failures = compare_daemon(
            label=label,
            repo_env=repo_env,
            installed_env=installed_env,
        )
        if daemon_failures:
            failures.extend(daemon_failures)
        elif installed_env is not None:
            key = expected_subscription_env_key(label)
            value = subscription_entries(repo_env).get(key) or subscription_entries(installed_env).get(
                key, "(none)"
            )
            successes.append(f"{label}: {key}={value}")

    return successes, failures


def main(argv: list[str] | None = None) -> int:
    repo_daemons_dir = default_repo_daemons_dir()
    launch_agents_dir = default_launch_agents_dir()

    if argv and len(argv) >= 3:
        repo_daemons_dir = Path(argv[1])
        launch_agents_dir = Path(argv[2])

    successes, failures = check_parity(
        repo_daemons_dir=repo_daemons_dir,
        launch_agents_dir=launch_agents_dir,
    )

    for line in successes:
        print(f"OK {line}")

    for failure in failures:
        print(f"FAIL [{failure.kind}] {failure.daemon}: {failure.detail}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
