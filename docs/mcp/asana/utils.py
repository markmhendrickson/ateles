"""
Utility functions for Asana MCP Server.
"""

import json
import sys
from typing import Any

from mcp.types import TextContent


def handle_error(error: Exception, context: str = "") -> list[TextContent]:
    """
    Handle errors and return structured error response.

    Args:
        error: Exception that occurred
        context: Context string describing where error occurred

    Returns:
        List[TextContent] with error information
    """
    error_response = {
        "error": str(error),
        "type": type(error).__name__,
        "context": context,
    }

    # Log error to stderr (not stdout)
    print(f"Error in {context}: {error}", file=sys.stderr)

    return [TextContent(type="text", text=json.dumps(error_response, indent=2))]


def format_result(result: dict[str, Any]) -> list[TextContent]:
    """
    Format a successful result as TextContent.

    Args:
        result: Result dictionary to format

    Returns:
        List[TextContent] with formatted result
    """
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
