#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audio_client import CosyVoiceClient, _loopback_origin
from gvs_common import MAX_MEDIA_BYTES, SkillError, api_url, atomic_write_bytes, request_bytes, request_json
from voice_contracts import canonical_voice_contract


def _voicebox_language(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {"zh-cn": "zh", "zh-hans": "zh", "en-us": "en", "en-gb": "en", "ja-jp": "ja", "ko-kr": "ko"}
    return aliases.get(normalized, normalized.split("-", 1)[0] or "zh")


class CosyVoiceProvider:
    id = "cosyvoice"

    def __init__(self, base_url: str) -> None:
        self.client = CosyVoiceClient(base_url)

    def health(self) -> dict[str, Any]:
        return self.client.health()

    def list_voices(self, *, engine: str = "") -> dict[str, Any]:
        health = self.health()
        return {"provider": self.id, "engine": engine, "voices": health.get("speakers", []), "health": health}

    def synthesize(
        self,
        text: str,
        output: Path,
        *,
        voice: dict[str, Any],
        reference_audio: Path | None,
        language: str,
        emotion: str = "",
        profile_name: str = "",
    ) -> dict[str, Any]:
        del language, profile_name
        result = self.client.synthesize(
            text,
            output,
            voice_id=str(voice.get("voice_id", "")),
            reference_audio=reference_audio,
            reference_text=str(voice.get("reference_text", "")),
            instruct_text=str(voice.get("instruct_text", "")) or emotion,
        )
        return {**result, "provider": self.id}


class VoiceboxClient:
    """Loopback-only adapter for Voicebox's stable REST API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = _loopback_origin(base_url, "Voicebox")

    def _json(self, method: str, path: str, value: dict[str, Any] | None = None, *, timeout: int = 90) -> Any:
        if method == "GET":
            payload, _ = request_bytes("GET", api_url(self.base_url, path), timeout=timeout)
            try:
                return json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SkillError("Voicebox returned invalid JSON") from error
        return request_json(method, api_url(self.base_url, path), key="", value=value, timeout=timeout)

    def health(self) -> dict[str, Any]:
        try:
            value = self._json("GET", "/health", timeout=5)
            return value if isinstance(value, dict) else {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)[:500]}

    def models_status(self) -> dict[str, Any]:
        try:
            value = self._json("GET", "/models/status", timeout=15)
            return value if isinstance(value, dict) else {"models": value}
        except Exception as error:
            return {"ok": False, "error": str(error)[:500]}

    def require_model(
        self,
        engine: str,
        model_size: str,
        *,
        expected_repository: str = "",
        expected_revision: str = "",
    ) -> dict[str, Any]:
        value = self.models_status()
        models = value.get("models") if isinstance(value, dict) else None
        if not isinstance(models, list):
            raise SkillError(f"Voicebox model status is unavailable: {value.get('error', value) if isinstance(value, dict) else value}")
        expected_name = f"qwen-custom-voice-{model_size}" if engine == "qwen_custom_voice" else engine
        model = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and (
                    str(item.get("model_name", "")) == expected_name
                    or (str(item.get("engine", "")) == engine and str(item.get("model_size", "")) == model_size)
                )
            ),
            None,
        )
        if model is None:
            raise SkillError(f"Voicebox does not advertise the required model {expected_name}")
        if not bool(model.get("downloaded", False)):
            state = "downloading" if bool(model.get("downloading", False)) else "not downloaded"
            raise SkillError(
                f"Voicebox model {expected_name} is {state}; approve and complete the pinned model download before auditioning"
            )
        repository = expected_repository or str(model.get("hf_repo_id", ""))
        actual_revision = ""
        if expected_revision:
            cache_value = self._json("GET", "/models/cache-dir", timeout=15)
            cache_dir = Path(str(cache_value.get("path", ""))) if isinstance(cache_value, dict) else Path()
            if not cache_dir.is_absolute() or not repository:
                raise SkillError("Voicebox did not expose an auditable absolute model cache directory")
            ref = cache_dir / ("models--" + repository.replace("/", "--")) / "refs" / "main"
            if ref.is_file():
                actual_revision = ref.read_text(encoding="utf-8", errors="replace").strip()
            if actual_revision != expected_revision:
                raise SkillError(
                    f"Voicebox model revision mismatch for {repository}: expected {expected_revision}, got {actual_revision or 'unknown'}"
                )
        return {**model, "actual_revision": actual_revision}

    def list_profiles(self) -> list[dict[str, Any]]:
        value = self._json("GET", "/profiles")
        if not isinstance(value, list):
            raise SkillError("Voicebox profiles response must be an array")
        return [item for item in value if isinstance(item, dict)]

    def list_presets(self, engine: str) -> dict[str, Any]:
        value = self._json("GET", f"/profiles/presets/{engine}")
        if not isinstance(value, dict):
            raise SkillError("Voicebox presets response must be an object")
        return value

    def ensure_preset_profile(
        self,
        *,
        profile_id: str,
        profile_name: str,
        preset_engine: str,
        preset_voice_id: str,
        language: str,
    ) -> str:
        profiles = self.list_profiles()
        if profile_id:
            current = next((item for item in profiles if str(item.get("id", "")) == profile_id), None)
            if current is not None:
                if preset_voice_id and (
                    str(current.get("preset_engine", "")) != preset_engine
                    or str(current.get("preset_voice_id", "")) != preset_voice_id
                ):
                    raise SkillError("Voicebox profile no longer matches the approved preset voice")
                return profile_id
        for profile in profiles:
            if (
                str(profile.get("voice_type", "")) == "preset"
                and str(profile.get("preset_engine", "")) == preset_engine
                and str(profile.get("preset_voice_id", "")) == preset_voice_id
                and str(profile.get("name", "")) == profile_name
            ):
                return str(profile.get("id", ""))
        created = self._json(
            "POST",
            "/profiles",
            {
                "name": profile_name[:100],
                "description": "Managed by Grok Video Studio",
                "language": _voicebox_language(language),
                "voice_type": "preset",
                "preset_engine": preset_engine,
                "preset_voice_id": preset_voice_id,
                "default_engine": preset_engine,
            },
        )
        if not isinstance(created, dict) or not str(created.get("id", "")).strip():
            raise SkillError("Voicebox did not return a profile id")
        return str(created["id"])

    @staticmethod
    def _terminal_sse(payload: bytes) -> dict[str, Any]:
        terminal: dict[str, Any] = {}
        for raw_line in payload.decode("utf-8", errors="replace").splitlines():
            if not raw_line.startswith("data:"):
                continue
            try:
                value = json.loads(raw_line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                terminal = value
        if not terminal:
            raise SkillError("Voicebox generation status stream returned no events")
        if terminal.get("status") != "completed":
            raise SkillError(f"Voicebox generation failed: {terminal.get('error') or terminal.get('status')}")
        return terminal

    def generate(self, request: dict[str, Any], output: Path) -> dict[str, Any]:
        generation = self._json("POST", "/generate", request, timeout=90)
        if not isinstance(generation, dict) or not str(generation.get("id", "")).strip():
            raise SkillError("Voicebox did not return a generation id")
        generation_id = str(generation["id"])
        try:
            status_payload, _ = request_bytes(
                "GET",
                api_url(self.base_url, f"/generate/{generation_id}/status"),
                accept="text/event-stream",
                timeout=1800,
                max_bytes=4 * 1024 * 1024,
            )
            terminal = self._terminal_sse(status_payload)
        except SkillError as error:
            raise SkillError(f"Voicebox generation {generation_id} did not complete: {error}") from error
        audio, headers = request_bytes(
            "GET",
            api_url(self.base_url, f"/audio/{generation_id}"),
            accept="audio/wav,application/octet-stream",
            timeout=300,
            max_bytes=MAX_MEDIA_BYTES,
        )
        if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise SkillError("Voicebox returned invalid WAV audio")
        atomic_write_bytes(output.resolve(), audio)
        return {
            "path": str(output.resolve()),
            "bytes": len(audio),
            "provider": "voicebox",
            "generation_id": generation_id,
            "status": terminal.get("status"),
            "content_type": headers.get("content-type", ""),
        }


class VoiceboxProvider:
    id = "voicebox"

    def __init__(self, base_url: str) -> None:
        self.client = VoiceboxClient(base_url)

    def health(self) -> dict[str, Any]:
        return self.client.health()

    def doctor(self) -> dict[str, Any]:
        return {"provider": self.id, "health": self.health(), "models": self.client.models_status()}

    def list_voices(self, *, engine: str = "qwen_custom_voice") -> dict[str, Any]:
        return {"provider": self.id, **self.client.list_presets(engine or "qwen_custom_voice")}

    def synthesize(
        self,
        text: str,
        output: Path,
        *,
        voice: dict[str, Any],
        reference_audio: Path | None,
        language: str,
        emotion: str = "",
        profile_name: str = "",
    ) -> dict[str, Any]:
        if reference_audio is not None:
            raise SkillError("Voicebox reference profiles must be imported and approved before rendering")
        value = canonical_voice_contract(voice, "voicebox")
        engine = str(value.get("preset_engine", value.get("engine", "qwen_custom_voice"))).strip() or "qwen_custom_voice"
        preset = str(value.get("preset_voice_id", "")).strip()
        model_size = str(value.get("model_size", "0.6B"))
        model_status = self.client.require_model(
            engine,
            model_size,
            expected_repository=str(value.get("model", "")),
            expected_revision=str(value.get("model_revision", "")),
        )
        profile_id = self.client.ensure_preset_profile(
            profile_id=str(value.get("provider_profile_id", "")).strip(),
            profile_name=(profile_name or f"GVS {preset}")[:100],
            preset_engine=engine,
            preset_voice_id=preset,
            language=language,
        )
        instruct = str(value.get("instruct_text", "")).strip() or emotion.strip()
        request: dict[str, Any] = {
            "profile_id": profile_id,
            "text": text.strip(),
            "language": _voicebox_language(language),
            "engine": engine,
            "model_size": model_size,
            "normalize": bool(value.get("normalize", True)),
            "seed": int(value.get("seed", 42)),
        }
        if instruct:
            request["instruct"] = instruct[:500]
        result = self.client.generate(request, output)
        return {
            **result,
            "provider_profile_id": profile_id,
            "preset_engine": engine,
            "preset_voice_id": preset,
            "model_size": request["model_size"],
            "model_revision": model_status.get("actual_revision", ""),
            "seed": request["seed"],
        }


class VoxCPMProvider:
    id = "voxcpm"

    def __init__(self, base_url: str) -> None:
        self.base_url = _loopback_origin(base_url, "VoxCPM")

    def health(self) -> dict[str, Any]:
        return {"ok": False, "error": "VoxCPM remains experimental; no stable Grok Video Studio service contract is installed"}

    def list_voices(self, *, engine: str = "") -> dict[str, Any]:
        return {"provider": self.id, "engine": engine, "voices": [], "experimental": True}

    def synthesize(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SkillError("VoxCPM production rendering is not enabled; audition first, then approve its output as a reference voice")


def create_tts_provider(provider: str, services: dict[str, Any], *, service_url: str | None = None) -> Any:
    selected = provider.strip().lower()
    defaults = {
        "cosyvoice": "http://127.0.0.1:9880",
        "voicebox": "http://127.0.0.1:17493",
        "voxcpm": "http://127.0.0.1:9882",
    }
    url = service_url or str(services.get(selected, defaults.get(selected, "")))
    if selected == "cosyvoice":
        return CosyVoiceProvider(url)
    if selected == "voicebox":
        return VoiceboxProvider(url)
    if selected == "voxcpm":
        return VoxCPMProvider(url)
    raise SkillError(f"unsupported TTS provider: {provider}")
