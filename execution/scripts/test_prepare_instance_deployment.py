"""Actual local CLI effects with an owned parser executable, no provider."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from execution.lib.instance_profile_guard import digest, installed_config_projection  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "owned_fixtures", ROOT / "execution/scripts/test_instance_profile_guard.py"
)
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


class LocalCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = self.root / "context"
        self.context.mkdir()
        self.gate = self.context / "test_owned_gate.py"
        self.gate.write_text(
            'from pathlib import Path\nimport unittest\nclass Gate(unittest.TestCase):\n def test_input(self):\n  self.assertEqual(Path("Dockerfile").read_text(),"owned safe build")\n  Path("gate-ran").write_text("local check")\n'
        )
        (self.context / "Dockerfile").write_text("owned safe build")
        (self.context / ".gitignore").write_text("gate-ran\n")
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=Owned Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "owned",
            ],
        ):
            subprocess.run(command, cwd=self.context, check=True, capture_output=True)
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.context, text=True
        ).strip()
        self.manifest, self.evidence, self.image = fixture_module.preparation_fixture()
        self.manifest["packaging_gate"]["path"] = self.gate.name
        self.manifest["packaging_gate"]["sha256"] = hashlib.sha256(
            self.gate.read_bytes()
        ).hexdigest()
        self.manifest["packaging_gate"]["inputs"] = {
            "Dockerfile": hashlib.sha256(
                (self.context / "Dockerfile").read_bytes()
            ).hexdigest()
        }
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fly = self.bin / "fly"
        # This owned executable actually parses the emitted TOML. It models only
        # the selected installed-tool interface; private witnesses use real Fly.
        fly.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """import sys,json,tomllib,os
from pathlib import Path
with open(os.environ['OWNED_CALLS'],'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
a=sys.argv[1:]
if a==['version']: print('owned-tool')
elif a==['deploy','--help']: print('--app --config --image --primary-region --ha --strategy --only-machines --update-only --no-public-ips --deploy-retries --exclude-machines --skip-release-command')
elif a[:3]==['config','show','--local']:
 if os.environ.get('OWNED_MUTATE'): Path(os.environ['OWNED_MUTATE']).write_text('changed during parser')
 c=tomllib.loads(Path(a[-1]).read_text());v=c['http_service']['auto_stop_machines']
 if v in ('off','stop'): c['http_service']['auto_stop_machines']=v=='stop'
 print(json.dumps(c))
else: raise SystemExit(93)
"""
        )
        fly.chmod(0o700)
        self.raw = self.root / "raw.toml"
        self.raw.write_text(
            'app="owned-fixture"\n[http_service]\nauto_stop_machines="off"\nmin_machines_running=1\n'
        )
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "OWNED_CALLS": str(self.root / "calls"),
        }
        self.output = self.root / "prepared"

    def execute(self, manifest=None, evidence=None, optimized=False):
        manifest = manifest or self.manifest
        evidence = evidence or self.evidence
        mp = self.root / "manifest.json"
        ep = self.root / "evidence.json"
        mp.write_text(json.dumps(manifest))
        ep.write_text(json.dumps(evidence))
        args = (
            [sys.executable]
            + (["-O"] if optimized else [])
            + [
                str(ROOT / "execution/scripts/prepare_instance_deployment.py"),
                "--manifest",
                str(mp),
                "--manifest-sha256",
                digest(manifest),
                "--evidence",
                str(ep),
                "--context",
                str(self.context),
                "--commit",
                self.commit,
                "--image",
                self.image,
                "--saved-toml",
                str(self.raw),
                "--output",
                str(self.output),
            ]
        )
        return subprocess.run(
            args, env=self.env, capture_output=True, text=True, timeout=15
        )

    def test_actual_cli_runs_gate_and_local_parser_no_deployment(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads((self.output / "prepared.json").read_text())
        self.assertEqual(prepared["commit"], self.commit)
        self.assertEqual(prepared["manifest_sha256"], digest(self.manifest))
        self.assertEqual(
            prepared["packaging_check"]["inputs"],
            self.manifest["packaging_gate"]["inputs"],
        )
        self.assertEqual(prepared["packaging_check"]["tests_run"], 1)
        self.assertEqual(prepared["packaging_check"]["skips"], 0)
        self.assertTrue((self.context / "gate-ran").exists())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.output / "fly.toml").stat().st_mode & 0o777, 0o600)
        calls = [json.loads(x) for x in (self.root / "calls").read_text().splitlines()]
        self.assertEqual(
            calls,
            [
                ["version"],
                ["deploy", "--help"],
                [
                    "config",
                    "show",
                    "--local",
                    "--config",
                    str(self.output / "fly.toml"),
                ],
            ],
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"prepared": True, "provider_writes": 0, "deployed": False},
        )

    def test_optimized_cli_same_binding(self):
        result = self.execute(optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_mismatch_refuses_before_packaging_or_tool(self):
        bad = copy.deepcopy(self.evidence)
        bad["inventory"][0]["config"]["services"][0]["min_machines_running"] = 1
        result = self.execute(evidence=bad)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.context / "gate-ran").exists())
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse(self.output.exists())

    def test_unpinned_packaging_gate_and_dirty_context_refuse(self):
        bad = copy.deepcopy(self.manifest)
        bad["packaging_gate"]["sha256"] = "0" * 64
        self.assertEqual(self.execute(manifest=bad).returncode, 2)
        self.assertFalse((self.root / "calls").exists())
        (self.context / "untracked").write_text("unexpected")
        self.assertEqual(self.execute().returncode, 2)
        self.assertFalse(self.output.exists())

    def replace_gate(self, body):
        self.gate.write_text(body)
        self.manifest["packaging_gate"]["sha256"] = hashlib.sha256(
            self.gate.read_bytes()
        ).hexdigest()
        subprocess.run(
            ["git", "add", "."], cwd=self.context, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Owned Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "owned check",
            ],
            cwd=self.context,
            check=True,
            capture_output=True,
        )
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.context, text=True
        ).strip()

    def test_skip_only_packaging_refuses_command(self):
        self.replace_gate(
            "import unittest\nclass Gate(unittest.TestCase):\n"
            ' @unittest.skip("owned unavailable dependency")\n'
            " def test_input(self): pass\n"
        )
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "calls").exists())

    def test_packaging_mutation_refuses_command(self):
        self.replace_gate(
            "from pathlib import Path\nimport unittest\n"
            "class Gate(unittest.TestCase):\n def test_input(self):\n"
            '  self.assertEqual(Path("Dockerfile").read_text(),'
            '"owned safe build")\n'
            '  Path("Dockerfile").write_text("changed after check")\n'
        )
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "calls").exists())

    def test_parser_mutation_refuses_final_emission(self):
        self.env["OWNED_MUTATE"] = str(self.context / "Dockerfile")
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_mixed_skipped_and_executed_gate_refuses(self):
        self.replace_gate(
            "import unittest\nclass Gate(unittest.TestCase):\n"
            " def test_positive(self): self.assertTrue(True)\n"
            ' @unittest.skip("owned unavailable dependency")\n'
            " def test_input(self): pass\n"
        )
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def bind_service_checks(self, path):
        from execution.lib.instance_profile_guard import stable_source

        self.evidence["saved"]["http_service"]["checks"] = [
            {"path": path, "interval": "30s", "timeout": "30s", "grace_period": "1m"}
        ]
        self.evidence["inventory"][0]["config"]["services"][0]["checks"] = [
            {
                "path": "/health",
                "interval": "30s",
                "timeout": "30s",
                "grace_period": "1m",
                "type": "http",
            }
        ]
        normalized = copy.deepcopy(self.evidence["saved"])
        normalized["http_service"]["auto_stop_machines"] = "stop"
        normalized["http_service"]["min_machines_running"] = 0
        profile = self.manifest["profile"]
        profile["saved_config_before_sha256"] = digest(self.evidence["saved"])
        profile["saved_config_after_sha256"] = digest(normalized)
        profile["source_static_config_sha256"] = digest(
            stable_source(self.evidence["inventory"][0]["config"])
        )
        self.raw.write_text(
            self.raw.read_text() + "[[http_service.checks]]\n"
            'path="' + path + '"\ninterval="30s"\n'
            'timeout="30s"\ngrace_period="1m"\n'
        )

    def test_saved_and_machine_check_drift_refuses_command(self):
        self.bind_service_checks("/ready")
        result = self.execute()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.context / "gate-ran").exists())

    def test_matching_saved_and_machine_checks_runs_real_gate(self):
        self.bind_service_checks("/health")
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.context / "gate-ran").exists())
        self.assertTrue((self.output / "prepared.json").exists())

    def test_installed_encoding_projection_is_narrow(self):
        source = {
            "http_service": {"auto_stop_machines": "stop", "min_machines_running": 0},
            "env": {"UNCHANGED": "value"},
        }
        self.assertEqual(
            installed_config_projection(source),
            {
                "http_service": {"auto_stop_machines": True, "min_machines_running": 0},
                "env": {"UNCHANGED": "value"},
            },
        )
        self.assertEqual(source["http_service"]["auto_stop_machines"], "stop")
        source["http_service"]["auto_stop_machines"] = "suspend"
        self.assertEqual(installed_config_projection(source), source)


if __name__ == "__main__":
    unittest.main()
