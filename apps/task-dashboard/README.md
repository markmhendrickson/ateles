# Task Dashboard

A live view of `task` entities in Neotoma, so the operator can watch work
propagate into the swarm while talking to the orchestrating agent — instead of
the agent narrating every dispatch in chat.

Tasks are the central dispatch entity, so this is deliberately task-centric:
one screen, newest first, refreshing every 10 seconds.

## Running it

```bash
cd apps/task-dashboard
npm install
npm run dev
```

Then open the URL Vite prints (http://localhost:5273 by default). Hot reload is
on — edit anything under `src/` and the page updates without a restart.

## The token, and why there is a proxy

The dashboard reads Neotoma at `https://neotoma.markmhendrickson.com`, which
requires `NEOTOMA_BEARER_TOKEN`.

**A browser cannot safely hold that token.** It cannot read
`~/.config/neotoma/.env`, and putting the token in client-side JS would expose
it to anyone with devtools and bake it into any bundle we ever build.

So the token stays in the Node process:

```
browser ──GET /api/tasks (no credentials)──▶ Vite dev server
                                              │  attaches Authorization: Bearer …
                                              ▼
                                       Neotoma /entities/query
```

`server/neotomaProxy.ts` is a Vite dev-server middleware that owns the token and
forwards the query. The browser never sees it. The token is never logged, never
echoed back, and error responses deliberately stay generic rather than relaying
the upstream body (which can quote the request we sent).

The proxy reads the token from `NEOTOMA_BEARER_TOKEN` in the environment,
falling back to `~/.config/neotoma/.env` (where the operator's SOPS
materialization puts it). It extracts only the two `NEOTOMA_*` keys it needs —
that file holds unrelated secrets, so it is never loaded wholesale into
`process.env`. Set `NEOTOMA_BASE_URL` to point at a different instance.

> **Dev-only.** This is a dev-server middleware and exists only under
> `npm run dev`. `npm run build` produces a static bundle with no proxy and no
> data source. Deploying this as-is would require a real server-side component;
> don't put the token in a client bundle to get around that.

## What it shows

- The 200 most recently touched tasks, newest first.
- Per task: title, status, priority, assignee, and when it was last touched.
- Filter chips for each status bucket, with live counts.
- **Undispatched tasks are flagged** with an amber border and
  "unassigned — not dispatched": filed, but not routed to any agent. There is a
  dedicated filter for them, because it is the distinction that says whether
  work actually reached the swarm.
- Tasks appearing since the last poll flash briefly, so new work is visible
  without diffing the list by eye.
- Clicking a task opens its Neotoma entity page.

## Notes on the Neotoma API

Things worth knowing, each verified against prod rather than assumed:

- The read route is `POST /entities/query`. `POST /retrieve_entities` 404s —
  that path only exists behind the MCP layer.
- **Sorting: use `sort_by` / `sort_order`.** The plausible-looking `sort` and
  `order_by` are accepted and then *silently ignored* — no error, but results
  come back in entity-id order. That looks like real data while quietly not
  being recent-first, which is exactly the bug this dashboard would exist to
  avoid.
- Send `User-Agent: ateles-neotoma-sync/1.0`. Neotoma is behind Cloudflare,
  which 1010-blocks default library user-agents. Every daemon in this repo
  sends this same UA.
- Entity pages live at `/entities/<id>`. `/inspector/entities/<id>` 308-redirects.
- The timestamp to sort and display on is `last_observation_at` (falling back to
  `computed_at`). There is no `updated_at` on these rows.
- Statuses in prod are messier than the canonical four: `open`, `todo`,
  `completed`, `awaiting_input`, `canceled`, and
  `awaiting_release_confirmation` all occur alongside
  `pending`/`in_progress`/`done`/`blocked`. `src/tasks.ts` maps them into
  buckets; anything unrecognized lands in "Other" rather than vanishing.
- Many tasks have no `title`, only a `description` — and their `canonical_name`
  is a punctuation-stripped copy of that entire description, sometimes thousands
  of characters. The UI derives a headline from the first sentence instead.

## Running it: local dev, and the deployed instance

Two modes, and they do not conflict.

### Local dev (unchanged)

```
npm run dev --prefix apps/task-dashboard
```

Vite on port 5273 with HMR, no sign-in, reading Neotoma through the same
`/api/*` routes the deployed server exposes. This is still the loop for
iterating on the app.

### The deployed instance

The dashboard also runs on Fly, so its links resolve for the operator on any
device and survive his laptop being closed. Task links used to be
`http://localhost:5273/#/entities/...`, which died with the local process and
never resolved for anyone else in the first place.

`server/serve.ts` serves the built SPA plus those same routes, from the *same*
definition (`registerApiRoutes` in `server/neotomaProxy.ts`) — one route table,
two hosts, so dev and production cannot drift.

**Deploys are automatic**: a merge to `main` touching `apps/task-dashboard/**`
triggers `.github/workflows/deploy-task-dashboard.yml`, which waits for CI to be
green on that exact commit, deploys with `-c fly.dashboard.toml`, and then
verifies the deploy actually happened. The app name, hostname and region come
from repository secrets; the canonical binding lives in the
`deployment_configuration` entity in Neotoma, never in this public repo.

### Sign-in

Deployed, the dashboard is behind Google sign-in against an email allowlist —
the same mechanism Neotoma itself uses. Without it, a Fly URL would publish the
entire task graph to anyone who learned the address.

It **fails closed**: if `DASHBOARD_GOOGLE_CLIENT_ID`,
`DASHBOARD_GOOGLE_CLIENT_SECRET`, `DASHBOARD_APPROVED_EMAILS` or
`DASHBOARD_SESSION_KEY` is missing, the server refuses to serve anything rather
than serving it unprotected. `/healthz` is the only unauthenticated route (Fly's
health checker has no Google account) and exposes no graph data.

### What the post-deploy check proves

`flyctl deploy` exiting 0 is not evidence that anything shipped — Neotoma
shipped a month-old image twice while reporting success. So the workflow checks
the *running* instance:

- `/healthz` reports a `git_sha`, and it must equal the commit just deployed —
  this is what catches a silently stale build.
- `auth_configured` must be `true`, so a misconfigured deploy fails loudly.
- An anonymous `GET /` must not return the app, and an anonymous
  `GET /api/tasks` must not return data. Shipping the gate and the gate working
  are different claims, and only the second one matters.
