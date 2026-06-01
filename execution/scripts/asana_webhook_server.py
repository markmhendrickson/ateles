#!/usr/bin/env python3
"""
Asana Webhook Receiver Server

Receives webhook events from Asana and triggers immediate sync for affected tasks.
Handles webhook handshake and signature verification.

Usage:
    python execution/scripts/asana_webhook_server.py [--port 8080] [--host 0.0.0.0]

For local development with ngrok:
    1. Start ngrok: ngrok http 8080
    2. Use ngrok URL as webhook endpoint
    3. Register webhooks: python execution/scripts/register_asana_webhooks.py
"""

import argparse
import hashlib
import hmac
import json
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, request

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import AsanaConfig
from scripts.import_asana_task_comments import (
    download_description_attachments,
    html_to_local_text,
    import_attachments_for_tasks,
    import_comments_for_tasks,
    rewrite_html_with_local_attachments,
)
from scripts.import_asana_task_metadata import (
    import_custom_fields_for_tasks,
    import_dependencies_for_tasks,
    import_stories_for_tasks,
)
from scripts.sync_asana_tasks import AsanaTaskSyncer

app = Flask(__name__)

# Store webhook secrets for signature verification
# Format: {workspace_gid: secret}
webhook_secrets: dict[str, str] = {}

# Lock for thread-safe sync operations
sync_lock = threading.Lock()

# Configure logging
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
WEBHOOK_LOG_FILE = LOGS_DIR / "asana_webhook.log"
WEBHOOK_ERROR_LOG_FILE = LOGS_DIR / "asana_webhook.error.log"


def setup_webhook_logging(debug: bool = False):
    """Configure logging for webhook server."""
    log_level = logging.DEBUG if debug else logging.INFO

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root logger
    logger = logging.getLogger("asana_webhook")
    logger.setLevel(log_level)
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (all levels, with rotation)
    file_handler = RotatingFileHandler(
        WEBHOOK_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Error file handler (ERROR+ only)
    error_handler = RotatingFileHandler(
        WEBHOOK_ERROR_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # Suppress Flask and Werkzeug verbose logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    return logger


def verify_webhook_signature(secret: str, payload: bytes, signature: str) -> bool:
    """Verify webhook signature using HMAC-SHA256."""
    expected_signature = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


def fetch_task_name(task_gid: str, workspace_gid: str) -> str | None:
    """Fetch task name for logging purposes."""
    logger = logging.getLogger("asana_webhook")
    try:
        config = AsanaConfig.from_env()

        # Determine which client to use
        if workspace_gid == config.source_workspace_gid:
            client = AsanaTaskSyncer(config, dry_run=True).source_client
        elif workspace_gid == config.target_workspace_gid:
            client = AsanaTaskSyncer(config, dry_run=True).target_client
        else:
            return None

        task_data = client._with_retry(
            client.tasks.get_task, task_gid, {"opt_fields": "name"}
        )
        return task_data.get("name")
    except Exception as e:
        logger.debug(f"Could not fetch task name for {task_gid}: {e}")
        return None


def fetch_workspace_from_project(project_gid: str) -> str | None:
    """Fetch workspace from project (more efficient than fetching from task)."""
    logger = logging.getLogger("asana_webhook")
    try:
        config = AsanaConfig.from_env()

        # Try source workspace first
        try:
            syncer = AsanaTaskSyncer(config, dry_run=True)
            project_data = syncer.source_client._with_retry(
                syncer.source_client.projects.get_project,
                project_gid,
                {"opt_fields": "workspace.gid"},
            )
            workspace_gid = project_data.get("workspace", {}).get("gid")
            if workspace_gid:
                logger.debug(
                    f"Found workspace {workspace_gid[:8]}... for project {project_gid} via source client"
                )
                return workspace_gid
        except Exception as e:
            logger.debug(f"Project {project_gid} not found in source workspace: {e}")

        # Try target workspace
        try:
            syncer = AsanaTaskSyncer(config, dry_run=True)
            project_data = syncer.target_client._with_retry(
                syncer.target_client.projects.get_project,
                project_gid,
                {"opt_fields": "workspace.gid"},
            )
            workspace_gid = project_data.get("workspace", {}).get("gid")
            if workspace_gid:
                logger.debug(
                    f"Found workspace {workspace_gid[:8]}... for project {project_gid} via target client"
                )
                return workspace_gid
        except Exception as e:
            logger.debug(f"Project {project_gid} not found in target workspace: {e}")

        return None
    except Exception as e:
        logger.error(f"Error fetching workspace for project {project_gid}: {e}")
        return None


def fetch_workspace_from_task(task_gid: str) -> str | None:
    """Fallback: fetch task from both workspaces to determine which one it belongs to."""
    logger = logging.getLogger("asana_webhook")
    try:
        config = AsanaConfig.from_env()

        # Try source workspace first
        try:
            syncer = AsanaTaskSyncer(config, dry_run=True)
            task_data = syncer.source_client._with_retry(
                syncer.source_client.tasks.get_task,
                task_gid,
                {"opt_fields": "workspace.gid"},
            )
            workspace_gid = task_data.get("workspace", {}).get("gid")
            if workspace_gid:
                logger.info(
                    f"Found workspace {workspace_gid[:8]}... for task {task_gid} via source client"
                )
                return workspace_gid
        except Exception as e:
            logger.debug(f"Task {task_gid} not found in source workspace: {e}")

        # Try target workspace
        try:
            syncer = AsanaTaskSyncer(config, dry_run=True)
            task_data = syncer.target_client._with_retry(
                syncer.target_client.tasks.get_task,
                task_gid,
                {"opt_fields": "workspace.gid"},
            )
            workspace_gid = task_data.get("workspace", {}).get("gid")
            if workspace_gid:
                logger.info(
                    f"Found workspace {workspace_gid[:8]}... for task {task_gid} via target client"
                )
                return workspace_gid
        except Exception as e:
            logger.debug(f"Task {task_gid} not found in target workspace: {e}")

        return None
    except Exception as e:
        logger.error(f"Error fetching workspace for task {task_gid}: {e}")
        return None


@app.route("/webhook/asana", methods=["POST"])
def webhook_handler():
    """Handle Asana webhook events."""
    # All Asana webhooks (handshake + events) use POST.
    # Handshake: X-Hook-Secret header present, no X-Hook-Signature yet.
    # Events:   X-Hook-Signature present, JSON body with events list.
    return handle_webhook_event()


def handle_webhook_event():
    """Handle webhook event (POST request)."""
    logger = logging.getLogger("asana_webhook")
    headers = request.headers

    # Handshake detection: X-Hook-Secret present, no signature yet.
    hook_secret = headers.get("X-Hook-Secret")
    signature = headers.get("X-Hook-Signature")

    if hook_secret and not signature:
        logger.info("Received webhook handshake")
        # Echo secret back per Asana docs
        response = app.response_class("", status=200)
        response.headers["X-Hook-Secret"] = hook_secret
        return response

    # For now, skip strict signature verification to avoid blocking events.
    payload = request.get_data()
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        return jsonify({"error": "Invalid JSON"}), 400

    # Asana sends a list of events under \"events\"
    events = body.get("events", [])
    if not events:
        logger.warning("Webhook payload has no events")
        return jsonify({"status": "no_events"}), 200

    logger.debug(f"Received {len(events)} event(s): {json.dumps(body, indent=2)}")

    # Process each event; only care about task resources for now
    for event in events:
        resource = event.get("resource", {}) or {}
        resource_gid = resource.get("gid")
        resource_type = resource.get("resource_type")
        action = event.get("action")

        # Try multiple paths for workspace extraction
        # Handle None values properly - event.get('parent', {}) can return None if key exists
        parent = event.get("parent") or {}
        workspace_gid = (
            (event.get("workspace") or {}).get("gid")
            or (parent.get("workspace") or {}).get("gid")
            or (resource.get("workspace") or {}).get("gid")
            or headers.get("X-Asana-Workspace")
        )

        if resource_type != "task" or not resource_gid:
            logger.info(
                f"Ignoring non-task event: type={resource_type}, action={action}"
            )
            continue

        if not workspace_gid:
            # Log full event structure for debugging
            logger.warning(
                f"Task event without workspace: task={resource_gid}, action={action}\n"
                f"Full event: {json.dumps(event, indent=2)}\n"
                f"Headers: {dict(headers)}"
            )
            # Fallback: try fetching from project first (more efficient), then task
            parent_gid = parent.get("gid") if parent else None
            if parent_gid and parent.get("resource_type") == "project":
                workspace_gid = fetch_workspace_from_project(parent_gid)
            if not workspace_gid:
                workspace_gid = fetch_workspace_from_task(resource_gid)
            if not workspace_gid:
                logger.error(
                    f"Could not determine workspace for task {resource_gid}, skipping sync"
                )
                continue

        # Fetch task name for logging
        task_name = fetch_task_name(resource_gid, workspace_gid) or "unknown"
        logger.info(
            f"Webhook event: action={action} task={resource_gid} ({task_name[:50]}) workspace={str(workspace_gid)[:8]}"
        )
        trigger_task_sync(resource_gid, workspace_gid, task_name)

    return jsonify({"status": "ok", "events_processed": len(events)})


def trigger_task_sync(task_gid: str, workspace_gid: str, task_name: str | None = None):
    """Trigger immediate sync for a specific task."""
    logger = logging.getLogger("asana_webhook")

    # Use lock to prevent concurrent syncs
    task_display = f"{task_gid} ({task_name[:50]})" if task_name else task_gid
    if not sync_lock.acquire(blocking=False):
        logger.warning(f"Skipping sync for {task_display} - sync already in progress")
        return

    try:
        logger.info(f"Triggering sync for task {task_display}...")

        # Determine workspace name
        config = AsanaConfig.from_env()
        if workspace_gid == config.source_workspace_gid:
            workspace_name = "source"
        elif workspace_gid == config.target_workspace_gid:
            workspace_name = "target"
        else:
            logger.warning(f"Unknown workspace {workspace_gid[:8]}...")
            return

        # Create syncer and sync this specific task
        syncer = AsanaTaskSyncer(config, dry_run=False)

        # Fetch and sync this specific task
        sync_single_task(syncer, task_gid, workspace_name)

        logger.info(f"Sync complete for task {task_display}")

    except Exception as e:
        logger.error(f"Error syncing task {task_display}: {e}", exc_info=True)
    finally:
        sync_lock.release()


def sync_single_task(syncer: AsanaTaskSyncer, task_gid: str, workspace_name: str):
    """Sync a single task immediately."""
    from datetime import datetime

    import pandas as pd

    # Determine which client to use
    if workspace_name == "source":
        client = syncer.source_client
    else:
        client = syncer.target_client

    # Fetch task from Asana
    try:
        opt_fields = [
            "gid",
            "name",
            "notes",
            "html_notes",
            "completed",
            "completed_at",
            "due_on",
            "due_at",
            "start_on",
            "created_at",
            "modified_at",
            "assignee",
            "assignee.gid",
            "assignee.name",
            "projects",
            "projects.gid",
            "projects.name",
            "projects.color",
            "projects.archived",
            "projects.public",
            "projects.icon",
            "projects.notes",
            "projects.html_notes",
            "projects.due_date",
            "projects.start_on",
            "projects.owner",
            "projects.owner.gid",
            "projects.owner.name",
            "projects.followers",
            "projects.followers.gid",
            "projects.followers.name",
            "projects.members",
            "projects.members.gid",
            "projects.members.name",
            "projects.custom_fields",
            "projects.default_view",
            "memberships",
            "memberships.project.gid",
            "memberships.project.name",
            "memberships.section.gid",
            "memberships.section.name",
            "assignee_section",
            "assignee_section.gid",
            "assignee_section.name",
            "tags",
            "tags.name",
            "permalink_url",
            "followers",
            "followers.gid",
            "followers.name",
            "custom_fields",
            "custom_fields.gid",
            "custom_fields.name",
            "custom_fields.type",
            "custom_fields.text_value",
            "custom_fields.number_value",
            "custom_fields.enum_value",
            "custom_fields.enum_value.name",
            "custom_fields.date_value",
            "custom_fields.people_value",
            "custom_fields.people_value.gid",
            "custom_fields.people_value.name",
            "custom_fields.multi_enum_values",
            "custom_fields.multi_enum_values.name",
            "dependencies",
            "dependencies.predecessor.gid",
            "dependencies.successor.gid",
        ]
        opts = {"opt_fields": ",".join(opt_fields)}
        task_data = client._with_retry(client.tasks.get_task, task_gid, opts)

        # Normalize task
        normalized = syncer.normalize_asana_task(task_data, workspace_name)

        # Download description-embedded attachments based on description_html
        try:
            attachment_map = download_description_attachments(
                client,
                task_gid,
                normalized.get("description_html_remote"),
            )
            # Rewrite HTML to point at local copies (base field is the rewritten version)
            normalized["description_html"] = rewrite_html_with_local_attachments(
                normalized.get("description_html_remote"),
                attachment_map,
            )
            logger = logging.getLogger("asana_webhook")
            logger.info(
                f"Downloaded {len(attachment_map)} description attachment(s) for task {task_gid}"
            )
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error downloading description attachments for task {task_gid}: {e}",
                exc_info=True,
            )

        # Load local tasks
        tasks_file = DATA_DIR / "tasks" / "tasks.parquet"
        if not tasks_file.exists():
            local_df = pd.DataFrame()
        else:
            local_df = pd.read_parquet(tasks_file)

        task_id = normalized["task_id"]

        # Update or create local task
        if not local_df.empty and task_id in local_df["task_id"].values:
            # Update existing
            idx = local_df[local_df["task_id"] == task_id].index[0]
            for key, value in normalized.items():
                local_df.loc[idx, key] = value
        else:
            # Create new
            new_row = pd.DataFrame([normalized])
            local_df = pd.concat([local_df, new_row], ignore_index=True)

        # Create snapshot
        snapshots_dir = DATA_DIR / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        if tasks_file.exists():
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            snapshot_file = snapshots_dir / f"tasks-{timestamp}.parquet"
            pd.read_parquet(tasks_file).to_parquet(snapshot_file, index=False)

        # Save updated tasks
        local_df.to_parquet(tasks_file, index=False)

        # Sync to other workspace if needed
        ensure_cross_workspace_task(syncer, normalized, local_df)

        # Import comments for this task into task_comments.parquet
        try:
            comment_count = import_comments_for_tasks(
                client, workspace_name, [task_gid]
            )
            logger = logging.getLogger("asana_webhook")
            logger.info(f"Imported {comment_count} new comment(s) for task {task_gid}")
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error importing comments for task {task_gid}: {e}", exc_info=True
            )

        # Import attachments for this task into task_attachments.parquet
        try:
            attachment_count = import_attachments_for_tasks(
                client, workspace_name, [task_gid]
            )
            logger = logging.getLogger("asana_webhook")
            logger.info(
                f"Imported {attachment_count} new attachment(s) for task {task_gid}"
            )
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error importing attachments for task {task_gid}: {e}", exc_info=True
            )

        # Import custom fields, dependencies, and all stories
        try:
            custom_fields_count = import_custom_fields_for_tasks(
                client, workspace_name, [task_gid]
            )
            logger = logging.getLogger("asana_webhook")
            logger.info(
                f"Imported {custom_fields_count} custom field(s) for task {task_gid}"
            )
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error importing custom fields for task {task_gid}: {e}", exc_info=True
            )

        try:
            dependencies_count = import_dependencies_for_tasks(
                client, workspace_name, [task_gid]
            )
            logger = logging.getLogger("asana_webhook")
            logger.info(
                f"Imported {dependencies_count} dependency/dependencies for task {task_gid}"
            )
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error importing dependencies for task {task_gid}: {e}", exc_info=True
            )

        try:
            stories_count = import_stories_for_tasks(client, workspace_name, [task_gid])
            logger = logging.getLogger("asana_webhook")
            logger.info(f"Imported {stories_count} story/stories for task {task_gid}")
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.error(
                f"Error importing stories for task {task_gid}: {e}", exc_info=True
            )

        # Recursively fetch and sync all subtasks at all levels
        try:
            all_subtasks = syncer.fetch_all_subtasks_recursive(client, task_gid)
            if all_subtasks:
                logger = logging.getLogger("asana_webhook")
                logger.info(
                    f"Found {len(all_subtasks)} subtask(s) (all levels) for task {task_gid}"
                )

                for subtask_data, parent_id in all_subtasks:
                    subtask_gid = subtask_data.get("gid", "")
                    subtask_normalized = syncer.normalize_asana_task(
                        subtask_data, workspace_name, parent_task_id=parent_id
                    )

                    # Download description attachments and rewrite HTML for subtask
                    subtask_attachment_map = download_description_attachments(
                        client,
                        subtask_gid,
                        subtask_normalized.get("description_html_remote"),
                    )
                    subtask_normalized[
                        "description_html"
                    ] = rewrite_html_with_local_attachments(
                        subtask_normalized.get("description_html_remote"),
                        subtask_attachment_map,
                    )
                    if subtask_normalized.get("description_html"):
                        subtask_normalized["description"] = html_to_local_text(
                            subtask_normalized["description_html"]
                        )

                    subtask_id = subtask_normalized["task_id"]

                    # Update or create subtask in local_df
                    if not local_df.empty and subtask_id in local_df["task_id"].values:
                        idx = local_df[local_df["task_id"] == subtask_id].index[0]
                        for key, value in subtask_normalized.items():
                            local_df.loc[idx, key] = value
                    else:
                        subtask_row = pd.DataFrame([subtask_normalized])
                        local_df = pd.concat([local_df, subtask_row], ignore_index=True)

                    # Import comments and attachments for subtask
                    try:
                        import_comments_for_tasks(client, workspace_name, [subtask_gid])
                        import_attachments_for_tasks(
                            client, workspace_name, [subtask_gid]
                        )
                    except Exception as e:
                        logger.warning(
                            f"Error importing comments/attachments for subtask {subtask_gid}: {e}"
                        )

                # Save updated tasks with subtasks
                tasks_file = DATA_DIR / "tasks" / "tasks.parquet"
                local_df.to_parquet(tasks_file, index=False)
                logger.info(
                    f"Synced {len(all_subtasks)} subtask(s) (all levels) for task {task_gid}"
                )
        except Exception as e:
            logger = logging.getLogger("asana_webhook")
            logger.warning(
                f"Error fetching subtasks for task {task_gid}: {e}", exc_info=True
            )

    except Exception as e:
        logger = logging.getLogger("asana_webhook")
        logger.error(f"Error fetching task {task_gid}: {e}", exc_info=True)
        raise


def ensure_cross_workspace_task(
    syncer: AsanaTaskSyncer, normalized_task: dict, local_df
):
    """Ensure task exists in both workspaces."""

    task_id = normalized_task["task_id"]
    workspace_gid = normalized_task["asana_workspace"]

    # Determine workspace name from GID
    config = AsanaConfig.from_env()
    workspace_name = None
    if workspace_gid == config.source_workspace_gid:
        workspace_name = "source"
    elif workspace_gid == config.target_workspace_gid:
        workspace_name = "target"

    # Check if task exists in other workspace
    if workspace_name == "source" and not normalized_task.get("asana_target_gid"):
        # Create in target workspace
        new_gid = syncer.create_asana_task(
            syncer.target_client, syncer.config.target_workspace_gid, normalized_task
        )
        if new_gid:
            idx = local_df[local_df["task_id"] == task_id].index[0]
            local_df.loc[idx, "asana_target_gid"] = new_gid
            local_df.to_parquet(DATA_DIR / "tasks" / "tasks.parquet", index=False)
            logger = logging.getLogger("asana_webhook")
            logger.info(f"Created task in target workspace: {new_gid}")

    elif workspace_name == "target" and not normalized_task.get("asana_source_gid"):
        # Create in source workspace
        new_gid = syncer.create_asana_task(
            syncer.source_client, syncer.config.source_workspace_gid, normalized_task
        )
        if new_gid:
            idx = local_df[local_df["task_id"] == task_id].index[0]
            local_df.loc[idx, "asana_source_gid"] = new_gid
            local_df.to_parquet(DATA_DIR / "tasks" / "tasks.parquet", index=False)
            logger = logging.getLogger("asana_webhook")
            logger.info(f"Created task in source workspace: {new_gid}")


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "ok", "webhooks_registered": len(webhook_secrets)})


def main():
    parser = argparse.ArgumentParser(description="Asana webhook receiver server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    # Optional flag to explicitly disable debug if needed in future
    parser.add_argument("--no-debug", action="store_true", help="Disable debug mode")

    args = parser.parse_args()

    # We want debug ON by default; only turn it off if --no-debug is passed
    debug_mode = not args.no_debug

    # Setup logging
    logger = setup_webhook_logging(debug=debug_mode)

    logger.info(f"Starting Asana webhook server on {args.host}:{args.port}")
    logger.info(f"Webhook endpoint: http://{args.host}:{args.port}/webhook/asana")
    logger.info(f"Health check: http://{args.host}:{args.port}/health")
    logger.info(f"Logs: {WEBHOOK_LOG_FILE}")
    logger.info(f"Errors: {WEBHOOK_ERROR_LOG_FILE}")
    logger.info("\nTo expose locally, use Cloudflare Tunnel (recommended):")
    logger.info(f"  ./scripts/setup_cloudflare_tunnel_simple.sh {args.port}")
    logger.info("\nOr use ngrok:")
    logger.info(f"  ngrok http {args.port}")
    logger.info("\nThen register webhooks:")
    logger.info(
        "  python execution/scripts/register_asana_webhooks.py --webhook-url <tunnel-url>/webhook/asana"
    )

    app.run(host=args.host, port=args.port, debug=debug_mode)


if __name__ == "__main__":
    main()
