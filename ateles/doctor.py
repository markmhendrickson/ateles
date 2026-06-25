"""``ateles doctor`` — preflight diagnostics (W1).

Inspects the environment and reports the *next missing rung* toward a runnable
swarm: Python version, declared-config completeness, presence of the external
CLIs the swarm shells out to, and (best-effort) Neotoma reachability. Read-only
— it never mutates anything.
"""

from __future__ import annotations

import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import SETTINGS, AtelesConfig, load

MIN_PYTHON = (3, 13)

# External CLIs the swarm shells out to (the Brewfile mirrors these).
EXTERNAL_CLIS: tuple[tuple[str, str], ...] = (
    ("gh", "GitHub CLI"),
    ("op", "1Password CLI (secret materialization)"),
    ("node", "Node.js (claude CLI + github_harness MCP)"),
    ("claude", "Claude CLI (agent dispatch)"),
    ("gws", "Google Workspace bridge (Calendar/Gmail)"),
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    @property
    def symbol(self) -> str:
        return "✓" if self.ok else "✗"  # ✓ / ✗


def check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if not ok:
        detail += f" (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"
    return Check("python", ok, detail)


def check_config(cfg: AtelesConfig) -> list[Check]:
    problems = cfg.validate()
    if not problems:
        resolved = sum(1 for s in SETTINGS if cfg.values.get(s.key))
        return [Check("config", True, f"{resolved} settings resolved, no problems")]
    return [Check("config", False, p) for p in problems]


def check_external_clis() -> list[Check]:
    checks: list[Check] = []
    for cmd, desc in EXTERNAL_CLIS:
        path = shutil.which(cmd)
        checks.append(Check(f"cli:{cmd}", path is not None, path or f"not found — {desc}"))
    return checks


def check_neotoma(
    cfg: AtelesConfig,
    *,
    timeout: float = 5.0,
    opener=urllib.request.urlopen,
) -> Check:
    """Probe Neotoma reachability. Any HTTP response (even 4xx) proves the host
    is up; only connection-level failures count as unreachable."""
    url = cfg.get("neotoma_base_url")
    if not url:
        return Check("neotoma", False, "neotoma_base_url not set")
    try:
        with opener(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return Check("neotoma", True, f"{url} reachable (HTTP {status})")
    except urllib.error.HTTPError as exc:
        return Check("neotoma", True, f"{url} reachable (HTTP {exc.code})")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Check("neotoma", False, f"{url} unreachable: {reason}")


def run_checks(cfg: AtelesConfig | None = None, *, check_network: bool = True) -> list[Check]:
    cfg = load() if cfg is None else cfg
    checks = [check_python()]
    checks += check_config(cfg)
    checks += check_external_clis()
    if check_network:
        checks.append(check_neotoma(cfg))
    return checks


def next_rung(checks: list[Check]) -> str | None:
    """Name of the first failing check — the next thing to fix — or None."""
    for c in checks:
        if not c.ok:
            return c.name
    return None


def render(checks: list[Check]) -> str:
    lines = [f"  {c.symbol} {c.name}: {c.detail}" for c in checks]
    rung = next_rung(checks)
    if rung is None:
        lines.append("\nAll checks passed — ready to `ateles provision`.")
    else:
        lines.append(f"\nNext rung: fix '{rung}', then re-run `ateles doctor`.")
    return "\n".join(lines)
