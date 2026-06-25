#!/usr/bin/env python3
"""
AST-based linter to detect direct parquet file access violations.

This linter enforces the MCP-only access policy by detecting direct
parquet file reads/writes that should go through the MCP server instead.

Usage:
    python scripts/linters/ast_parquet_linter.py [file1.py] [file2.py] ...
"""

import ast
import sys
from pathlib import Path
from typing import Any


class ParquetAccessChecker(ast.NodeVisitor):
    """AST visitor that detects direct parquet file access patterns."""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.violations: list[dict[str, Any]] = []
        self.data_dir_patterns = [
            "data_dir",
            "data-dir",
            "data/",
            "$data_dir",
            "$data-dir",
            "$data_dir/",
            "data_dir/",
        ]

    def _is_exception_path(self) -> bool:
        """Check if filepath is in exception list (MCP server, import scripts)."""
        path_str = str(self.filepath)
        exception_patterns = [
            "mcp-servers/parquet/",
            "mcp-servers/parquet",
            "scripts/.*_import.py",
            "scripts/troubleshoot_.*.py",
        ]

        # Check if path matches any exception pattern
        import re

        for pattern in exception_patterns:
            if re.search(pattern, path_str):
                return True
        return False

    def _is_data_dir_path(self, node: ast.AST) -> bool:
        """Check if AST node represents a path to $DATA_DIR or data/."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            path_str = node.value.lower()
            return any(pattern in path_str for pattern in self.data_dir_patterns)

        if isinstance(node, ast.JoinedStr):  # f-string
            # Check if f-string contains DATA_DIR references
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    if any(
                        pattern in part.value.lower()
                        for pattern in self.data_dir_patterns
                    ):
                        return True
                # Check for variable references that might be DATA_DIR
                if isinstance(part, ast.FormattedValue):
                    # This is a placeholder - in practice, we'd need to track variable names
                    # For now, flag any f-string with parquet in it
                    return True

        # Check for os.getenv('DATA_DIR') or similar
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "getenv":
                    if (
                        len(node.args) > 0
                        and isinstance(node.args[0], ast.Constant)
                        and "DATA_DIR" in str(node.args[0].value).upper()
                    ):
                        return True

        return False

    def _check_parquet_path(self, node: ast.Call, operation: str) -> None:
        """Check if call involves parquet file access."""
        if len(node.args) == 0:
            return

        first_arg = node.args[0]

        # Check for .parquet extension in path
        path_str = None
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            path_str = first_arg.value
        elif isinstance(first_arg, ast.JoinedStr):
            # f-string - check if it contains .parquet
            path_str = "".join(
                part.value if isinstance(part, ast.Constant) else "{...}"
                for part in first_arg.values
            )

        if path_str and ".parquet" in path_str.lower():
            if self._is_data_dir_path(first_arg):
                self.violations.append(
                    {
                        "line": node.lineno,
                        "col": node.col_offset,
                        "type": f"direct_parquet_{operation}",
                        "message": (
                            f"Direct parquet file {operation} detected at line {node.lineno}. "
                            f"Use MCP server instead. See docs/policies/agent-mcp-access-policy.md"
                        ),
                    }
                )

    def visit_Call(self, node: ast.Call) -> None:
        """Visit function calls to detect parquet access."""
        # Check for pd.read_parquet() or similar
        if isinstance(node.func, ast.Attribute):
            # read_parquet calls
            if node.func.attr == "read_parquet":
                self._check_parquet_path(node, "read")

            # to_parquet calls
            if node.func.attr == "to_parquet":
                self._check_parquet_path(node, "write")

            # DataFrame.read_parquet() pattern
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in ["pd", "pandas", "DataFrame"]
                and node.func.attr == "read_parquet"
            ):
                self._check_parquet_path(node, "read")

        # Check for open() calls with parquet files
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            if len(node.args) > 0:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if ".parquet" in arg.value.lower() and self._is_data_dir_path(arg):
                        self.violations.append(
                            {
                                "line": node.lineno,
                                "col": node.col_offset,
                                "type": "direct_parquet_open",
                                "message": (
                                    f"Direct parquet file open() detected at line {node.lineno}. "
                                    f"Use MCP server instead."
                                ),
                            }
                        )

        self.generic_visit(node)


def check_file(filepath: str) -> list[dict[str, Any]]:
    """Check a single Python file for parquet access violations."""
    checker = ParquetAccessChecker(filepath)

    # Skip exception paths
    if checker._is_exception_path():
        return []

    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()

        tree = ast.parse(content, filename=filepath)
        checker.visit(tree)

        return checker.violations
    except SyntaxError:
        # Skip files with syntax errors (let other linters handle this)
        return []
    except Exception as e:
        print(f"Error checking {filepath}: {e}", file=sys.stderr)
        return []


def main():
    """Main entry point for the linter."""
    if len(sys.argv) < 2:
        # If no files provided, check all staged Python files
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        files = [
            f for f in result.stdout.strip().split("\n") if f.endswith(".py") and f
        ]
    else:
        files = sys.argv[1:]

    if not files:
        sys.exit(0)

    all_violations = []
    for filepath in files:
        violations = check_file(filepath)
        all_violations.extend(violations)

    if all_violations:
        print(
            "ERROR: Direct parquet file access violations detected:\n", file=sys.stderr
        )
        for violation in all_violations:
            print(f"  {violation['type']}: {violation['message']}", file=sys.stderr)
        print(
            "\nAll parquet file access must go through the MCP server.", file=sys.stderr
        )
        print(
            "See docs/policies/agent-mcp-access-policy.md for details.", file=sys.stderr
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
