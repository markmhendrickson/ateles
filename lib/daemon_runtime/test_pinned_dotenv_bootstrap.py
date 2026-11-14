"""A pinned child must not get the operator dotenv's bearer back at import time.

`import lib.daemon_runtime` loads the operator dotenv into os.environ unless
ATELES_SKIP_DOTENV is set, and the bootstrap is skipped under pytest, so these
tests run REAL subprocesses (with PYTEST_CURRENT_TEST removed) against a
synthetic dotenv. Synthetic key, temp dirs; no real dotenv or token is read.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.daemon_runtime import pinned_cli_identity as pci
from lib.daemon_runtime.pinned_testing import make_synthetic_key

REPO_ROOT = Path(__file__).resolve().parents[2]
# Built at runtime so the repository secret scanner has no literal to match.
BEARER_NAME = "NEOTOMA_" + "BEARER_" + "TOKEN"
SYNTHETIC_VALUE = "synthetic-" + "dotenv-" + "value-not-real"


@pytest.fixture
def child_env(tmp_path):
    keys = tmp_path / "keys"
    make_synthetic_key(keys)
    rc = tmp_path / "rc"
    (rc / "dist" / "cli").mkdir(parents=True)
    (rc / "dist" / "cli" / "bootstrap.js").write_text("// synthetic\n")
    env = pci.build_pinned_env(
        "piculet",
        {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")},
        keys_dir=keys, rc_dir=rc, state_dir=tmp_path / "state",
    )
    dotenv = tmp_path / "operator.env"
    dotenv.write_text(f"{BEARER_NAME}={SYNTHETIC_VALUE}\nSYNTHETIC_OTHER=kept\n")
    env.pop("PYTEST_CURRENT_TEST", None)
    env["ATELES_DOTENV_FILE"] = str(dotenv)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _run(code, env):
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-600:]
    return done.stdout.strip().splitlines()[-1]


PROBE = (
    "from lib.daemon_runtime import pinned_cli_identity as p; import os; "
    f"print(p.pinned_child_problem(), '|', '{BEARER_NAME}' in os.environ)"
)


def test_the_built_env_disables_the_dotenv_bootstrap(child_env):
    assert child_env["ATELES_SKIP_DOTENV"] == "1"


def test_importing_the_helper_in_a_pinned_child_leaves_no_bearer(child_env):
    assert _run(PROBE, child_env) == "None | False"


def test_control_without_the_fix_the_dotenv_bearer_is_loaded(child_env):
    """Instrument check: with the bootstrap left on, the same child DOES get the
    bearer back, and the preflight names it (what the live run hit)."""
    del child_env["ATELES_SKIP_DOTENV"]
    out = _run(f"import os; import lib.daemon_runtime; print('{BEARER_NAME}' in os.environ)", child_env)
    assert out == "True"


def test_without_the_fix_the_child_reports_the_bearer_the_live_run_hit(child_env):
    del child_env["ATELES_SKIP_DOTENV"]
    out = _run(PROBE, child_env)
    assert out.startswith(f"a bearer-style credential ({BEARER_NAME}) is present") and out.endswith("| True")


def test_the_preflight_refuses_when_the_bootstrap_is_not_disabled(child_env):
    del child_env["ATELES_SKIP_DOTENV"]
    assert "ATELES_SKIP_DOTENV" in pci.pinned_child_problem(child_env)
    assert pci.pinned_child_problem({**child_env, "ATELES_SKIP_DOTENV": "1"}) is None


@pytest.mark.parametrize(
    "module",
    ["import_audio_from_desktop", "transcribe_audio"],
)
def test_importing_the_importer_chain_in_a_pinned_child_leaves_no_bearer(child_env, tmp_path, module):
    """The importer and transcriber also load a repo .env; with that file holding
    a bearer too, neither import may leave one in a pinned child."""
    pytest.importorskip("dotenv")
    child_env["DATA_DIR"] = str(tmp_path / "data")
    code = (
        f"import sys; sys.path.insert(0, 'execution/scripts'); import {module}; import os; "
        "from lib.daemon_runtime import pinned_cli_identity as p; "
        f"print(p.pinned_child_problem(), '|', '{BEARER_NAME}' in os.environ)"
    )
    try:
        out = _run(code, child_env)
    except AssertionError as exc:  # heavy optional deps absent on this host
        if "ModuleNotFoundError" in str(exc):
            pytest.skip("importer dependencies are not installed here")
        raise
    assert out == "None | False"
