# Run a lens review on Fly

## Purpose

Operator ruling 2026-11-12 (agent_policy `ent_161873dfe2a269554d60187f`): lens
reviews run on Fly first. This is the one supported command for a session lead
or a subagent. A local subagent lens is the fallback, not the default.

## Scope

This runbook covers the Fly lens runner, its local fallback, and verdict posting.

## The command

Run it from the Ateles deployment checkout, with the operator's Neotoma env pair
sourced into the program (`set -a`, source the Neotoma env file, `set +a`; the
Neotoma env file is `~/.config/neotoma/.env`). Never print or read that file.

```sh
cd ~/ateles-rc-src
# source the Neotoma env file here with set -a / set +a, then:
.venv/bin/python execution/scripts/fly_lens_review.py \
    --repo <owner/name> --pr <n> --lens <pm|arch|security|qa|ux|legal|all> \
    --task-entity-id ent_<neotoma task> \
    [--extra-brief round2-notes.md] [--out-dir <dir>] [--post]
```

Put the whole thing in a script file rather than an inline interpreter: the
credential-read hook refuses an inline interpreter whose source mentions the
env file.

Lens to role: pm=pavo, arch=waxwing, security=falco, qa=phoenicurus,
ux=accipiter, legal=buteo. `all` runs, one after another, the lenses the
approval tool requires for the PR's current diff: the five bootstrap lenses
(pm, arch, security, qa, ux), plus legal when the diff touches a legal path
such as `package.json` (the same `review_panel.approval_floor` selection
`approve_pr_as_app.py` uses). content/Corvus is never run. If the changed-file
list cannot be read, `all` also runs legal.

## What it does

- Resolves the PR's current head and names it in each lens's brief. If the head
  moves while a lens runs, that verdict is marked STALE and treated as a
  fallback case, and the remaining lenses are not dispatched.
- Dispatches through `dispatch_role.py --runner fly --github-read
  --action-class lens_review:<lens>`, one Fly job at a time, with the 45 minute
  per-job limit.
- Retries once on a mechanical failure (unknown outcome, "no result recorded
  for run", a missing or malformed public verdict block). A refusal, or a queue wait
  over the limit, is not retried.
- Requires exactly one `<<<PUBLIC-VERDICT>>>` line followed by exactly one
  `<<<END-PUBLIC-VERDICT>>>` line. Only the unchanged text between them is
  eligible for posting, including its attribution header and bold verdict.
  Text outside the block is held in a 0600 private file. A lens that ignores
  the format is retried once, then falls back locally; its reply is never
  posted as-is. Text placed inside a valid block is public, regardless of its
  wording, so the lens must keep out-of-scope details outside it or omit them.
- Writes `<out-dir>/<lens>.verdict.md` (the public block's attribution header,
  bold verdict token and findings: the form `post_lens_verdict.py` reads), plus
  `<lens>.brief.md`, per-attempt JSON, and `summary.json` (provider, duration,
  Fly run id, reviewed head, attempts, verdict).
- Posts nothing unless `--post`, which then calls `post_lens_verdict.py` per
  verdict (it refuses if the PR is no longer at the reviewed head). Without
  `--post`, post by hand with that script, using the head from `summary.json`.
- `--extra-brief` appends your file to every lens brief (round notes, prior
  findings to re-check).

## Output location

`--out-dir` defaults to `~/.ateles-work/fly-lens-review/<repo>-<pr>-<head8>-<time>`
(never the current directory, which may be the deployment checkout). The path is
printed first. `summary.json` is rewritten after every lens, so an interrupt
keeps the partial results. A verdict whose head moved is renamed
`<lens>.verdict.md.stale` so it cannot be mistaken for a delivered one.

## Exit codes

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Every requested lens delivered (and, with `--post`, posted) | Nothing |
| 2 | Usage error, or the PR head could not be read | Fix the arguments |
| 3 | At least one `FALL BACK TO LOCAL LENS: <lens> (<reason>)` line | Run exactly those lenses locally and post as usual; delivered lenses keep their verdicts |
| 4 | Verdicts delivered but a `--post` failed (`POST FAILED: <lens> (...)` line) | The verdict is NOT on the PR; post it by hand (below) or re-run if the head moved |

When both 3 and 4 apply, the exit is 3; read every line. Exit 0 with `--post`
means the verdicts are on the PR.

Reasons in the fallback line: `queue wait` (the 60 minute queue limit, passed to
the Fly runner as `APIS_FLY_QUEUE_MAX_WAIT_SECONDS`; set that variable first to
use another value), `refusal` (the router will not run it), `deterministic`
(a failure a second job cannot cure), `failed 2 times`, `STALE` and
`not run: PR head moved`.

Manual post of a delivered verdict:

    python3 execution/scripts/post_lens_verdict.py --repo <owner/name> --pr <n> \
        --lens <lens> --body-file <out-dir>/<lens>.verdict.md --head <reviewed head>

If the head moved, the lens must be re-run; do not post it.

## Fallback rule

Run the lens locally as a subagent from its `agent_definition` and post as
usual whenever the command says so. Do not retry by hand beyond the one
automatic retry. Fall back locally, without retrying, on these known failures:

- **qa and ux 403 grant gap.** The job cannot write its result; it appears as
  "no result recorded for run ..." (exit 95). When the dispatch output says 403
  or exit 95 the command treats it as `deterministic` and does not spend a
  second job. If the output does not say so, it looks mechanical and is retried
  once; that retry is wasted, so run qa and ux locally.
- **Slow hosted Neotoma.** Errors such as "unreachable" or "agent_definition
  unavailable (timeout)" mean the job could not load its role. Retrying rarely
  helps while Neotoma is slow; fall back locally.

## Limits (approved)

45 minutes per job, 200 jobs per day, one job per provider at a time, 60
minutes maximum queue wait, $20 per month. The Fly runner
(`execution/daemons/apis/fly_runner.py`) enforces them; the command relies on
that rather than re-implementing it.

## Known limitations

- Claude and Codex can run on Fly with Neotoma MCP access through the loopback
  signing proxy when the role has an admitted AAuth key and the required tool
  grant. Codex receives the proxy URL through CLI MCP overrides; no static
  bearer is supplied. A seated read-only review may run without Neotoma tools.
  Cursor remains unavailable on Fly because its CLI cannot deny one MCP tool.
  Provider headroom and role admission can still refuse an individual job.
- Roughly a third of first runs ended with "no result recorded for run ..."
  (unknown outcome, 70 to 170 seconds); the one retry usually clears it.
- Seated Fly reviews without a role's Neotoma tool grant use the repository
  prompt mirror `.claude/skills/<role>/SKILL.md`; changes present only in
  Neotoma are not visible to those jobs until mirrored.
- The Fly job holds a read-only GitHub token for `--repo`; it cannot comment or
  approve, which is why posting is a separate step.
- It needs the Neotoma env pair in the environment and a Neotoma task id (the
  dispatch rule requires one).
