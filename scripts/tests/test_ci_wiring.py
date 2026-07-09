"""QA §7 (P1): confirm render_hooks.py --check and render_agent_docs.py
--check are both wired into the same CI job/step, not two separate ones.

Deliberately avoids a YAML parser dependency (not declared in
pyproject.toml) — parses the workflow's job structure with plain text
scanning, sufficient for this file's simple shape (one `jobs:` block, 2-space
indented job names)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "generated-mirrors.yml"


def test_workflow_file_exists():
    assert WORKFLOW_PATH.exists(), "expected .github/workflows/generated-mirrors.yml"


def _job_blocks(text: str) -> dict[str, str]:
    """Split the `jobs:` section into {job_name: block_text} by top-level
    (2-space indented) job name lines."""
    lines = text.splitlines()
    try:
        jobs_idx = next(i for i, l in enumerate(lines) if l.rstrip() == "jobs:")
    except StopIteration:
        return {}
    job_start = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    blocks: dict[str, list[str]] = {}
    current = None
    for line in lines[jobs_idx + 1:]:
        m = job_start.match(line)
        if m:
            current = m.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def test_both_checks_present_in_same_job():
    text = WORKFLOW_PATH.read_text()
    jobs = _job_blocks(text)
    assert jobs, "workflow has no jobs"

    hits = {"render_agent_docs.py --check": None, "render_hooks.py --check": None}
    for job_name, body in jobs.items():
        for needle in list(hits):
            if needle in body:
                hits[needle] = job_name

    missing = [k for k, v in hits.items() if v is None]
    assert not missing, f"missing CI step(s) for: {missing}"

    job_names = set(hits.values())
    assert len(job_names) == 1, (
        f"render_agent_docs.py --check and render_hooks.py --check must run in the "
        f"SAME job, found in different jobs: {hits}"
    )
