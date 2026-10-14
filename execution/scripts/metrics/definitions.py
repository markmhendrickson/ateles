"""The v1 ``metric_definition`` catalog (measurement design, section 3).

Each definition is built from a small spec so the shared defaults live in one
place. Rulings of 2026-10-13 encoded here:

* ``default_for_kinds`` is a fixed default metric per kind of work. It is
  stored with ``status: proposed``; the operator ratifies the mapping once and
  only then does ``status`` become ``ratified``. The kind names are proposals.
* Low-volume metrics are scored over pooled 4-week windows or 8-week horizons
  and name a leading indicator (``pooling``, ``leading_indicator``).
* Anything derived from people is ``aggregate_only``.
* Cost is measured in dispatch minutes per dispatch, a proxy (most dispatches carry no task id).

``baseline_status`` says what can run now:
``exists`` (history retrievable, a collector in this package produces it),
``capture`` (forward-only or waiting on a producer), ``awaiting_source``
(web, product-usage and newsletter keys are not minted yet).
"""

from __future__ import annotations

from typing import Any

from execution.scripts.metrics import schemas

POOLING_LOW_VOLUME = (
    "Low volume: score over pooled 4-week windows or an 8-week horizon, never a single week; "
    "name the leading indicator beside the headline metric."
)
POOLING_ENOUGH = "Weekly points are usable on their own; pool 4 weeks when the noise floor exceeds the expected change."
NOISE_RULE = "F = max(2 x MAD of baseline points, minimum detectable change)"

_COMMON: dict[str, Any] = {
    "cadence": "weekly",
    "min_points": 4,
    "baseline_window": "28d",
    "noise_floor_rule": NOISE_RULE,
    "coverage_ratio": 0.9,
    "aggregate_only": True,
    "max_horizon": "P8W",
    "strategy_weight": None,  # the operator sets weights through the strategy entity
    "status": "proposed",
}


def _spec(metric_id: str, title: str, description: str, source_system: str, artifact_kind: str,
          query: dict[str, Any], unit: str, polarity: str, kinds: list[str], *, baseline_status: str,
          baseline_note: str, collector: str = "", leading_indicator: str = "",
          pooling: str = POOLING_LOW_VOLUME, probe: dict[str, Any] | None = None,
          cadence: str | None = None) -> dict[str, Any]:
    values = dict(_COMMON)
    code = metric_id
    metric_id = f"{code}_{title}"
    if cadence:
        values["cadence"] = cadence
    values.update(
        metric_id=metric_id, title=title, description=description, source_system=source_system,
        artifact_kind=artifact_kind, query=query, unit=unit, polarity=polarity,
        default_for_kinds=kinds, baseline_status=baseline_status, baseline_note=baseline_note,
        collector=collector or None, leading_indicator=leading_indicator or None, pooling=pooling,
        known_positive_probe=probe,
    )
    return values


_NPM = "execution/scripts/metrics/repo_baseline.py"
_OPS = "execution/scripts/metrics/swarm_ops_baseline.py"
_AWAIT_USAGE = "Neotoma usage_digest emitter is not built and hosted Neotoma is unopened; forward-only once it runs."
_AWAIT_WEB = "The Umami Cloud API key is not minted yet; history retention is unverified."

SPECS: list[dict[str, Any]] = [
    # --- product usage (awaiting source) -------------------------------------
    _spec("P1", "activation_first_write",
          "Share of new hosted or sandbox instances with at least one durable write within 24h of creation.",
          "product_usage", "neotoma_instance",
          {"adapter": "A1a", "read": "usage_digest", "derive": "instances_with_write_24h / instances_created"},
          "ratio", "up_good", ["feature_onboarding", "feature_activation"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_USAGE,
          leading_indicator="quickstart CTA clicks (W2)"),
    _spec("P2", "second_session_return",
          "Share of activated instances active again within 7 days.",
          "product_usage", "neotoma_instance",
          {"adapter": "A1a", "read": "usage_digest", "derive": "returning_within_7d / activated"},
          "ratio", "up_good", ["feature_retention"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_USAGE,
          leading_indicator="activation_first_write (P1)"),
    _spec("P3", "weekly_active_instances",
          "Instances with at least one write in the week.",
          "product_usage", "neotoma_instance",
          {"adapter": "A1a", "read": "GET /stats + usage_digest", "derive": "count(instances with write in week)"},
          "instances", "up_good", ["growth"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_USAGE,
          leading_indicator="npm_downloads_weekly (D1)"),
    _spec("P4", "paid_subscriptions_net",
          "Net new paid subscriptions per week (hosted tier).",
          "product_usage", "billing",
          {"adapter": "A1b", "read": "stripe subscriptions", "derive": "new - cancelled per week"},
          "subscriptions", "up_good", ["pricing", "offer"],
          baseline_status="awaiting_source",
          baseline_note="No Stripe code exists; the operator mints a restricted read key once Checkout exists. Prove the "
                        "instrument with a Stripe test-mode event before believing a zero.",
          leading_indicator="checkout_conversion (P5)"),
    _spec("P5", "checkout_conversion",
          "Checkout sessions completed divided by started.",
          "product_usage", "billing",
          {"adapter": "A1b", "read": "stripe checkout sessions", "derive": "completed / started"},
          "ratio", "up_good", ["checkout_flow", "pricing_page"],
          baseline_status="awaiting_source",
          baseline_note="Waits on Stripe Checkout and a provider-minted restricted read key.",
          leading_indicator="pricing page visits (W1)"),
    _spec("P6", "anchor_users_stage1",
          "Count of anchor users meeting the Stage 1 bar of the two-stage value test. Operator-attested, not measured.",
          "product_usage", "people_attested",
          {"adapter": "attested", "read": "operator-attested entity", "derive": "count"},
          "users", "up_good", ["anchor_outreach"],
          baseline_status="awaiting_source",
          baseline_note="Judgement, not a measurement: label readings attested. No attestation entity yet."),
    # --- web and content (awaiting source) ------------------------------------
    _spec("W1", "site_unique_visitors_weekly",
          "Weekly unique visitors, per site.",
          "web", "site",
          {"adapter": "A2a", "read": "Umami Cloud", "derive": "unique visitors per site per week"},
          "visitors", "up_good", ["content", "seo", "launch_post"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_WEB,
          leading_indicator="page views of the changed page"),
    _spec("W2", "cta_to_install_rate",
          "Landing-page visits followed by an install or quickstart CTA click, per landing page.",
          "web", "landing_page",
          {"adapter": "A2a", "read": "Umami events cta_click, docs_nav_click", "derive": "clicks / visits"},
          "ratio", "up_good", ["landing_page_copy", "web_design"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_WEB,
          leading_indicator="cta_click count"),
    _spec("W3", "docs_depth",
          "Share of visits reaching a docs page past the first.",
          "web", "docs",
          {"adapter": "A2a", "read": "Umami events", "derive": "visits with 2+ docs pages / docs visits"},
          "ratio", "up_good", ["docs"],
          baseline_status="awaiting_source", baseline_note=_AWAIT_WEB,
          leading_indicator="docs_nav_click count"),
    _spec("W4", "newsletter_subscribers_net",
          "Net new newsletter subscribers per week.",
          "newsletter", "newsletter_list",
          {"adapter": "A2b", "read": "SendGrid contacts", "derive": "new - unsubscribed per week"},
          "subscribers", "up_good", ["newsletter_growth", "signup_form"],
          baseline_status="awaiting_source",
          baseline_note="Needs a stats-only SendGrid key (not minted yet) and a canonical subscriber list (not chosen yet)."),
    _spec("W5", "newsletter_click_rate",
          "Unique link clicks divided by delivered, per send.",
          "newsletter", "newsletter_send",
          {"adapter": "A2b", "read": "SendGrid stats", "derive": "unique clicks / delivered per send"},
          "ratio", "up_good", ["email_experiment", "newsletter_send"],
          baseline_status="awaiting_source", baseline_note="Same SendGrid key and canonical list as W4."),
    _spec("W6", "post_link_clicks",
          "Link clicks per post, per platform.",
          "social", "social_post",
          {"adapter": "A2c", "read": "Typefully analytics", "derive": "clicks per post"},
          "clicks", "up_good", ["social", "outreach_post"],
          baseline_status="awaiting_source",
          baseline_note="Typefully authorization is session-bound; a daemon needs its own grant."),
    # --- developer and repo ---------------------------------------------------
    _spec("D1", "npm_downloads_weekly",
          "Weekly downloads of the published package (Monday to Sunday, UTC); the latest day or two lag at the registry.",
          "repo", "package",
          {"adapter": "A3a", "endpoint": "https://api.npmjs.org/downloads/range/{start}:{end}/{package}",
           "derive": "sum of daily downloads per Monday-Sunday week"},
          "downloads", "up_good", ["release", "distribution", "package_docs"],
          baseline_status="exists",
          baseline_note="Weeks backfilled by repo_baseline.py. At today's volume the noise floor is about the size of the "
                        "median, so pool 4 weeks; the readings carry the numbers, not this note.",
          collector=_NPM, leading_indicator="quickstart CTA clicks (W2) and docs visits (W1)",
          probe={"expect": "last full week downloads > 0", "artifact": "npm:<package>"}),
    _spec("D2", "github_stars",
          "Weekly new stars still starred (value); cumulative stars at window end in detail.",
          "repo", "repository",
          {"adapter": "A3a", "endpoint": "GET /repos/{repo}/stargazers (Accept: application/vnd.github.star+json)",
           "derive": "count of starred_at per week"},
          "stars", "up_good", ["launch_announcement", "outreach_post"],
          baseline_status="exists",
          baseline_note="Star history backfilled from starred_at timestamps. Only stars that still exist are visible, so "
                        "an unstar removes a past star and a past week can shrink when the reading is re-run.",
          collector=_NPM, leading_indicator="repo unique visitors (D3)",
          probe={"expect": "cumulative stars > 0", "artifact": "github:<owner>/<repo>"}),
    _spec("D3", "repo_unique_visitors_clones",
          "Daily unique visitors and unique clones from the GitHub traffic API.",
          "repo", "repository",
          {"adapter": "A3b", "endpoint": "GET /repos/{repo}/traffic/views and /clones", "derive": "daily snapshot"},
          "visitors", "up_good", ["repo_readme", "launch_announcement"],
          baseline_status="awaiting_source",
          baseline_note="Forward-only (14-day retention at GitHub) and needs a token with traffic access; "
                        "the daily capture waits for a token decision. Every uncaptured day is lost.",
          cadence="daily"),
    _spec("D4", "external_issues_opened",
          "Issues (not pull requests) opened per week by authors outside the owner, bots and the swarm's agents.",
          "repo", "repository",
          {"adapter": "A3a", "endpoint": "GET /repos/{repo}/issues?state=all", "derive": "external authors per week"},
          "issues", "up_good", ["community", "support_docs"],
          baseline_status="exists",
          baseline_note="Backfilled weekly; detail carries total opened, closed and the exclusion list applied. "
                        "Collaborator and team accounts count as external unless listed as swarm logins.",
          collector=_NPM, leading_indicator="repo unique visitors (D3)"),
    _spec("D5", "external_contributors",
          "Distinct authors of merged pull requests over the trailing 28 days, read weekly, "
          "excluding the repository owner, bots and the swarm's agents.",
          "repo", "repository",
          {"adapter": "A3a", "endpoint": "GET /repos/{repo}/pulls?state=closed", "derive": "distinct merged authors in 28d"},
          "contributors", "up_good", ["contributor_onboarding"],
          baseline_status="exists",
          baseline_note="Pooled 28-day window by design (monthly metric read weekly). Collaborator and team accounts "
                        "count as external unless listed as swarm logins.",
          collector=_NPM, pooling="Already pooled over 28 days; score over an 8-week horizon."),
    _spec("D6", "directory_listing_presence",
          "Per MCP directory: listed yes or no, listed version, and any visible installs or rank.",
          "repo", "directory",
          {"adapter": "A3c", "read": "public directory pages or registry API", "derive": "per directory"},
          "directories", "up_good", ["directory_listing"],
          baseline_status="awaiting_source",
          baseline_note="Collector not built in this slice; the directory list is data on the definition."),
    # --- swarm operations (derived reads) ----------------------------------------
    _spec("S1", "time_to_completion",
          "Hours from a swarm task's first record to the first observation of status done, median per week of completion.",
          "swarm_ops", "swarm",
          {"read": "task observations", "derive": "first done - first observation, swarm tasks only",
           "kind_field": "action_type"},
          "hours", "down_good", ["workflow_change", "dispatch_change"],
          baseline_status="exists",
          baseline_note="Derived from task observations; the weekly median is the reading, per-kind medians are in detail. "
                        "Covers completed swarm tasks only (a task with routing metadata): open tasks and tasks first "
                        "recorded already done are excluded (the latter counted in detail), so the median is biased low.",
          collector=_OPS, pooling=POOLING_ENOUGH,
          probe={"expect": "tasks with a done transition in the last 4 weeks > 0"}),
    _spec("S2", "dispatch_failure_rate",
          "Share of completed dispatches whose success is not true, per week; per role in detail.",
          "swarm_ops", "swarm",
          {"read": "harness_event observations, event_type subprocess", "derive": "failed / completed dispatches"},
          "ratio", "down_good", ["reliability", "gate_change"],
          baseline_status="exists",
          baseline_note="Counts completion rows only (success true or false); start rows are not dispatches. A failure "
                        "with no duration (a launch failure) is a failed dispatch and is counted.",
          collector=_OPS, pooling=POOLING_ENOUGH,
          probe={"expect": "completed dispatches in the last full week > 0"}),
    _spec("S3p", "operator_intervention_proxy",
          "Interim proxy for S3: checkpoints whose status records a decision, per week (value); "
          "decision_shape_check events per week in detail.",
          "swarm_ops", "swarm",
          {"read": "checkpoint_brief and decision_shape_check observations",
           "derive": "checkpoints first reaching a decided status (approved, approved_no_release, rejected, redirected) per week"},
          "events", "down_good", ["automation"],
          baseline_status="exists",
          baseline_note="Proxy only. The record carries no resolver identity, so this is an upper bound on operator "
                        "interventions: decided statuses may include non-operator resolutions. Machine approvals "
                        "(auto-build, by-dispatch), pending_* states, superseded, resolved and prepared are not counted. "
                        "A week with no checkpoint record at all has no reading (absence is not zero); a week with "
                        "records and no decision is a measured zero. S3 proper needs write attribution by principal.",
          collector=_OPS, pooling=POOLING_ENOUGH,
          probe={"expect": "checkpoints decided in the last 8 weeks > 0"}),
    _spec("S3", "operator_interventions_per_task",
          "Writes attributed to the operator principal on a task after its first claim.",
          "swarm_ops", "swarm",
          {"read": "principal-attributed writes", "derive": "operator writes per task after first claim"},
          "writes_per_task", "down_good", ["automation"],
          baseline_status="capture",
          baseline_note="Not derivable today: needs principal attribution on writes (foundation work). Use S3p meanwhile."),
    _spec("S4", "review_rounds_per_pr",
          "Distinct head commits that received a formal review, mean per reviewed merged PR, per week of merge.",
          "swarm_ops", "pull_request",
          {"read": "GitHub pull requests and reviews", "derive": "distinct review commit_ids per merged PR"},
          "rounds", "down_good", ["review_process", "lens_change"],
          baseline_status="exists",
          baseline_note="Counts formal reviews only; lens verdicts posted as plain comments are not seen, so a "
                        "prediction about lens or review-process changes is scored on a metric blind to that work. "
                        "Reviews submitted after merge are not filtered out. PRs with no formal review are counted "
                        "in detail, not in the mean.",
          collector=_OPS, pooling=POOLING_ENOUGH,
          probe={"expect": "merged PRs with a formal review in the last 8 weeks > 0"}),
    _spec("S5", "cost_per_task",
          "Mean wall-clock minutes per completed, timed dispatch in the week. A proxy for cost, and not per task: "
          "most dispatches carry no task id. Dollar cost is not the marginal cost here. Tokens appear in detail "
          "only where a harness reported them; otherwise not metered.",
          "swarm_ops", "swarm",
          {"read": "harness_event observations, event_type subprocess",
           "derive": "sum(duration_ms)/60000 / timed dispatches"},
          "minutes_per_dispatch", "down_good", ["cost_efficiency", "model_tiering"],
          baseline_status="exists",
          baseline_note="Operator ruling 2026-10-13: minutes as a proxy. Dispatch count per task is not captured, so a "
                        "change that cuts minutes per dispatch while adding dispatches per task would look like an "
                        "improvement here. Per-task minutes appear in detail only where a task id exists (about 4% of "
                        "dispatches). Tokens are mostly unreported, never zero; the token figure in detail is a partial "
                        "sum over the dispatches that reported, next to how many did.",
          collector=_OPS, pooling=POOLING_ENOUGH,
          probe={"expect": "completed dispatches with a duration in the last full week > 0"}),
]


def build_definitions() -> list[dict[str, Any]]:
    seen: set[str] = set()
    records = []
    for spec in SPECS:
        if spec["metric_id"] in seen:
            raise ValueError(f"duplicate metric_id {spec['metric_id']}")
        seen.add(spec["metric_id"])
        records.append(schemas.build_record("metric_definition", **spec))
    return records
