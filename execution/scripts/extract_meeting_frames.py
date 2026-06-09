#!/usr/bin/env python3
"""
Extract still frames from a meeting video recording and optionally link them to a
Neotoma transcription entity.

Usage:
    python extract_meeting_frames.py meeting.mp4
    python extract_meeting_frames.py meeting.mp4 --interval 60
    python extract_meeting_frames.py meeting.mp4 --transcription-id ent_abc123
    python extract_meeting_frames.py meeting.mp4 --output-dir /custom/frames/dir

Frames are written as JPEG files alongside the video (or to --output-dir) and can
later be sent to a vision LLM for contextual analysis against the transcript.

Optional env:
    RECORD_MEETING_FRAME_INTERVAL  seconds between frames (default 60)
    NEOTOMA_PROD_BASE_URL          prod Neotoma base URL (default http://localhost:3180)
    NEOTOMA_BEARER_TOKEN           bearer token for Neotoma API
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

DEFAULT_INTERVAL = int(os.environ.get("RECORD_MEETING_FRAME_INTERVAL", "60"))


def extract_frames(video_path: Path, output_dir: Path, interval_sec: int) -> list[Path]:
    """Extract one frame every interval_sec seconds using ffmpeg. Returns sorted frame paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = video_path.stem
    pattern = output_dir / f"{stem}_frame_%04d.jpg"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps=1/{interval_sec},format=yuvj420p",
        "-qscale:v",
        "3",
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error:\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"ffmpeg frame extraction failed (exit {result.returncode})")

    frames = sorted(output_dir.glob(f"{stem}_frame_*.jpg"))
    return frames


def _neotoma_prod_base_url() -> str:
    return os.environ.get("NEOTOMA_PROD_BASE_URL", "http://localhost:3180")


def _neotoma_cli_available() -> bool:
    result = subprocess.run(["neotoma", "--help"], capture_output=True)
    return result.returncode == 0


def link_frames_to_transcription(
    frames: list[Path],
    transcription_id: str,
    video_path: Path,
) -> None:
    """Store each frame as a source file in Neotoma and create REFERS_TO from transcription."""
    base_url = _neotoma_prod_base_url()
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not bearer:
        print(
            "Warning: NEOTOMA_BEARER_TOKEN not set; skipping Neotoma frame linking.",
            file=sys.stderr,
        )
        return
    if not _neotoma_cli_available():
        print(
            "Warning: neotoma CLI not found; skipping frame linking.", file=sys.stderr
        )
        return

    print(f"Linking {len(frames)} frames to transcription {transcription_id} …")
    linked = 0
    for frame in frames:
        try:
            result = subprocess.run(
                [
                    "neotoma",
                    "--json",
                    "--api-only",
                    "--base-url",
                    base_url,
                    "store",
                    "--file-path",
                    str(frame),
                    "--entities",
                    f'[{{"entity_type":"meeting_frame","source_file":"{frame.name}","transcription_entity_id":"{transcription_id}","video_file":"{video_path.name}"}}]',
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "NEOTOMA_BEARER_TOKEN": bearer},
            )
            if result.returncode != 0:
                print(
                    f"  {frame.name}: store error — {result.stderr.strip()[:120]}",
                    file=sys.stderr,
                )
                continue
            linked += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"  {frame.name}: {e}", file=sys.stderr)
    print(f"Linked {linked}/{len(frames)} frames.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract still frames from a meeting video for LLM analysis."
    )
    parser.add_argument(
        "video_file", type=Path, help="Path to the meeting video (MP4)."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"Extract one frame every N seconds (default: {DEFAULT_INTERVAL}, "
        "env: RECORD_MEETING_FRAME_INTERVAL).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write frames (default: same directory as video).",
    )
    parser.add_argument(
        "--transcription-id",
        type=str,
        default=None,
        metavar="ENT_ID",
        help="Neotoma transcription entity ID to link frames to (REFERS_TO).",
    )
    parser.add_argument(
        "--no-link",
        action="store_true",
        help="Extract frames only; skip Neotoma linking even if --transcription-id is given.",
    )
    args = parser.parse_args()

    video_path = args.video_file.resolve()
    if not video_path.exists():
        print(f"Error: video file not found: {video_path}", file=sys.stderr)
        return 1

    output_dir = args.output_dir or video_path.parent / "frames"

    print(f"Extracting frames every {args.interval}s from: {video_path}")
    try:
        frames = extract_frames(video_path, output_dir, args.interval)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not frames:
        print("No frames extracted (video may be too short for the given interval).")
        return 0

    print(f"Extracted {len(frames)} frame(s) to: {output_dir}")
    for f in frames:
        print(f"  {f}")

    if args.transcription_id and not args.no_link:
        link_frames_to_transcription(frames, args.transcription_id, video_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
