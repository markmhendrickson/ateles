#!/usr/bin/env python3
"""
Find and parse all ChatGPT conversations that track exercise sets.

This script:
1. Scans conversations.json for conversations that contain exercise sets
2. Extracts detailed set information using LLM inference with context
3. Outputs results for review or import
"""

import importlib.util
import json
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


def find_exercise_conversations(conversations_path: Path) -> list[dict[str, Any]]:
    """
    Find conversations that likely contain exercise sets.

    Returns list of conversation dicts with title, id, and metadata.
    """
    with open(conversations_path) as f:
        conversations = json.load(f)

    # Primary keywords that strongly indicate exercise tracking
    primary_keywords = [
        "lifting",
        "workout",
        "gym",
        "training",
        "exercise",
        "bench press",
        "squat",
        "deadlift",
        "progression",
        "strength training",
        "fitness",
        "weight training",
    ]

    # Secondary keywords that need context
    secondary_keywords = ["sets", "reps", "repetitions", "weight", "muscle"]

    # Phrases that indicate exercise tracking
    exercise_phrases = [
        "track lifting",
        "track workout",
        "exercise log",
        "workout log",
        "lifting progression",
        "track sets",
        "track reps",
    ]

    found = []

    for conv in conversations:
        if not isinstance(conv, dict):
            continue

        title = (conv.get("title") or "").lower()
        mapping = conv.get("mapping", {})

        # Check title for exercise phrases (strongest signal)
        title_phrase_match = any(phrase in title for phrase in exercise_phrases)
        title_primary_match = any(keyword in title for keyword in primary_keywords)

        # Check first few messages for exercise-related content
        content_phrase_match = False
        content_primary_match = False
        content_secondary_count = 0
        message_count = 0

        for node_id, node in list(mapping.items())[:30]:  # Check first 30 nodes
            msg = node.get("message")
            if not msg:
                continue

            content = msg.get("content", {})
            if content.get("content_type") != "text":
                continue

            parts = content.get("parts", [])
            if not parts or not parts[0]:
                continue

            text = parts[0].lower()

            # Check for phrases (strongest signal)
            if any(phrase in text for phrase in exercise_phrases):
                content_phrase_match = True
                break

            # Check for primary keywords
            if any(keyword in text for keyword in primary_keywords):
                content_primary_match = True

            # Count secondary keywords (need multiple for confidence)
            if any(keyword in text for keyword in secondary_keywords):
                content_secondary_count += 1

            message_count += 1
            if message_count > 15:  # Limit search depth
                break

        # Match if:
        # - Title has exercise phrase OR
        # - Title has primary keyword OR
        # - Content has exercise phrase OR
        # - Content has primary keyword AND at least 2 secondary keywords
        title_match = title_phrase_match or title_primary_match
        content_match = content_phrase_match or (
            content_primary_match and content_secondary_count >= 2
        )

        if title_match or content_match:
            found.append(
                {
                    "title": conv.get("title") or "Untitled",
                    "create_time": conv.get("create_time"),
                    "update_time": conv.get("update_time"),
                    "conversation": conv,
                }
            )

    return found


def extract_conversation_to_file(conv_dict: dict[str, Any], output_path: Path) -> None:
    """Extract a single conversation to its own JSON file."""
    with open(output_path, "w") as f:
        json.dump(conv_dict["conversation"], f, indent=2)
    print(f"  Extracted to: {output_path}")


def extract_sets_from_conversation(
    conv_path: Path,
    conversation_title: str | None = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Extract all sets from the conversation JSON using LLM inference.

    Args:
        conv_path: Path to conversation JSON
        conversation_title: Optional title for context
        dry_run: If True, don't write files
    """

    # Import conversation_parser from same directory
    scripts_dir = Path(__file__).parent
    parser_path = scripts_dir / "conversation_parser.py"
    if not parser_path.exists():
        raise ImportError(f"conversation_parser.py not found at {parser_path}")

    spec = importlib.util.spec_from_file_location("conversation_parser", parser_path)
    conversation_parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conversation_parser)
    conversation_parser_class = conversation_parser.ConversationParser
    get_exercise_sets_schema = conversation_parser.get_exercise_sets_schema

    try:
        parser = conversation_parser_class()
    except (ImportError, ValueError) as e:
        print(f"  ERROR: Cannot initialize parser: {e}")
        print("  Make sure OPENAI_API_KEY is set in environment")
        return pd.DataFrame()

    messages = parser.load_conversation(conv_path)
    schema = get_exercise_sets_schema()

    print(f"  Messages: {len(messages)}")
    print(f"  Title: {conversation_title or conv_path.stem}")

    records = parser.extract_with_schema(
        messages,
        schema,
        conversation_title=conversation_title or conv_path.stem,
    )

    print(f"  Extracted {len(records)} raw records via inference")

    # Convert to DataFrame with expected schema
    all_sets = []
    skipped_no_reps = 0
    skipped_bad_reps = 0
    skipped_no_exercise = 0

    for idx, record in enumerate(records):
        # Handle None values for repetitions
        reps = record.get("repetitions")
        if reps is None:
            skipped_no_reps += 1
            continue  # Skip records without repetitions
        try:
            reps = float(reps)
        except (ValueError, TypeError):
            skipped_bad_reps += 1
            continue  # Skip invalid repetition values

        # Skip records without exercise name
        exercise_name = (record.get("exercise_name") or "").strip()
        if not exercise_name:
            skipped_no_exercise += 1
            continue

        # Build notes with location context
        notes_parts = []
        if record.get("location"):
            notes_parts.append(f"Location: {record.get('location')}")
        if record.get("time_of_day"):
            notes_parts.append(f"Time: {record.get('time_of_day')}")
        if record.get("laterality") and record.get("laterality") != "bilateral":
            notes_parts.append(f"Laterality: {record.get('laterality')}")
        if record.get("notes"):
            notes_parts.append(record.get("notes"))

        all_sets.append(
            {
                "set_id": str(uuid.uuid4())[:16],
                "name": "",
                "exercise": "",
                "exercise_name": exercise_name,
                "date": record.get("date", ""),
                "repetitions": reps,
                "weight": str(record.get("weight", "")),
                "type": record.get("type", ""),
                "notes": ". ".join(notes_parts) if notes_parts else "",
                "import_date": date.today().isoformat(),
                "import_source_file": f"chatgpt:{conversation_title or conv_path.stem}",
            }
        )

    if skipped_no_reps or skipped_bad_reps or skipped_no_exercise:
        print(
            f"  Filtered: {skipped_no_reps} no reps, {skipped_bad_reps} bad reps, {skipped_no_exercise} no exercise"
        )

    # Convert to DataFrame
    df = pd.DataFrame(all_sets)

    if len(df) > 0:
        # Ensure proper types
        df["repetitions"] = df["repetitions"].astype(float)

        # Show summary
        print(f"  Final sets: {len(df)}")
        print(f"  Date range: {df['date'].min()} to {df['date'].max()}")
        print(f"  Unique exercises: {df['exercise_name'].nunique()}")

    return df


def integrate_sets_with_updates(
    new_df: pd.DataFrame,
    sets_path: Path,
    snapshot_dir: Path,
    source_identifier: str,
    dry_run: bool = False,
) -> None:
    """
    Integrate new sets with existing sets, updating matches instead of duplicating.

    Matches sets by key fields: date, exercise_name, repetitions, weight, type
    - If match found: updates existing set (preserves set_id, updates other fields)
    - If no match: adds as new set

    Args:
        new_df: DataFrame of new sets to integrate
        sets_path: Path to sets.parquet
        snapshot_dir: Directory for snapshots
        source_identifier: Source identifier to match/update (e.g., "chatgpt:track_lifting_progression")
        dry_run: If True, don't write files
    """
    if len(new_df) == 0:
        print("  No sets to integrate")
        return

    # Read existing sets
    existing_df = pd.read_parquet(sets_path)
    print(f"\nExisting sets: {len(existing_df)}")

    # Create snapshot
    if not dry_run:
        snapshot_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_path = snapshot_dir / f"sets-{timestamp}.parquet"
        existing_df.to_parquet(snapshot_path, index=False)
        print(f"Created snapshot: {snapshot_path}")

    # Convert date fields to ensure matching works
    # Handle new_df dates
    if new_df["date"].dtype == "object":
        new_df["date"] = pd.to_datetime(new_df["date"], errors="coerce").dt.date
    elif hasattr(new_df["date"].iloc[0], "date"):  # Already datetime
        new_df["date"] = new_df["date"].dt.date

    if new_df["import_date"].dtype == "object":
        new_df["import_date"] = pd.to_datetime(
            new_df["import_date"], errors="coerce"
        ).dt.date
    elif hasattr(new_df["import_date"].iloc[0], "date"):  # Already datetime
        new_df["import_date"] = new_df["import_date"].dt.date

    # Ensure existing dates are date objects (they should already be from parquet)
    # Just ensure they're comparable
    if existing_df["date"].dtype != "object":
        if hasattr(existing_df["date"].iloc[0], "date"):
            existing_df["date"] = existing_df["date"].dt.date

    # Key fields for matching

    # Normalize key fields for matching (handle string weight comparisons)
    def normalize_key(row):
        """Create normalized key tuple for matching"""
        # Normalize date to string for comparison
        date_val = row.get("date")
        if date_val is not None:
            if hasattr(date_val, "isoformat"):
                date_str = date_val.isoformat()
            else:
                date_str = str(date_val)
        else:
            date_str = ""

        weight_str = str(row.get("weight", "")).strip().lower()
        reps_val = row.get("repetitions")
        try:
            reps_float = float(reps_val) if reps_val is not None else 0.0
        except (ValueError, TypeError):
            reps_float = 0.0

        return (
            date_str,
            str(row.get("exercise_name", "")).strip().lower(),
            reps_float,
            weight_str,
            str(row.get("type", "")).strip().lower(),
        )

    # Build lookup of existing sets by normalized key
    existing_lookup = {}
    for idx, row in existing_df.iterrows():
        key = normalize_key(row)
        if key not in existing_lookup:
            existing_lookup[key] = []
        existing_lookup[key].append(idx)

    # Process new sets: update existing or add new
    updated_count = 0
    added_count = 0
    updated_indices = set()

    for _, new_row in new_df.iterrows():
        key = normalize_key(new_row)

        # Check for exact match
        if key in existing_lookup:
            # Use first matching set (if multiple, update first)
            existing_idx = existing_lookup[key][0]
            if existing_idx not in updated_indices:
                # Update existing set
                existing_df.loc[existing_idx, "notes"] = new_row.get("notes", "")
                existing_df.loc[existing_idx, "import_date"] = new_row.get(
                    "import_date"
                )
                existing_df.loc[existing_idx, "import_source_file"] = new_row.get(
                    "import_source_file", ""
                )
                # Preserve set_id, update other fields if they're more complete
                if new_row.get("name") and not existing_df.loc[existing_idx, "name"]:
                    existing_df.loc[existing_idx, "name"] = new_row.get("name", "")
                if (
                    new_row.get("exercise")
                    and not existing_df.loc[existing_idx, "exercise"]
                ):
                    existing_df.loc[existing_idx, "exercise"] = new_row.get(
                        "exercise", ""
                    )
                updated_indices.add(existing_idx)
                updated_count += 1
        else:
            # No match found, add as new set
            new_set = new_row.to_dict()
            new_set["set_id"] = str(uuid.uuid4())[:16]  # Generate new ID
            existing_df = pd.concat(
                [existing_df, pd.DataFrame([new_set])], ignore_index=True
            )
            added_count += 1

    print("\nIntegration summary:")
    print(f"  Sets updated: {updated_count}")
    print(f"  Sets added: {added_count}")
    print(f"  Total sets: {len(existing_df)}")

    # Write back
    if not dry_run:
        existing_df.to_parquet(sets_path, index=False)
        print(f"✓ Successfully wrote to {sets_path}")

        # Verify
        verify_df = pd.read_parquet(sets_path)
        print(f"✓ Verified: {len(verify_df)} rows in sets.parquet")
    else:
        print("DRY RUN: Would write to sets.parquet")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Find and parse all ChatGPT conversations containing exercise sets"
    )
    parser.add_argument(
        "--list-only", action="store_true", help="Only list conversations, do not parse"
    )
    parser.add_argument(
        "--extract-conversations",
        action="store_true",
        help="Extract found conversations to individual JSON files",
    )
    parser.add_argument(
        "--parse-all",
        action="store_true",
        help="Parse all found conversations (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/imports/chatgpt",
        help="Directory for extracted conversations and output (relative to project root)",
    )
    parser.add_argument(
        "--output-csv", type=str, help="Output all parsed sets to CSV for inspection"
    )
    parser.add_argument(
        "--integrate",
        action="store_true",
        help="Integrate parsed sets into data/sets/sets.parquet (updates existing, adds new)",
    )
    parser.add_argument(
        "--no-integrate",
        action="store_true",
        help="Do not integrate sets (only parse and output)",
    )

    args = parser.parse_args()

    # Paths - go up to project root (execution/scripts -> execution -> project root)
    base_dir = Path(__file__).parent.parent.parent
    # Import config from execution/scripts (same directory as this script)
    # This loads .env from project root automatically
    scripts_dir = Path(__file__).parent
    sys.path.insert(0, str(scripts_dir))
    from config import get_data_dir

    data_dir = get_data_dir()
    conversations_path = data_dir / "imports" / "chatgpt" / "conversations.json"
    output_dir = base_dir / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if not conversations_path.exists():
        print(f"ERROR: conversations.json not found at {conversations_path}")
        return

    print("Scanning conversations.json for exercise-related conversations...")
    print(f"File: {conversations_path}")
    print()

    # Find conversations
    exercise_convs = find_exercise_conversations(conversations_path)

    print(
        f"Found {len(exercise_convs)} conversations that may contain exercise sets:\n"
    )

    for i, conv in enumerate(exercise_convs, 1):
        title = conv["title"]
        create_time = conv.get("create_time")
        conv.get("update_time")

        if create_time:
            create_date = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d")
        else:
            create_date = "Unknown"

        print(f"{i}. {title}")
        print(f"   Created: {create_date}")
        print()

    if args.list_only:
        return

    # Extract conversations to individual files
    if args.extract_conversations:
        print("\nExtracting conversations to individual files...")
        for conv in exercise_convs:
            title = conv["title"]
            # Sanitize filename
            safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
            output_path = output_dir / f"{safe_title}.json"
            extract_conversation_to_file(conv, output_path)
        print(f"\nExtracted {len(exercise_convs)} conversations to {output_dir}")

    # Parse all conversations
    if args.parse_all:
        print("\n" + "=" * 60)
        print("Parsing all conversations (requires OPENAI_API_KEY)")
        print("=" * 60 + "\n")

        all_sets = []

        for i, conv in enumerate(exercise_convs, 1):
            title = conv["title"]
            safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
            conv_path = output_dir / f"{safe_title}.json"

            # If not extracted yet, extract it
            if not conv_path.exists():
                extract_conversation_to_file(conv, conv_path)

            print(f"\n[{i}/{len(exercise_convs)}] Processing: {title}")
            print("-" * 60)

            try:
                df = extract_sets_from_conversation(
                    conv_path,
                    conversation_title=title,
                    dry_run=False,
                )

                if len(df) > 0:
                    all_sets.append(df)
                    print(f"  ✓ Extracted {len(df)} sets")
                else:
                    print("  ⚠ No sets extracted")

            except Exception as e:
                print(f"  ✗ ERROR: {e}")
                import traceback

                traceback.print_exc()
                continue

        # Integrate all sets with updates (instead of just combining)
        if all_sets:
            combined_df = pd.concat(all_sets, ignore_index=True)

            # Integrate if requested (default: integrate unless --no-integrate)
            if args.integrate or not args.no_integrate:
                print("\n" + "=" * 60)
                print("INTEGRATING WITH EXISTING SETS")
                print("=" * 60)

                # Use data_dir from environment
                sets_path = data_dir / "sets/sets.parquet"
                snapshot_dir = data_dir / "snapshots"

                # Group by source and integrate each source separately
                sources = combined_df["import_source_file"].unique()
                for source in sources:
                    source_df = combined_df[
                        combined_df["import_source_file"] == source
                    ].copy()
                    print(f"\nIntegrating sets from: {source}")
                    integrate_sets_with_updates(
                        source_df,
                        sets_path,
                        snapshot_dir,
                        source_identifier=source,
                        dry_run=False,
                    )

            # Final summary
            print("\n" + "=" * 60)
            print("FINAL SUMMARY")
            print("=" * 60)
            print(f"Total conversations processed: {len(exercise_convs)}")
            print(f"Conversations with sets: {len(all_sets)}")

            if len(combined_df) > 0:
                print(f"\nExtracted sets: {len(combined_df)}")
                print(
                    f"Date range: {combined_df['date'].min()} to {combined_df['date'].max()}"
                )
                print(f"Unique exercises: {combined_df['exercise_name'].nunique()}")
                print("\nTop exercises in extracted sets:")
                print(combined_df["exercise_name"].value_counts().head(10))

                # Read final state for summary if integrated
                if args.integrate or not args.no_integrate:
                    sets_path = base_dir / "data/sets/sets.parquet"
                    if sets_path.exists():
                        final_df = pd.read_parquet(sets_path)
                        print(f"\nFinal sets in database: {len(final_df)}")

            if args.output_csv:
                csv_path = Path(args.output_csv)
                combined_df.to_csv(csv_path, index=False)
                print(f"\n✓ Wrote extracted sets to: {csv_path}")
            else:
                print("\nSample extracted sets:")
                print(combined_df.head(20).to_string())
        else:
            print("\nNo sets extracted from any conversations.")


if __name__ == "__main__":
    main()
