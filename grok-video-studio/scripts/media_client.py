#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import time
import urllib.parse
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


COMPLETED = {"completed", "complete", "succeeded", "success", "done"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled", "expired"}
MAX_PROMPT_CHARS = 4096
MAX_VIDEO_SECONDS = 15


def _validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise SkillError("prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SkillError(f"prompt has {len(prompt)} characters; provider maximum is {MAX_PROMPT_CHARS}")


def _unwrap_object(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


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


def _task_id(payload: dict[str, Any]) -> str:
    value = _unwrap_object(payload)
    return str(value.get("id") or value.get("request_id") or value.get("task_id") or "").strip()


def _status(payload: dict[str, Any]) -> str:
    value = _unwrap_object(payload)
    return str(value.get("status") or value.get("state") or "").strip().lower()


def _task_error(payload: dict[str, Any]) -> str:
    value = _unwrap_object(payload)
    error = value.get("error") or payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or error.get("code") or "video generation failed")[:1000]
    if isinstance(error, str):
        return error[:1000]
    return str(value.get("message") or value.get("msg") or "video generation failed")[:1000]


def _result_url(payload: dict[str, Any]) -> str:
    value = _unwrap_object(payload)
    candidates: list[Any] = [value.get("url"), value.get("result_url"), value.get("video_url")]
    for container_name in ("content", "video", "metadata"):
        container = value.get(container_name)
        if isinstance(container, dict):
            candidates.extend([container.get("url"), container.get("video_url")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


class QuickAIImageClient:
    def __init__(self, base_url: str, key: str, model: str) -> None:
        self.base_url = base_url
        self.key = key
        self.model = model

    def list_models(self) -> list[str]:
        return _model_ids(request_json("GET", api_url(self.base_url, "/v1/models"), key=self.key, timeout=60))

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

    def list_models(self) -> list[str]:
        return _model_ids(request_json("GET", api_url(self.base_url, "/v1/models"), key=self.key, timeout=60))

    def create(self, prompt: str, *, seconds: int, size: str, references: Iterable[Path]) -> str:
        _validate_prompt(prompt)
        if seconds < 1 or seconds > MAX_VIDEO_SECONDS:
            raise SkillError(f"video seconds must be from 1 to {MAX_VIDEO_SECONDS}")
        fields = [("model", self.model), ("prompt", prompt), ("seconds", str(seconds))]
        if size and size != "auto":
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
        task_id = _task_id(payload)
        if not task_id:
            raise SkillError("video provider returned no task ID")
        return task_id

    def query(self, task_id: str) -> tuple[str, dict[str, Any]]:
        encoded = urllib.parse.quote(task_id, safe="")
        payload = request_json("GET", api_url(self.base_url, f"/v1/videos/{encoded}"), key=self.key, timeout=60)
        status = _status(payload)
        if not status and _result_url(payload):
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
                if error.status not in {408, 425, 429, 500, 502, 503, 504}:
                    raise
                status, payload = last_status, {}
            last_status = status
            if on_status:
                on_status(status, payload)
            if status in COMPLETED:
                return payload
            if status in FAILED:
                raise SkillError(_task_error(payload))
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
            download_file(content_url, destination, key=self.key, timeout=300)
            assert_mp4(destination)
            return
        except (APIError, SkillError) as error:
            content_error = error
            if destination.exists():
                destination.unlink()
        result_url = _result_url(status_payload)
        if result_url:
            download_file(result_url, destination, timeout=300)
            assert_mp4(destination)
            return
        raise SkillError(f"video content is unavailable: {content_error}")


def save_image_bytes(data: bytes, target_stem: Path) -> Path:
    extension = image_extension(data)
    destination = target_stem.with_suffix(extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination
