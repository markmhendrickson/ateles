"""Tests for lib/github_app_token.py — the one App-token minting helper.

No network: `_request` is replaced by a recorder. The RSA key is generated at
test time (never a PEM literal in source — gitleaks' private-key rule matches
the header text itself, see execution/daemons/apis/test_reviewer_app_private_key.py).

Run: pytest lib/test_github_app_token.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from lib import github_app_token as gat

PREFIX = gat.DEFAULT_APP_ENV_PREFIX
_ENV = (
    f"{PREFIX}_ID",
    f"{PREFIX}_PRIVATE_KEY",
    f"{PREFIX}_PRIVATE_KEY_PATH",
    f"{PREFIX}_INSTALLATION_ID",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ATELES_AGENT_PAT",
)


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    gat.clear_cache()
    yield
    gat.clear_cache()


class FakeGitHub:
    """Records calls; mints a new distinct token per POST."""

    def __init__(self, lifetime=timedelta(hours=1)):
        self.calls: list[tuple[str, str]] = []
        self.lifetime = lifetime
        self.minted = 0

    def __call__(self, method, path, bearer):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/repos/") and path.endswith("/installation"):
            return {"id": 4242}
        if method == "POST" and path.endswith("/access_tokens"):
            self.minted += 1
            exp = datetime.now(timezone.utc) + self.lifetime
            return {
                "token": f"ghs_fake_{self.minted}",
                "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": {"contents": "write"},
            }
        if method == "GET" and path == "/app":
            return {"slug": "ateles-agents", "id": 1}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def configured(monkeypatch, keypair):
    pem, _ = keypair
    monkeypatch.setenv(f"{PREFIX}_ID", "123")
    monkeypatch.setenv(f"{PREFIX}_PRIVATE_KEY", pem.replace("\n", "\\n"))
    fake = FakeGitHub()
    monkeypatch.setattr(gat, "_request", fake)
    return fake


def test_unconfigured_raises_without_calling_github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(gat, "_request", fake)
    with pytest.raises(gat.AppTokenError):
        gat.installation_token("o/r")
    assert fake.calls == []


def test_jwt_is_rs256_signed_by_the_app_key(configured, keypair):
    _, public = keypair
    claims = pyjwt.decode(gat.mint_app_jwt(), public, algorithms=["RS256"])
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's ceiling


def test_token_is_cached_until_refresh_margin(configured):
    first = gat.installation_token("o/r")
    second = gat.installation_token("o/r")
    assert first == second
    assert configured.minted == 1
    # installation looked up once, then remembered
    assert [c for c in configured.calls if c[0] == "GET"] == [("GET", "/repos/o/r/installation")]


def test_token_inside_refresh_margin_is_reminted(monkeypatch, keypair):
    pem, _ = keypair
    monkeypatch.setenv(f"{PREFIX}_ID", "123")
    monkeypatch.setenv(f"{PREFIX}_PRIVATE_KEY", pem)
    fake = FakeGitHub(lifetime=gat.REFRESH_MARGIN - timedelta(minutes=1))
    monkeypatch.setattr(gat, "_request", fake)
    a = gat.installation_token("o/r")
    b = gat.installation_token("o/r")
    assert a != b
    assert fake.minted == 2


def test_configured_installation_id_skips_lookup(configured, monkeypatch):
    monkeypatch.setenv(f"{PREFIX}_INSTALLATION_ID", "999")
    gat.installation_token()
    assert configured.calls == [("POST", "/app/installations/999/access_tokens")]


def test_no_installation_id_and_no_repo_is_an_error(configured):
    with pytest.raises(gat.AppTokenError, match="INSTALLATION_ID"):
        gat.installation_token()


def test_token_env_overrides_both_gh_variables(configured, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_pat")
    env = gat.token_env("o/r", extra_names=("NEOTOMA_ISSUES_GITHUB_TOKEN",))
    assert set(env) == {"GH_TOKEN", "GITHUB_TOKEN", "NEOTOMA_ISSUES_GITHUB_TOKEN"}
    assert len(set(env.values())) == 1
    assert env["GITHUB_TOKEN"] != "ghp_ambient_pat"


def test_repr_never_renders_the_token(configured):
    info = gat.installation_token_info("o/r")
    assert info.token not in repr(info)
    assert "<redacted>" in repr(info)


def test_read_token_prefers_app_over_pat(configured, monkeypatch):
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_pat")
    assert gat.read_token("o/r", fallback_env=("ATELES_AGENT_PAT",)).startswith("ghs_fake_")


def test_read_token_falls_back_to_env_when_app_unconfigured(monkeypatch):
    monkeypatch.setattr(gat, "_request", FakeGitHub())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_pat")
    assert gat.read_token("o/r", fallback_env=("GITHUB_TOKEN", "ATELES_AGENT_PAT")) == "ghp_pat"


def test_read_token_falls_back_when_mint_fails(configured, monkeypatch):
    def refuse(method, path, bearer):
        raise gat.AppTokenError("HTTP 401")

    monkeypatch.setattr(gat, "_request", refuse)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    assert gat.read_token("o/r", fallback_env=("GITHUB_TOKEN",)) == "ghp_env"


def test_unreadable_key_path_fails_closed_and_logs(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(f"{PREFIX}_ID", "123")
    monkeypatch.setenv(f"{PREFIX}_PRIVATE_KEY_PATH", str(tmp_path / "absent.pem"))
    with caplog.at_level("ERROR"):
        assert gat.app_private_key_pem() == ""
    assert f"{PREFIX}_PRIVATE_KEY_PATH" in caplog.text
    assert gat.app_configured() is False


def test_bot_login_is_slug_bot(configured):
    assert gat.bot_login() == "ateles-agents[bot]"


def test_cli_exec_puts_token_only_in_child_env(configured, monkeypatch, capsys):
    seen = {}

    def fake_exec(file, args, env):
        seen.update(file=file, args=args, env=env)

    monkeypatch.setattr(gat.os, "execvpe", fake_exec)
    gat.main(["exec", "--repo", "o/r", "--env", "EXTRA", "--", "gh", "api", "x"])
    assert seen["args"] == ["gh", "api", "x"]
    tok = seen["env"]["GH_TOKEN"]
    assert tok.startswith("ghs_fake_")
    assert seen["env"]["GITHUB_TOKEN"] == tok == seen["env"]["EXTRA"]
    out = capsys.readouterr()
    assert tok not in out.out and tok not in out.err


def test_cli_exec_unconfigured_exits_2_without_running(monkeypatch, capsys):
    monkeypatch.setattr(gat, "_request", FakeGitHub())
    ran = []
    monkeypatch.setattr(gat.os, "execvpe", lambda *a: ran.append(a))
    assert gat.main(["exec", "--repo", "o/r", "--", "true"]) == 2
    assert ran == []
    assert "not configured" in capsys.readouterr().err


def test_gh_read_env_injects_app_token(configured, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_pat")
    env = gat.gh_read_env("o/r")
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"]
    assert env["GITHUB_TOKEN"].startswith("ghs_fake_")
    assert "PATH" in env or not gat.os.environ.get("PATH")


def test_gh_read_env_unconfigured_is_ambient_env(monkeypatch):
    monkeypatch.setattr(gat, "_request", FakeGitHub())
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_pat")
    env = gat.gh_read_env("o/r")
    assert env["GITHUB_TOKEN"] == "ghp_ambient_pat"
    assert "GH_TOKEN" not in env


def test_unparseable_key_is_an_app_token_error_and_read_falls_back(monkeypatch):
    monkeypatch.setenv(f"{PREFIX}_ID", "123")
    monkeypatch.setenv(f"{PREFIX}_PRIVATE_KEY", "not-a-key")
    monkeypatch.setattr(gat, "_request", FakeGitHub())
    with pytest.raises(gat.AppTokenError, match="could not sign"):
        gat.mint_app_jwt()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    assert gat.read_token("o/r", fallback_env=("GITHUB_TOKEN",)) == "ghp_env"


# ── per-identity Apps (operator ruling 2026-10-14) ───────────────────────────


def _configure_identity(monkeypatch, keypair, identity: str, app_id: str = "777") -> str:
    pem, _ = keypair
    prefix = gat.identity_env_prefix(identity)
    monkeypatch.setenv(f"{prefix}_ID", app_id)
    monkeypatch.setenv(f"{prefix}_PRIVATE_KEY", pem.replace("\n", "\\n"))
    return prefix


class FakeGitHubByApp(FakeGitHub):
    """Answers `GET /app` with a slug derived from the App JWT's issuer."""

    SLUGS = {"777": "ateles-pr-opener", "888": "ateles-reviewer-pm"}

    def __call__(self, method, path, bearer):
        if method == "GET" and path == "/app":
            iss = pyjwt.decode(bearer, options={"verify_signature": False})["iss"]
            return {"slug": self.SLUGS[iss], "id": int(iss)}
        return super().__call__(method, path, bearer)


@pytest.mark.parametrize(
    "identity,prefix",
    [
        ("pr_opener", "ATELES_APP_PR_OPENER"),
        ("reviewer_pm", "ATELES_APP_REVIEWER_PM"),
        ("reviewer_security", "ATELES_APP_REVIEWER_SECURITY"),
        ("Reviewer-QA", "ATELES_APP_REVIEWER_QA"),
    ],
)
def test_identity_name_maps_to_one_env_prefix(identity, prefix):
    assert gat.identity_env_prefix(identity) == prefix


def test_reviewer_prefix_matches_the_identity_rule():
    assert gat.reviewer_prefix("arch") == gat.identity_env_prefix(gat.reviewer_identity("arch"))


def test_an_empty_identity_name_is_refused():
    with pytest.raises(gat.AppTokenError):
        gat.identity_env_prefix("  ")


def test_identity_is_absent_declared_or_configured(monkeypatch, keypair):
    assert not gat.identity_declared("pr_opener") and not gat.identity_configured("pr_opener")
    monkeypatch.setenv("ATELES_APP_PR_OPENER_ID", "777")
    # An id with no key is declared but NOT usable: the readers treat that as
    # "an App exists but cannot speak", never as "no App".
    assert gat.identity_declared("pr_opener") and not gat.identity_configured("pr_opener")
    _configure_identity(monkeypatch, keypair, "pr_opener")
    assert gat.identity_configured("pr_opener")


def test_declared_reviewer_lenses_are_discovered_from_the_environment(monkeypatch):
    assert gat.declared_reviewer_lenses() == ()
    monkeypatch.setenv("ATELES_APP_REVIEWER_PM_ID", "1")
    monkeypatch.setenv("ATELES_APP_REVIEWER_ARCH_PRIVATE_KEY_PATH", "/nowhere.pem")
    # A lens named only by its installation id is still that lens, not a lens
    # called "qa_installation".
    monkeypatch.setenv("ATELES_APP_REVIEWER_QA_INSTALLATION_ID", "9")
    monkeypatch.setenv("ATELES_APP_REVIEWER_UX_ID", "")  # blank does not declare
    monkeypatch.setenv("ATELES_APP_PR_OPENER_ID", "5")  # the opener is not a reviewer
    assert gat.declared_reviewer_lenses() == ("arch", "pm", "qa")


def test_each_identity_mints_from_its_own_app_not_the_default(configured, monkeypatch, keypair):
    _configure_identity(monkeypatch, keypair, "pr_opener")
    monkeypatch.setenv("ATELES_APP_PR_OPENER_INSTALLATION_ID", "555")
    tok = gat.identity_token("pr_opener")
    assert tok.startswith("ghs_fake_")
    # The default App's installation (looked up per repo) was never touched.
    assert configured.calls == [("POST", "/app/installations/555/access_tokens")]


def test_identity_bot_login_is_that_apps_slug(monkeypatch, keypair):
    _configure_identity(monkeypatch, keypair, "pr_opener", "777")
    _configure_identity(monkeypatch, keypair, "reviewer_pm", "888")
    monkeypatch.setattr(gat, "_request", FakeGitHubByApp())
    assert gat.identity_bot_login("pr_opener") == "ateles-pr-opener[bot]"
    assert gat.identity_bot_login("reviewer_pm") == "ateles-reviewer-pm[bot]"


def test_an_unconfigured_identity_never_falls_back_to_the_default_app(configured):
    # The default App IS configured here; the PR opener is not, and must not
    # silently borrow it (it would then be the approving identity).
    with pytest.raises(gat.AppTokenError, match="ATELES_APP_PR_OPENER_ID"):
        gat.identity_token("pr_opener", "o/r")


def test_cli_exec_identity_uses_that_apps_token(monkeypatch, keypair, capsys):
    _configure_identity(monkeypatch, keypair, "pr_opener")
    monkeypatch.setenv("ATELES_APP_PR_OPENER_INSTALLATION_ID", "555")
    monkeypatch.setattr(gat, "_request", FakeGitHub())
    seen = {}
    monkeypatch.setattr(gat.os, "execvpe", lambda f, a, e: seen.update(args=a, env=e))
    gat.main(["exec", "--identity", "pr_opener", "--", "gh", "pr", "create"])
    assert seen["args"] == ["gh", "pr", "create"]
    assert seen["env"]["GH_TOKEN"] == seen["env"]["GITHUB_TOKEN"]
    assert seen["env"]["GH_TOKEN"].startswith("ghs_fake_")
    assert seen["env"]["GH_TOKEN"] not in capsys.readouterr().out


def test_cli_identity_and_prefix_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        gat.main(["check", "--identity", "pr_opener", "--prefix", "X"])
    assert "mutually exclusive" in capsys.readouterr().err


def test_cli_status_reports_each_identity_without_network(monkeypatch, keypair, capsys):
    monkeypatch.setattr(gat, "_request", lambda *a: pytest.fail("status must be offline"))
    _configure_identity(monkeypatch, keypair, "reviewer_pm")
    monkeypatch.setenv("ATELES_APP_REVIEWER_ARCH_ID", "1")  # id, no key
    assert gat.main(["status"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert rows["reviewer_pm"].endswith("configured")
    assert "DECLARED BUT UNUSABLE" in rows["reviewer_arch"]
    assert rows["pr_opener"].endswith("not configured")
    assert "reviewer_content" in rows
