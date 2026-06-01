import json
from pathlib import Path


def test_mcp_config_uses_relative_commands():
    repo_root = Path(__file__).resolve().parents[3]
    config_path = repo_root / ".cursor" / "mcp.json"
    config = json.loads(config_path.read_text())
    servers = config.get("mcpServers", {})

    absolute_commands = [
        name
        for name, server in servers.items()
        if str(server.get("command", "")).startswith("/")
    ]

    assert (
        not absolute_commands
    ), f"Commands must be relative paths: {', '.join(sorted(absolute_commands))}"


def test_asana_uses_local_runner():
    repo_root = Path(__file__).resolve().parents[3]
    config_path = repo_root / ".cursor" / "mcp.json"
    config = json.loads(config_path.read_text())
    asana = config.get("mcpServers", {}).get("asana", {})

    assert asana.get("command") == "mcp/asana/run-asana-mcp.sh"
