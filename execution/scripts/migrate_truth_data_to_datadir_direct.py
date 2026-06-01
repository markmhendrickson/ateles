#!/usr/bin/env python3
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ""))
if not DATA_DIR or not DATA_DIR.exists():
    print(f"ERROR: DATA_DIR not set or does not exist: {DATA_DIR}")
    exit(1)

TRUTH_DATA = PROJECT_ROOT / "truth" / "data"
TRUTH_TRUTH_DATA = PROJECT_ROOT / "truth" / "truth" / "data"

print(f"Migrating to: {DATA_DIR}\n")

# truth/data migrations
if TRUTH_DATA.exists():
    print(f"Processing {TRUTH_DATA}:")

    # imports/audio
    src = TRUTH_DATA / "imports" / "audio"
    dst = DATA_DIR / "imports" / "audio"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            for item in src.iterdir():
                shutil.move(str(src / item.name), str(dst / item.name))
            src.rmdir()
        else:
            shutil.move(str(src), str(dst))
        print("  ✓ Moved imports/audio")

    # logs
    src = TRUTH_DATA / "logs"
    dst = DATA_DIR / "logs"
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            shutil.move(str(src / item.name), str(dst / item.name))
        src.rmdir()
        print("  ✓ Merged logs")

    # orders
    src = TRUTH_DATA / "orders"
    dst = DATA_DIR / "orders"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print("  ✓ Moved orders")

    # snapshots
    src = TRUTH_DATA / "snapshots"
    dst = DATA_DIR / "snapshots"
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            shutil.move(str(src / item.name), str(dst / item.name))
        src.rmdir()
        print("  ✓ Merged snapshots")

    # transcriptions
    src = TRUTH_DATA / "transcriptions"
    dst = DATA_DIR / "transcriptions"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print("  ✓ Moved transcriptions")

    # Clean up empty directories
    if (TRUTH_DATA / "imports").exists() and not any(
        (TRUTH_DATA / "imports").iterdir()
    ):
        (TRUTH_DATA / "imports").rmdir()
    if TRUTH_DATA.exists() and not any(TRUTH_DATA.iterdir()):
        TRUTH_DATA.rmdir()
        print("  ✓ Removed empty truth/data")

# truth/truth/data migrations
if TRUTH_TRUTH_DATA.exists():
    print(f"\nProcessing {TRUTH_TRUTH_DATA}:")

    # tasks
    src = TRUTH_TRUTH_DATA / "tasks"
    dst = DATA_DIR / "tasks"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print("  ✓ Moved tasks")

    # snapshots
    src = TRUTH_TRUTH_DATA / "snapshots"
    dst = DATA_DIR / "snapshots"
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            shutil.move(str(src / item.name), str(dst / item.name))
        src.rmdir()
        print("  ✓ Merged snapshots")

    # Clean up empty directories
    if TRUTH_TRUTH_DATA.exists() and not any(TRUTH_TRUTH_DATA.iterdir()):
        TRUTH_TRUTH_DATA.rmdir()
        print("  ✓ Removed empty truth/truth/data")
    if (PROJECT_ROOT / "truth" / "truth").exists() and not any(
        (PROJECT_ROOT / "truth" / "truth").iterdir()
    ):
        (PROJECT_ROOT / "truth" / "truth").rmdir()
        print("  ✓ Removed empty truth/truth")

print("\n✓ Migration complete")
