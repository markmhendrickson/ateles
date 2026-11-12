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
  fallback case.
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

## Fallback rule

When Fly cannot deliver a lens, the command prints one line per lens and exits 3:

    FALL BACK TO LOCAL LENS: <lens> (<reason>)

Run exactly that lens as a local subagent from its `agent_definition` and post
as usual. Lenses that were delivered keep their verdicts. A stale verdict also
goes to the fallback. Exit 2 is a usage error or an unreadable PR head. Do not
retry by hand beyond the one automatic retry.

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
