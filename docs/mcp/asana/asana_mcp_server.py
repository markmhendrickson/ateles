#!/usr/bin/env python3
"""
MCP Server for Asana Integration

Provides tools for bidirectional sync, import, export, and metadata management
between Asana workspaces and local parquet data.
"""

import sys
from pathlib import Path
from typing import Any

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from client import AsanaClientWrapper
from config import AsanaConfig
from export_engine import AsanaExporter
from import_engine import AsanaImporter
from parquet_client import ParquetMCPClient
from sync_engine import AsanaTaskSyncer
from utils import format_result, handle_error

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Initialize MCP server
app = Server("asana")

# Global instances (initialized on first use)
_config: AsanaConfig | None = None
_parquet_client: ParquetMCPClient | None = None


def get_config() -> AsanaConfig:
    """Get or create Asana configuration."""
    global _config
    if _config is None:
        _config = AsanaConfig.from_env()
    return _config


def get_parquet_client() -> ParquetMCPClient:
    """Get or create parquet MCP client."""
    global _parquet_client
    if _parquet_client is None:
        _parquet_client = ParquetMCPClient()
    return _parquet_client


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available Asana tools."""
    return [
        Tool(
            name="import_asana_tasks",
            description=(
                "Import tasks from Asana workspace to local parquet. "
                "Fetches tasks from source workspace and intelligently merges with existing data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Workspace to import from (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    },
                    "only_incomplete": {
                        "type": "boolean",
                        "description": "Only fetch incomplete tasks",
                        "default": False,
                    },
                    "assignee_gid": {
                        "type": "string",
                        "description": "Filter tasks by assignee GID (optional)",
                    },
                    "max_tasks": {
                        "type": "integer",
                        "description": "Maximum number of tasks to import (optional)",
                    },
                    "recalculate": {
                        "type": "boolean",
                        "description": "Force recalculation of domain",
                        "default": False,
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include tasks from archived projects",
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="export_asana_tasks",
            description=(
                "Export local tasks to Asana workspace. "
                "Creates or updates tasks in target workspace. "
                "Can export specific tasks by ID or filter by sync_log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Workspace to export to (target)",
                        "enum": ["target"],
                        "default": "target",
                    },
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific task IDs to export (if provided, ignores limit and sync_log_filter)",
                        "default": None,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tasks to export (ignored if task_ids provided)",
                        "default": 10,
                    },
                    "sync_log_filter": {
                        "type": "string",
                        "description": "Filter tasks by sync_log value (e.g., 'pending_export') (ignored if task_ids provided)",
                        "default": None,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="sync_asana_tasks",
            description=(
                "Bidirectional sync between Asana workspaces and local parquet. "
                "Uses three-way merge for field-level reconciliation. "
                "Can sync specific tasks by ID or all tasks based on sync_scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific task IDs to sync (if provided, syncs only these tasks)",
                        "default": None,
                    },
                    "sync_scope": {
                        "type": "string",
                        "description": "Scope of sync: both, source, or target (ignored if task_ids provided)",
                        "enum": ["both", "source", "target"],
                        "default": "both",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without applying them",
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="import_asana_task_comments",
            description=(
                "Import comments (stories of type 'comment') for specific tasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_gids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task GIDs to import comments for",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    },
                },
                "required": ["task_gids"],
            },
        ),
        Tool(
            name="import_asana_task_metadata",
            description=(
                "Import task metadata: custom fields, dependencies, and stories."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_gids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task GIDs to import metadata for",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    },
                    "metadata_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["custom_fields", "dependencies", "stories"],
                        },
                        "description": "Types of metadata to import",
                        "default": ["custom_fields", "dependencies", "stories"],
                    },
                },
                "required": ["task_gids"],
            },
        ),
        Tool(
            name="register_asana_webhooks",
            description=(
                "Register Asana webhooks for projects in workspaces. "
                "Webhooks notify the webhook server when tasks are modified."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "webhook_url": {
                        "type": "string",
                        "description": "Public webhook URL (must be HTTPS)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace to register webhooks for",
                        "enum": ["source", "target", "both"],
                        "default": "both",
                    },
                },
                "required": ["webhook_url"],
            },
        ),
        Tool(
            name="get_asana_task",
            description=("Get a single task from Asana by GID."),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_gid": {"type": "string", "description": "Asana task GID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    },
                },
                "required": ["task_gid"],
            },
        ),
        Tool(
            name="list_asana_projects",
            description=("List projects in an Asana workspace."),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Workspace (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Include archived projects",
                        "default": None,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_asana_workspace_info",
            description=("Get information about an Asana workspace."),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Workspace (source or target)",
                        "enum": ["source", "target"],
                        "default": "source",
                    }
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "import_asana_tasks":
            return await handle_import_tasks(arguments)
        elif name == "export_asana_tasks":
            return await handle_export_tasks(arguments)
        elif name == "sync_asana_tasks":
            return await handle_sync_tasks(arguments)
        elif name == "import_asana_task_comments":
            return await handle_import_comments(arguments)
        elif name == "import_asana_task_metadata":
            return await handle_import_metadata(arguments)
        elif name == "register_asana_webhooks":
            return await handle_register_webhooks(arguments)
        elif name == "get_asana_task":
            return await handle_get_task(arguments)
        elif name == "list_asana_projects":
            return await handle_list_projects(arguments)
        elif name == "get_asana_workspace_info":
            return await handle_get_workspace_info(arguments)
        else:
            return handle_error(ValueError(f"Unknown tool: {name}"), f"Tool: {name}")
    except Exception as e:
        return handle_error(e, f"Tool: {name}")


# Tool handlers
async def handle_import_tasks(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle import_asana_tasks tool."""
    config = get_config()
    parquet_client = get_parquet_client()

    workspace = arguments.get("workspace", "source")
    only_incomplete = arguments.get("only_incomplete", False)
    assignee_gid = arguments.get("assignee_gid")
    max_tasks = arguments.get("max_tasks")
    recalculate = arguments.get("recalculate", False)
    include_archived = arguments.get("include_archived", True)

    importer = AsanaImporter(
        config=config, parquet_client=parquet_client, workspace=workspace
    )

    stats = await importer.import_tasks(
        only_incomplete=only_incomplete,
        assignee_gid=assignee_gid,
        max_tasks=max_tasks,
        recalculate=recalculate,
        include_archived=include_archived,
    )

    return format_result(stats)


async def handle_export_tasks(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle export_asana_tasks tool."""
    config = get_config()
    parquet_client = get_parquet_client()

    workspace = arguments.get("workspace", "target")
    task_ids = arguments.get("task_ids")
    limit = arguments.get("limit", 10)
    sync_log_filter = arguments.get("sync_log_filter")

    exporter = AsanaExporter(
        config=config, parquet_client=parquet_client, workspace=workspace
    )

    stats = await exporter.export_tasks(
        task_ids=task_ids, limit=limit, sync_log_filter=sync_log_filter
    )

    return format_result(stats)


async def handle_sync_tasks(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle sync_asana_tasks tool."""
    config = get_config()
    parquet_client = get_parquet_client()

    task_ids = arguments.get("task_ids")
    sync_scope = arguments.get("sync_scope", "both")
    dry_run = arguments.get("dry_run", False)

    syncer = AsanaTaskSyncer(
        config=config,
        parquet_client=parquet_client,
        sync_scope=sync_scope,
        dry_run=dry_run,
    )

    stats = await syncer.sync(task_ids=task_ids)

    return format_result(stats)


async def handle_import_comments(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle import_asana_task_comments tool."""
    config = get_config()
    parquet_client = get_parquet_client()

    task_gids = arguments["task_gids"]
    workspace = arguments.get("workspace", "source")

    importer = AsanaImporter(
        config=config, parquet_client=parquet_client, workspace=workspace
    )

    stats = await importer.import_comments(task_gids)

    return format_result(stats)


async def handle_import_metadata(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle import_asana_task_metadata tool."""
    config = get_config()
    parquet_client = get_parquet_client()

    task_gids = arguments["task_gids"]
    workspace = arguments.get("workspace", "source")
    metadata_types = arguments.get(
        "metadata_types", ["custom_fields", "dependencies", "stories"]
    )

    importer = AsanaImporter(
        config=config, parquet_client=parquet_client, workspace=workspace
    )

    stats = await importer.import_metadata(task_gids, metadata_types)

    return format_result(stats)


async def handle_register_webhooks(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle register_asana_webhooks tool."""
    config = get_config()

    webhook_url = arguments["webhook_url"]
    workspace = arguments.get("workspace", "both")

    results = {}

    if workspace in ["source", "both"]:
        source_client = AsanaClientWrapper.from_config_source(config)
        source_results = await register_webhooks_for_workspace(
            source_client, config.source_workspace_gid, webhook_url, "source"
        )
        results["source"] = source_results

    if workspace in ["target", "both"]:
        target_client = AsanaClientWrapper.from_config_target(config)
        target_results = await register_webhooks_for_workspace(
            target_client, config.target_workspace_gid, webhook_url, "target"
        )
        results["target"] = target_results

    return format_result(results)


async def handle_get_task(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_asana_task tool."""
    config = get_config()

    task_gid = arguments["task_gid"]
    workspace = arguments.get("workspace", "source")

    if workspace == "source":
        client = AsanaClientWrapper.from_config_source(config)
    else:
        client = AsanaClientWrapper.from_config_target(config)

    opt_fields = [
        "gid",
        "name",
        "notes",
        "html_notes",
        "completed",
        "completed_at",
        "due_on",
        "start_on",
        "created_at",
        "modified_at",
        "assignee",
        "assignee.gid",
        "assignee.name",
        "projects",
        "projects.name",
        "memberships",
        "memberships.section.name",
        "tags",
        "tags.name",
        "permalink_url",
    ]

    opts = {"opt_fields": ",".join(opt_fields)}
    task_data = client._with_retry(client.tasks.get_task, task_gid, opts)

    result = {"success": True, "workspace": workspace, "task": task_data}

    return format_result(result)


async def handle_list_projects(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle list_asana_projects tool."""
    config = get_config()

    workspace = arguments.get("workspace", "source")
    archived = arguments.get("archived")

    if workspace == "source":
        client = AsanaClientWrapper.from_config_source(config)
        workspace_gid = config.source_workspace_gid
    else:
        client = AsanaClientWrapper.from_config_target(config)
        workspace_gid = config.target_workspace_gid

    projects = list(client.list_projects(workspace_gid, archived=archived))

    result = {
        "success": True,
        "workspace": workspace,
        "workspace_gid": workspace_gid,
        "count": len(projects),
        "projects": projects,
    }

    return format_result(result)


async def handle_get_workspace_info(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_asana_workspace_info tool."""
    config = get_config()

    workspace = arguments.get("workspace", "source")

    if workspace == "source":
        client = AsanaClientWrapper.from_config_source(config)
        workspace_gid = config.source_workspace_gid
    else:
        client = AsanaClientWrapper.from_config_target(config)
        workspace_gid = config.target_workspace_gid

    opts = {"opt_fields": "gid,name,email_domains,is_organization"}
    workspace_data = client._with_retry(
        client.workspaces.get_workspace, workspace_gid, opts
    )

    result = {"success": True, "workspace": workspace, "workspace_data": workspace_data}

    return format_result(result)


async def register_webhooks_for_workspace(
    client: AsanaClientWrapper,
    workspace_gid: str,
    webhook_url: str,
    workspace_name: str,
) -> dict[str, Any]:
    """Register webhooks for all projects in a workspace."""
    import requests

    projects = list(client.list_projects(workspace_gid, archived=False))

    registered = []
    failed = []

    for project in projects:
        project_gid = project.get("gid")
        project_name = project.get("name", "")

        if not project_gid:
            continue

        try:
            headers = {
                "Authorization": f"Bearer {client._pat}",
                "Content-Type": "application/json",
            }

            payload = {
                "data": {
                    "resource": project_gid,
                    "target": webhook_url,
                }
            }

            response = requests.post(
                "https://app.asana.com/api/1.0/webhooks",
                headers=headers,
                json=payload,
                timeout=30,
            )

            if response.status_code in [200, 201]:
                webhook_data = response.json().get("data", {})
                registered.append(
                    {
                        "project_gid": project_gid,
                        "project_name": project_name,
                        "webhook_gid": webhook_data.get("gid"),
                        "status": "registered",
                    }
                )
            else:
                failed.append(
                    {
                        "project_gid": project_gid,
                        "project_name": project_name,
                        "error": f"HTTP {response.status_code}: {response.text}",
                    }
                )
        except Exception as e:
            failed.append(
                {
                    "project_gid": project_gid,
                    "project_name": project_name,
                    "error": str(e),
                }
            )

    return {
        "workspace": workspace_name,
        "registered_count": len(registered),
        "failed_count": len(failed),
        "registered": registered,
        "failed": failed,
    }


# Main entry point
async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
