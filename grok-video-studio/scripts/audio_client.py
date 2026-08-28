#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import urllib.parse
import wave
from pathlib import Path
from typing import Any

from gvs_common import MAX_MEDIA_BYTES, SkillError, api_url, atomic_write_bytes, multipart_body, normalize_base_url, request_bytes


def _loopback_origin(value: str, component: str) -> str:
    normalized = normalize_base_url(value)
    hostname = urllib.parse.urlsplit(normalized).hostname
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SkillError(f"{component} service URL must be loopback-only")
    return normalized


def _pcm_to_wav(payload: bytes, sample_rate: int) -> bytes:
    if payload.startswith(b"RIFF") and payload[8:12] == b"WAVE":
        return payload
    if len(payload) < 2 or len(payload) % 2:
        raise SkillError("TTS service returned invalid 16-bit PCM audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)
    return output.getvalue()


class CosyVoiceClient:
    """Adapter for the official CosyVoice FastAPI inference endpoints."""

    def __init__(self, base_url: str, *, sample_rate: int = 22050) -> None:
        self.base_url = _loopback_origin(base_url, "CosyVoice")
        if sample_rate < 8000 or sample_rate > 192000:
            raise SkillError("CosyVoice sample rate is invalid")
        self.sample_rate = sample_rate

    def health(self) -> dict[str, Any]:
        try:
            payload, _ = request_bytes("GET", api_url(self.base_url, "/health"), timeout=5, max_bytes=65536)
            value = json.loads(payload.decode("utf-8")) if payload else {}
            return value if isinstance(value, dict) else {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)[:500]}

    def synthesize(
        self,
        text: str,
        output: Path,
        *,
        voice_id: str = "",
        reference_audio: Path | None = None,
        reference_text: str = "",
        instruct_text: str = "",
    ) -> dict[str, Any]:
        if not text.strip():
            raise SkillError("dialogue text is required for TTS")
        if reference_audio is not None and instruct_text.strip():
            if not reference_audio.is_file():
                raise SkillError(f"voice reference does not exist: {reference_audio}")
            endpoint = "/inference_instruct2"
            fields = [("tts_text", text.strip()), ("instruct_text", instruct_text.strip())]
            files = [("prompt_wav", reference_audio)]
        elif reference_audio is not None:
            if not reference_audio.is_file():
                raise SkillError(f"voice reference does not exist: {reference_audio}")
            endpoint = "/inference_zero_shot"
            fields = [("tts_text", text.strip()), ("prompt_text", reference_text.strip())]
            files = [("prompt_wav", reference_audio)]
        elif instruct_text.strip():
            if not voice_id.strip():
                raise SkillError("voice_id is required for instructed TTS without reference audio")
            endpoint = "/inference_instruct"
            fields = [("tts_text", text.strip()), ("spk_id", voice_id.strip()), ("instruct_text", instruct_text.strip())]
            files = []
        else:
            if not voice_id.strip():
                raise SkillError("voice_id or a consented reference_audio is required for local TTS")
            endpoint = "/inference_sft"
            fields = [("tts_text", text.strip()), ("spk_id", voice_id.strip())]
            files = []
        body, content_type = multipart_body(fields, files)
        payload, headers = request_bytes(
            "POST",
            api_url(self.base_url, endpoint),
            body=body,
            content_type=content_type,
            accept="audio/wav,audio/pcm,application/octet-stream",
            timeout=600,
            max_bytes=MAX_MEDIA_BYTES,
        )
        try:
            response_sample_rate = int(headers.get("x-sample-rate", self.sample_rate))
        except (TypeError, ValueError):
            response_sample_rate = self.sample_rate
        wav = _pcm_to_wav(payload, response_sample_rate)
        atomic_write_bytes(output.resolve(), wav)
        return {
            "path": str(output.resolve()),
            "bytes": len(wav),
            "sample_rate": response_sample_rate,
            "endpoint": endpoint,
            "content_type": headers.get("content-type", ""),
        }


class MuseTalkClient:
    """Adapter for the optional localhost MuseTalk wrapper service shipped by this skill."""

    def __init__(self, base_url: str) -> None:
        self.base_url = _loopback_origin(base_url, "MuseTalk")

    def health(self) -> dict[str, Any]:
        try:
            payload, _ = request_bytes("GET", api_url(self.base_url, "/health"), timeout=5, max_bytes=65536)
            value = json.loads(payload.decode("utf-8")) if payload else {}
            return value if isinstance(value, dict) else {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)[:500]}

    def render(self, video: Path, audio: Path, output: Path) -> dict[str, Any]:
        if not video.is_file() or not audio.is_file():
            raise SkillError("MuseTalk requires existing video and audio files")
        body, content_type = multipart_body([], [("video", video), ("audio", audio)])
        payload, _ = request_bytes(
            "POST",
            api_url(self.base_url, "/v1/lipsync"),
            body=body,
            content_type=content_type,
            accept="video/mp4,application/octet-stream",
            timeout=3600,
            max_bytes=MAX_MEDIA_BYTES,
        )
        if not payload.startswith(b"\x00\x00") or b"ftyp" not in payload[:64]:
            raise SkillError("MuseTalk service returned an invalid MP4")
        atomic_write_bytes(output.resolve(), payload)
        return {"path": str(output.resolve()), "bytes": len(payload)}
