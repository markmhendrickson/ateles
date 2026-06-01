#!/usr/bin/env python3
"""
Stylize profile image for minimal website aesthetic.

Applies moderate illustration-style treatment matching dionysiandesigns.com minimalism:
- Grayscale conversion with subtle contrast enhancement
- Gentle background simplification
- Moderate posterization for illustrated look
- Subtle edge emphasis
- Minimal, refined aesthetic

Usage:
    python stylize_profile_image.py <input_image> [--output <output_path>] [--background white|black]
"""

import argparse
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:
    print("Error: Missing required libraries. Install with: pip install pillow numpy")
    sys.exit(1)


def stylize_for_minimalism(
    image_path: Path, output_path: Path = None, background: str = "white"
) -> Path:
    """
    Stylize image to match minimal website aesthetic.

    Args:
        image_path: Input image path
        output_path: Output path (default: adds '_minimal' suffix)
        background: 'white' or 'black' for background color

    Returns:
        Path to output image
    """
    # Load image
    img = Image.open(image_path)

    # Convert to RGB if needed (handles RGBA, P, etc.)
    if img.mode != "RGB":
        rgb_img = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            rgb_img.paste(img, mask=img.split()[3])  # Use alpha channel as mask
        else:
            rgb_img.paste(img)
        img = rgb_img

    # Convert to grayscale
    gray = img.convert("L")

    # Convert to numpy for processing
    img_array = np.array(gray, dtype=np.float32)

    # Background detection: assume background is at image edges
    # Sample edge pixels to determine background color
    edge_width = min(50, img_array.shape[1] // 10, img_array.shape[0] // 10)
    edge_pixels = np.concatenate(
        [
            img_array[:edge_width, :].flatten(),  # Top edge
            img_array[-edge_width:, :].flatten(),  # Bottom edge
            img_array[:, :edge_width].flatten(),  # Left edge
            img_array[:, -edge_width:].flatten(),  # Right edge
        ]
    )

    if background == "white":
        # For white background, subtly lighten edge areas
        bg_threshold = np.percentile(edge_pixels, 70)  # 70th percentile of edge pixels
        h, w = img_array.shape
        edge_distance = np.minimum(
            np.minimum(np.arange(h)[:, None], h - 1 - np.arange(h)[:, None]),
            np.minimum(np.arange(w)[None, :], w - 1 - np.arange(w)[None, :]),
        )
        # Create gradient: pixels near edges that are light get gradually lightened
        edge_mask = edge_distance < edge_width
        light_mask = img_array > bg_threshold

        # Subtle lightening: blend toward white based on edge distance
        blend_factor = 1.0 - (edge_distance / edge_width)
        blend_factor = np.clip(blend_factor, 0, 1)
        # Only apply to light pixels near edges
        blend_mask = light_mask & edge_mask
        img_array[blend_mask] = img_array[blend_mask] * (
            1 - blend_factor[blend_mask] * 0.4
        ) + 255 * (blend_factor[blend_mask] * 0.4)
    else:
        # For black background, subtly darken edge areas
        bg_threshold = np.percentile(edge_pixels, 30)  # 30th percentile of edge pixels
        h, w = img_array.shape
        edge_distance = np.minimum(
            np.minimum(np.arange(h)[:, None], h - 1 - np.arange(h)[:, None]),
            np.minimum(np.arange(w)[None, :], w - 1 - np.arange(w)[None, :]),
        )
        edge_mask = edge_distance < edge_width
        dark_mask = img_array < bg_threshold
        dark_mask & edge_mask

        # Subtle darkening: blend toward black based on edge distance
        blend_factor = 1.0 - (edge_distance / edge_width)
        blend_factor = np.clip(blend_factor, 0, 1)
        blend_mask = dark_mask & edge_mask
        img_array[blend_mask] = img_array[blend_mask] * (
            1 - blend_factor[blend_mask] * 0.4
        )

    # Convert back to uint8
    img_array = img_array.astype(np.uint8)

    # Subtle contrast enhancement
    gray = Image.fromarray(img_array)
    enhancer = ImageEnhance.Contrast(gray)
    gray = enhancer.enhance(1.2)  # Subtle contrast boost

    # Brightness adjustment to ensure image is visible
    brightness = ImageEnhance.Brightness(gray)
    gray = brightness.enhance(1.15)  # Moderate brightness boost to prevent darkening

    # Convert back to array for illustration effects
    img_array = np.array(gray, dtype=np.uint8)

    # Stronger posterization for illustration effect (fewer levels = more stylized)
    levels = 6  # Fewer levels to reduce detail and create more illustrated look
    img_array = (img_array / (256 / levels)).astype(np.uint8) * (256 / levels)

    # Convert back to PIL for filtering (ensure L mode)
    gray = Image.fromarray(img_array).convert("L")

    # More aggressive smoothing to reduce photographic detail
    gray = gray.filter(ImageFilter.SMOOTH_MORE)

    # Convert back to array
    img_array = np.array(gray, dtype=np.uint8)

    # Subtle edge emphasis - use edge enhancement filter (lighter than FIND_EDGES)
    gray_pil = Image.fromarray(img_array).convert("L")
    # Use edge enhance for subtle line definition (doesn't darken like FIND_EDGES)
    gray_pil = gray_pil.filter(ImageFilter.EDGE_ENHANCE)

    # Convert back to array
    img_array = np.array(gray_pil, dtype=np.uint8)

    # Convert back to PIL Image
    result = Image.fromarray(img_array)

    # Convert to RGB for output (maintains grayscale but RGB format)
    result_rgb = result.convert("RGB")

    # Determine output path
    if output_path is None:
        stem = image_path.stem
        suffix = image_path.suffix
        output_path = image_path.parent / f"{stem}_minimal{suffix}"

    # Save
    result_rgb.save(output_path, quality=95, optimize=True)

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Stylize profile image for minimal website aesthetic"
    )
    parser.add_argument("input", type=Path, help="Input image path")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output image path (default: adds '_minimal' suffix)",
    )
    parser.add_argument(
        "--background",
        "-b",
        choices=["white", "black"],
        default="white",
        help="Background color: white or black (default: white)",
    )

    args = parser.parse_args()

    # Validate input
    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    try:
        output = stylize_for_minimalism(args.input, args.output, args.background)
        print(f"Stylized image saved to: {output}")
    except Exception as e:
        print(f"Error processing image: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
