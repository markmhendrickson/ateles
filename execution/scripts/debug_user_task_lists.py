#!/usr/bin/env python3
"""
Debug script to investigate user_task_lists endpoint issues.
"""

import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def debug_user_task_lists():
    """Debug the user_task_lists endpoint."""

    config = AsanaConfig.from_env()
    client = AsanaClientWrapper.from_config_target(config)
    headers = {"Authorization": f"Bearer {client._pat}"}

    print("=== Debugging user_task_lists endpoint ===\n")

    # Step 1: Get current user info
    print("1. Getting current user info...")
    url = "https://app.asana.com/api/1.0/users/me"
    params = {"opt_fields": "gid,name,email,workspaces,workspaces.gid,workspaces.name"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        user_data = response.json().get("data", {})
        user_gid = user_data.get("gid")
        user_name = user_data.get("name")
        user_email = user_data.get("email")
        workspaces = user_data.get("workspaces", [])
        print(f"   User GID: {user_gid}")
        print(f"   User Name: {user_name}")
        print(f"   User Email: {user_email}")
        print(f"   Workspaces: {len(workspaces)}")
        for ws in workspaces:
            ws_gid = ws.get("gid") if isinstance(ws, dict) else ws
            ws_name = ws.get("name") if isinstance(ws, dict) else "Unknown"
            print(f"     - {ws_name} ({ws_gid})")
            if str(ws_gid) == str(config.target_workspace_gid):
                print("       ^ This is the target workspace")
    else:
        print(f"   Error: {response.text}")
        return

    print()

    # Step 2: Try user_task_lists with user GID
    print("2. Trying user_task_lists with user GID...")
    url = f"https://app.asana.com/api/1.0/users/{user_gid}/user_task_lists"
    params = {"workspace": config.target_workspace_gid, "opt_fields": "gid,workspace"}
    print(f"   URL: {url}")
    print(f"   Params: {params}")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code != 200:
        print(f"   Response: {response.text[:500]}")

    print()

    # Step 3: Try user_task_lists with "me" endpoint
    print("3. Trying user_task_lists with 'me' endpoint...")
    url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
    params = {"workspace": config.target_workspace_gid, "opt_fields": "gid,workspace"}
    print(f"   URL: {url}")
    print(f"   Params: {params}")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code != 200:
        print(f"   Response: {response.text[:500]}")

    print()

    # Step 4: Try without workspace filter
    print("4. Trying user_task_lists without workspace filter...")
    url = f"https://app.asana.com/api/1.0/users/{user_gid}/user_task_lists"
    params = {"opt_fields": "gid,workspace,workspace.gid,workspace.name"}
    print(f"   URL: {url}")
    print(f"   Params: {params}")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json().get("data", [])
        print(f"   Found {len(data)} user task lists:")
        for utl in data:
            print(f"     - GID: {utl.get('gid')}")
            workspace = utl.get("workspace", {})
            if isinstance(workspace, dict):
                print(
                    f"       Workspace: {workspace.get('name')} ({workspace.get('gid')})"
                )
                if str(workspace.get("gid")) == str(config.target_workspace_gid):
                    print("       ^ Matches target workspace!")
            else:
                print(f"       Workspace: {workspace}")
    else:
        print(f"   Response: {response.text[:500]}")

    print()

    # Step 5: Try with "me" endpoint without workspace filter
    print("5. Trying user_task_lists with 'me' endpoint without workspace filter...")
    url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
    params = {"opt_fields": "gid,workspace,workspace.gid,workspace.name"}
    print(f"   URL: {url}")
    print(f"   Params: {params}")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json().get("data", [])
        print(f"   Found {len(data)} user task lists:")
        for utl in data:
            print(f"     - GID: {utl.get('gid')}")
            workspace = utl.get("workspace", {})
            if isinstance(workspace, dict):
                print(
                    f"       Workspace: {workspace.get('name')} ({workspace.get('gid')})"
                )
                if str(workspace.get("gid")) == str(config.target_workspace_gid):
                    print("       ^ Matches target workspace!")
            else:
                print(f"       Workspace: {workspace}")
    else:
        print(f"   Response: {response.text[:500]}")

    print()

    # Step 6: Check if we can access the workspace directly
    print("6. Checking workspace access...")
    url = f"https://app.asana.com/api/1.0/workspaces/{config.target_workspace_gid}"
    params = {"opt_fields": "gid,name,is_organization"}
    print(f"   URL: {url}")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"   Status: {response.status_code}")
    if response.status_code == 200:
        ws_data = response.json().get("data", {})
        print(f"   Workspace Name: {ws_data.get('name')}")
        print(f"   Is Organization: {ws_data.get('is_organization')}")
    else:
        print(f"   Response: {response.text[:500]}")


if __name__ == "__main__":
    debug_user_task_lists()
