"""Which environment-variable names may pass, declared once.

Two places hand an environment on and must never hand on a credential: the
repo-root ``conftest.py`` (the environment every test, and every process a
test starts, sees) and ``execution/scripts/render_daemon_plist.py`` (the
``EnvironmentVariables`` of an installed launchd job). Both use THIS list as
an allowlist: a name passes only when it is declared here AND is not
credential-named (``lib/credential_env_names.py``). Everything else is dropped
by the test scrub and refused by the renderer, which names the key and never
shows its value.

An allowlist fails closed. A new way of carrying authentication through the
environment (another git configuration channel, a proxy variable, a helper
program, a socket) is not on the list, so it is dropped without anyone having
to recognise it first. The cost is that a daemon or test that genuinely needs
a new setting must declare it here; the renderer's refusal and the scrub's
removal both name the key, so the missing declaration is visible.

What is on the list:

* ``PLATFORM_NAMES``: what any process needs to run (search path, home and
  user, temporary directory, locale, terminal, time zone, the macOS developer
  tool selection, XDG data/state/cache locations).
* ``PYTHON_NAMES``: interpreter and virtual-environment settings.
* ``TEST_RUNNER_NAMES``: names pytest manages itself, and the CI markers.
* ``DAEMON_CONFIG_NAMES``: every non-credential setting the daemons and their
  shared libraries read, derived from their source and the tracked plist
  templates (a test keeps this in step with the code).

Deliberately NOT on the list, because they carry or redirect authentication:
every ``GIT_*`` name, ``XDG_CONFIG_HOME`` (git and other tools read their
configuration, credentials included, from it), proxy variables, agent
sockets, and any credential-named variable.

Stdlib-only with no package ``__init__``, like its sibling modules.
"""

from __future__ import annotations

from lib.credential_env_names import is_credential_env_name

PLATFORM_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        "TERM",
        "COLUMNS",
        "LINES",
        "NO_COLOR",
        "FORCE_COLOR",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "__CF_USER_TEXT_ENCODING",
        "DEVELOPER_DIR",
        "SDKROOT",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)

PYTHON_NAMES: frozenset[str] = frozenset(
    {
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONWARNINGS",
        "VIRTUAL_ENV",
    }
)

TEST_RUNNER_NAMES: frozenset[str] = frozenset(
    {
        "PYTEST_CURRENT_TEST",
        "PYTEST_VERSION",
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_WORKSPACE",
        "RUNNER_OS",
        "RUNNER_TEMP",
    }
)

DAEMON_CONFIG_NAMES: frozenset[str] = frozenset(
    {
        "APIS_A2A_ENABLE",
        "APIS_A2A_HOST",
        "APIS_A2A_PORT",
        "APIS_A2A_PUBLIC_URL",
        "APIS_A2A_REQUIRE_AUTH",
        "APIS_A2A_TASK_VISIBILITY",
        "APIS_ALLOW_METERED_HARNESS",
        "APIS_APPROVAL_MERGE_METHOD",
        "APIS_APPROVAL_TRIGGERS_MERGE",
        "APIS_APPROVED_STALE_DAYS",
        "APIS_APPROVE_EMAIL_URL",
        "APIS_APPROVE_RELEASE_URL",
        "APIS_AUTONOMY_AUTO_MERGE",
        "APIS_AUTO_EXECUTE",
        "APIS_CHECKPOINT_DENIAL_DIR",
        "APIS_CHECKPOINT_PRODUCER_ISS",
        "APIS_CHECKPOINT_PRODUCER_JKT",
        "APIS_CHECKPOINT_REQUIRED_APPROVER_JKT",
        "APIS_CHECKPOINT_REQUIRED_APPROVER_SUB",
        "APIS_CLAUDE_BIN",
        "APIS_CODEX_BIN",
        "APIS_CURSOR_BIN",
        "APIS_DEFERRED_REVIEW_SWEEP_SECONDS",
        "APIS_DISPATCH_TIMEOUT",
        "APIS_DRY_RUN",
        "APIS_GITHUB_WEBHOOK_PORT",
        "APIS_HARNESS_COOLDOWN_SECONDS",
        "APIS_HARNESS_HEADROOM",
        "APIS_HARNESS_HEADROOM_FILE",
        "APIS_HARNESS_MIN_HEADROOM",
        "APIS_HARNESS_PROVIDERS",
        "APIS_HARNESS_USAGE_FILE",
        "APIS_HARNESS_USAGE_MAX_AGE_SECONDS",
        "APIS_LIMIT_RESET_DEFAULT_SECONDS",
        "APIS_MAX_CONCURRENT_ISSUE_PIPELINES",
        "APIS_MAX_FIX_ROUNDS",
        "APIS_MAX_TASK_ATTEMPTS",
        "APIS_MISSING_LENS_MAX_RETRIES",
        "APIS_OPERATOR_LOGIN",
        "APIS_PANEL_MAX",
        "APIS_READINESS_GATE",
        "APIS_READINESS_THRESHOLD",
        "APIS_RECONCILE_ENABLED",
        "APIS_RECONCILE_GRACE_SECONDS",
        "APIS_RECONCILE_INTERVAL_SECONDS",
        "APIS_RECONCILE_MAX_PER_SWEEP",
        "APIS_RECONCILE_PAGES_PER_SWEEP",
        "APIS_RECONCILE_QUERY_LIMIT",
        "APIS_RELEASE_PUSH_REF",
        "APIS_RESUME_REPOSITORIES",
        "APIS_RETRY_BACKOFF_BASE_SECONDS",
        "APIS_RETRY_BACKOFF_CAP_SECONDS",
        "APIS_REVIEW_MODELS",
        "APIS_REVIEW_STALL_MAX_RETRIES",
        "APIS_REVIEW_STALL_SECONDS",
        "APIS_REVISION_MAX_RETRIES",
        "APIS_REVISION_STALE_SECONDS",
        "APIS_RUN_CONVERSATIONS",
        "APIS_RUN_EMAIL",
        "APIS_STALL_SECONDS",
        "APIS_UNREADABLE_ATTEMPTS",
        "APIS_UNROUTABLE_LEDGER",
        "APIS_UNROUTABLE_REASSERT_SECONDS",
        "APIS_UNROUTABLE_WINDOW_SECONDS",
        "APIS_USAGE_GATE",
        "APIS_USAGE_GATED_PROVIDERS",
        "APIS_USAGE_PROBE",
        "APIS_USAGE_PROBE_CODEX_MODEL",
        "APIS_USAGE_PROBE_CURSOR_MODEL",
        "APIS_USAGE_PROBE_MODEL",
        "APIS_WATCHDOG_INTERVAL_SECONDS",
        "APIS_WATCHDOG_QUERY_LIMIT",
        "APUS_PORT",
        "AQUILA_TIMEOUT_SECONDS",
        "ATELES_AAUTH_KEYS_DIR",
        "ATELES_AGENT_GIT_EMAIL",
        "ATELES_AGENT_GIT_NAME",
        "ATELES_DEDUPE_JOURNAL_PATH",
        "ATELES_DIGEST_QUEUE_PATH",
        "ATELES_DISPATCH_FAILURE_LOG_DIR",
        "ATELES_DISPATCH_FAILURE_NOTIFY_WINDOW",
        "ATELES_DOTENV_FILE",
        "ATELES_EMAIL_DOMAIN",
        "ATELES_FOUNDATION_ROOT",
        "ATELES_GMAIL_SEND_CMD",
        "ATELES_LOG_BACKUP_COUNT",
        "ATELES_LOG_MAX_BYTES",
        "ATELES_MAIL_AUTHSERV_ID",
        "ATELES_MIRROR_REBUILD_SCRIPT",
        "ATELES_NOTIFY_EMAIL",
        "ATELES_NOTIFY_TO",
        "ATELES_PLAN_ENTITY_ID",
        "ATELES_PRIVATE_KEYS_DIR",
        "ATELES_PRIVATE_REPO_PATH",
        "ATELES_REPO_PATH",
        "ATELES_REPO_ROOT",
        "ATELES_REVIEWER_APP_ID",
        "ATELES_REVIEWER_APP_INSTALLATION_ID",
        "ATELES_RUN_EMAIL_DIR",
        "ATELES_SECURITY_LENS_PROVIDER",
        "ATELES_SKIP_DOTENV",
        "ATELES_SWARM_AUTO_BUILD",
        "ATELES_SWARM_AUTO_REREVIEW",
        "ATELES_SWARM_COMBINED_REVIEW_PASS",
        "ATELES_SWARM_EMAIL",
        "ATELES_SWARM_GWS_CONFIG_DIR",
        "ATELES_SWARM_NARROW_REREVIEW",
        "CODEX_HOME",
        "CONFIDENCE_RUBRIC_ID",
        "COTINGA_CALENDAR_IDS",
        "CYPHORHINUS_POLL_TIMEOUT",
        "CYPHORHINUS_TELEGRAM_CHAT_ID",
        "DATA_DIR",
        "DUE_DATE_HYGIENE_ENABLED",
        "END_SKILL_ID",
        "EXECUTION_POLICY_DEFAULT_ID",
        "FORMICA_CLAUDE_BIN",
        "FORMICA_DISPATCH_TIMEOUT",
        "FORMICA_DRY_RUN",
        "FORMICA_TRIAGE_LABEL",
        "GORILLA_DRY_RUN",
        "GORILLA_LOOKBACK_DAYS",
        "GORILLA_NUDGE_AFTER_DAYS",
        "GORILLA_POLL_INTERVAL",
        "GORILLA_SUMMARY_HOUR",
        "GORILLA_SUMMARY_WEEKDAY",
        "GWS_CREDENTIALS_PATH",
        "MONEDULA_CONSENT_CHANNEL",
        "MONEDULA_DEAD_GATE_THRESHOLD",
        "MONEDULA_POLICY_ID",
        "MONEDULA_PROFILES",
        "NEOTOMA_AAUTH_ISS",
        "NEOTOMA_AAUTH_SUB",
        "NEOTOMA_AAUTH_VIA_CLI",
        "NEOTOMA_AGENT_CLAUDE_BIN",
        "NEOTOMA_AGENT_DISPATCH_TIMEOUT",
        "NEOTOMA_AGENT_DRY_RUN",
        "NEOTOMA_AGENT_REPO",
        "NEOTOMA_AGENT_TRIAGE_LABEL",
        "NEOTOMA_BASE_URL",
        "NEOTOMA_COMPANY_ENTITY_ID",
        "NEOTOMA_EVENT_LOOP_BLOCKED_MS",
        "NEOTOMA_FLY_APP",
        "NEOTOMA_FLY_MACHINE",
        "NEOTOMA_FORENSICS_BUDGET_SECONDS",
        "NEOTOMA_FORENSICS_DIR",
        "NEOTOMA_GITHUB_REPO",
        "NEOTOMA_HTTP_MAX_ATTEMPTS",
        "NEOTOMA_HTTP_PORT",
        "NEOTOMA_HTTP_RETRY_BASE_S",
        "NEOTOMA_LOCAL_CHECKOUT",
        "NEOTOMA_PROJECT_ROOT",
        "NEOTOMA_RC_DIR",
        "NEOTOMA_REPO_PATH",
        "NEOTOMA_REPO_ROOT",
        "NEOTOMA_SANDBOX_URL",
        "NEOTOMA_SSE_SUBSCRIPTION_ID",
        "NODE_BIN",
        "OPERATOR_EMAIL",
        "OPERATOR_NAME",
        "PHOENICURUS_ACTIONS_RUNS_URL",
        "PHOENICURUS_MAX_SPAWNS_PER_HEAD",
        "PHOENICURUS_MIN_COMMITS",
        "PHOENICURUS_NPM_PUBLISH_MODE",
        "PHOENICURUS_NPM_PUBLISH_POLL_S",
        "PHOENICURUS_NPM_PUBLISH_WAIT_S",
        "PHOENICURUS_PREPARE_TIMEOUT_S",
        "PHOENICURUS_PUBLISH_TIMEOUT_S",
        "PHOENICURUS_RELEASE_BRANCH",
        "PHOENICURUS_RELEASE_REPO",
        "PHOENICURUS_STALE_RELEASE_BLOCK_DAYS",
        "PHOENICURUS_STALE_RELEASE_REESCALATE_HOURS",
        "PRIORITY_RUBRIC_ENTITY_ID",
        "PYTHONPATH",
        "RECORD_MEETING_DIARIZE",
        "RECORD_MEETING_DIR",
        "RIPARIA_MAX_FETCH",
        "RIPARIA_POLL_INTERVAL",
        "SOPS_AGE_KEY_FILE",
        "SYLVIA_CALENDAR_LOOKAHEAD_DAYS",
        "SYLVIA_REMINDER_LOOKAHEAD_DAYS",
        "TELEGRAM_ALLOWED_USER_ID",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_TOPIC_COTINGA",
        "TELEGRAM_TOPIC_CYPHORHINUS",
        "TELEGRAM_TOPIC_MONEDULA",
        "TELEGRAM_TOPIC_PAYMENTS",
        "TELEGRAM_TOPIC_PHOENICURUS",
        "TELEGRAM_TOPIC_PICULET",
        "TELEGRAM_TOPIC_RELEASES",
        "TELEGRAM_TOPIC_SYLVIA",
        "TELEGRAM_TOPIC_TASKS",
        "TRANSCRIBE_BACKEND",
        "TURDUS_DRY_RUN",
        "TURDUS_MAX_MESSAGES",
        "TURDUS_POLL_INTERVAL",
        "TYTO_ANALYZE_ENABLED",
        "TYTO_ANALYZE_MEETING_SKILL",
        "TYTO_ANALYZE_NEOTOMA_SKILL",
        "TYTO_NATIVE_RECORDINGS_DIR",
        "TYTO_POLL_INTERVAL",
        "TYTO_RECORDINGS_DIR",
        "TYTO_RECORDING_SETTLE_SECS",
        "TYTO_RETRY_MAX_ATTEMPTS",
        "TYTO_RETRY_MAX_DELAY_SECS",
        "TYTO_SCREENSHOTS_DIR",
        "TYTO_TRANSCRIBE_ENABLED",
        "TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS",
        "TYTO_TRANSCRIBE_SCRIPT",
        "TYTO_VOICE_MEMOS_DIR",
        "TYTO_VOICE_MEMO_INCLUDE_QTA",
        "TYTO_VOICE_MEMO_MAX_AGE_SECS",
        "TYTO_VOICE_MEMO_RETRY_SECS",
        "TYTO_VOICE_MEMO_RETRY_STATE",
        "VIRTUAL_ENV",
        "XDG_STATE_HOME",
    }
)

ALLOWED_ENVIRONMENT_NAMES: frozenset[str] = (
    PLATFORM_NAMES | PYTHON_NAMES | TEST_RUNNER_NAMES | DAEMON_CONFIG_NAMES
)


def is_allowed_environment_name(name: str) -> bool:
    """True when *name* is declared here and is not credential-named."""
    return name in ALLOWED_ENVIRONMENT_NAMES and not is_credential_env_name(name)


def plan_env_scrub(environ) -> tuple[list[str], dict[str, str]]:
    """What the test scrub must do to *environ*: ``(names_to_remove,
    values_to_set)``. Every name that is not allowed is removed; nothing is
    rewritten (the second element stays empty and is kept so callers can
    apply a plan uniformly)."""
    return sorted(n for n in list(environ) if not is_allowed_environment_name(n)), {}
