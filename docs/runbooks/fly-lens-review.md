# Run a lens review on Fly

Operator ruling 2026-11-12 (agent_policy `ent_161873dfe2a269554d60187f`): lens
reviews run on Fly first. This is the one supported command for a session lead
or a subagent. A local subagent lens is the fallback, not the default.

## The command

Run it from the Ateles deployment checkout, with the operator's Neotoma env pair
sourced into the program (`set -a`, source the Neotoma env file, `set +a`; the
Neotoma env file is `~/.config/neotoma/.env`). Never print or read that file.

```sh
cd ~/ateles-rc-src
# source the Neotoma env file here with set -a / set +a, then:
.venv/bin/python execution/scripts/fly_lens_review.py \
    --repo <owner/name> --pr <n> --lens <pm|arch|security|qa|ux|all> \
    --task-entity-id ent_<neotoma task> \
    [--extra-brief round2-notes.md] [--out-dir <dir>] [--post]
```

Put the whole thing in a script file rather than an inline interpreter: the
credential-read hook refuses an inline interpreter whose source mentions the
env file.

Lens to role: pm=pavo, arch=waxwing, security=falco, qa=phoenicurus,
ux=accipiter. `all` runs them one after another.

## What it does

- Resolves the PR's current head and names it in each lens's brief. If the head
  moves while a lens runs, that verdict is marked STALE and treated as a
  fallback case, and the remaining lenses are not dispatched.
- Dispatches through `dispatch_role.py --runner fly --github-read
  --action-class lens_review:<lens>`, one Fly job at a time, with the 45 minute
  per-job limit.
- Retries once on a mechanical failure (unknown outcome, "no result recorded
  for run", a reply with no header and verdict line). A refusal, or a queue wait
  over the limit, is not retried.
- Writes `<out-dir>/<lens>.verdict.md` (attribution header line, bold verdict
  token line, findings: the form `post_lens_verdict.py` reads), plus
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

- Every job so far routed to the claude provider. `--provider claude` is
  refused as not pinnable, codex is refused because the Fly runner cannot give
  it Neotoma tools, and cursor has no headroom.
- Roughly a third of first runs ended with "no result recorded for run ..."
  (unknown outcome, 70 to 170 seconds); the one retry usually clears it.
- Fly jobs cannot read Neotoma. Each lens uses the repository prompt mirror
  `.claude/skills/<role>/SKILL.md`, so a lens prompt that exists only in
  Neotoma is not seen until it is mirrored.
- The Fly job holds a read-only GitHub token for `--repo`; it cannot comment or
  approve, which is why posting is a separate step.
- It needs the Neotoma env pair in the environment and a Neotoma task id (the
  dispatch rule requires one).
