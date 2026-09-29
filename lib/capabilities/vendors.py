"""Vendor adapters behind one small protocol.

Adapters are selected by ``vendor_binding.vendor`` (``recraft``,
``google_image``, ``veo``). They never see the binding store or the spend
ledger: the client authorizes spend first, resolves the credential, and only
then hands the adapter a ``Secret``. HTTP goes through an injected
``Transport`` so tests use fixtures and never touch a network.

No adapter starts a subprocess with the credential in its environment. The one
subprocess (``ffmpeg`` audio stripping) gets an explicit minimal environment.

Pricing
-------
List prices below are used as spend ESTIMATES and, because the vendors return
no billed amount, as the cost recorded per call. They are deliberately
conservative (an upper bound) so the cap errs toward refusing. A model with no
known price refuses (``CAP_UNREADABLE``: the cost cannot be estimated) rather
than being treated as free. Update the tables when a vendor changes prices.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from lib.credential_scrub import GOOGLE_KEY_NAMES, RECRAFT_KEY_NAMES
from .credentials import Secret, redact
from .errors import CapabilityError, EmptyArtifact, VendorFailure
from .slots import IMAGE_GENERATION, VECTOR_MARK_GENERATION, VIDEO_GENERATION

GOOGLE_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GOOGLE_HOST = "generativelanguage.googleapis.com"
_USER_AGENT = "ateles-capabilities/1.0"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class PriceUnknown(CapabilityError):
    """The cost of this call cannot be estimated, so it cannot be authorized."""


class CredentialRouteUnavailable(CapabilityError):
    """This adapter has no host-held credential route yet."""

    def __init__(self, message: str, hint: str) -> None:
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


def urllib_transport(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """Default transport. Never follows redirects, so a credential header is
    never forwarded to a host the adapter did not choose."""
    if not url.startswith("https://"):
        raise VendorFailure("refusing a non-HTTPS vendor URL")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            return HttpResponse(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers or {}))
    except urllib.error.URLError as exc:
        # Only a failure that provably never reached the vendor is "no charge".
        never_sent = isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError))
        raise VendorFailure(
            f"transport failure: {type(exc.reason).__name__}", no_charge=never_sent
        ) from None
    except (TimeoutError, OSError) as exc:
        raise VendorFailure(f"transport failure: {type(exc).__name__}") from None


@dataclass
class VendorOutput:
    data: bytes
    media_type: str
    cost_usd: float
    model_tier: str
    vendor: str
    warnings: tuple[str, ...] = ()


class VendorAdapter(Protocol):
    vendor_id: str
    default_slot: str
    credential_names: tuple[str, ...]

    def estimate_cost(self, model: str, opts: Mapping[str, Any]) -> float: ...

    def generate(
        self,
        *,
        prompt: str,
        model: str,
        opts: Mapping[str, Any],
        credential: Secret | None,
    ) -> VendorOutput: ...


# --- stub (tests) -----------------------------------------------------------


class StubVendor:
    """Deterministic in-memory vendor for tests. Counts every call."""

    def __init__(
        self,
        vendor_id: str = "stub",
        *,
        default_slot: str = IMAGE_GENERATION,
        cost_usd: float = 0.5,
        data: bytes = b"\x89PNG-stub",
        media_type: str = "image/png",
        credential_names: tuple[str, ...] = (),
        fail_with: Exception | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.vendor_id = vendor_id
        self.default_slot = default_slot
        self.credential_names = credential_names
        self.cost_usd = cost_usd
        self.data = data
        self.media_type = media_type
        self.fail_with = fail_with
        self.on_call = on_call
        self.calls = 0
        self.estimates = 0
        self.last_credential: Secret | None = None

    def estimate_cost(self, model: str, opts: Mapping[str, Any]) -> float:
        self.estimates += 1
        return self.cost_usd

    def generate(
        self,
        *,
        prompt: str,
        model: str,
        opts: Mapping[str, Any],
        credential: Secret | None,
    ) -> VendorOutput:
        self.calls += 1
        self.last_credential = credential
        if self.on_call:
            self.on_call()
        if self.fail_with is not None:
            raise self.fail_with
        return VendorOutput(
            data=self.data,
            media_type=self.media_type,
            cost_usd=self.cost_usd,
            model_tier=model,
            vendor=self.vendor_id,
        )


# --- Recraft (OAuth subscription route; no host-held key exists) -------------


class RecraftAdapter:
    """Recraft is a Pro SUBSCRIPTION reached through a hosted MCP server with an
    OAuth session; it has no API key and no API units. A host-held OAuth
    session is not built here, so this adapter refuses with
    ``CREDENTIAL_UNRESOLVED`` before any request or spend. It never assumes a
    ``RECRAFT_API_KEY`` exists (tracked as a follow-up issue).
    """

    vendor_id = "recraft"
    default_slot = VECTOR_MARK_GENERATION
    credential_names = RECRAFT_KEY_NAMES

    def estimate_cost(self, model: str, opts: Mapping[str, Any]) -> float:
        raise CredentialRouteUnavailable(
            "the Recraft route is an OAuth subscription session, which this "
            "client cannot hold yet",
            "No API key exists for Recraft. Operator: use the subscription "
            "connector directly for now; the host-held OAuth adapter is a "
            "tracked follow-up. Nothing was requested or spent.",
        )

    def generate(self, **_: Any) -> VendorOutput:  # pragma: no cover - unreachable
        raise CredentialRouteUnavailable("Recraft route unavailable", "See estimate_cost.")


# --- Google -----------------------------------------------------------------

# Estimated USD per generated image. Upper bounds pending one live smoke call;
# the provider also holds a prepaid backstop.
IMAGE_PRICE_USD: dict[str, float] = {
    "gemini-3-pro-image": 0.24,
    "gemini-3.1-flash-lite-image": 0.10,
}

# USD per second of video by model and resolution.
VEO_PRICE_PER_SECOND_USD: dict[str, dict[str, float]] = {
    "veo-3.1-fast-generate-preview": {"720p": 0.10, "1080p": 0.12, "4k": 0.30},
    "veo-3.1-generate-preview": {"720p": 0.40, "1080p": 0.40, "4k": 0.60},
    "veo-3.1-lite-generate-preview": {"720p": 0.05, "1080p": 0.08},
}
VEO_DURATIONS = (4, 6, 8)


def _check_model(model: str) -> str:
    if not _MODEL_RE.match(model or ""):
        raise PriceUnknown(f"model id {model!r} is not a plain model name")
    return model


def _google_headers(credential: Secret, *, json_body: bool) -> dict[str, str]:
    headers = {"x-goog-api-key": credential.reveal(), "User-Agent": _USER_AGENT}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _json(resp: HttpResponse, credential: Secret, what: str) -> dict[str, Any]:
    if resp.status >= 400:
        detail = redact(resp.body[:300].decode("utf-8", "replace"), credential)
        raise VendorFailure(f"{what} returned HTTP {resp.status}: {detail}", http_status=resp.status)
    try:
        data = json.loads(resp.body or b"{}")
    except ValueError:
        raise VendorFailure(f"{what} returned a non-JSON body", http_status=resp.status) from None
    if not isinstance(data, dict):
        raise VendorFailure(f"{what} returned an unexpected body shape", http_status=resp.status)
    return data


class GoogleImageAdapter:
    """Nano Banana family via ``models/<model>:generateContent``.

    The request shape follows Google's documented ``generateContent`` image
    generation and is exercised with recorded fixtures only. It needs one live
    smoke call before it is relied on.
    """

    vendor_id = "google_image"
    default_slot = IMAGE_GENERATION
    credential_names = GOOGLE_KEY_NAMES

    def __init__(self, transport: Transport | None = None, *, timeout_s: float = 120.0) -> None:
        self._t = transport or urllib_transport
        self._timeout = timeout_s

    def estimate_cost(self, model: str, opts: Mapping[str, Any]) -> float:
        _check_model(model)
        if model not in IMAGE_PRICE_USD:
            raise PriceUnknown(f"no known price for image model {model!r}")
        return IMAGE_PRICE_USD[model]

    def generate(
        self,
        *,
        prompt: str,
        model: str,
        opts: Mapping[str, Any],
        credential: Secret | None,
    ) -> VendorOutput:
        if credential is None:
            raise VendorFailure("no credential supplied to the adapter")
        image_cfg: dict[str, str] = {}
        if opts.get("aspect_ratio"):
            image_cfg["aspectRatio"] = str(opts["aspect_ratio"])
        if opts.get("image_size"):
            image_cfg["imageSize"] = str(opts["image_size"])
        gen_cfg: dict[str, Any] = {"responseModalities": ["IMAGE"]}
        if image_cfg:
            gen_cfg["imageConfig"] = image_cfg
        body = json.dumps(
            {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
        ).encode()
        resp = self._t(
            "POST",
            f"{GOOGLE_API_BASE}/models/{urllib.parse.quote(model, safe='')}:generateContent",
            headers=_google_headers(credential, json_body=True),
            body=body,
            timeout=self._timeout,
        )
        data = _json(resp, credential, "image generation")
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise VendorFailure(f"prompt was blocked by the vendor ({block})", http_status=400)
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                mime = inline.get("mimeType") or inline.get("mime_type") or ""
                blob = inline.get("data") or ""
                if mime.startswith("image/") and blob:
                    try:
                        raw = base64.b64decode(blob, validate=True)
                    except ValueError:
                        raise VendorFailure("image payload was not valid base64") from None
                    if not raw:
                        raise EmptyArtifact("vendor returned an empty image")
                    return VendorOutput(
                        raw, mime, self.estimate_cost(model, opts), model, self.vendor_id
                    )
        raise EmptyArtifact("vendor answered but returned no image part")


class VeoAdapter:
    """Veo 3.1 via ``models/<model>:predictLongRunning`` with polling.

    Verified call shape. ``negativePrompt`` is never sent (the Lite model
    rejects it); ``exclusions`` are folded into the prompt instead. Hero clips
    are silent by default: ``strip_audio`` (default True) removes the
    soundtrack with ``ffmpeg -an -c:v copy``.
    """

    vendor_id = "veo"
    default_slot = VIDEO_GENERATION
    credential_names = GOOGLE_KEY_NAMES

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        poll_interval_s: float = 10.0,
        timeout_s: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ffmpeg: str | None = None,
    ) -> None:
        self._t = transport or urllib_transport
        self._poll = poll_interval_s
        self._deadline = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._ffmpeg = ffmpeg

    @staticmethod
    def _params(opts: Mapping[str, Any]) -> tuple[int, str, str]:
        duration = int(opts.get("duration_seconds", 8))
        resolution = str(opts.get("resolution", "1080p")).lower()
        aspect = str(opts.get("aspect_ratio", "16:9"))
        return duration, resolution, aspect

    def estimate_cost(self, model: str, opts: Mapping[str, Any]) -> float:
        _check_model(model)
        prices = VEO_PRICE_PER_SECOND_USD.get(model)
        if prices is None:
            raise PriceUnknown(f"no known price for video model {model!r}")
        try:
            duration, resolution, _ = self._params(opts)
        except (TypeError, ValueError):
            raise PriceUnknown("duration_seconds is not an integer") from None
        if duration not in VEO_DURATIONS:
            raise PriceUnknown(f"duration_seconds must be one of {VEO_DURATIONS}")
        if resolution not in prices:
            raise PriceUnknown(f"no known price for {model} at resolution {resolution!r}")
        return round(prices[resolution] * duration, 6)

    def _strip_audio(self, data: bytes) -> tuple[bytes, tuple[str, ...]]:
        ffmpeg = self._ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            return data, ("audio_not_stripped: ffmpeg is not installed on this host",)
        with tempfile.TemporaryDirectory(prefix="ateles-veo-") as tmp:
            src, dst = Path(tmp, "in.mp4"), Path(tmp, "out.mp4")
            src.write_bytes(data)
            try:
                proc = subprocess.run(  # noqa: S603
                    [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-an", "-c:v", "copy", str(dst)],
                    capture_output=True,
                    timeout=120,
                    # Explicit minimal environment: no credential ever rides along.
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return data, ("audio_not_stripped: ffmpeg failed to run",)
            if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
                return data, ("audio_not_stripped: ffmpeg could not process the clip",)
            return dst.read_bytes(), ()

    def _download(self, uri: str, credential: Secret) -> bytes:
        parsed = urllib.parse.urlparse(uri)
        try:
            explicit_port = parsed.port
        except ValueError:
            explicit_port = -1
        if (
            parsed.scheme != "https"
            or parsed.hostname != _GOOGLE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or explicit_port is not None
            or "\\" in uri
            or "@" in parsed.netloc
        ):
            # The response is untrusted data: never send the key to a host the
            # adapter did not choose. Userinfo, backslashes and explicit ports
            # are refused outright rather than reasoned about.
            raise VendorFailure("vendor returned a download URI that is not the plain API host")
        resp = self._t(
            "GET", uri, headers=_google_headers(credential, json_body=False), body=None, timeout=180.0
        )
        if not 200 <= resp.status < 300:
            raise VendorFailure(f"video download returned HTTP {resp.status}", http_status=resp.status)
        declared = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if declared is not None and str(declared).isdigit() and int(declared) != len(resp.body):
            raise VendorFailure("video download was truncated")
        return resp.body

    def generate(
        self,
        *,
        prompt: str,
        model: str,
        opts: Mapping[str, Any],
        credential: Secret | None,
    ) -> VendorOutput:
        if credential is None:
            raise VendorFailure("no credential supplied to the adapter")
        cost = self.estimate_cost(model, opts)
        duration, resolution, aspect = self._params(opts)
        text = prompt.strip()
        exclusions = [str(x).strip() for x in (opts.get("exclusions") or []) if str(x).strip()]
        if exclusions:
            text += "\nAvoid: " + "; ".join(exclusions) + "."
        body = json.dumps(
            {
                "instances": [{"prompt": text}],
                "parameters": {
                    "aspectRatio": aspect,
                    "durationSeconds": duration,
                    "resolution": resolution,
                },
            }
        ).encode()
        start = self._t(
            "POST",
            f"{GOOGLE_API_BASE}/models/{urllib.parse.quote(model, safe='')}:predictLongRunning",
            headers=_google_headers(credential, json_body=True),
            body=body,
            timeout=60.0,
        )
        name = _json(start, credential, "video generation start").get("name")
        # From here the vendor has answered 2xx: the job may exist and be
        # billable, so no failure below may be classified as 'did not bill'.
        try:
            if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9/_.-]+$", name):
                raise VendorFailure("video generation did not return an operation name")
            t0 = self._clock()
            while True:
                self._sleep(self._poll)
                op = _json(
                    self._t(
                        "GET",
                        f"{GOOGLE_API_BASE}/{name}",
                        headers=_google_headers(credential, json_body=False),
                        body=None,
                        timeout=60.0,
                    ),
                    credential,
                    "video generation poll",
                )
                if op.get("done"):
                    break
                if self._clock() - t0 > self._deadline:
                    raise VendorFailure("video generation timed out while polling")
            if "error" in op:
                detail = redact(json.dumps(op["error"])[:300], credential)
                raise VendorFailure(f"video generation failed: {detail}", http_status=400)
            samples = (
                (op.get("response") or {}).get("generateVideoResponse", {}).get("generatedSamples") or []
            )
            uri = ((samples[0] if samples else {}).get("video") or {}).get("uri")
            if not uri:
                raise EmptyArtifact("vendor finished but returned no video sample")
            data = self._download(uri, credential)
            if not data:
                raise EmptyArtifact("vendor returned an empty video")
            warnings: tuple[str, ...] = ()
            if opts.get("strip_audio", True):
                data, warnings = self._strip_audio(data)
        except VendorFailure as failure:
            failure.no_charge = False
            raise
        return VendorOutput(data, "video/mp4", cost, model, self.vendor_id, warnings)


def default_adapters(transport: Transport | None = None) -> dict[str, VendorAdapter]:
    adapters: list[VendorAdapter] = [
        RecraftAdapter(),
        GoogleImageAdapter(transport),
        VeoAdapter(transport),
    ]
    return {a.vendor_id: a for a in adapters}
