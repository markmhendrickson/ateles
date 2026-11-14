"""Judge the post-deploy security probe report per host class.

`neotoma/scripts/security/deployed_probes.sh` expects every protected route to
answer 401 with no bearer. The public sandbox answers anonymous requests by
design (it is a try-it host), so against it the script reports roughly 3 pass /
85 fail on every release (v0.27.0, v0.28.0, v0.29.0): a number that cannot move,
so it cannot warn. The script lives in the neotoma repo; this module judges its
report on the ateles side:

- a CLOSED host (anything that is not the sandbox) is judged strictly, by the
  script's own per-row verdict, so a real auth regression still fails;
- the OPEN-BY-DESIGN sandbox is judged against its own recorded expectations:
  anonymous access is allowed (the no-auth status must only show the route is
  served: not 000, not 5xx), while a bad bearer must still be refused: the
  invalid-auth status must match the manifest's expectation, or the route's
  recorded status in SANDBOX_INVALID_AUTH_RECORDED. Any other status is drift.

SANDBOX_INVALID_AUTH_RECORDED was seeded from the v0.15.0 report (routes whose
bad-bearer answer legitimately differs from 401: public OAuth/discovery routes).
Routes added since are judged by the manifest expectation until recorded; refresh
it from a current report with `recorded_from_report`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "METHOD /route" -> statuses accepted for the INVALID-bearer probe on the sandbox.
SANDBOX_INVALID_AUTH_RECORDED: dict[str, list[int]] = {
    "GET /.well-known/oauth-authorization-server": [200],
    "GET /.well-known/oauth-protected-resource": [200],
    "POST /auth/dev-signin": [403],
    "GET /mcp/oauth/authorization-details": [400],
    "GET /mcp/oauth/authorize": [400],
    "GET /mcp/oauth/callback": [400],
    "POST /mcp/oauth/initiate": [400],
    "GET /mcp/oauth/key-auth": [200],
    "GET /mcp/oauth/local-login": [400],
    "POST /mcp/oauth/register": [400],
    "GET /mcp/oauth/status": [400],
    "POST /mcp/oauth/token": [400],
    "GET /server-info": [200],
}

_ROW = re.compile(
    r"^\|\s*(?P<host>https?://\S+)\s*\|\s*(?P<method>[A-Z]+)\s*\|\s*(?P<route>\S+)\s*\|"
    r"\s*(?P<no_auth>\d+)\s*\|\s*(?P<exp_no>\[[^\]]*\])\s*\|"
    r"\s*(?P<invalid>\d+)\s*\|\s*(?P<exp_inv>\[[^\]]*\])\s*\|\s*(?P<verdict>\w+)\s*\|\s*$"
)


@dataclass
class ProbeRow:
    host: str
    method: str
    route: str
    no_auth: int
    expected_no_auth: list[int]
    invalid_auth: int
    expected_invalid_auth: list[int]
    verdict: str


@dataclass
class ProbeJudgement:
    total: int = 0
    ok: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.total > 0 and not self.failures

    def summary(self) -> str:
        if self.total == 0:
            return "probes: no rows parsed from report (see log)"
        if self.passed:
            return f"probes: {self.ok}/{self.total} as expected"
        shown = "; ".join(self.failures[:3])
        more = f" (+{len(self.failures) - 3} more)" if len(self.failures) > 3 else ""
        return f"probes: FAIL {len(self.failures)}/{self.total} unexpected: {shown}{more}"


def _codes(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text)]


def parse_report(text: str) -> list[ProbeRow]:
    rows = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if m:
            rows.append(ProbeRow(
                m["host"], m["method"], m["route"], int(m["no_auth"]), _codes(m["exp_no"]),
                int(m["invalid"]), _codes(m["exp_inv"]), m["verdict"].lower(),
            ))
    return rows


def _norm(url: str) -> str:
    return url.rstrip("/").lower()


def judge(rows: list[ProbeRow], open_hosts: list[str]) -> ProbeJudgement:
    open_set = {_norm(h) for h in open_hosts if h}
    out = ProbeJudgement(total=len(rows))
    for r in rows:
        key = f"{r.method} {r.route}"
        if _norm(r.host) not in open_set:
            if r.verdict == "pass":
                out.ok += 1
            else:
                out.failures.append(
                    f"{r.host} {key} no-auth {r.no_auth} (want {r.expected_no_auth}), "
                    f"bad-bearer {r.invalid_auth} (want {r.expected_invalid_auth})"
                )
            continue
        served = r.no_auth != 0 and r.no_auth < 500
        allowed = SANDBOX_INVALID_AUTH_RECORDED.get(key, r.expected_invalid_auth)
        if served and r.invalid_auth in allowed:
            out.ok += 1
        elif not served:
            out.failures.append(f"sandbox {key} not served (no-auth {r.no_auth})")
        else:
            out.failures.append(
                f"sandbox {key} bad-bearer {r.invalid_auth} (recorded {allowed})"
            )
    return out


def recorded_from_report(text: str, open_hosts: list[str]) -> dict[str, list[int]]:
    """Routes on open hosts whose bad-bearer status differs from the manifest's:
    the dict to paste into SANDBOX_INVALID_AUTH_RECORDED after reviewing it."""
    open_set = {_norm(h) for h in open_hosts}
    return {
        f"{r.method} {r.route}": [r.invalid_auth]
        for r in parse_report(text)
        if _norm(r.host) in open_set and r.invalid_auth not in r.expected_invalid_auth
    }
