"""Tests for fly_lens_review.py with the Fly dispatch mocked."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest


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


def run(tmp_path, dispatch, *extra, heads=(HEAD,), poster=None, lens="pm",
        files=("README.md",)):
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
                poster=poster or default_poster,
                files_resolver=lambda repo, pr: list(files))
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


@pytest.mark.parametrize("private", [
    "PRIVATE-NOTE: the details of an out-of-scope observation",
    "`PRIVATE-NOTE:` the details",
    "---\n*Private to the requesting session (do not post):* the details of an observation",
    "\n\n---\n\n**Private to the requesting session**: the details",
])
def test_a_private_section_is_never_in_the_posted_verdict(tmp_path, private):
    reply = GOOD + "\nOne out-of-scope hardening observation was recorded privately.\n\n" + private + "\n"
    rc, posts = run(tmp_path, Script(ok_env(reply)), "--post")
    assert rc == 0
    verdict = (tmp_path / "pm.verdict.md").read_text()
    assert "details" not in verdict and "Private to the requesting session" not in verdict
    assert "recorded privately" in verdict and verdict.endswith("\n")
    note = tmp_path / "pm.private.txt"
    assert "details" in note.read_text() and (note.stat().st_mode & 0o777) == 0o600
    assert all("details" not in str(v) for post in posts for v in post.values())


def test_a_private_marker_above_the_verdict_fails_closed_and_posts_nothing(tmp_path):
    # The marker before the verdict lines would leave no verdict: never post an empty body.
    reply = "**\U0001F916 Pavo - Ateles swarm, pm lens panelist**\nPRIVATE-NOTE: the details\n**SIGNED_OFF**\n"
    rc, posts = run(tmp_path, Script(ok_env(reply), ok_env(reply)), "--post")
    assert posts == [] and not (tmp_path / "pm.verdict.md").exists()
    assert rc != 0


def test_a_reply_without_a_private_section_is_posted_whole(tmp_path):
    run(tmp_path, Script(ok_env()))
    assert (tmp_path / "pm.verdict.md").read_text() == GOOD
    assert not (tmp_path / "pm.private.txt").exists()


def test_the_brief_names_the_private_note_marker_and_never_tells_a_lens_to_use_its_reply(tmp_path):
    run(tmp_path, Script(ok_env()))
    brief = (tmp_path / "pm.brief.md").read_text()
    assert "PRIVATE-NOTE:" in brief
    assert "give the details only in your final reply" not in brief


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
    for a in ("--runner", "fly", "--github-read", "lens_review:pm", "ent_x", "2700",
              "--caller", "fly_lens_review", "--caller-kind", "daemon"):
        assert a in c


def test_local_eval_dispatch_names_its_launcher(monkeypatch, tmp_path):
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "local_llm" / "eval_lean_prompt.py"
    spec = importlib.util.spec_from_file_location("eval_lean_prompt", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    seen = {}

    class P:
        stdout, stderr = '{"ok": true}', ""

    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or P())
    assert module._dispatch("cicada", "test", cwd=tmp_path,
                            work_class="worktree_hygiene", timeout=1)["ok"]
    assert seen["cmd"][seen["cmd"].index("--caller") + 1] == "eval_lean_prompt"
    assert seen["cmd"][seen["cmd"].index("--caller-kind") + 1] == "daemon"


def test_bad_task_id_is_usage_error(tmp_path):
    rc = f.main(["--repo", "o/r", "--pr", "1", "--lens", "pm", "--task-entity-id", "nope",
                 "--out-dir", str(tmp_path)])
    assert rc == 2


def test_failed_post_exits_4_with_actionable_line(tmp_path, capsys):
    rc, _ = run(tmp_path, Script(ok_env()), "--post",
                poster=lambda **kw: (False, "head moved: comment pinned to older head"))
    out = capsys.readouterr().out
    assert rc == 4
    assert "POST FAILED: pm (head moved" in out and "post_lens_verdict.py" in out
    assert "--head " + HEAD in out
    rec = json.loads((tmp_path / "summary.json").read_text())["lenses"][0]
    assert rec["posted"] is False and rec["status"] == "post_failed"


def test_fallback_exit_wins_over_post_failure(tmp_path):
    rc, _ = run(tmp_path, Script(ok_env(), *[fail_env() for _ in range(8)]), "--post", lens="all",
                poster=lambda **kw: (False, "x"))
    assert rc == 3


def test_deterministic_403_not_retried(tmp_path, capsys):
    d = Script(fail_env(stderr="fly runner: no result recorded; result write 403, exit 95"))
    rc, _ = run(tmp_path, d)
    assert rc == 3 and len(d.calls) == 1
    assert "FALL BACK TO LOCAL LENS: pm (deterministic" in capsys.readouterr().out


def test_queue_labelled_as_queue_wait(tmp_path, capsys):
    d = Script({"ok": False, "reason": "launch refused: queue wait exceeded", "returncode": 1,
                "stderr": ""})
    run(tmp_path, d)
    assert "(queue wait:" in capsys.readouterr().out


def test_queue_limit_passed_to_runner(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""

    monkeypatch.delenv("APIS_FLY_QUEUE_MAX_WAIT_SECONDS", raising=False)
    monkeypatch.setattr(f.subprocess, "run", lambda cmd, **kw: seen.update(kw) or P())
    f.dispatch_once(python="py", dispatch_script=Path("d.py"), role="pavo", lens="pm",
                    repo="o/r", task_entity_id="ent_x", task_file=tmp_path / "b.md")
    assert seen["env"]["APIS_FLY_QUEUE_MAX_WAIT_SECONDS"] == "3600"


def test_head_move_stops_remaining_and_marks_stale(tmp_path, capsys):
    d = Script(ok_env(), ok_env())
    rc, posts = run(tmp_path, d, "--post", lens="all", heads=(HEAD, "b" * 40))
    out = capsys.readouterr().out
    assert rc == 3 and len(d.calls) == 1 and posts == []
    assert "stopped dispatching" in out
    assert out.count("FALL BACK TO LOCAL LENS") == 5
    assert (tmp_path / "pm.verdict.md.stale").exists()
    assert not (tmp_path / "pm.verdict.md").exists()


def test_summary_written_after_each_lens_and_on_interrupt(tmp_path):
    def boom(**kw):
        if kw["lens"] == "arch":
            raise KeyboardInterrupt
        return ok_env()

    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, boom, lens="all")
    summ = json.loads((tmp_path / "summary.json").read_text())
    assert [x["lens"] for x in summ["lenses"]] == ["pm"] and summ["complete"] is False


def test_default_out_dir_is_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = f.main(["--repo", "o/r", "--pr", "7", "--lens", "pm", "--task-entity-id", "ent_x"],
                dispatch=Script(ok_env()), head_resolver=lambda r, p: HEAD)
    assert rc == 0
    assert not list(tmp_path.glob("fly-lens-review-*"))
    assert list((tmp_path / "home" / ".ateles-work" / "fly-lens-review").glob("*/summary.json"))


def lens_env(role, lname):
    return ok_env(GOOD.replace("Pavo", role).replace("pm lens", lname + " lens"))


def test_legal_lens_accepted_and_dispatched_as_buteo(tmp_path):
    d = Script(lens_env("Buteo", "legal"))
    rc, posts = run(tmp_path, d, "--post", lens="legal")
    assert rc == 0
    assert [(c["lens"], c["role"]) for c in d.calls] == [("legal", "buteo")]
    assert ".claude/skills/buteo/SKILL.md" in (tmp_path / "legal.brief.md").read_text()
    rec = json.loads((tmp_path / "summary.json").read_text())["lenses"][0]
    assert (rec["role"], rec["verdict"]) == ("buteo", "SIGNED_OFF")
    assert [p["lens"] for p in posts] == ["legal"]


def test_all_adds_legal_for_a_package_json_diff(tmp_path):
    roles = [("Pavo", "pm"), ("Waxwing", "arch"), ("Falco", "security"),
             ("Phoenicurus", "qa"), ("Accipiter", "ux"), ("Buteo", "legal")]
    d = Script(*[lens_env(r, n) for r, n in roles])
    rc, _ = run(tmp_path, d, lens="all", files=("package.json", "src/a.ts"))
    assert rc == 0
    assert [c["lens"] for c in d.calls] == [*f.LENS_ORDER, "legal"]
    assert d.calls[-1]["role"] == "buteo"


def test_all_without_legal_path_is_the_five_in_order(tmp_path):
    d = Script(*[lens_env("Pavo", "pm")] * 5)
    rc, _ = run(tmp_path, d, lens="all", files=("execution/scripts/x.py", "README.md"))
    assert rc == 0
    assert [c["lens"] for c in d.calls] == ["pm", "arch", "security", "qa", "ux"]


def test_all_never_includes_content():
    # a large docs diff is what lets select_panel pick the forward-looking content lens
    many = [f"docs/page{i}.md" for i in range(12)] + ["package.json"]
    for files in (many, ["README.md"], None):
        assert "content" not in f.lenses_for_all(files)
    assert "content" not in f.LENS_ROLES


def test_all_unreadable_file_list_runs_legal_too(tmp_path):
    def broken(repo, pr):
        raise RuntimeError("gh failed")

    d = Script(*[lens_env("Pavo", "pm")] * 6)
    rc = f.main(["--repo", "o/r", "--pr", "7", "--lens", "all", "--task-entity-id",
                 "ent_x", "--out-dir", str(tmp_path)],
                dispatch=d, head_resolver=lambda r, p: HEAD,
                poster=lambda **kw: (True, ""), files_resolver=broken)
    assert rc == 0
    assert [c["lens"] for c in d.calls] == [*f.LENS_ORDER, "legal"]


@pytest.mark.parametrize(
    ("query", "failure"),
    [("files", "timeout"), ("count", "timeout"),
     ("files", "oserror"), ("count", "oserror")],
)
def test_file_list_timeout_runs_every_requirable_lens(
    monkeypatch, tmp_path, capsys, query, failure,
):
    row = json.dumps({"filename": "README.md", "status": "modified"}) + "\n"

    def fake_run(argv, **kw):
        current = "count" if argv[-1] == ".changed_files" else "files"
        if current == query:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            raise OSError("synthetic launch failure")
        return _Proc(stdout="1\n" if current == "count" else row)

    monkeypatch.setattr(f.subprocess, "run", fake_run)
    d = Script(*[lens_env("Pavo", "pm")] * 6)
    rc = f.main(["--repo", "o/r", "--pr", "7", "--lens", "all",
                 "--task-entity-id", "ent_x", "--out-dir", str(tmp_path)],
                dispatch=d, head_resolver=lambda r, p: HEAD)
    assert rc == 0
    assert [c["lens"] for c in d.calls] == [*f.LENS_ORDER, "legal"]
    error = capsys.readouterr().err
    assert "could not read the changed files" in error
    assert "running every lens the approval tool could require" in error


@pytest.mark.parametrize("panel_max", ["0", "-1", "abc", "below_floor"])
def test_all_rejects_panel_max_below_approval_bound(
    monkeypatch, tmp_path, capsys, panel_max,
):
    sys.path.insert(0, str(f.APIS_DIR))
    import review_panel

    floor_count = sum(lens.always for lens in review_panel.LENSES)
    monkeypatch.setenv("APIS_PANEL_MAX", str(floor_count - 1) if panel_max == "below_floor"
                       else panel_max)
    d = Script()
    rc, _ = run(tmp_path, d, lens="all")
    assert rc == 2
    assert d.calls == []
    assert "APIS_PANEL_MAX" in capsys.readouterr().err


def test_all_accepts_panel_max_at_approval_bound(monkeypatch, tmp_path):
    sys.path.insert(0, str(f.APIS_DIR))
    import review_panel

    monkeypatch.setenv("APIS_PANEL_MAX", str(sum(lens.always for lens in review_panel.LENSES)))
    d = Script(*[lens_env("Pavo", "pm")] * 5)
    rc, _ = run(tmp_path, d, lens="all")
    assert rc == 0
    assert [c["lens"] for c in d.calls] == list(f.LENS_ORDER)


# --- resolve_changed_files: renames and empty lists (security review, PR #1787) ---
class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake_gh(monkeypatch, stdout, returncode=0, changed_files="auto"):
    """Fake `gh`: the files listing returns *stdout*; the PR read returns
    *changed_files* ("auto" = the number of non-blank rows in *stdout*)."""
    calls = []
    if changed_files == "auto":
        changed_files = sum(1 for line in stdout.splitlines() if line.strip())

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[-1] == ".changed_files":
            return _Proc(stdout=json.dumps(changed_files) + "\n")
        return _Proc(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(f.subprocess, "run", fake_run)
    return calls


def test_rename_out_of_a_legal_path_keeps_the_old_path_and_selects_legal(monkeypatch):
    rows = [{"filename": "src/deps.json", "previous_filename": "package.json", "status": "renamed"},
            {"filename": "README.md", "previous_filename": None, "status": "modified"}]
    _fake_gh(monkeypatch, "\n".join(json.dumps(r) for r in rows) + "\n")
    paths = f.resolve_changed_files("o/r", 7)
    assert paths == ["src/deps.json", "package.json", "README.md"]
    assert "legal" in f.lenses_for_all(paths)
    assert "legal" not in f.lenses_for_all(["src/deps.json", "README.md"])


def test_file_list_query_asks_for_previous_filename_and_status(monkeypatch):
    calls = _fake_gh(monkeypatch, json.dumps({"filename": "a.py", "status": "modified"}) + "\n")
    f.resolve_changed_files("o/r", 7)
    assert "previous_filename" in " ".join(calls[0])
    assert "status" in " ".join(calls[0])


def test_empty_file_list_is_unreadable_and_runs_every_lens(monkeypatch):
    _fake_gh(monkeypatch, "\n")
    with pytest.raises(RuntimeError, match="empty"):
        f.resolve_changed_files("o/r", 7)


def test_non_json_or_nameless_row_is_unreadable(monkeypatch):
    for out in ("package.json\n", json.dumps({"previous_filename": "x"}) + "\n", "[1]\n"):
        _fake_gh(monkeypatch, out)
        with pytest.raises(RuntimeError, match="could not read"):
            f.resolve_changed_files("o/r", 7)


def test_failed_gh_call_still_raises(monkeypatch):
    _fake_gh(monkeypatch, "", returncode=1)
    with pytest.raises(RuntimeError, match="could not list"):
        f.resolve_changed_files("o/r", 7)


# --- second review round on 69cfae19: completeness, single source, fallback ---
def test_file_list_shorter_than_the_prs_changed_files_is_unreadable(monkeypatch):
    rows = [json.dumps({"filename": "README.md", "status": "modified"})]
    _fake_gh(monkeypatch, "\n".join(rows) + "\n", changed_files=2)
    with pytest.raises(RuntimeError, match="not complete"):
        f.resolve_changed_files("o/r", 7)


def test_file_list_at_the_api_cap_is_unreadable(monkeypatch):
    rows = [json.dumps({"filename": f"f{i}.py", "status": "modified"}) for i in range(3000)]
    _fake_gh(monkeypatch, "\n".join(rows) + "\n")  # count matches, but at the cap
    with pytest.raises(RuntimeError, match="not complete"):
        f.resolve_changed_files("o/r", 7)


def test_pr_with_no_readable_file_count_is_unreadable(monkeypatch):
    _fake_gh(monkeypatch, json.dumps({"filename": "a.py", "status": "modified"}) + "\n", changed_files=None)
    with pytest.raises(RuntimeError, match="not complete"):
        f.resolve_changed_files("o/r", 7)


@pytest.mark.parametrize("row", [
    {"filename": "src/deps.json", "status": "renamed", "previous_filename": None},
    {"filename": "src/deps.json", "status": "renamed", "previous_filename": 7},
    {"filename": "src/deps.json", "status": "renamed", "previous_filename": ""},
    {"filename": "src/deps.json", "status": "renamed", "previous_filename": "  "},
    {"filename": "", "status": "modified"},
    {"filename": None, "status": "modified"},
    {"filename": " package.json", "status": "modified"},
    {"filename": "package.json ", "status": "modified"},
    {"filename": "package.json", "status": "weird"},
    {"filename": "package.json", "status": ["modified"]},
    {"filename": "package.json", "status": "copied", "previous_filename": None},
    {"filename": "package.json", "status": "modified", "previous_filename": 7},
    {"filename": "package.json"},
    ["package.json"],
])
def test_malformed_rows_fail_closed_in_both_file_readers(monkeypatch, row):
    sys.path.insert(0, str(f.APIS_DIR))
    import review_scope

    _fake_gh(monkeypatch, json.dumps(row) + "\n")
    with pytest.raises(RuntimeError, match="could not read"):
        f.resolve_changed_files("o/r", 7)
    assert "legal" in f.lenses_for_all(None)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [row]

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(ValueError):
        asyncio.run(review_scope.fetch_pr_files(
            Client(), api_base="https://api.github.com", repo="o/r", pr=7, headers={}
        ))


def test_valid_rename_has_identical_paths_in_both_file_readers(monkeypatch):
    sys.path.insert(0, str(f.APIS_DIR))
    import review_scope

    row = {"filename": "src/deps.json", "status": "renamed", "previous_filename": "package.json"}
    _fake_gh(monkeypatch, json.dumps(row) + "\n")
    fly_paths = f.resolve_changed_files("o/r", 7)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [row]

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response()

    shared_paths, count = asyncio.run(review_scope.fetch_pr_files(
        Client(), api_base="https://api.github.com", repo="o/r", pr=7, headers={}
    ))
    assert count == 1
    assert set(fly_paths) == set(shared_paths) == {"src/deps.json", "package.json"}


def test_shared_file_reader_rejects_a_non_list_response():
    sys.path.insert(0, str(f.APIS_DIR))
    import review_scope

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": "error"}

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(ValueError, match="not a list"):
        asyncio.run(review_scope.fetch_pr_files(
            Client(), api_base="https://api.github.com", repo="o/r", pr=7, headers={}
        ))


def test_lens_roles_match_the_review_panel_registry():
    sys.path.insert(0, str(f.APIS_DIR))
    import review_panel

    registry = {lens.lens: lens.agent for lens in review_panel.LENSES}
    for name, role in f.LENS_ROLES.items():
        assert registry.get(name) == role, (name, role, registry.get(name))


def test_unreadable_list_runs_every_requirable_lens_the_runner_has():
    sys.path.insert(0, str(f.APIS_DIR))
    import review_panel

    got = f.lenses_for_all(None)
    want = review_panel.every_requirable_lens() & f.LENS_ROLES.keys()
    assert set(got) == set(want) | set(f.LENS_ORDER)
    assert "legal" in got


def test_main_passes_the_repo_qualified_pr_to_dispatch_role(monkeypatch, tmp_path):
    """Spend attribution (ent_e5259618cae6cafd4be120e0): the natural call path
    (main -> run_lens -> dispatch_once -> dispatch_role.py) carries
    ``--pr owner/repo#N``, so the tier-ledger rows name the reviewed PR."""
    seen = []

    class P:
        returncode, stdout, stderr = 0, json.dumps(ok_env()), ""

    monkeypatch.setattr(f.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or P())
    rc = f.main(["--repo", "o/r", "--pr", "7", "--lens", "pm", "--task-entity-id",
                 "ent_x", "--out-dir", str(tmp_path)],
                head_resolver=lambda r, p: HEAD, poster=lambda **kw: (True, "posted"))
    assert rc == 0 and len(seen) == 1
    cmd = seen[0]
    assert cmd[cmd.index("--pr") + 1] == "o/r#7"
# --------------------------------------------------------------------------
# Colon-separated header from a Fly lens run (2026-11-16)
# --------------------------------------------------------------------------
COLON = "**\U0001F916 Pavo: Ateles swarm, pm lens panelist**"
REST = "\n**SIGNED_OFF**\n\nFine: no change needed — see `a: b`.\n"


def _swarm_dispatch():
    apis = Path(__file__).resolve().parent.parent / "daemons" / "apis"
    if str(apis) not in sys.path:
        sys.path.insert(0, str(apis))
    import swarm_dispatch
    return swarm_dispatch


def test_colon_header_normalized_and_accepted_by_gate_reader(tmp_path):
    sd = _swarm_dispatch()
    # the reply as the Fly run returned it is refused by the gate reader
    assert (sd.describe_gate_verdict_position(COLON + REST, lens_agent="pavo")
            == "first line is not the lens header")
    d = Script(ok_env(COLON + REST))
    rc, _ = run(tmp_path, d, "--post")
    assert rc == 0
    body = (tmp_path / "pm.verdict.md").read_text()
    assert body.splitlines()[0] == "**\U0001F916 Pavo — Ateles swarm, pm lens panelist**"
    assert sd.lens_own_verdict(body, lens_agent="pavo") == "signed_off"
    assert (sd.describe_gate_verdict_position(body, lens_agent="pavo")
            == "the reply carries a second header or a second verdict line")


def test_colon_header_naming_another_agent_is_not_rewritten(tmp_path):
    sd = _swarm_dispatch()
    other = "**\U0001F916 Waxwing: Ateles swarm, pm lens panelist**" + REST
    assert f.normalize_header(other, role="pavo") == other
    d = Script(ok_env(other))
    run(tmp_path, d)
    body = (tmp_path / "pm.verdict.md").read_text()
    assert body == other
    assert sd.lens_own_verdict(body, lens_agent="pavo") is None
    assert (sd.describe_gate_verdict_position(body, lens_agent="pavo")
            == "first line is not the lens header")


def test_colon_header_normalization_keeps_rest_byte_identical():
    out = f.normalize_header(COLON + REST, role="pavo")
    first, _, rest = out.partition("\n")
    assert first == "**\U0001F916 Pavo — Ateles swarm, pm lens panelist**"
    assert "\n" + rest == REST
    # a dash header and any later colon line are left untouched
    assert f.normalize_header(GOOD, role="pavo") == GOOD
    later = GOOD + COLON + "\n"
    assert f.normalize_header(later, role="pavo") == later


def test_brief_requires_the_dash_separator(tmp_path):
    run(tmp_path, Script(ok_env()))
    brief = (tmp_path / "pm.brief.md").read_text()
    assert "must be the dash character shown" in brief
