#!/usr/bin/env python3
"""Find duplicate tasks using fuzzy matching on title and description."""

import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def normalize_text(text):
    """Normalize text for comparison."""
    if pd.isna(text) or text is None:
        return ""
    return str(text).strip().lower()


def similarity_ratio(str1, str2):
    """Calculate similarity ratio between two strings (0-100)."""
    return SequenceMatcher(None, str1, str2).ratio() * 100


def find_duplicates(
    df, title_threshold=85, description_threshold=80, combined_threshold=75
):
    """
    Find duplicate tasks using fuzzy matching.

    Args:
        df: DataFrame with tasks
        title_threshold: Similarity threshold for title (0-100)
        description_threshold: Similarity threshold for description (0-100)
        combined_threshold: Combined similarity threshold (0-100)
    """
    print(f"Analyzing {len(df)} tasks for duplicates...")

    # Prepare data
    df = df.copy()
    df["title_norm"] = df["title"].apply(normalize_text)
    df["description_norm"] = df["description"].apply(normalize_text)
    df["combined_text"] = df["title_norm"] + " " + df["description_norm"]

    # Track duplicates
    duplicates = defaultdict(list)
    processed = set()

    # Compare each task with all others
    for i, row1 in df.iterrows():
        if i in processed:
            continue

        matches = []

        for j, row2 in df.iterrows():
            if i >= j or j in processed:
                continue

            # Skip if both have same task_id (shouldn't happen but check anyway)
            if row1["task_id"] == row2["task_id"]:
                continue

            # Calculate similarity scores
            title_sim = similarity_ratio(row1["title_norm"], row2["title_norm"])
            desc_sim = (
                similarity_ratio(row1["description_norm"], row2["description_norm"])
                if row1["description_norm"] and row2["description_norm"]
                else 0
            )
            combined_sim = similarity_ratio(
                row1["combined_text"], row2["combined_text"]
            )

            # Check if duplicate based on thresholds
            is_duplicate = False
            reason = []

            if title_sim >= title_threshold:
                is_duplicate = True
                reason.append(f"title:{title_sim:.1f}%")

            if (
                desc_sim >= description_threshold
                and row1["description_norm"]
                and row2["description_norm"]
            ):
                is_duplicate = True
                reason.append(f"desc:{desc_sim:.1f}%")

            if combined_sim >= combined_threshold:
                is_duplicate = True
                reason.append(f"combined:{combined_sim:.1f}%")

            if is_duplicate:
                matches.append(
                    {
                        "index": j,
                        "task_id": row2["task_id"],
                        "title": row2["title"],
                        "title_similarity": title_sim,
                        "description_similarity": desc_sim,
                        "combined_similarity": combined_sim,
                        "reason": ", ".join(reason),
                    }
                )

        if matches:
            duplicates[i] = {
                "task_id": row1["task_id"],
                "title": row1["title"],
                "matches": matches,
            }
            processed.add(i)
            for match in matches:
                processed.add(match["index"])

    return duplicates


def print_duplicates(duplicates, df):
    """Print duplicate groups in a readable format."""
    if not duplicates:
        print("\n✓ No duplicates found.")
        return

    print(f"\nFound {len(duplicates)} duplicate groups:\n")
    print("=" * 80)

    for idx, dup_info in duplicates.items():
        print("\nDUPLICATE GROUP:")
        print("Primary task:")
        print(f"  Task ID: {dup_info['task_id']}")
        print(f"  Title: {dup_info['title']}")

        # Get additional info from original dataframe
        primary_row = df.loc[idx]
        print(f"  Domain: {primary_row.get('domain', 'N/A')}")
        print(f"  Status: {primary_row.get('status', 'N/A')}")
        print(
            f"  Created: {primary_row.get('created_at', primary_row.get('created_date', 'N/A'))}"
        )
        print(f"  Import source: {primary_row.get('import_source_file', 'N/A')}")

        print(f"\n  Duplicate matches ({len(dup_info['matches'])}):")
        for match in dup_info["matches"]:
            match_row = df.loc[match["index"]]
            print(f"    - Task ID: {match['task_id']}")
            print(
                f"      Title: {match['title'][:80]}..."
                if len(match["title"]) > 80
                else f"      Title: {match['title']}"
            )
            print(f"      Similarity: {match['reason']}")
            print(f"      Domain: {match_row.get('domain', 'N/A')}")
            print(f"      Status: {match_row.get('status', 'N/A')}")
            print(
                f"      Created: {match_row.get('created_at', match_row.get('created_date', 'N/A'))}"
            )
            print(f"      Import source: {match_row.get('import_source_file', 'N/A')}")
        print("=" * 80)


def main():
    from scripts.config import get_data_dir

    tasks_file = get_data_dir() / "tasks" / "tasks.parquet"

    if not tasks_file.exists():
        print(f"Error: {tasks_file} not found")
        sys.exit(1)

    print(f"Loading tasks from {tasks_file}...")
    df = pd.read_parquet(tasks_file)

    print(f"Loaded {len(df)} tasks")

    # Find duplicates with configurable thresholds
    duplicates = find_duplicates(
        df,
        title_threshold=85,  # Titles must be 85% similar
        description_threshold=80,  # Descriptions must be 80% similar
        combined_threshold=75,  # Combined text must be 75% similar
    )

    print_duplicates(duplicates, df)

    # Summary statistics
    if duplicates:
        total_duplicate_tasks = sum(
            1 + len(dup_info["matches"]) for dup_info in duplicates.values()
        )
        print("\n\nSUMMARY:")
        print(f"  Duplicate groups: {len(duplicates)}")
        print(f"  Total tasks involved: {total_duplicate_tasks}")
        print(f"  Unique tasks: {len(df) - total_duplicate_tasks + len(duplicates)}")


if __name__ == "__main__":
    main()
