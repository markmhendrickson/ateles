#!/usr/bin/env python3
"""
Test script for Asana MCP Server.

Tests tool listing and basic functionality.
"""

import asyncio
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from asana_mcp_server import list_tools


async def test_list_tools():
    """Test listing tools."""
    print("Testing tool listing...")
    tools = await list_tools()

    print(f"\nFound {len(tools)} tools:")
    for tool in tools:
        print(f"  - {tool.name}: {tool.description[:80]}...")

    return tools


async def main():
    """Run tests."""
    print("=" * 60)
    print("Asana MCP Server Tests")
    print("=" * 60)

    try:
        # Test 1: List tools
        tools = await test_list_tools()

        # Validate expected tools
        expected_tools = [
            "import_asana_tasks",
            "export_asana_tasks",
            "sync_asana_tasks",
            "import_asana_task_comments",
            "import_asana_task_metadata",
            "register_asana_webhooks",
            "get_asana_task",
            "list_asana_projects",
            "get_asana_workspace_info",
        ]

        tool_names = [t.name for t in tools]
        missing = [t for t in expected_tools if t not in tool_names]

        if missing:
            print(f"\n❌ Missing tools: {missing}")
            return False

        print(f"\n✓ All {len(expected_tools)} expected tools are present")

        # Test 2: Validate tool schemas
        print("\nValidating tool schemas...")
        for tool in tools:
            if not tool.inputSchema:
                print(f"  ❌ {tool.name}: Missing inputSchema")
                return False
            if "properties" not in tool.inputSchema:
                print(f"  ❌ {tool.name}: inputSchema missing properties")
                return False

        print("✓ All tool schemas are valid")

        print("\n" + "=" * 60)
        print("All tests passed!")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
