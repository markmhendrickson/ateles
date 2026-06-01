#!/usr/bin/env python3
"""
YouTube Transcript Extraction Script

Extracts transcripts from YouTube videos using youtube-transcript-api.

Usage:
    python get_youtube_transcript.py <youtube_url> [--language <language_code>] [--save]

Examples:
    python get_youtube_transcript.py https://www.youtube.com/watch?v=BYizgB2FcAQ
    python get_youtube_transcript.py https://www.youtube.com/watch?v=BYizgB2FcAQ --language en
    python get_youtube_transcript.py https://www.youtube.com/watch?v=BYizgB2FcAQ --save
"""

import argparse
import re
import sys
from pathlib import Path

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )
except ImportError:
    print("Error: youtube-transcript-api not installed.")
    print("Install with: pip install youtube-transcript-api")
    sys.exit(1)

# Try to import list_transcripts if available (newer API)
try:
    from youtube_transcript_api import YouTubeTranscriptApi as YTApi

    HAS_LIST_TRANSCRIPTS = hasattr(YTApi, "list_transcripts")
except:
    HAS_LIST_TRANSCRIPTS = False


def extract_video_id(url: str) -> str:
    """Extract video ID from YouTube URL."""
    # Handle various YouTube URL formats
    patterns = [
        r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com\/watch\?.*v=([a-zA-Z0-9_-]{11})",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    # If no pattern matches, assume the input is already a video ID
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url):
        return url

    raise ValueError(f"Could not extract video ID from URL: {url}")


def get_transcript(video_id: str, language_codes: list[str] | None = None) -> dict:
    """
    Get transcript for a YouTube video.

    Args:
        video_id: YouTube video ID
        language_codes: List of language codes to try (e.g., ['en', 'es'])

    Returns:
        Dictionary with transcript data and metadata
    """
    try:
        # Create API instance
        api = YouTubeTranscriptApi()

        # Try to get transcript in requested language(s)
        transcript_data = None
        used_language = None

        if language_codes:
            for lang_code in language_codes:
                try:
                    transcript_data = api.fetch(video_id, languages=[lang_code])
                    used_language = lang_code
                    break
                except NoTranscriptFound:
                    continue
                except Exception:
                    continue

        # If no specific language found, try without language specification
        if transcript_data is None:
            try:
                transcript_data = api.fetch(video_id)
                used_language = "auto"
            except Exception as e:
                return {
                    "success": False,
                    "error": f"No transcript found: {str(e)}",
                }

        # Get metadata (limited with simple API)
        metadata = {
            "video_id": video_id,
            "language": used_language if used_language else "unknown",
            "language_code": used_language if used_language else "unknown",
            "is_generated": None,  # Not available with simple API
            "is_translatable": None,  # Not available with simple API
        }

        # Extract text from transcript snippets
        # Handle both dict-like and object-like transcript items
        transcript_list = []
        full_text_parts = []

        for item in transcript_data:
            if isinstance(item, dict):
                transcript_list.append(item)
                full_text_parts.append(item.get("text", ""))
            else:
                # It's a FetchedTranscriptSnippet object
                transcript_list.append(
                    {
                        "text": item.text,
                        "start": item.start,
                        "duration": item.duration,
                    }
                )
                full_text_parts.append(item.text)

        return {
            "success": True,
            "metadata": metadata,
            "transcript": transcript_list,
            "full_text": " ".join(full_text_parts),
        }

    except TranscriptsDisabled:
        return {
            "success": False,
            "error": "Transcripts are disabled for this video",
        }
    except NoTranscriptFound:
        return {
            "success": False,
            "error": "No transcript found for this video",
        }
    except VideoUnavailable:
        return {
            "success": False,
            "error": "Video is unavailable",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error fetching transcript: {str(e)}",
        }


def save_transcript(
    transcript_data: dict, video_id: str, output_dir: Path | None = None
) -> Path:
    """
    Save transcript to a text file.

    Args:
        transcript_data: Transcript data dictionary
        video_id: YouTube video ID
        output_dir: Optional output directory (defaults to current directory)

    Returns:
        Path to saved file
    """
    if output_dir is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"youtube_transcript_{video_id}.txt"

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("YouTube Video Transcript\n")
        f.write(f"Video ID: {video_id}\n")
        f.write(
            f"Language: {transcript_data['metadata']['language']} ({transcript_data['metadata']['language_code']})\n"
        )
        f.write(
            f"Generated: {'Yes' if transcript_data['metadata']['is_generated'] else 'No'}\n"
        )
        f.write(f"{'=' * 80}\n\n")
        f.write(transcript_data["full_text"])

    return output_file


def main():
    parser = argparse.ArgumentParser(
        description="Extract transcript from YouTube video"
    )
    parser.add_argument("url", type=str, help="YouTube video URL or video ID")
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Preferred language code (e.g., en, es, fr). Will try multiple if not found.",
    )
    parser.add_argument("--save", action="store_true", help="Save transcript to file")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for saved transcript (default: current directory)",
    )

    args = parser.parse_args()

    # Extract video ID
    try:
        video_id = extract_video_id(args.url)
        print(f"Video ID: {video_id}")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Determine language codes to try
    language_codes = None
    if args.language:
        language_codes = [args.language]

    # Get transcript
    print("Fetching transcript...")
    result = get_transcript(video_id, language_codes)

    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    # Display metadata
    metadata = result["metadata"]
    print("\nTranscript found:")
    print(f"  Language: {metadata['language']} ({metadata['language_code']})")
    print(
        f"  Generated: {'Yes (auto-generated)' if metadata['is_generated'] else 'No (manually created)'}"
    )
    print(f"  Translatable: {'Yes' if metadata['is_translatable'] else 'No'}")

    # Display transcript
    print(f"\n{'=' * 80}")
    print("TRANSCRIPT:")
    print(f"{'=' * 80}\n")
    print(result["full_text"])
    print(f"\n{'=' * 80}")

    # Save if requested
    if args.save:
        output_dir = Path(args.output_dir) if args.output_dir else None
        output_file = save_transcript(result, video_id, output_dir)
        print(f"\nTranscript saved to: {output_file}")


if __name__ == "__main__":
    main()
