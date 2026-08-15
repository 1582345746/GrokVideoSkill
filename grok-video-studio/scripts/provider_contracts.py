#!/usr/bin/env python3
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from gvs_common import APIError, SkillError


COMPLETED_STATES = {"completed", "complete", "succeeded", "success", "done", "finished"}
FAILED_STATES = {"failed", "failure", "error", "cancelled", "canceled", "expired", "rejected"}
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
WRAPPER_KEYS = ("data", "result", "output", "response", "task", "video")
TASK_ID_KEYS = ("id", "task_id", "taskId", "request_id", "requestId", "job_id", "jobId")
STATUS_KEYS = ("status", "state", "task_status", "taskStatus")
PROGRESS_KEYS = ("progress", "percent", "percentage")
URL_KEYS = ("url", "result_url", "resultUrl", "video_url", "videoUrl", "download_url", "downloadUrl")


def _objects(value: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 5:
        return
    if isinstance(value, dict):
        yield value
        for key in WRAPPER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yield from _objects(child, depth=depth + 1)
        for key in ("content", "metadata", "media", "files", "videos"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yield from _objects(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:20]:
            if isinstance(child, (dict, list)):
                yield from _objects(child, depth=depth + 1)


def _first(payload: dict[str, Any], keys: Iterable[str]) -> Any:
    for item in _objects(payload):
        for key in keys:
            value = item.get(key)
            if value not in (None, "", []):
                return value
    return None


def task_id(payload: dict[str, Any]) -> str:
    return str(_first(payload, TASK_ID_KEYS) or "").strip()


def task_status(payload: dict[str, Any]) -> str:
    value = _first(payload, STATUS_KEYS)
    if isinstance(value, bool):
        return "completed" if value else "failed"
    normalized = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "processing": "in_progress",
        "running": "in_progress",
        "pending": "queued",
        "waiting": "queued",
        "created": "queued",
        "successful": "completed",
        "finished": "completed",
    }
    return aliases.get(normalized, normalized)


def task_progress(payload: dict[str, Any]) -> float | None:
    value = _first(payload, PROGRESS_KEYS)
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= result <= 1:
        result *= 100
    return round(max(0.0, min(100.0, result)), 2)


def task_error(payload: dict[str, Any]) -> str:
    for item in _objects(payload):
        error = item.get("error") or item.get("failure")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("reason") or error.get("code")
            if message:
                return str(message)[:1000]
        if isinstance(error, str) and error.strip():
            return error.strip()[:1000]
    value = _first(payload, ("message", "msg", "detail", "reason"))
    return str(value or "video generation failed")[:1000]


def result_urls(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in _objects(payload):
        for key in URL_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in values:
                values.append(value.strip())
    return values


def is_completed(payload: dict[str, Any]) -> bool:
    return task_status(payload) in COMPLETED_STATES or bool(result_urls(payload))


@dataclass
class CircuitBreaker:
    failure_threshold: int = 4
    cooldown_seconds: float = 30.0
    consecutive_failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def before_request(self) -> None:
        remaining = self.opened_until - time.monotonic()
        if remaining > 0:
            raise SkillError(f"provider circuit is open; retry safe operation in {remaining:.1f}s")

    def success(self) -> None:
        self.consecutive_failures = 0
        self.opened_until = 0.0
        self.last_error = ""

    def failure(self, error: Exception) -> None:
        self.consecutive_failures += 1
        self.last_error = str(error)[:500]
        self.history.append({"at": int(time.time()), "error": self.last_error})
        self.history = self.history[-10:]
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_until = time.monotonic() + self.cooldown_seconds

    def snapshot(self) -> dict[str, Any]:
        remaining = max(0.0, self.opened_until - time.monotonic())
        return {
            "state": "open" if remaining > 0 else "closed",
            "consecutive_failures": self.consecutive_failures,
            "retry_after_seconds": round(remaining, 2),
            "last_error": self.last_error,
        }


def safe_operation(operation: Any, *, breaker: CircuitBreaker, attempts: int = 3, initial_delay: float = 0.4) -> Any:
    """Retry only idempotent reads. Never wrap create or other billable writes."""
    delay = initial_delay
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        breaker.before_request()
        try:
            result = operation()
            breaker.success()
            return result
        except APIError as error:
            last_error = error
            if error.status not in RETRYABLE_HTTP:
                raise
            breaker.failure(error)
            if attempt >= attempts:
                raise
        except SkillError as error:
            last_error = error
            breaker.failure(error)
            if attempt >= attempts:
                raise
        time.sleep(delay)
        delay = min(4.0, delay * 2)
    if last_error:
        raise last_error
    raise SkillError("safe provider operation failed")
