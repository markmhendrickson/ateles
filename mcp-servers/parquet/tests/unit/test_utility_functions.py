"""
Unit tests for parquet MCP server utility functions.
Tests the extracted utility functions that are already testable.
"""

import json
from pathlib import Path

import pandas as pd


class TestListAvailableDataTypes:
    """Tests for list_available_data_types function."""

    def test_list_empty_directory(self, test_data_dir: Path):
        """Test listing data types when directory is empty."""
        from parquet_mcp_server import list_available_data_types

        result = list_available_data_types()

        assert isinstance(result, list)
        assert len(result) == 0

    def test_list_with_data(self, test_data_dir: Path, sample_parquet_file: Path):
        """Test listing data types with existing parquet files."""
        from parquet_mcp_server import list_available_data_types

        result = list_available_data_types()

        assert "test_records" in result
        assert isinstance(result, list)

    def test_list_ignores_non_parquet(self, test_data_dir: Path):
        """Test that directories without parquet files are ignored."""
        from parquet_mcp_server import list_available_data_types

        # Create a directory without parquet file
        other_dir = test_data_dir / "other_files"
        other_dir.mkdir()
        (other_dir / "readme.md").write_text("test")

        result = list_available_data_types()

        assert "other_files" not in result

    def test_list_ignores_underscore_dirs(self, test_data_dir: Path):
        """Test that directories starting with underscore are ignored."""
        from parquet_mcp_server import list_available_data_types

        # Create a directory starting with underscore
        private_dir = test_data_dir / "_private"
        private_dir.mkdir()
        parquet_file = private_dir / "_private.parquet"
        pd.DataFrame({"id": [1]}).to_parquet(parquet_file, index=False)

        result = list_available_data_types()

        assert "_private" not in result


class TestGetParquetFilePath:
    """Tests for get_parquet_file_path function."""

    def test_get_path_format(self, test_data_dir: Path):
        """Test parquet file path format."""
        from parquet_mcp_server import get_parquet_file_path

        path = get_parquet_file_path("test_records")

        assert path.name == "test_records.parquet"
        assert path.parent.name == "test_records"
        assert "test_records/test_records.parquet" in str(path)

    def test_get_path_different_types(self, test_data_dir: Path):
        """Test path generation for different data types."""
        from parquet_mcp_server import get_parquet_file_path

        paths = {
            "flows": get_parquet_file_path("flows"),
            "transactions": get_parquet_file_path("transactions"),
            "contacts": get_parquet_file_path("contacts"),
        }

        for data_type, path in paths.items():
            assert path.name == f"{data_type}.parquet"
            assert path.parent.name == data_type


class TestGetSchemaPath:
    """Tests for get_schema_path function."""

    def test_get_schema_path_exists(self, test_data_dir: Path, sample_schema: dict):
        """Test getting schema path for existing schema."""
        from parquet_mcp_server import get_schema_path

        path = get_schema_path("test_records")

        assert path is not None
        assert path.exists()
        assert path.name == "test_records_schema.json"

    def test_get_schema_path_not_exists(self, test_data_dir: Path):
        """Test getting schema path for non-existent schema."""
        from parquet_mcp_server import get_schema_path

        path = get_schema_path("nonexistent")

        assert path is None


class TestApplyEnhancedFilter:
    """Tests for apply_enhanced_filter function."""

    def test_filter_simple_equality(self, sample_parquet_file: Path):
        """Test simple equality filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "category", "A")

        assert len(filtered) == 2
        assert all(filtered["category"] == "A")

    def test_filter_contains(self, sample_parquet_file: Path):
        """Test $contains filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "name", {"$contains": "li"})

        assert len(filtered) == 2  # Alice, Charlie
        assert all("li" in name.lower() for name in filtered["name"])

    def test_filter_gt(self, sample_parquet_file: Path):
        """Test $gt (greater than) filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "value", {"$gt": 20})

        assert len(filtered) == 3
        assert all(filtered["value"] > 20)

    def test_filter_gte(self, sample_parquet_file: Path):
        """Test $gte (greater than or equal) filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "value", {"$gte": 20.3})

        assert len(filtered) == 3
        assert all(filtered["value"] >= 20.3)

    def test_filter_lt(self, sample_parquet_file: Path):
        """Test $lt (less than) filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "value", {"$lt": 20})

        assert len(filtered) == 2
        assert all(filtered["value"] < 20)

    def test_filter_lte(self, sample_parquet_file: Path):
        """Test $lte (less than or equal) filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "value", {"$lte": 20.3})

        assert len(filtered) == 3
        assert all(filtered["value"] <= 20.3)

    def test_filter_in(self, sample_parquet_file: Path):
        """Test $in filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "category", {"$in": ["A", "C"]})

        assert len(filtered) == 3
        assert all(cat in ["A", "C"] for cat in filtered["category"])

    def test_filter_ne(self, sample_parquet_file: Path):
        """Test $ne (not equal) filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "category", {"$ne": "A"})

        assert len(filtered) == 3
        assert all(filtered["category"] != "A")

    def test_filter_starts_with(self, sample_parquet_file: Path):
        """Test $starts_with filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "name", {"$starts_with": "Al"})

        assert len(filtered) == 1
        assert filtered.iloc[0]["name"] == "Alice"

    def test_filter_ends_with(self, sample_parquet_file: Path):
        """Test $ends_with filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "name", {"$ends_with": "e"})

        assert len(filtered) >= 1  # Alice, Charlie
        assert all(name.endswith("e") for name in filtered["name"])

    def test_filter_regex(self, sample_parquet_file: Path):
        """Test $regex filter."""
        from parquet_mcp_server import apply_enhanced_filter

        df = pd.read_parquet(sample_parquet_file)
        filtered = apply_enhanced_filter(df, "name", {"$regex": "^[AB]"})

        assert len(filtered) == 2  # Alice, Bob
        assert all(name[0] in ["A", "B"] for name in filtered["name"])


class TestApplySorting:
    """Tests for apply_sorting function."""

    def test_sort_ascending(self, sample_parquet_file: Path):
        """Test sorting in ascending order."""
        from parquet_mcp_server import apply_sorting

        df = pd.read_parquet(sample_parquet_file)
        sorted_df = apply_sorting(df, [{"column": "value", "ascending": True}])

        values = sorted_df["value"].tolist()
        assert values == sorted(values)

    def test_sort_descending(self, sample_parquet_file: Path):
        """Test sorting in descending order."""
        from parquet_mcp_server import apply_sorting

        df = pd.read_parquet(sample_parquet_file)
        sorted_df = apply_sorting(df, [{"column": "value", "ascending": False}])

        values = sorted_df["value"].tolist()
        assert values == sorted(values, reverse=True)

    def test_sort_multiple_columns(self, sample_parquet_file: Path):
        """Test sorting by multiple columns."""
        from parquet_mcp_server import apply_sorting

        df = pd.read_parquet(sample_parquet_file)
        sorted_df = apply_sorting(
            df,
            [
                {"column": "category", "ascending": True},
                {"column": "value", "ascending": False},
            ],
        )

        # Verify category groups are sorted
        categories = sorted_df["category"].tolist()
        assert categories[0] <= categories[-1]


class TestGetSchemaPath:
    """Tests for get_schema_path function."""

    def test_get_schema_exists(self, test_data_dir: Path, sample_schema: dict):
        """Test getting schema path for existing schema."""
        from parquet_mcp_server import get_schema_path

        path = get_schema_path("test_records")

        assert path is not None
        assert path.exists()
        assert path.name == "test_records_schema.json"

        # Verify schema content
        with open(path) as f:
            schema = json.load(f)
        assert schema["title"] == "Test Records"

    def test_get_schema_not_exists(self, test_data_dir: Path):
        """Test getting schema path for non-existent schema."""
        from parquet_mcp_server import get_schema_path

        path = get_schema_path("nonexistent")

        assert path is None
