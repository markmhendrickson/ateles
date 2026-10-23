import pytest

from lib.capabilities.errors import EMPTY_RESULT, GenerationRefused, RasterizerUnavailable
from lib.capabilities.rasterize import default_renderer, rasterize_svg

SVG = b"<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/>"


def test_renders_via_injected_renderer_and_locks_permissions(tmp_path):
    dest = rasterize_svg(SVG, tmp_path / "out" / "p.png", renderer=lambda d, w: b"PNGDATA")
    assert dest.read_bytes() == b"PNGDATA"
    assert dest.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("payload", [b"", b"   ", b"<html></html>", b"plain text"])
def test_empty_or_non_svg_is_empty_result(tmp_path, payload):
    with pytest.raises(GenerationRefused) as exc:
        rasterize_svg(payload, tmp_path / "p.png", slot="vector_mark_generation", renderer=lambda d, w: b"x")
    assert exc.value.code == EMPTY_RESULT


def test_renderer_failure_is_unavailable_not_a_crash(tmp_path):
    def bad(d, w):
        raise OSError("boom")

    with pytest.raises(RasterizerUnavailable):
        rasterize_svg(SVG, tmp_path / "p.png", renderer=bad)
    with pytest.raises(RasterizerUnavailable):
        rasterize_svg(SVG, tmp_path / "p.png", renderer=lambda d, w: b"")


def test_default_renderer_reports_unavailable_when_nothing_is_installed(monkeypatch):
    import shutil
    import sys

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "cairosvg", None)  # makes `import cairosvg` fail
    with pytest.raises(RasterizerUnavailable):
        default_renderer(SVG, 64)
