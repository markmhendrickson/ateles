#!/usr/bin/env python3
"""
Generate a cover image for X/Twitter profile.

Creates a 1500x500px cover image with customizable text, colors, and style.

Usage:
    python generate_cover_image.py [--text "YOUR TEXT"] [--style minimal|bold|gradient] [--output cover.png]
"""

import argparse
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    print("Error: Missing required libraries. Install with: pip install pillow numpy")
    sys.exit(1)


def create_gradient_background(width, height, color1, color2, direction="horizontal"):
    """Create a gradient background."""
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    if direction == "horizontal":
        for x in range(width):
            r = int(color1[0] + (color2[0] - color1[0]) * x / width)
            g = int(color1[1] + (color2[1] - color1[1]) * x / width)
            b = int(color1[2] + (color2[2] - color1[2]) * x / width)
            draw.line([(x, 0), (x, height)], fill=(r, g, b))
    else:  # vertical
        for y in range(height):
            r = int(color1[0] + (color2[0] - color1[0]) * y / height)
            g = int(color1[1] + (color2[1] - color1[1]) * y / height)
            b = int(color1[2] + (color2[2] - color1[2]) * y / height)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

    return img


def create_cover_image(
    text="GROW YOUR BITCOIN",
    subtitle=None,
    style="minimal",
    output_path="cover_image.png",
    width=1500,
    height=500,
):
    """
    Generate a cover image for X/Twitter.

    Args:
        text: Main text to display on cover
        subtitle: Optional subtitle text (displayed below main text)
        style: Style preset ('minimal', 'bold', 'gradient', 'crypto')
        output_path: Output file path
        width: Image width (default 1500 for X/Twitter)
        height: Image height (default 500 for X/Twitter)
    """
    # Create base image
    if style == "minimal":
        # Clean white/light gray background
        img = Image.new("RGB", (width, height), color=(245, 245, 245))
        text_color = (20, 20, 20)
    elif style == "bold":
        # Bold dark background with bright text
        img = Image.new("RGB", (width, height), color=(15, 15, 15))
        text_color = (255, 255, 255)
    elif style == "gradient":
        # Gradient background
        img = create_gradient_background(
            width, height, (30, 30, 50), (10, 10, 20), "horizontal"
        )
        text_color = (255, 255, 255)
    elif style == "crypto":
        # Bitcoin/crypto theme - dark with orange accents
        img = Image.new("RGB", (width, height), color=(20, 20, 25))
        text_color = (255, 140, 0)  # Bitcoin orange
    else:
        # Default: minimal
        img = Image.new("RGB", (width, height), color=(245, 245, 245))
        text_color = (20, 20, 20)

    draw = ImageDraw.Draw(img)

    # Try to load fonts, fallback to default
    try:
        # Try system fonts
        font_paths = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Windows/Fonts/arialbd.ttf",
        ]
        main_font = None
        subtitle_font = None
        for path in font_paths:
            if Path(path).exists():
                try:
                    if main_font is None:
                        main_font = ImageFont.truetype(path, size=80)
                    if subtitle_font is None:
                        subtitle_font = ImageFont.truetype(path, size=50)
                    if main_font and subtitle_font:
                        break
                except:
                    continue

        if main_font is None:
            main_font = ImageFont.load_default()
        if subtitle_font is None:
            subtitle_font = ImageFont.load_default()
    except:
        main_font = ImageFont.load_default()
        subtitle_font = ImageFont.load_default()

    # Calculate main text size and position (centered)
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=main_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    else:
        # Fallback for older PIL versions
        text_width, text_height = draw.textsize(text, font=main_font)

    # Calculate subtitle size if present
    subtitle_height = 0
    subtitle_width = 0
    if subtitle:
        if hasattr(draw, "textbbox"):
            sub_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
            subtitle_width = sub_bbox[2] - sub_bbox[0]
            subtitle_height = sub_bbox[3] - sub_bbox[1]
        else:
            subtitle_width, subtitle_height = draw.textsize(
                subtitle, font=subtitle_font
            )

    # Center both texts vertically
    total_height = text_height + (subtitle_height + 20 if subtitle else 0)
    y_start = (height - total_height) // 2

    x = (width - text_width) // 2
    y = y_start

    # Draw main text with optional shadow/outline for better visibility
    if style in ["bold", "crypto", "gradient"]:
        # Add subtle text shadow for contrast
        shadow_offset = 2
        draw.text(
            (x + shadow_offset, y + shadow_offset),
            text,
            font=main_font,
            fill=(0, 0, 0, 128),
        )

    # Draw main text
    draw.text((x, y), text, font=main_font, fill=text_color)

    # Draw subtitle if provided
    if subtitle:
        subtitle_y = y + text_height + 20
        subtitle_x = (width - subtitle_width) // 2
        subtitle_color = tuple(int(c * 0.7) for c in text_color)  # Slightly dimmer

        if style in ["bold", "crypto", "gradient"]:
            draw.text(
                (subtitle_x + shadow_offset, subtitle_y + shadow_offset),
                subtitle,
                font=subtitle_font,
                fill=(0, 0, 0, 128),
            )

        draw.text(
            (subtitle_x, subtitle_y), subtitle, font=subtitle_font, fill=subtitle_color
        )

    # Save image
    output_path = Path(output_path)
    img.save(output_path, quality=95, optimize=True)

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate a cover image for X/Twitter profile"
    )
    parser.add_argument(
        "--text",
        "-t",
        type=str,
        default="GROW YOUR BITCOIN",
        help='Main text to display (default: "GROW YOUR BITCOIN")',
    )
    parser.add_argument(
        "--subtitle",
        type=str,
        default=None,
        help="Optional subtitle text (displayed below main text)",
    )
    parser.add_argument(
        "--style",
        "-s",
        choices=["minimal", "bold", "gradient", "crypto"],
        default="minimal",
        help="Style preset (default: minimal)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default="cover_image.png",
        help="Output image path (default: cover_image.png)",
    )
    parser.add_argument(
        "--width",
        "-w",
        type=int,
        default=1500,
        help="Image width in pixels (default: 1500)",
    )
    parser.add_argument(
        "--height",
        "-H",
        type=int,
        default=500,
        help="Image height in pixels (default: 500)",
    )

    args = parser.parse_args()

    try:
        output = create_cover_image(
            text=args.text,
            subtitle=args.subtitle,
            style=args.style,
            output_path=args.output,
            width=args.width,
            height=args.height,
        )
        print(f"Cover image generated: {output}")
        print(f"Dimensions: {args.width}x{args.height}px")
    except Exception as e:
        print(f"Error generating cover image: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
