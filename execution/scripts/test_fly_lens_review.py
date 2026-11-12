"""Tests for fly_lens_review.py with the Fly dispatch mocked."""
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import fly_lens_review as f  # noqa: E402

HEAD = "a" * 40
GOOD = "**\U0001F916 Pavo - Ateles swarm, pm lens panelist**\n**SIGNED_OFF**\n\nFine.\n"


def ok_env(stdout=GOOD, **kw):
    return {"ok": True, "provider": "claude", "remote_run_id": "fly-1", "stdout": stdout, **kw}


def fail_env(reason="dispatch failed", **kw):
    return {"ok": False, "reason": reason, "provider": "claude", "returncode": 1,
            "stderr": "fly runner: no result recorded for run fly-2",
            "remote_outcome_unknown": True, "remote_run_id": "fly-2", **kw}


class Script:
    """Scripted dispatch: returns queued envelopes, records the calls."""

    def __init__(self, *envs):
        self.envs, self.calls = list(envs), []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.envs.pop(0)


def run(tmp_path, dispatch, *extra, heads=(HEAD,), poster=None, lens="pm"):
    seq = list(heads)

    def head_resolver(repo, pr):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    posts = []

    def default_poster(**kw):
        posts.append(kw)
        return True, "posted"

    rc = f.main(["--repo", "o/r", "--pr", "7", "--lens", lens, "--task-entity-id",
                 "ent_x", "--out-dir", str(tmp_path), *extra],
                dispatch=dispatch, head_resolver=head_resolver,
                poster=poster or default_poster)
    return rc, posts


def test_head_is_pinned_in_brief_and_record(tmp_path):
    d = Script(ok_env())
    rc, _ = run(tmp_path, d)
    assert rc == 0
    brief = (tmp_path / "pm.brief.md").read_text()
    assert f"Review ONLY head `{HEAD}`" in brief and f"(head {HEAD})" in brief
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert summ["head"] == HEAD and summ["lenses"][0]["reviewed_head"] == HEAD


def test_verdict_file_shape_and_summary(tmp_path):
    d = Script(ok_env("preamble line\n" + GOOD))
    run(tmp_path, d)
    lines = (tmp_path / "pm.verdict.md").read_text().splitlines()
    assert lines[0].startswith("**\U0001F916") and lines[1] == "**SIGNED_OFF**"
    rec = json.loads((tmp_path / "summary.json").read_text())["lenses"][0]
    assert (rec["provider"], rec["run_id"], rec["verdict"]) == ("claude", "fly-1", "SIGNED_OFF")
    assert "duration_s" in rec


def test_retries_once_on_mechanical_failure(tmp_path):
    d = Script(fail_env(), ok_env())
    rc, _ = run(tmp_path, d)
    assert rc == 0 and len(d.calls) == 2
    rec = json.loads((tmp_path / "summary.json").read_text())["lenses"][0]
    assert len(rec["attempts"]) == 2


def test_fallback_after_two_failures(tmp_path, capsys):
    d = Script(fail_env(), fail_env())
    rc, _ = run(tmp_path, d)
    out = capsys.readouterr().out
    assert rc == 3 and len(d.calls) == 2
    assert "FALL BACK TO LOCAL LENS: pm (failed 2 times" in out
    assert not (tmp_path / "pm.verdict.md").exists()


def test_refusal_not_retried(tmp_path, capsys):
    d = Script({"ok": False, "reason": "provider claude is refused: not pinnable",
                "returncode": None, "stderr": ""})
    rc, _ = run(tmp_path, d)
    assert rc == 3 and len(d.calls) == 1
    assert "FALL BACK TO LOCAL LENS: pm (refusal" in capsys.readouterr().out


def test_queue_wait_falls_back_without_retry(tmp_path, capsys):
    d = Script({"ok": False, "reason": "queue wait exceeded 3600s", "returncode": 1,
                "stderr": "waited 3600s in the queue"})
    rc, _ = run(tmp_path, d)
    assert rc == 3 and len(d.calls) == 1
    assert "FALL BACK TO LOCAL LENS: pm (queue" in capsys.readouterr().out


def test_unreadable_reply_retried_then_falls_back(tmp_path, capsys):
    d = Script(ok_env("no verdict here"), ok_env("still none"))
    rc, _ = run(tmp_path, d)
    assert rc == 3 and len(d.calls) == 2


def test_no_posting_without_post_flag(tmp_path):
    rc, posts = run(tmp_path, Script(ok_env()))
    assert rc == 0 and posts == []


def test_post_flag_posts_with_reviewed_head(tmp_path):
    rc, posts = run(tmp_path, Script(ok_env()), "--post")
    assert rc == 0 and len(posts) == 1
    assert posts[0]["head"] == HEAD and posts[0]["lens"] == "pm"
    assert posts[0]["body_file"].endswith("pm.verdict.md")


def test_moved_head_is_stale_and_not_posted(tmp_path, capsys):
    rc, posts = run(tmp_path, Script(ok_env()), "--post", heads=(HEAD, "b" * 40))
    assert rc == 3 and posts == []
    assert "STALE" in capsys.readouterr().out


def test_extra_brief_passed_through(tmp_path):
    extra = tmp_path / "notes.md"
    extra.write_text("ROUND TWO: check the fixed retry path")
    run(tmp_path, Script(ok_env()), "--extra-brief", str(extra))
    assert "ROUND TWO: check the fixed retry path" in (tmp_path / "pm.brief.md").read_text()


def test_all_runs_five_lenses_sequentially(tmp_path):
    envs = [ok_env(GOOD.replace("Pavo", r).replace("pm lens", lname + " lens"))
            for r, lname in [("Pavo", "pm"), ("Waxwing", "arch"), ("Falco", "security"),
                         ("Phoenicurus", "qa"), ("Accipiter", "ux")]]
    d = Script(*envs)
    rc, _ = run(tmp_path, d, lens="all")
    assert rc == 0
    assert [c["lens"] for c in d.calls] == list(f.LENS_ORDER)
    assert [c["role"] for c in d.calls] == ["pavo", "waxwing", "falco", "phoenicurus", "accipiter"]


def test_dispatch_command_line(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""

    monkeypatch.setattr(f.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or P())
    f.dispatch_once(python="py", dispatch_script=Path("d.py"), role="pavo", lens="pm",
                    repo="o/r", task_entity_id="ent_x", task_file=tmp_path / "b.md")
    c = seen["cmd"]
    for a in ("--runner", "fly", "--github-read", "lens_review:pm", "ent_x", "2700"):
        assert a in c


def test_bad_task_id_is_usage_error(tmp_path):
    rc = f.main(["--repo", "o/r", "--pr", "1", "--lens", "pm", "--task-entity-id", "nope",
                 "--out-dir", str(tmp_path)])
    assert rc == 2
