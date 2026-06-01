#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

script_path = Path(__file__).parent / "migrate-truth-data-to-datadir.py"
result = subprocess.run(
    [sys.executable, str(script_path)], capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
