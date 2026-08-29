#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from gvs_common import SkillError, atomic_write_json


TTS_PROVIDERS = {"cosyvoice", "voicebox", "voxcpm"}
VOICE_TYPES = {"preset", "reference", "designed"}
VOICE_STATUSES = {"draft", "auditioned", "approved", "temporary-test", "rejected"}
VOICE_CONSENTS = {"synthetic", "owned", "licensed"}
CATALOG_VERSION = 1


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_voice_contract(voice: dict[str, Any], default_provider: str = "cosyvoice") -> dict[str, Any]:
    value = dict(voice)
    provider = str(value.get("provider", default_provider)).strip().lower() or default_provider
    value["provider"] = provider
    reference = str(value.get("reference_audio", "")).strip()
    preset = str(value.get("preset_voice_id", value.get("voice_id", ""))).strip()
    profile = str(value.get("provider_profile_id", "")).strip()
    voice_type = str(value.get("voice_type", "")).strip().lower()
    if voice_type == "cloned":
        voice_type = "reference"
    if not voice_type:
        voice_type = "reference" if reference else ("preset" if preset or profile else "designed")
    value["voice_type"] = voice_type
    status = str(value.get("voice_status", value.get("status", ""))).strip().lower()
    # Compatibility: a usable pre-1.9 voice was already a production voice.
    if not status:
        status = "approved" if reference or preset or profile else "draft"
    value["voice_status"] = status
    if provider == "voicebox" and preset and not str(value.get("preset_voice_id", "")).strip():
        value["preset_voice_id"] = preset
    return value


def _identity_fields(voice: dict[str, Any]) -> tuple[str, str]:
    provider = str(voice.get("provider", "")).strip().lower()
    reference = str(voice.get("reference_audio", "")).strip()
    if reference:
        return "reference_audio", reference
    preset = str(voice.get("preset_voice_id", voice.get("voice_id", ""))).strip()
    if preset:
        return "preset_voice_id" if provider == "voicebox" else "voice_id", preset
    profile = str(voice.get("provider_profile_id", "")).strip()
    if profile:
        return "provider_profile_id", profile
    design = str(voice.get("design_prompt", "")).strip()
    if design:
        return "design_prompt", design
    return "", ""


def validate_voice_contract(
    voice: dict[str, Any],
    *,
    prefix: str,
    default_provider: str = "cosyvoice",
    resolve_path: Callable[[str], Path] | None = None,
    require_identity: bool = True,
    require_approved: bool = True,
    allow_temporary: bool = False,
) -> list[str]:
    errors: list[str] = []
    value = canonical_voice_contract(voice, default_provider)
    provider = str(value["provider"])
    voice_type = str(value["voice_type"])
    status = str(value["voice_status"])
    if provider not in TTS_PROVIDERS:
        errors.append(f"{prefix}.provider must be cosyvoice, voicebox, or voxcpm")
    if voice_type not in VOICE_TYPES:
        errors.append(f"{prefix}.voice_type must be preset, reference, or designed")
    if status not in VOICE_STATUSES:
        errors.append(f"{prefix}.voice_status is invalid")
    elif require_approved and status != "approved" and not (status == "temporary-test" and allow_temporary):
        errors.append(f"{prefix}.voice_status must be approved before local dialogue rendering")
    if provider == "voicebox":
        engine = str(value.get("preset_engine", value.get("engine", ""))).strip()
        if require_identity and str(value.get("preset_voice_id", "")).strip() and engine not in {"qwen_custom_voice", "kokoro"}:
            errors.append(f"{prefix}.preset_engine must be qwen_custom_voice or kokoro")
        model_size = str(value.get("model_size", "0.6B")).strip()
        if engine == "qwen_custom_voice" and model_size not in {"0.6B", "1.7B"}:
            errors.append(f"{prefix}.model_size must be 0.6B or 1.7B for Qwen CustomVoice")
    if value.get("seed") is not None:
        try:
            seed = int(value["seed"])
            if seed < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{prefix}.seed must be a non-negative integer")
    if require_approved and provider == "voxcpm":
        errors.append(f"{prefix}.provider voxcpm is experimental and cannot render production dialogue yet")

    identity_field, identity = _identity_fields(value)
    if require_identity and not identity:
        if provider == "voicebox":
            errors.append(f"{prefix} requires preset_voice_id or provider_profile_id")
        elif provider == "voxcpm":
            errors.append(f"{prefix} requires an approved reference_audio; design_prompt alone cannot render production dialogue")
        else:
            errors.append(f"{prefix} requires voice_id or reference_audio")

    reference = str(value.get("reference_audio", "")).strip()
    if reference:
        consent = str(value.get("consent", "")).strip().lower()
        if consent not in VOICE_CONSENTS:
            errors.append(f"{prefix}.consent must be synthetic, owned, or licensed when reference_audio is used")
        if not str(value.get("reference_text", "")).strip():
            errors.append(f"{prefix}.reference_text is required for reference TTS")
        if resolve_path is not None:
            try:
                resolve_path(reference)
            except SkillError as error:
                errors.append(str(error))
    if require_identity and provider == "voxcpm" and voice_type == "designed" and not reference:
        errors.append(f"{prefix} must promote a reviewed VoxCPM audition to reference_audio before production rendering")
    if identity_field == "design_prompt" and provider != "voxcpm":
        errors.append(f"{prefix}.design_prompt is not a renderable identity for provider {provider}")
    return errors


def voice_identity_key(
    voice: dict[str, Any],
    *,
    default_provider: str = "cosyvoice",
    resolve_path: Callable[[str], Path] | None = None,
) -> str:
    value = canonical_voice_contract(voice, default_provider)
    field, identity = _identity_fields(value)
    if not identity:
        return ""
    if field == "reference_audio" and resolve_path is not None:
        try:
            identity = file_digest(resolve_path(identity))
        except SkillError:
            pass
    engine = str(value.get("preset_engine", value.get("engine", ""))).strip().lower()
    return f"{value['provider']}:{engine}:{field}:{identity}"


def duplicate_voice_errors(
    characters: list[dict[str, Any]],
    *,
    default_provider: str,
    resolve_path: Callable[[str], Path] | None = None,
    allow_shared: bool = False,
) -> list[str]:
    if allow_shared:
        return []
    owners: dict[str, str] = {}
    errors: list[str] = []
    for character in characters:
        character_id = str(character.get("id", "")).strip()
        voice = character.get("voice")
        if not character_id or not isinstance(voice, dict):
            continue
        key = voice_identity_key(voice, default_provider=default_provider, resolve_path=resolve_path)
        if not key:
            continue
        if key in owners:
            errors.append(
                f"characters {owners[key]} and {character_id} share the same voice identity; set audio.allow_shared_voices=true only when intentional"
            )
        else:
            owners[key] = character_id
    return errors


def catalog_path(root: Path) -> Path:
    return root / "voice-catalog.json"


def load_voice_catalog(root: Path) -> dict[str, Any]:
    path = catalog_path(root)
    if not path.is_file():
        return {"version": CATALOG_VERSION, "updated_at": int(time.time()), "candidates": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"voice catalog is invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION or not isinstance(value.get("candidates"), list):
        raise SkillError("voice catalog has an unsupported format")
    return value


def save_voice_catalog(root: Path, catalog: dict[str, Any]) -> None:
    catalog["version"] = CATALOG_VERSION
    catalog["updated_at"] = int(time.time())
    atomic_write_json(catalog_path(root), catalog)


def catalog_candidate(catalog: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for candidate in catalog.get("candidates", []):
        if isinstance(candidate, dict) and str(candidate.get("id", "")) == candidate_id:
            return candidate
    raise SkillError(f"unknown voice candidate: {candidate_id}")
