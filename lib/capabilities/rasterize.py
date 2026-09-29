"""SVG -> raster preview, so a reviewer (or an image-capable critic) can look.

No new dependency is added. The default renderer uses whichever of these is
already on the host: the ``rsvg-convert`` binary, or the ``cairosvg`` module.
If neither exists ``RasterizerUnavailable`` is raised and the caller keeps the
paid SVG artifact without a preview (it is never discarded). A renderer can
also be injected, which is how tests run.

An empty or non-SVG payload is ``EMPTY_RESULT``: nothing usable came back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .errors import EMPTY_RESULT, GenerationRefused, RasterizerUnavailable

Renderer = Callable[[bytes, int], bytes]


def looks_like_svg(data: bytes) -> bool:
    head = (data or b"")[:4096].lower()
    return b"<svg" in head


def _rsvg(data: bytes, width: int) -> bytes:
    binary = shutil.which("rsvg-convert")
    if not binary:
        raise RasterizerUnavailable("rsvg-convert not found")
    with tempfile.TemporaryDirectory(prefix="ateles-svg-") as tmp:
        src, dst = Path(tmp, "in.svg"), Path(tmp, "out.png")
        src.write_bytes(data)
        subprocess.run(  # noqa: S603
            [binary, "-w", str(width), "-f", "png", "-o", str(dst), str(src)],
            check=True,
            capture_output=True,
            timeout=60,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        return dst.read_bytes()


def _cairosvg(data: bytes, width: int) -> bytes:
    try:
        import cairosvg  # type: ignore[import-not-found]
    except ImportError:
        raise RasterizerUnavailable("cairosvg not installed") from None
    return cairosvg.svg2png(bytestring=data, output_width=width)


def default_renderer(data: bytes, width: int) -> bytes:
    for candidate in (_rsvg, _cairosvg):
        try:
            return candidate(data, width)
        except RasterizerUnavailable:
            continue
    raise RasterizerUnavailable(
        "no SVG rasterizer on this host (install rsvg-convert or cairosvg)"
    )


def rasterize_svg(
    data: bytes,
    dest: Path,
    *,
    slot: str | None = None,
    width: int = 1024,
    renderer: Renderer | None = None,
) -> Path:
    """Write a PNG preview of ``data`` to ``dest`` (mode 0600) and return it."""
    if not data or not looks_like_svg(data):
        raise GenerationRefused(
            EMPTY_RESULT,
            slot,
            "the artifact is empty or is not an SVG document",
            "Treat this as a failed generation; it was not recorded as usable.",
        )
    try:
        png = (renderer or default_renderer)(data, width)
    except RasterizerUnavailable:
        raise
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise RasterizerUnavailable(f"rasterizer failed: {type(exc).__name__}") from None
    if not png:
        raise RasterizerUnavailable("rasterizer produced no bytes")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, png)
    finally:
        os.close(fd)
    return dest
