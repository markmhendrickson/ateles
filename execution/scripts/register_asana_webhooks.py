#!/usr/bin/env python3
"""
Register Asana webhooks for projects in source and target workspaces.

Webhooks notify the server immediately when tasks are created, updated, or deleted.

Usage:
    python execution/scripts/register_asana_webhooks.py [--webhook-url URL] [--workspace source|target|both]

Requirements:
    - Webhook server must be running and accessible
    - For local development, use ngrok to expose server
    - Webhook URL must be publicly accessible (HTTPS)
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def register_webhook(
    client: AsanaClientWrapper,
    workspace_gid: str,
    webhook_url: str,
    resource_gid: str,
    resource_type: str = "project",
) -> dict | None:
    """Register a webhook for a resource."""
    try:
        # Asana webhook API endpoint
        import requests

        headers = {
            "Authorization": f"Bearer {client._pat}",
            "Content-Type": "application/json",
        }

        # Asana expects payload wrapped in a top-level \"data\" object
        payload = {
            "data": {
                "resource": resource_gid,
                "target": webhook_url,
            }
        }

        # Register webhook
        response = requests.post(
            "https://app.asana.com/api/1.0/webhooks",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code == 201:
            webhook_data = response.json()
            webhook = webhook_data.get("data", {})
            print(f"  ✓ Registered webhook for {resource_type} {resource_gid[:8]}...")
            return webhook
        elif response.status_code == 400:
            error = response.json()
            if "already exists" in str(error).lower():
                print(
                    f"  - Webhook already exists for {resource_type} {resource_gid[:8]}..."
                )
                return None
            else:
                print(f"  ✗ Error registering webhook: {error}")
                return None
        else:
            print(
                f"  ✗ Failed to register webhook: {response.status_code} - {response.text}"
            )
            return None

    except Exception as e:
        print(f"  ✗ Error registering webhook: {e}")
        return None


def list_webhooks(client: AsanaClientWrapper, workspace_gid: str) -> list[dict]:
    """List existing webhooks for a workspace."""
    try:
        import requests

        headers = {
            "Authorization": f"Bearer {client._pat}",
        }

        params = {
            "workspace": workspace_gid,
        }

        response = requests.get(
            "https://app.asana.com/api/1.0/webhooks",
            headers=headers,
            params=params,
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            return data.get("data", [])
        else:
            print(f"Warning: Could not list webhooks: {response.status_code}")
            return []

    except Exception as e:
        print(f"Warning: Error listing webhooks: {e}")
        return []


def delete_webhook(client: AsanaClientWrapper, webhook_gid: str) -> bool:
    """Delete a webhook."""
    try:
        import requests

        headers = {
            "Authorization": f"Bearer {client._pat}",
        }

        response = requests.delete(
            f"https://app.asana.com/api/1.0/webhooks/{webhook_gid}",
            headers=headers,
            timeout=30,
        )

        return response.status_code == 200

    except Exception as e:
        print(f"Error deleting webhook: {e}")
        return False


def register_workspace_webhooks(
    client: AsanaClientWrapper,
    workspace_gid: str,
    workspace_name: str,
    webhook_url: str,
) -> int:
    """Register webhooks for all projects in a workspace."""
    print(
        f"\nRegistering webhooks for {workspace_name} workspace ({workspace_gid[:8]}...)..."
    )

    # Get all non-archived projects
    try:
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
    except Exception as e:
        print(f"Error fetching projects: {e}")
        return 0

    print(f"Found {len(projects)} projects")

    registered_count = 0

    for project in projects:
        project_gid = project.get("gid")
        project_name = project.get("name", "Unknown")

        # Add workspace parameter to webhook URL for identification
        webhook_url_with_workspace = f"{webhook_url}?workspace={workspace_gid}"

        webhook = register_webhook(
            client,
            workspace_gid,
            webhook_url_with_workspace,
            project_gid,
            f"project '{project_name}'",
        )

        if webhook:
            registered_count += 1

    print(f"\nRegistered {registered_count} webhooks for {workspace_name} workspace")
    return registered_count


def main():
    parser = argparse.ArgumentParser(description="Register Asana webhooks")
    parser.add_argument(
        "--webhook-url",
        type=str,
        required=True,
        help="Public webhook URL (e.g., https://your-domain.com/webhook/asana or ngrok URL)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        choices=["source", "target", "both"],
        default="both",
        help="Which workspace(s) to register webhooks for",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing webhooks instead of registering",
    )
    parser.add_argument(
        "--delete-all", action="store_true", help="Delete all existing webhooks"
    )

    args = parser.parse_args()

    try:
        config = AsanaConfig.from_env()

        if args.list:
            # List existing webhooks
            print("Listing existing webhooks...\n")

            source_client = AsanaClientWrapper.from_config_source(config)
            source_webhooks = list_webhooks(source_client, config.source_workspace_gid)
            print(f"Source workspace: {len(source_webhooks)} webhooks")
            for wh in source_webhooks:
                print(f"  - {wh.get('gid')}: {wh.get('target')}")

            target_client = AsanaClientWrapper.from_config_target(config)
            target_webhooks = list_webhooks(target_client, config.target_workspace_gid)
            print(f"\nTarget workspace: {len(target_webhooks)} webhooks")
            for wh in target_webhooks:
                print(f"  - {wh.get('gid')}: {wh.get('target')}")

            return

        if args.delete_all:
            # Delete all webhooks
            print("Deleting all webhooks...\n")

            source_client = AsanaClientWrapper.from_config_source(config)
            source_webhooks = list_webhooks(source_client, config.source_workspace_gid)
            for wh in source_webhooks:
                if delete_webhook(source_client, wh.get("gid")):
                    print(f"  ✓ Deleted webhook {wh.get('gid')}")

            target_client = AsanaClientWrapper.from_config_target(config)
            target_webhooks = list_webhooks(target_client, config.target_workspace_gid)
            for wh in target_webhooks:
                if delete_webhook(target_client, wh.get("gid")):
                    print(f"  ✓ Deleted webhook {wh.get('gid')}")

            print("\nAll webhooks deleted")
            return

        # Register webhooks
        total_registered = 0

        if args.workspace in ["source", "both"]:
            source_client = AsanaClientWrapper.from_config_source(config)
            count = register_workspace_webhooks(
                source_client, config.source_workspace_gid, "source", args.webhook_url
            )
            total_registered += count

        if args.workspace in ["target", "both"]:
            target_client = AsanaClientWrapper.from_config_target(config)
            count = register_workspace_webhooks(
                target_client, config.target_workspace_gid, "target", args.webhook_url
            )
            total_registered += count

        print("\n=== Registration Complete ===")
        print(f"Total webhooks registered: {total_registered}")
        print(
            f"\nWebhook server should be running and accessible at: {args.webhook_url}"
        )
        print("\nFor local development with ngrok:")
        print(
            "  1. Start webhook server: python execution/scripts/asana_webhook_server.py"
        )
        print("  2. Start ngrok: ngrok http 8080")
        print("  3. Use ngrok HTTPS URL as --webhook-url")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
