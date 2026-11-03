# Automatic rendered-page links

The agent publishing workflow uses `execution/scripts/publish_rendered_page.py`
for approved guest publications. It publishes through Neotoma, registers or
reuses a readable `/p/<slug>/` alias, rebuilds the existing Pages site when
necessary, and returns the shortened URL only after verifying the served alias.
Native Neotoma `publish_rendered_page` MCP calls and `/rendered-pages/publish`
HTTP calls still return their original long URLs; this helper is the owning
agent entrance, not a server hook.

Resolve the owning `deployment_configuration` at runtime. With the existing
Neotoma environment and authenticated `gh` identity, invoke:

```sh
python3 execution/scripts/publish_rendered_page.py --configuration "$DEPLOYMENT_CONFIGURATION_ID"
```

Supply JSON on stdin, using an in-memory subprocess input rather than a shell
argument, shell history, temporary file or logged command. An existing-page
request has this shape (all examples are synthetic):

```json
{"audience":"guest","entity_id":"ent_aaaaaaaaaaaaaaaaaaaaaaaa"}
```

Inline creation accepts `title`, `html_body`, optional `custom_css` and
`meta_description`, and requires a stable caller `idempotency_key`. Reuse the
same key and exact content after an interrupted creation; Neotoma reuses its
created entity while issuing a fresh page-scoped token. For an existing entity, update and read back its content before publishing;
combining `entity_id` with inline content is rejected because the native service
ignores inline fields on that path. An optional `slug`
chooses the initial alias. Existing registered aliases survive title changes;
collisions never replace another page's alias.

Guest publication must already be approved under the caller's publication,
access and sending rules. Keep authenticated-member access as the default;
never mint guest access merely to shorten a URL. A member request
`{"audience":"member","member_url":"https://members.example/page"}` returns
the supplied member URL without publication or alias effects; its caller must
verify the authenticated recipient path. Any native conformance warning blocks
sharing until corrected; output retains warning codes and severity but omits
potentially sensitive free-form messages.

Stdout is one JSON result. `alias_status: verified` contains the shortened
`share_url` (exit 0). `fallback` contains only the independently HTTP-200-verified
original URL, a stable `error_code` and outstanding work (exit 2); keep that work
owned and retry or reconcile it before claiming alias completion. `blocked`
returns a stable error code without a URL (exit 1). An existing pending
transaction is completed or held before a new page is published. Never share a
predicted short URL or disregard a blocked result. Treat successful result URLs
as sensitive: their own destination token appears in `t`, not in canonical
configuration, diagnostic messages or persistence.

## Runtime binding and initial import

The optional `deployment_configuration.short_links` object has this contract:

```json
{
  "version": 1,
  "publisher_host": "configured-hostname",
  "repository": "example/site",
  "github_host": "github.example",
  "branch": "main",
  "workflow": "deploy.yml",
  "template_path": "react-app/scripts/short-link-template.json",
  "secret_name": "PAGE_LINKS_JSON",
  "secret_updated_at": "verified-existing-secret-updated-at",
  "expected_github_login": "publisher",
  "expected_user_id": "authenticated-graph-owner-id",
  "site_origin": "https://site.example",
  "target_origin": "https://records.example",
  "registry": {"existing-page":"ent_cccccccccccccccccccccccc"}
}
```

`branch` must equal the enclosing `deploy_branch` and the repository's current
default branch. Origins must be HTTPS origins without path/query/credentials;
`target_origin` must equal the configured Neotoma HTTP origin. The helper checks
`gh api user` and Neotoma `/me.user_id` against the configured identities.
Neotoma entity reads are owner-scoped by the service and do not expose user_id.
Configuration IDs, identities, hostnames, origins and aliases come from private
runtime data, never public source or prompt literals.

Before first use, import the **complete known existing** token-free secret map
into `registry` and its freshly verified GitHub metadata into `secret_updated_at`.
GitHub cannot return encrypted secret contents: do not assume an empty map or
reconstruct it from a subset. If an existing authoritative copy cannot be
proved, hold initialization. The helper exports the entire registry over stdin
to the existing Actions secret. It needs the operator's existing secret-write
capability; it creates no credential, grant, store, hosting resource or spend.

## Serialization and recovery limits

This supports one configured publisher host and one local OS account/lock
directory, with every writer to this registry/secret using the helper. It uses a
retained `fcntl` lock under `~/.config/ateles/rendered-page-links` and Neotoma's
`expected_version` compare-and-set. Reject other hosts; do not run competing
manual secret writers. Secret `updated_at` detects observed drift but GitHub's
encrypted-secret PUT has no compare-and-set. This is not distributed publisher
coordination, and timestamp metadata cannot exclude an uncoordinated race.

Token-free `pending` state binds an opaque request id, page id, input digest,
full registry/configuration digest and exact default-branch source SHA. The
helper preserves it through failures and verifies exact readback after every
canonical update. It never hands a later caller the pending page's result.
Pages runs are correlated by opaque id, confined to the default branch, and
reject stale source both before building and before deploying. Success must
match the bound SHA and remain terminal across three spaced reads; every
registry alias is then checked for HTTP 200, the complete approved HTML, redirect script and hash CSP against the shared
versioned JSON template fetched from `template_path` at the qualified source SHA.
Queued runs read the current private secret when they build.

An interrupted `registry_committed` transaction retries a secret write only
while the recorded metadata still matches. If the PUT happened but its metadata
receipt was not saved, it holds with `secret_changed_outside_publisher` rather
than overwrite an unknown remote value. The owning operator must inspect the
original effect evidence and complete authoritative registry, reconcile the
known secret value using the existing private configuration process, then CAS
update its verified metadata and pending phase. Missing evidence remains a hold.

`dispatch_requested` is saved before dispatch. Recovery only observes the
matching run and never blindly repeats an ambiguous dispatch. A crash before
sending, failed run, or stale-source rejection needs owning recovery: prove the
original run absent or terminal, preserve the full registry, choose the current
default-branch source, and CAS reset pending to `secret_updated` with a fresh
opaque request id and source SHA (recompute its binding digest if binding or
registry changed). No old-source workflow rerun is a recovery shortcut. These
ambiguous effect windows are safely held, not automatically repaired. Deploy
failure keeps the verified original fallback and outstanding work; it does not
claim successful short-link delivery.

Run the helper and shared capability tests with the CI-supported Python 3.13 locked development
environment: `uv run --frozen --extra dev python -m pytest execution/scripts/test_rendered_page_links.py lib/capabilities/ -q`.
Production qualification still requires the deployed site workflow, canonical
private binding, authorized native page and recipient browser/token checks.
