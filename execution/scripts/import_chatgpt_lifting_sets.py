#!/usr/bin/env python3
"""
Import lifting sets from ChatGPT "Track lifting progression" conversation.

This script uses LLM inference to extract structured exercise set data from
conversational chat exports, analyzing both user and assistant messages together
with full context.
"""

import importlib.util
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd


def extract_sets_from_conversation(
    conv_path: Path,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Extract all sets from the conversation JSON using LLM inference.

    Args:
        conv_path: Path to conversation JSON
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

    parser = conversation_parser_class()
    messages = parser.load_conversation(conv_path)
    schema = get_exercise_sets_schema()

    print(f"Using inference-based extraction for {len(messages)} messages...")
    print(f"Conversation title: {conv_path.stem}")

    records = parser.extract_with_schema(
        messages,
        schema,
        conversation_title=conv_path.stem,
    )

    print(f"\nExtracted {len(records)} raw records via inference")

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
                "notes": f"Location: {record.get('location', '')}. Time: {record.get('time_of_day', '')}. Laterality: {record.get('laterality', 'bilateral')}. {record.get('notes', '')}",
                "import_date": date.today().isoformat(),
                "import_source_file": "chatgpt:track_lifting_progression",
            }
        )

    print("\nRecord filtering summary:")
    print(f"  Raw records from inference: {len(records)}")
    print(f"  Skipped (no repetitions):   {skipped_no_reps}")
    print(f"  Skipped (invalid reps):     {skipped_bad_reps}")
    print(f"  Skipped (no exercise):      {skipped_no_exercise}")
    print(f"  Final records kept:         {len(all_sets)}")

    # Convert to DataFrame
    df = pd.DataFrame(all_sets)

    if len(df) > 0:
        # Ensure proper types
        df["repetitions"] = df["repetitions"].astype(float)

        # Show summary
        print("\nSummary:")
        print(f"  Date range: {df['date'].min()} to {df['date'].max()}")
        print(f"  Unique exercises: {df['exercise_name'].nunique()}")
        print(f"  Total sets: {len(df)}")
        print(f"  Warmup sets: {len(df[df['type'] == 'Warmup'])}")
        print(f"  Working sets: {len(df[df['type'] == 'Target failure'])}")

        print("\nTop exercises:")
        print(df["exercise_name"].value_counts().head(10))

    return df


def integrate_with_existing_sets(
    new_df: pd.DataFrame, sets_path: Path, snapshot_dir: Path, dry_run: bool = False
) -> None:
    """Integrate new sets with existing sets.parquet."""

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

    # Remove any existing sets from this specific ChatGPT import to make re-runs idempotent
    mask_chatgpt = (
        existing_df["import_source_file"] == "chatgpt:track_lifting_progression"
    )
    removed_count = int(mask_chatgpt.sum())
    if removed_count > 0:
        existing_df = existing_df[~mask_chatgpt].copy()
        print(
            f"\nRemoved {removed_count} existing ChatGPT-derived sets (re-import safe)."
        )

    # Convert date fields from strings to date objects to match parquet schema
    if new_df["date"].dtype == "object":
        new_df["date"] = pd.to_datetime(new_df["date"]).dt.date
    if new_df["import_date"].dtype == "object":
        new_df["import_date"] = pd.to_datetime(new_df["import_date"]).dt.date

    # Combine
    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    print(f"Combined total: {len(combined_df)} sets")

    # Write back
    if not dry_run:
        combined_df.to_parquet(sets_path, index=False)
        print(f"✓ Successfully wrote to {sets_path}")

        # Verify
        verify_df = pd.read_parquet(sets_path)
        print(f"✓ Verified: {len(verify_df)} rows in sets.parquet")
    else:
        print("DRY RUN: Would write to sets.parquet")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Import lifting sets from ChatGPT conversation using LLM inference"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and display results without writing to parquet",
    )
    parser.add_argument(
        "--output-csv", type=str, help="Output parsed sets to CSV for inspection"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Auto-confirm writing to parquet"
    )

    args = parser.parse_args()

    # Paths
    base_dir = Path(__file__).parent.parent
    conv_path = base_dir / "data/imports/chatgpt/track_lifting_progression.json"
    sets_path = base_dir / "data/sets/sets.parquet"
    snapshot_dir = base_dir / "data/snapshots"

    # Extract sets
    new_df = extract_sets_from_conversation(conv_path, dry_run=args.dry_run)

    if len(new_df) == 0:
        print("No sets extracted. Exiting.")
        return

    # Save to CSV if requested
    if args.output_csv:
        csv_path = Path(args.output_csv)
        new_df.to_csv(csv_path, index=False)
        print(f"\nWrote parsed sets to: {csv_path}")

    # Display sample
    print("\nSample sets:")
    print(new_df.head(20).to_string())

    # Integrate with existing
    if not args.dry_run:
        if args.yes:
            integrate_with_existing_sets(new_df, sets_path, snapshot_dir, dry_run=False)
        else:
            response = input("\nProceed with writing to sets.parquet? (yes/no): ")
            if response.lower() in ["yes", "y"]:
                integrate_with_existing_sets(
                    new_df, sets_path, snapshot_dir, dry_run=False
                )
            else:
                print("Cancelled")
    else:
        print("\n=== DRY RUN MODE ===")
        integrate_with_existing_sets(new_df, sets_path, snapshot_dir, dry_run=True)


if __name__ == "__main__":
    main()
