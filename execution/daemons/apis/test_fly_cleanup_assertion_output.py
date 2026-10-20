"""Exercise delivery cleanup failures through real pytest assertion rendering."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module,test_name", [
    ("test_fly_repo_delivery", "test_leftovers_are_cleared_without_following_links"),
    pytest.param("test_fly_repo_delivery_container", "test_the_agent_and_root_sides_stay_separate",
                 marks=pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                                          reason="container control needs root on Linux")),
])
def test_adjacent_cleanup_failures_render_only_safe_evidence(tmp_path, module, test_name):
    script = tmp_path / "test_cleanup_output.py"
    arguments = "tmp_path, monkeypatch" if module.endswith("_container") else "tmp_path"
    script.write_text(f'''import importlib
import sys
import pytest
sys.path.insert(0, {str(Path(__file__).parent)!r})
pytest.register_assert_rewrite({module!r})
target = importlib.import_module({module!r})

def test_actual_cleanup_assertion(tmp_path, monkeypatch):
    real_remove = target.rd.remove_work
    def residual(root):
        real_remove(root)
        (root / "synthetic_sensitive_filename_cleanup").write_text("synthetic_sensitive_content_cleanup")
    monkeypatch.setattr(target.rd, "remove_work", residual)
    target.{test_name}({arguments})
''')
    runtime = tmp_path / "runtime"
    done = subprocess.run([sys.executable, "-m", "pytest", str(script), "-q", "--tb=short",
                           "--basetemp", str(runtime), "-p", "no:cacheprovider"],
                          capture_output=True, text=True, timeout=90)
    output = done.stdout + done.stderr
    assert done.returncode == 1 and "1 failed" in output
    assert "delivery cleanup failed: " in output and '"relative": "<entry>"' in output
    assert '"kind": "file"' in output
    assert all(value not in output for value in (
        "synthetic_sensitive_filename_cleanup", "synthetic_sensitive_content_cleanup",
        str(runtime), "/work-container-check"))
