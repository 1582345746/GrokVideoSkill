#!/usr/bin/env python3
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable


CONFIG_VERSION = 3
USER_AGENT = "GrokVideoStudioSkill/1.1"
DEFAULT_QUICKAI_URL = "https://quickai.hn.takin.cc"
DEFAULT_QUICKAINEW_URL = "https://quickainew.hn.takin.cc"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_VIDEO_MODEL = "grok-imagine-video-1.5"
MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_MULTIPART_BYTES = 128 * 1024 * 1024
MAX_MEDIA_BYTES = 512 * 1024 * 1024


class SkillError(RuntimeError):
    pass


class APIError(SkillError):
    def __init__(self, status: int, message: str, request_id: str = "") -> None:
        self.status = status
        self.request_id = request_id
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"HTTP {status}: {message}{suffix}")


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
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    if path:
        raise SkillError("provider base URL must be an origin, without an API path")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


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
    quickai_image_key: str,
    quickai_video_key: str,
    quickainew_video_key: str,
    *,
    store_secrets: bool,
) -> None:
    normalized = {
        "version": CONFIG_VERSION,
        "quickai_base_url": normalize_base_url(str(config["quickai_base_url"])),
        "quickainew_base_url": normalize_base_url(str(config["quickainew_base_url"])),
        "image_model": str(config["image_model"]).strip(),
        "video_model": str(config["video_model"]).strip(),
        "default_video_provider": str(config.get("default_video_provider", "quickai")).strip() or "quickai",
        "secret_provider": "windows-dpapi" if store_secrets else "environment",
    }
    if not normalized["image_model"] or not normalized["video_model"]:
        raise SkillError("image and video models are required")
    if normalized["default_video_provider"] not in {"quickai", "quickainew"}:
        raise SkillError("default_video_provider must be quickai or quickainew")
    atomic_write_json(config_path(), normalized)
    if store_secrets:
        if not quickai_image_key.strip() and not quickai_video_key.strip() and not quickainew_video_key.strip():
            raise SkillError("at least one provider key is required")
        secret_payload = json.dumps(
            {
                "version": 2,
                "quickai_image_key": quickai_image_key.strip(),
                "quickai_video_key": quickai_video_key.strip(),
                "quickainew_video_key": quickainew_video_key.strip(),
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
    result = {
        "version": CONFIG_VERSION,
        "quickai_base_url": normalize_base_url(str(config.get("quickai_base_url", ""))),
        "quickainew_base_url": normalize_base_url(str(config.get("quickainew_base_url", ""))),
        "image_model": str(config.get("image_model", "")).strip(),
        "video_model": str(config.get("video_model", "")).strip(),
        "default_video_provider": str(config.get("default_video_provider", "quickai")).strip() or "quickai",
        "secret_provider": str(config.get("secret_provider", "")),
    }
    stored: dict[str, Any] = {}
    if secrets_path().is_file():
        try:
            stored_value = json.loads(dpapi_unprotect(secrets_path().read_bytes()).decode("utf-8"))
            if isinstance(stored_value, dict):
                stored = stored_value
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SkillError("encrypted secret file is invalid") from error
    legacy_quickai = os.environ.get("GVS_QUICKAI_KEY", "").strip() or str(stored.get("quickai_key", "")).strip()
    legacy_quickainew = os.environ.get("GVS_QUICKAINEW_KEY", "").strip() or str(stored.get("quickainew_key", "")).strip()
    result["quickai_image_key"] = (
        os.environ.get("GVS_QUICKAI_IMAGE_KEY", "").strip()
        or str(stored.get("quickai_image_key", "")).strip()
        or legacy_quickai
    )
    result["quickai_video_key"] = (
        os.environ.get("GVS_QUICKAI_VIDEO_KEY", "").strip()
        or str(stored.get("quickai_video_key", "")).strip()
        or legacy_quickai
    )
    result["quickainew_video_key"] = (
        os.environ.get("GVS_QUICKAINEW_VIDEO_KEY", "").strip()
        or str(stored.get("quickainew_video_key", "")).strip()
        or legacy_quickainew
    )
    # Deprecated aliases keep older callers and installations compatible.
    result["quickai_key"] = result["quickai_image_key"]
    result["quickainew_key"] = result["quickainew_video_key"]
    if require_secrets and not any(
        result[name] for name in ("quickai_image_key", "quickai_video_key", "quickainew_video_key")
    ):
        raise SkillError(
            "provider keys are unavailable; run configure or set a GVS_QUICKAI_*_KEY or GVS_QUICKAINEW_VIDEO_KEY"
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


def print_json(value: Any, *, stream: Any = None) -> None:
    import sys

    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream or sys.stdout)
