#!/usr/bin/env python3
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from ctypes import wintypes
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar


CONFIG_VERSION = 3
USER_AGENT = "GrokVideoStudioSkill/2.4.0"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_VIDEO_MODEL = "grok-imagine-video-1.5"
MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_MULTIPART_BYTES = 128 * 1024 * 1024
MAX_MEDIA_BYTES = 512 * 1024 * 1024

_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCK_STATE = threading.local()
_T = TypeVar("_T")


class SkillError(RuntimeError):
    pass


class APIError(SkillError):
    def __init__(self, status: int, message: str, request_id: str = "") -> None:
        self.status = status
        self.request_id = request_id
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"HTTP {status}: {message}{suffix}")


def _process_file_lock(handle: Any, *, acquire: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    import fcntl

    mode = fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN
    fcntl.flock(handle.fileno(), mode)


@contextmanager
def project_state_lock(root: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialize project state mutations across threads and CLI processes."""
    key = str(root.resolve()).casefold() if os.name == "nt" else str(root.resolve())
    with _PROJECT_LOCKS_GUARD:
        thread_lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        held = getattr(_PROJECT_LOCK_STATE, "held", {})
        if key in held:
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return

        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".gvs-state.lock"
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    _process_file_lock(handle, acquire=True)
                    break
                except (BlockingIOError, OSError) as error:
                    if time.monotonic() >= deadline:
                        raise SkillError(f"project is busy in another mutating command: {root}") from error
                    time.sleep(0.05)
            held[key] = 1
            _PROJECT_LOCK_STATE.held = held
            try:
                yield
            finally:
                held.pop(key, None)
                _process_file_lock(handle, acquire=False)


def locked_project_state(function: Callable[..., _T]) -> Callable[..., _T]:
    @wraps(function)
    def wrapped(root: Path, *args: Any, **kwargs: Any) -> _T:
        # Windows runners can expose the same temporary directory through both
        # its long name and an 8.3 alias. Keep locking, path containment, and
        # relative-path serialization on one canonical root for the operation.
        canonical_root = root.expanduser().resolve()
        with project_state_lock(canonical_root):
            return function(canonical_root, *args, **kwargs)

    return wrapped


def config_dir() -> Path:
    override = os.environ.get("GVS_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return (Path(local) / "GrokVideoSkill").resolve()
    return (Path.home() / ".config" / "GrokVideoSkill").resolve()


def config_path() -> Path:
    return config_dir() / "config.json"


def secrets_path() -> Path:
    return config_dir() / "secrets.dpapi"


def normalize_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SkillError("provider base URL is invalid")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise SkillError("provider base URL must use HTTPS; HTTP is allowed only for loopback tests")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"quickai.hn.takin.cc", "quickainew.hn.takin.cc"}:
        raise SkillError("the retired QuickAI upstream is no longer supported; enter your own provider URL")
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def api_url(base_url: str, path: str) -> str:
    if not path.startswith("/"):
        raise SkillError("API path must start with a slash")
    return normalize_base_url(base_url) + path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temp.exists():
            temp.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SkillError(f"file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise SkillError(f"invalid JSON in {path}: line {error.lineno}") from error
    if not isinstance(value, dict):
        raise SkillError(f"JSON root must be an object: {path}")
    return value


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _data_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SkillError("encrypted local secret storage currently requires Windows DPAPI; use environment variables")
    input_blob, input_buffer = _data_blob(data)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.restype = wintypes.BOOL
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "GrokVideoSkill",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SkillError("cannot decrypt Windows DPAPI secrets on this platform")
    input_blob, input_buffer = _data_blob(data)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        raise SkillError("local secrets cannot be decrypted by the current Windows user")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def save_settings(
    config: dict[str, Any],
    sub2api_key: str,
    newapi_key: str,
    *,
    store_secrets: bool,
    sub2api_image_key: str | None = None,
    sub2api_video_key: str | None = None,
    newapi_video_key: str | None = None,
) -> None:
    image_base_url = str(config.get("image_base_url", config.get("sub2api_base_url", ""))).strip()
    sub2api_video_base_url = str(config.get("sub2api_video_base_url", config.get("sub2api_base_url", ""))).strip()
    newapi_video_base_url = str(config.get("newapi_video_base_url", config.get("newapi_base_url", ""))).strip()
    normalized = {
        "version": CONFIG_VERSION,
        "image_base_url": normalize_base_url(image_base_url) if image_base_url else "",
        "sub2api_video_base_url": normalize_base_url(sub2api_video_base_url) if sub2api_video_base_url else "",
        "newapi_video_base_url": normalize_base_url(newapi_video_base_url) if newapi_video_base_url else "",
        "image_model": str(config["image_model"]).strip(),
        "video_model": str(config["video_model"]).strip(),
        "default_video_provider": str(config.get("default_video_provider", "sub2api")).strip() or "sub2api",
        "secret_provider": "windows-dpapi" if store_secrets else "environment",
    }
    if not normalized["image_model"] or not normalized["video_model"]:
        raise SkillError("image and video models are required")
    if normalized["default_video_provider"] not in {"sub2api", "newapi"}:
        raise SkillError("default_video_provider must be sub2api or newapi")
    image_key = (sub2api_image_key if sub2api_image_key is not None else sub2api_key).strip()
    video_key = (sub2api_video_key if sub2api_video_key is not None else sub2api_key).strip()
    new_video_key = (newapi_video_key if newapi_video_key is not None else newapi_key).strip()
    if image_key and not normalized["image_base_url"]:
        raise SkillError("image_base_url is required when an image key is configured")
    if video_key and not normalized["sub2api_video_base_url"]:
        raise SkillError("sub2api_video_base_url is required when a Sub2Api video key is configured")
    if new_video_key and not normalized["newapi_video_base_url"]:
        raise SkillError("newapi_video_base_url is required when a NewApi video key is configured")
    atomic_write_json(config_path(), normalized)
    if store_secrets:
        if not image_key and not video_key and not new_video_key:
            raise SkillError("at least one provider key is required")
        secret_payload = json.dumps(
            {
                "version": 2,
                "sub2api_image_key": image_key,
                "sub2api_video_key": video_key,
                "newapi_video_key": new_video_key,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_write_bytes(secrets_path(), dpapi_protect(secret_payload))
    elif secrets_path().exists():
        secrets_path().unlink()


def load_settings(*, require_secrets: bool = True) -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        raise SkillError(f"configuration not found: {path}; run configure first")
    config = read_json(path)
    if config.get("version") not in {1, 2, CONFIG_VERSION}:
        raise SkillError("unsupported configuration version")
    legacy_config_fields = {
        "quickai_base_url",
        "quickainew_base_url",
        "quickai_key",
        "quickainew_key",
        "quickai_image_key",
        "quickai_video_key",
        "quickainew_video_key",
    }
    allow_legacy_config_migration = config.get("version") != CONFIG_VERSION or any(
        field in config for field in legacy_config_fields
    )

    def configured_url(current_name: str, *legacy_names: str) -> str:
        names = (current_name, *legacy_names) if allow_legacy_config_migration else (current_name,)
        for name in names:
            value = str(config.get(name, "")).strip()
            if value:
                try:
                    return normalize_base_url(value)
                except SkillError:
                    # A retired legacy URL must not prevent the skill from
                    # starting. Ignore it and require a current user URL.
                    if name != current_name:
                        continue
                    raise
        return ""

    # The first names are the current schema. The remaining URL aliases below
    # are read only to migrate older local config files; save_settings never writes them.
    result = {
        "version": CONFIG_VERSION,
        "image_base_url": configured_url("image_base_url", "image_api_url", "sub2api_base_url", "quickai_base_url"),
        "sub2api_video_base_url": configured_url("sub2api_video_base_url", "video_base_url", "sub2api_base_url", "quickai_base_url"),
        "newapi_video_base_url": configured_url("newapi_video_base_url", "newapi_base_url", "quickainew_base_url"),
        "image_model": str(config.get("image_model", "")).strip(),
        "video_model": str(config.get("video_model", "")).strip(),
        "default_video_provider": str(config.get("default_video_provider", "sub2api")).strip() or "sub2api",
        "secret_provider": str(config.get("secret_provider", "")),
    }
    for key, environment_name in (
        ("image_base_url", "GVS_IMAGE_API_URL"),
        ("sub2api_video_base_url", "GVS_VIDEO_API_URL"),
        ("newapi_video_base_url", "GVS_NEWAPI_VIDEO_URL"),
    ):
        override = os.environ.get(environment_name, "").strip()
        if override:
            result[key] = normalize_base_url(override)
    if result["default_video_provider"] == "quickai":
        result["default_video_provider"] = "sub2api"
    elif result["default_video_provider"] == "quickainew":
        result["default_video_provider"] = "newapi"
    stored: dict[str, Any] = {}
    if secrets_path().is_file():
        try:
            stored_value = json.loads(dpapi_unprotect(secrets_path().read_bytes()).decode("utf-8"))
            if isinstance(stored_value, dict):
                stored = stored_value
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SkillError("encrypted secret file is invalid") from error
    # Read legacy secrets only when the encrypted payload has no current role
    # fields. New configuration never writes or advertises these names.
    current_secret_fields = {"sub2api_image_key", "sub2api_video_key", "newapi_video_key"}
    allow_legacy_secret_migration = not any(field in stored for field in current_secret_fields)
    legacy_sub2api = str(stored.get("quickai_key", "")).strip() if allow_legacy_secret_migration else ""
    legacy_newapi = str(stored.get("quickainew_key", "")).strip() if allow_legacy_secret_migration else ""
    result["sub2api_image_key"] = (
        os.environ.get("GVS_IMAGE_API_KEY", "").strip()
        or os.environ.get("GVS_SUB2API_IMAGE_KEY", "").strip()
        or str(stored.get("sub2api_image_key", "")).strip()
        or (str(stored.get("quickai_image_key", "")).strip() if allow_legacy_secret_migration else "")
    )
    result["sub2api_video_key"] = (
        os.environ.get("GVS_VIDEO_API_KEY", "").strip()
        or os.environ.get("GVS_SUB2API_VIDEO_KEY", "").strip()
        or str(stored.get("sub2api_video_key", "")).strip()
        or (str(stored.get("quickai_video_key", "")).strip() if allow_legacy_secret_migration else "")
    )
    result["newapi_video_key"] = (
        os.environ.get("GVS_NEWAPI_VIDEO_KEY", "").strip()
        or str(stored.get("newapi_video_key", "")).strip()
        or (str(stored.get("quickainew_video_key", "")).strip() if allow_legacy_secret_migration else "")
    )
    sub2api_key = (
        os.environ.get("GVS_SUB2API_KEY", "").strip()
        or str(stored.get("sub2api_key", "")).strip()
        or legacy_sub2api
    )
    newapi_key = (
        os.environ.get("GVS_NEWAPI_KEY", "").strip()
        or str(stored.get("newapi_key", "")).strip()
        or legacy_newapi
    )
    if not result["sub2api_image_key"]:
        result["sub2api_image_key"] = sub2api_key
    if not result["sub2api_video_key"]:
        result["sub2api_video_key"] = sub2api_key
    if not result["newapi_video_key"]:
        result["newapi_video_key"] = newapi_key
    required_urls = (
        ("sub2api_image_key", "image_base_url", "image_base_url"),
        ("sub2api_video_key", "sub2api_video_base_url", "sub2api_video_base_url"),
        ("newapi_video_key", "newapi_video_base_url", "newapi_video_base_url"),
    )
    for key_name, url_name, label in required_urls:
        if result[key_name] and not result[url_name]:
            raise SkillError(f"{label} is required when {key_name} is configured")
    if require_secrets and not any(result[name] for name in ("sub2api_image_key", "sub2api_video_key", "newapi_video_key")):
        raise SkillError(
            "provider keys are unavailable; run configure or set GVS_IMAGE_API_KEY, GVS_VIDEO_API_KEY, or GVS_NEWAPI_VIDEO_KEY"
        )
    return result


def redact(text: str, secret_values: Iterable[str]) -> str:
    value = text
    for secret_value in secret_values:
        if secret_value:
            value = value.replace(secret_value, "[REDACTED]")
    return value


def _decode_error(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "provider request failed"
    if isinstance(value, dict):
        error = value.get("error", value)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error.get("code") or "provider request failed")[:1000]
        if isinstance(error, str):
            return error[:1000]
        return str(value.get("message") or value.get("msg") or "provider request failed")[:1000]
    return "provider request failed"


def request_bytes(
    method: str,
    url: str,
    *,
    key: str = "",
    body: bytes | None = None,
    content_type: str = "",
    accept: str = "application/json",
    timeout: int = 90,
    max_bytes: int = MAX_JSON_BYTES,
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", accept)
    request.add_header("User-Agent", USER_AGENT)
    if content_type:
        request.add_header("Content-Type", content_type)
    if key:
        request.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise SkillError("provider response exceeds the configured size limit")
            headers = {name.lower(): value for name, value in response.headers.items()}
            return payload, headers
    except urllib.error.HTTPError as error:
        payload = error.read(min(max_bytes, 2 * 1024 * 1024))
        request_id = error.headers.get("x-request-id", "") or error.headers.get("x-oneapi-request-id", "")
        raise APIError(error.code, _decode_error(payload), request_id) from None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        name = type(reason).__name__ if reason is not None else "network error"
        raise SkillError(f"cannot connect to provider: {name}") from None


def request_json(
    method: str,
    url: str,
    *,
    key: str,
    value: dict[str, Any] | None = None,
    body: bytes | None = None,
    content_type: str = "application/json; charset=utf-8",
    timeout: int = 90,
) -> dict[str, Any]:
    if value is not None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    payload, _ = request_bytes(method, url, key=key, body=body, content_type=content_type, timeout=timeout)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillError("provider returned invalid JSON") from error
    if not isinstance(parsed, dict):
        raise SkillError("provider JSON root must be an object")
    return parsed


def multipart_body(fields: Iterable[tuple[str, str]], files: Iterable[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = "----GrokVideoSkill" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields:
        safe_name = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for field, path in files:
        data = path.read_bytes()
        safe_field = field.replace('"', "_").replace("\r", "_").replace("\n", "_")
        safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        extension = path.suffix.lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".mp4": "video/mp4",
        }.get(extension, "application/octet-stream")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{safe_field}"; filename="{safe_name}"\r\n'.encode("utf-8"),
                f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    result = b"".join(chunks)
    if len(result) > MAX_MULTIPART_BYTES:
        raise SkillError("multipart request exceeds 128 MB")
    return result, f"multipart/form-data; boundary={boundary}"


def decode_data_url(value: str) -> bytes:
    if not value.startswith("data:") or "," not in value:
        raise SkillError("invalid data URL")
    header, encoded = value.split(",", 1)
    if ";base64" not in header:
        raise SkillError("image data URL must use base64")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise SkillError("invalid base64 image") from error


def image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise SkillError("provider result is not a supported PNG, JPEG, or WebP image")


def assert_mp4(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < 12:
        raise SkillError(f"video file is missing or empty: {path}")
    with path.open("rb") as handle:
        header = handle.read(64)
    if b"ftyp" not in header[4:32]:
        raise SkillError(f"downloaded file is not an MP4: {path}")


def download_file(url: str, destination: Path, *, key: str = "", timeout: int = 300, max_bytes: int = MAX_MEDIA_BYTES) -> None:
    parsed = urllib.parse.urlsplit(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise SkillError("media URL must use HTTPS; HTTP is allowed only for loopback tests")
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "video/mp4,video/*,image/*,application/octet-stream")
    request.add_header("User-Agent", USER_AGENT)
    if key:
        request.add_unredirected_header("Authorization", "Bearer " + key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".download.tmp")
    total = 0
    try:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, temp.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SkillError("media download exceeds the configured size limit")
                    output.write(chunk)
        except urllib.error.HTTPError as error:
            payload = error.read(2 * 1024 * 1024)
            request_id = error.headers.get("x-request-id", "")
            raise APIError(error.code, _decode_error(payload), request_id) from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            raise SkillError(f"media download failed: {type(reason).__name__ or 'network error'}") from None
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()


def configure_utf8_stdio() -> None:
    for target in (sys.stdout, sys.stderr):
        reconfigure = getattr(target, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def print_json(value: Any, *, stream: Any = None) -> None:

    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream or sys.stdout)
