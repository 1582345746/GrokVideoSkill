#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterable

from gvs_common import (
    APIError,
    MAX_JSON_BYTES,
    SkillError,
    api_url,
    assert_mp4,
    decode_data_url,
    download_file,
    image_extension,
    multipart_body,
    request_bytes,
    request_json,
)
from provider_contracts import (
    COMPLETED_STATES,
    FAILED_STATES,
    TASK_POLL_RETRYABLE_HTTP,
    CircuitBreaker,
    ProviderTaskFailedError,
    is_completed,
    result_urls,
    safe_operation,
    task_error,
    task_id,
    task_status,
)


MAX_PROMPT_BYTES = 4096
# Compatibility alias retained for callers that imported the v1 name.
MAX_PROMPT_CHARS = MAX_PROMPT_BYTES
MAX_VIDEO_SECONDS = 15
VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
REFERENCE_CANVASES = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1024, 1024),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "2:3": (800, 1200),
    "3:2": (1200, 800),
}


def size_orientation_matches_aspect_ratio(size: str, aspect_ratio: str) -> bool:
    try:
        width, height = (int(item) for item in size.lower().split("x", 1))
        ratio_width, ratio_height = (int(item) for item in aspect_ratio.split(":", 1))
    except (TypeError, ValueError):
        return False
    return (width > height) == (ratio_width > ratio_height) and (width < height) == (ratio_width < ratio_height)


def _validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise SkillError("prompt is required")
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise SkillError(f"prompt has {prompt_bytes} UTF-8 bytes; provider maximum is {MAX_PROMPT_BYTES}")


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
            if segment_length >= 7:
                return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big")
        index += segment_length
    return None


def image_reference_report(path: Path, aspect_ratio: str) -> dict[str, Any]:
    """Inspect an I2V still without changing the user's source file."""
    report: dict[str, Any] = {
        "path": path.as_posix(),
        "bytes": 0,
        "format": "unknown",
        "width": 0,
        "height": 0,
        "aspect_ratio": "",
        "expected_aspect_ratio": aspect_ratio,
        "aspect_match": None,
        "clarity": "unknown",
        "warnings": [],
        "errors": [],
    }
    if not path.is_file():
        report["errors"].append("reference image is missing")
        return report
    report["bytes"] = path.stat().st_size
    if report["bytes"] <= 0:
        report["errors"].append("reference image is empty")
        return report
    if report["bytes"] > 50 * 1024 * 1024:
        report["errors"].append("reference image exceeds 50 MB")
        return report
    header = path.read_bytes()[:256 * 1024]
    dimensions = _png_dimensions(header)
    if dimensions:
        report["format"] = "png"
    else:
        dimensions = _jpeg_dimensions(header)
        if dimensions:
            report["format"] = "jpeg"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            report["format"] = "webp"
            # WebP variants have different headers; ffmpeg conversion below is
            # the authoritative decoder when dimensions cannot be read here.
    if dimensions:
        width, height = dimensions
        if not (1 <= width <= 16384 and 1 <= height <= 16384):
            dimensions = None
            report["format"] = "unknown"
    if dimensions:
        width, height = dimensions
        report["width"], report["height"] = width, height
        if width <= 0 or height <= 0:
            report["errors"].append("reference image dimensions are invalid")
        else:
            report["aspect_ratio"] = f"{width}:{height}"
            if aspect_ratio in REFERENCE_CANVASES:
                expected_width, expected_height = REFERENCE_CANVASES[aspect_ratio]
                actual = width / height
                expected = expected_width / expected_height
                report["aspect_match"] = abs(actual - expected) / expected <= 0.03
                if not report["aspect_match"]:
                    report["warnings"].append(f"reference ratio {width}:{height} differs from target {aspect_ratio}; request will be center-cropped")
            if min(width, height) < 512:
                report["clarity"] = "low"
                report["warnings"].append("reference image has a short side below 512 pixels")
            else:
                report["clarity"] = "ok"
    else:
        report["warnings"].append("reference dimensions could not be decoded before provider submission")
    return report


def _prepare_quickai_reference(path: Path, aspect_ratio: str, temporary_directory: Path) -> Path:
    """Convert a valid still to a gateway-compatible JPEG canvas."""
    report = image_reference_report(path, aspect_ratio)
    if report["errors"]:
        raise SkillError(f"invalid I2V reference {path.name}: {'; '.join(report['errors'])}")
    if report["format"] == "unknown":
        # Keep malformed test fixtures and let the provider return its own
        # error; real projects are caught by the preflight warning above.
        return path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to normalize QuickAI I2V references")
    width, height = REFERENCE_CANVASES.get(aspect_ratio, (1280, 720))
    output = temporary_directory / f"{uuid.uuid4().hex}.jpg"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise SkillError(f"could not normalize I2V reference {path.name} to JPEG: {detail or 'ffmpeg failed'}")
    return output


def _model_ids(payload: dict[str, Any]) -> list[str]:
    data: Any = payload.get("data", payload.get("models", []))
    if isinstance(data, dict):
        data = data.get("data", data.get("models", []))
    if not isinstance(data, list):
        return []
    result: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            if model_id:
                result.append(model_id)
    return sorted(set(result))


def _read_url_image(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise SkillError("image result URL must use HTTPS; HTTP is allowed only for loopback tests")
    payload, _ = request_bytes("GET", url, accept="image/*,application/octet-stream", timeout=180, max_bytes=48 * 1024 * 1024)
    image_extension(payload)
    return payload


def _extract_image(payload: dict[str, Any]) -> bytes:
    data: Any = payload.get("data")
    if isinstance(data, dict):
        data = data.get("data", data.get("images", data))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise SkillError("image provider returned no image data")
    item = data[0]
    if isinstance(item, str):
        if item.startswith("data:"):
            result = decode_data_url(item)
        elif item.startswith("http://") or item.startswith("https://"):
            result = _read_url_image(item)
        else:
            try:
                result = base64.b64decode(item, validate=True)
            except ValueError as error:
                raise SkillError("image provider returned an unsupported string result") from error
        image_extension(result)
        return result
    if not isinstance(item, dict):
        raise SkillError("image provider returned an unsupported result")
    encoded = item.get("b64_json") or item.get("base64")
    if isinstance(encoded, str) and encoded:
        try:
            result = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise SkillError("image provider returned invalid base64") from error
        image_extension(result)
        return result
    url = item.get("url") or item.get("image_url")
    if isinstance(url, str) and url:
        if url.startswith("data:"):
            result = decode_data_url(url)
            image_extension(result)
            return result
        return _read_url_image(url)
    raise SkillError("image provider response has neither b64_json nor URL")


class QuickAIImageClient:
    def __init__(self, base_url: str, key: str, model: str) -> None:
        self.base_url = base_url
        self.key = key
        self.model = model
        self.breaker = CircuitBreaker()

    def list_models(self) -> list[str]:
        return safe_operation(
            lambda: _model_ids(request_json("GET", api_url(self.base_url, "/v1/models"), key=self.key, timeout=60)),
            breaker=self.breaker,
        )

    def health_snapshot(self) -> dict[str, Any]:
        return self.breaker.snapshot()

    def generate(self, prompt: str, *, size: str, quality: str) -> bytes:
        _validate_prompt(prompt)
        payload = request_json(
            "POST",
            api_url(self.base_url, "/v1/images/generations"),
            key=self.key,
            value={
                "model": self.model,
                "prompt": prompt,
                "n": 1,
                "size": size,
                "quality": quality,
                "response_format": "b64_json",
                "output_format": "png",
            },
            timeout=600,
        )
        return _extract_image(payload)

    def edit(self, prompt: str, references: Iterable[Path], *, size: str, quality: str) -> bytes:
        _validate_prompt(prompt)
        files = [("image", path) for path in references]
        body, content_type = multipart_body(
            [
                ("model", self.model),
                ("prompt", prompt),
                ("n", "1"),
                ("size", size),
                ("quality", quality),
                ("response_format", "b64_json"),
                ("output_format", "png"),
            ],
            files,
        )
        payload = request_json(
            "POST",
            api_url(self.base_url, "/v1/images/edits"),
            key=self.key,
            body=body,
            content_type=content_type,
            timeout=600,
        )
        return _extract_image(payload)


class QuickAINewVideoClient:
    def __init__(self, base_url: str, key: str, model: str) -> None:
        self.base_url = base_url
        self.key = key
        self.model = model
        self.breaker = CircuitBreaker()

    def list_models(self) -> list[str]:
        return safe_operation(
            lambda: _model_ids(request_json("GET", api_url(self.base_url, "/v1/models"), key=self.key, timeout=60)),
            breaker=self.breaker,
        )

    def health_snapshot(self) -> dict[str, Any]:
        return self.breaker.snapshot()

    def create(
        self,
        prompt: str,
        *,
        seconds: int,
        size: str,
        resolution: str = "480p",
        aspect_ratio: str = "16:9",
        generate_audio: bool = False,
        references: Iterable[Path],
    ) -> str:
        _validate_prompt(prompt)
        if seconds < 1 or seconds > MAX_VIDEO_SECONDS:
            raise SkillError(f"video seconds must be from 1 to {MAX_VIDEO_SECONDS}")
        if resolution not in VIDEO_RESOLUTIONS:
            raise SkillError("video resolution must be one of 480p, 720p, 1080p")
        fields = [
            ("model", self.model),
            ("prompt", prompt),
            ("seconds", str(seconds)),
            ("resolution", resolution),
            ("aspect_ratio", aspect_ratio),
            ("generate_audio", "true" if generate_audio else "false"),
        ]
        if size and size != "auto" and size_orientation_matches_aspect_ratio(size, aspect_ratio):
            fields.append(("size", size))
        files = [("input_reference", path) for path in references]
        body, content_type = multipart_body(fields, files)
        payload = request_json(
            "POST",
            api_url(self.base_url, "/v1/videos"),
            key=self.key,
            body=body,
            content_type=content_type,
            timeout=120,
        )
        created_task_id = task_id(payload)
        if not created_task_id:
            raise SkillError("video provider returned no task ID")
        return created_task_id

    def query(self, task_id: str) -> tuple[str, dict[str, Any]]:
        encoded = urllib.parse.quote(task_id, safe="")
        payload = safe_operation(
            lambda: request_json("GET", api_url(self.base_url, f"/v1/videos/{encoded}"), key=self.key, timeout=60),
            breaker=self.breaker,
        )
        status = task_status(payload)
        if not status and is_completed(payload):
            status = "completed"
        return status or "unknown", payload

    def poll(
        self,
        task_id: str,
        *,
        timeout_seconds: int,
        on_status: Any = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = 4.0
        last_status = "unknown"
        while True:
            try:
                status, payload = self.query(task_id)
            except APIError as error:
                if error.status not in TASK_POLL_RETRYABLE_HTTP:
                    raise
                status, payload = last_status, {"warning": str(error)}
            except SkillError as error:
                message = str(error)
                if "circuit is open" not in message and "cannot connect" not in message:
                    raise
                status, payload = last_status, {"warning": message}
            last_status = status
            if on_status:
                on_status(status, payload)
            if status in FAILED_STATES:
                raise ProviderTaskFailedError(task_error(payload), status=status)
            if status in COMPLETED_STATES or is_completed(payload):
                return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"video task polling timed out: {task_id}")
            time.sleep(min(delay, remaining))
            delay = min(15.0, delay * 1.35)

    def download(self, task_id: str, status_payload: dict[str, Any], destination: Path) -> None:
        encoded = urllib.parse.quote(task_id, safe="")
        content_url = api_url(self.base_url, f"/v1/videos/{encoded}/content")
        content_error: Exception | None = None
        try:
            safe_operation(
                lambda: download_file(content_url, destination, key=self.key, timeout=300),
                breaker=self.breaker,
            )
            assert_mp4(destination)
            return
        except (APIError, SkillError) as error:
            content_error = error
            if destination.exists():
                destination.unlink()
        for result_url in result_urls(status_payload):
            try:
                safe_operation(lambda: download_file(result_url, destination, timeout=300), breaker=self.breaker)
                assert_mp4(destination)
                return
            except (APIError, SkillError) as error:
                content_error = error
                if destination.exists():
                    destination.unlink()
        raise SkillError(f"video content is unavailable: {content_error}")


class QuickAIVideoClient:
    """QuickAI JSON video adapter; its endpoints differ from QuickAI New."""

    def __init__(self, base_url: str, key: str, model: str) -> None:
        self.base_url = base_url
        self.key = key
        self.model = model
        self.breaker = CircuitBreaker()

    def list_models(self) -> list[str]:
        return safe_operation(
            lambda: _model_ids(request_json("GET", api_url(self.base_url, "/v1/models"), key=self.key, timeout=60)),
            breaker=self.breaker,
        )

    def health_snapshot(self) -> dict[str, Any]:
        return self.breaker.snapshot()

    @staticmethod
    def _data_url(path: Path) -> str:
        payload = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    def create(
        self,
        prompt: str,
        *,
        seconds: int,
        size: str,
        resolution: str = "480p",
        aspect_ratio: str = "16:9",
        generate_audio: bool = False,
        references: Iterable[Path],
    ) -> str:
        _validate_prompt(prompt)
        if seconds < 1 or seconds > MAX_VIDEO_SECONDS:
            raise SkillError(f"video seconds must be from 1 to {MAX_VIDEO_SECONDS}")
        if resolution not in VIDEO_RESOLUTIONS:
            raise SkillError("video resolution must be one of 480p, 720p, 1080p")
        reference_paths = list(references)
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "duration": seconds,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
        }
        # The QuickAI gateway currently rejects PNG data URIs for I2V even
        # though the upstream xAI contract accepts them. Normalize valid stills
        # to a cropped JPEG only for the outbound request; source files remain
        # untouched and malformed fixtures retain their original bytes.
        with tempfile.TemporaryDirectory(prefix="gvs-i2v-") as temporary:
            temporary_directory = Path(temporary)
            prepared = [_prepare_quickai_reference(path, aspect_ratio, temporary_directory) for path in reference_paths]
            if len(prepared) == 1:
                payload["image"] = {"url": self._data_url(prepared[0])}
            elif prepared:
                payload["reference_images"] = [{"url": self._data_url(path)} for path in prepared]
            response = request_json(
                "POST",
                api_url(self.base_url, "/v1/videos/generations"),
                key=self.key,
                value=payload,
                timeout=120,
            )
        created_task_id = task_id(response)
        if not created_task_id:
            raise SkillError("video provider returned no task ID")
        return created_task_id

    def query(self, task_id: str) -> tuple[str, dict[str, Any]]:
        encoded = urllib.parse.quote(task_id, safe="")
        payload = safe_operation(
            lambda: request_json("GET", api_url(self.base_url, f"/v1/videos/{encoded}"), key=self.key, timeout=60),
            breaker=self.breaker,
        )
        status = task_status(payload)
        if not status and is_completed(payload):
            status = "completed"
        return status or "unknown", payload

    def poll(self, task_id: str, *, timeout_seconds: int, on_status: Any = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = 4.0
        last_status = "unknown"
        while True:
            try:
                status, payload = self.query(task_id)
            except APIError as error:
                if error.status not in TASK_POLL_RETRYABLE_HTTP:
                    raise
                status, payload = last_status, {"warning": str(error)}
            except SkillError as error:
                message = str(error)
                if "circuit is open" not in message and "cannot connect" not in message:
                    raise
                status, payload = last_status, {"warning": message}
            last_status = status
            if on_status:
                on_status(status, payload)
            if status in FAILED_STATES:
                raise ProviderTaskFailedError(task_error(payload), status=status)
            if status in COMPLETED_STATES or is_completed(payload):
                return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"video task polling timed out: {task_id}")
            time.sleep(min(delay, remaining))
            delay = min(15.0, delay * 1.35)

    def download(self, task_id: str, status_payload: dict[str, Any], destination: Path) -> None:
        encoded = urllib.parse.quote(task_id, safe="")
        content_url = api_url(self.base_url, f"/v1/videos/{encoded}/content")
        content_error: Exception | None = None
        try:
            safe_operation(lambda: download_file(content_url, destination, key=self.key, timeout=300), breaker=self.breaker)
            assert_mp4(destination)
            return
        except (APIError, SkillError) as error:
            content_error = error
            if destination.exists():
                destination.unlink()
        for result_url in result_urls(status_payload):
            try:
                safe_operation(lambda: download_file(result_url, destination, timeout=300), breaker=self.breaker)
                assert_mp4(destination)
                return
            except (APIError, SkillError) as error:
                content_error = error
                if destination.exists():
                    destination.unlink()
        raise SkillError(f"video content is unavailable: {content_error}")


def save_image_bytes(data: bytes, target_stem: Path) -> Path:
    extension = image_extension(data)
    destination = target_stem.with_suffix(extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination
