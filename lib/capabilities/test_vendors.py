"""Adapters against fake transports and recorded-shape fixtures. No network."""

import base64
import json
import shutil
import subprocess

import pytest

from lib.capabilities import slots
from lib.capabilities.credentials import Secret
from lib.capabilities.errors import (
    CAP_UNREADABLE,
    CREDENTIAL_UNRESOLVED,
    EmptyArtifact,
    GenerationRefused,
    VendorFailure,
)
from lib.capabilities.vendors import (
    GoogleImageAdapter,
    HttpResponse,
    RecraftAdapter,
    VeoAdapter,
    default_adapters,
)

from .conftest import binding_row

KEY = Secret("fake-google-key-000111")


class Fake:
    """Scripted transport; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(obj):
    return HttpResponse(200, json.dumps(obj).encode())


# --- Google image -------------------------------------------------------------


def _image_response(data=b"\x89PNG-real", mime="image/png", key="inlineData"):
    part = {key: {("mimeType" if key == "inlineData" else "mime_type"): mime, "data": base64.b64encode(data).decode()}}
    return ok({"candidates": [{"content": {"parts": [{"text": "hi"}, part]}}]})


def test_image_request_shape_and_result():
    t = Fake(_image_response())
    out = GoogleImageAdapter(t).generate(
        prompt="a fox", model="gemini-3-pro-image",
        opts={"aspect_ratio": "16:9", "image_size": "2K"}, credential=KEY,
    )
    (method, url, headers, body), = t.requests
    assert method == "POST"
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent"
    assert headers["x-goog-api-key"] == KEY.reveal()
    payload = json.loads(body)
    assert payload["contents"][0]["parts"][0]["text"] == "a fox"
    assert payload["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert payload["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert out.data == b"\x89PNG-real" and out.media_type == "image/png"
    assert out.cost_usd == 0.24 and out.vendor == "google_image"


def test_image_accepts_snake_case_inline_data():
    t = Fake(_image_response(key="inline_data"))
    out = GoogleImageAdapter(t).generate(prompt="p", model="gemini-3-pro-image", opts={}, credential=KEY)
    assert out.data


def test_image_with_no_image_part_is_empty():
    t = Fake(ok({"candidates": [{"content": {"parts": [{"text": "sorry"}]}}]}))
    with pytest.raises(EmptyArtifact):
        GoogleImageAdapter(t).generate(prompt="p", model="gemini-3-pro-image", opts={}, credential=KEY)


def test_image_prompt_block_is_a_non_retryable_vendor_failure():
    t = Fake(ok({"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(VendorFailure) as exc:
        GoogleImageAdapter(t).generate(prompt="p", model="gemini-3-pro-image", opts={}, credential=KEY)
    assert not exc.value.retryable


def test_image_http_errors_are_classified_and_never_echo_the_key():
    body = json.dumps({"error": {"message": f"bad key {KEY.reveal()}"}}).encode()
    t = Fake(HttpResponse(403, body))
    with pytest.raises(VendorFailure) as exc:
        GoogleImageAdapter(t).generate(prompt="p", model="gemini-3-pro-image", opts={}, credential=KEY)
    assert exc.value.http_status == 403 and not exc.value.retryable
    assert KEY.reveal() not in str(exc.value)
    t = Fake(HttpResponse(503, b"{}"))
    with pytest.raises(VendorFailure) as exc:
        GoogleImageAdapter(t).generate(prompt="p", model="gemini-3-pro-image", opts={}, credential=KEY)
    assert exc.value.retryable


def test_model_id_cannot_inject_into_the_url():
    from lib.capabilities.vendors import PriceUnknown

    with pytest.raises(PriceUnknown):
        GoogleImageAdapter(Fake()).estimate_cost("x/../../evil?y=", {})


# --- Veo -----------------------------------------------------------------------

VIDEO_URI = "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"


def _veo(transport, **kw):
    return VeoAdapter(transport, sleep=lambda s: None, ffmpeg="/nonexistent/ffmpeg", **kw)


def _done(uri=VIDEO_URI):
    return ok({"done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}}})


def test_veo_call_sequence_and_shape():
    t = Fake(
        ok({"name": "models/veo-3.1-fast-generate-preview/operations/op1"}),
        ok({"done": False}),
        _done(),
        HttpResponse(200, b"mp4-bytes"),
    )
    out = _veo(t).generate(
        prompt="a desk", model="veo-3.1-fast-generate-preview",
        opts={"strip_audio": False, "exclusions": ["text", "people"]}, credential=KEY,
    )
    start, poll1, poll2, download = t.requests
    assert start[1].endswith("/models/veo-3.1-fast-generate-preview:predictLongRunning")
    body = json.loads(start[3])
    assert body["parameters"] == {"aspectRatio": "16:9", "durationSeconds": 8, "resolution": "1080p"}
    assert "negativePrompt" not in json.dumps(body)  # Lite rejects it; folded in instead
    assert "Avoid: text; people." in body["instances"][0]["prompt"]
    assert poll1[0] == "GET" and poll1[1].endswith("/operations/op1")
    assert download[1] == VIDEO_URI and download[2]["x-goog-api-key"] == KEY.reveal()
    assert out.data == b"mp4-bytes" and out.media_type == "video/mp4"
    assert out.cost_usd == pytest.approx(0.96)  # 8s * $0.12 at 1080p


@pytest.mark.parametrize(
    "model, res, dur, cost",
    [
        ("veo-3.1-fast-generate-preview", "720p", 8, 0.8),
        ("veo-3.1-fast-generate-preview", "4k", 8, 2.4),
        ("veo-3.1-generate-preview", "1080p", 8, 3.2),
        ("veo-3.1-lite-generate-preview", "1080p", 4, 0.32),
    ],
)
def test_veo_price_table(model, res, dur, cost):
    a = _veo(Fake())
    assert a.estimate_cost(model, {"resolution": res, "duration_seconds": dur}) == pytest.approx(cost)


@pytest.mark.parametrize(
    "model, opts",
    [
        ("veo-9-unknown", {}),
        ("veo-3.1-lite-generate-preview", {"resolution": "4k"}),
        ("veo-3.1-fast-generate-preview", {"duration_seconds": 30}),
        ("veo-3.1-fast-generate-preview", {"duration_seconds": "eight"}),
    ],
)
def test_veo_unpriced_calls_cannot_be_estimated(model, opts):
    from lib.capabilities.vendors import PriceUnknown

    with pytest.raises(PriceUnknown):
        _veo(Fake()).estimate_cost(model, opts)


def test_veo_never_sends_the_key_to_a_host_the_response_names():
    t = Fake(ok({"name": "operations/o"}), _done("https://evil.example.com/steal.mp4"))
    with pytest.raises(VendorFailure):
        _veo(t).generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={}, credential=KEY)
    assert len(t.requests) == 2  # no download request was made


def test_veo_timeout_is_a_retryable_vendor_failure():
    ticks = iter(range(0, 10_000, 400))
    t = Fake(ok({"name": "operations/o"}), ok({"done": False}), ok({"done": False}), ok({"done": False}))
    a = VeoAdapter(t, sleep=lambda s: None, clock=lambda: next(ticks), timeout_s=600, ffmpeg="/x")
    with pytest.raises(VendorFailure) as exc:
        a.generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={}, credential=KEY)
    assert exc.value.retryable


def test_veo_operation_error_and_empty_samples():
    t = Fake(ok({"name": "operations/o"}), ok({"done": True, "error": {"message": "quota"}}))
    with pytest.raises(VendorFailure):
        _veo(t).generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={}, credential=KEY)
    t = Fake(ok({"name": "operations/o"}), ok({"done": True, "response": {}}))
    with pytest.raises(EmptyArtifact):
        _veo(t).generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={}, credential=KEY)


def test_veo_without_ffmpeg_keeps_clip_and_warns_that_audio_remains():
    t = Fake(ok({"name": "operations/o"}), _done(), HttpResponse(200, b"mp4"))
    out = _veo(t).generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={}, credential=KEY)
    assert out.data == b"mp4"
    assert any(w.startswith("audio_not_stripped") for w in out.warnings)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg not installed")
def test_veo_strips_the_soundtrack_for_real(tmp_path):
    src = tmp_path / "with_audio.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10",
         "-f", "lavfi", "-i", "sine=d=1", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)],
        check=True,
    )
    t = Fake(ok({"name": "operations/o"}), _done(), HttpResponse(200, src.read_bytes()))
    a = VeoAdapter(t, sleep=lambda s: None)
    out = a.generate(prompt="p", model="veo-3.1-fast-generate-preview", opts={"duration_seconds": 4}, credential=KEY)
    assert out.warnings == ()
    dst = tmp_path / "out.mp4"
    dst.write_bytes(out.data)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True, check=True,
    )
    assert "audio" not in probe.stdout and "video" in probe.stdout


# --- Recraft: refuses, spends nothing -----------------------------------------


def test_recraft_refuses_with_credential_unresolved_and_makes_no_request(make_client):
    rows = [
        binding_row(
            slots.VECTOR_MARK_GENERATION, "recraft",
            constraints={"model_tier": "recraft-v4-vector", "monthly_cap_usd": 16, "billing": "subscription_credits"},
            credential_location="oauth:recraft-mcp (OAuth session; no RECRAFT_API_KEY exists)",
            fallback="None by design",
        )
    ]
    client = make_client(rows, adapters={"recraft": RecraftAdapter()})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(slots.VECTOR_MARK_GENERATION, "mark")
    assert exc.value.code == CREDENTIAL_UNRESOLVED
    assert "No API key exists" in exc.value.hint
    assert "export" not in exc.value.hint.lower()


def test_recraft_does_not_fall_through_to_a_paid_google_raster(make_client):
    """Real binding shape: vector fallback is google_image. A missing OAuth
    route must not silently spend on a raster."""
    called = []
    google = GoogleImageAdapter(lambda *a, **k: called.append(1))
    rows = [
        binding_row(slots.VECTOR_MARK_GENERATION, "recraft", constraints={"model_tier": "r", "monthly_cap_usd": 16},
                    credential_location="oauth:recraft-mcp", fallback="google_image"),
        binding_row(slots.IMAGE_GENERATION, "google_image",
                    constraints={"model_tier": "gemini-3-pro-image", "monthly_cap_usd": 50},
                    credential_location="GEMINI_API_KEY"),
    ]
    client = make_client(rows, adapters={"recraft": RecraftAdapter(), "google_image": google},
                         process_values={"GEMINI_API_KEY": "k" * 20})
    with pytest.raises(GenerationRefused) as exc:
        client.generate(slots.VECTOR_MARK_GENERATION, "mark")
    assert exc.value.code == CREDENTIAL_UNRESOLVED and called == []


def test_default_adapters_cover_the_three_vendor_ids():
    assert set(default_adapters()) == {"recraft", "google_image", "veo"}


# --- end-to-end through the client with the real Google adapter ----------------


def test_client_with_google_adapter_records_priced_spend(make_client, sink):
    t = Fake(_image_response())
    rows = [binding_row(slots.IMAGE_GENERATION, "google_image",
                        constraints={"model_tier": "gemini-3-pro-image", "monthly_cap_usd": 50,
                                     "cap_group": "google_generation", "cap_group_total_usd": 50},
                        credential_location="GEMINI_API_KEY", fallback="None by design")]
    client = make_client(rows, adapters={"google_image": GoogleImageAdapter(t)},
                         process_values={"GEMINI_API_KEY": "fake-google-key-000111"})
    result = client.generate(slots.IMAGE_GENERATION, "a fox")
    assert result.cost_usd == 0.24 and result.remaining_cap_usd == 49.76
    assert t.requests[0][2]["x-goog-api-key"] == "fake-google-key-000111"
    assert "fake-google-key-000111" not in json.dumps(sink.stored)
