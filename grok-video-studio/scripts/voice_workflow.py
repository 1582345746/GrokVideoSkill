#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, read_json
from media_tools import analyze_audio
from tts_providers import create_tts_provider
from voice_contracts import (
    VOICE_CONSENTS,
    canonical_voice_contract,
    catalog_candidate,
    duplicate_voice_errors,
    file_digest,
    load_voice_catalog,
    save_voice_catalog,
    validate_voice_contract,
)


SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
VOICEBOX_MODELS = {
    "0.6B": (
        "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        "85e237c12c027371202489a0ec509ded67b5e4b5",
        "Apache-2.0",
    ),
    "1.7B": (
        "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "0c0e3051f131929182e2c023b9537f8b1c68adfe",
        "Apache-2.0",
    ),
}


def _workspace(root: Path) -> tuple[str, Path, dict[str, Any]]:
    series_path = root / "series.json"
    project_path = root / "project.json"
    if series_path.is_file():
        return "series", series_path, read_json(series_path)
    if project_path.is_file():
        return "project", project_path, read_json(project_path)
    raise SkillError(f"voice workspace requires series.json or project.json: {root}")


def _characters(contract: dict[str, Any]) -> list[dict[str, Any]]:
    value = contract.get("characters")
    if not isinstance(value, list):
        raise SkillError("characters must be an array")
    return [item for item in value if isinstance(item, dict)]


def _character(contract: dict[str, Any], character_id: str) -> dict[str, Any]:
    for character in _characters(contract):
        if str(character.get("id", "")) == character_id:
            return character
    raise SkillError(f"unknown character: {character_id}")


def _relative_asset(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise SkillError("voice asset must stay inside the project workspace") from error


def _audition_qa(path: Path) -> dict[str, Any]:
    analysis = analyze_audio(path)
    errors: list[str] = []
    warnings: list[str] = []
    if int(analysis.get("sample_rate", 0)) < 16000:
        errors.append("sample rate is below 16 kHz")
    if int(analysis.get("channels", 0)) not in {1, 2}:
        errors.append("audio must be mono or stereo")
    if analysis.get("mean_volume_db") is None or float(analysis.get("silence_ratio", 1)) > 0.95:
        errors.append("audition is silent or almost entirely silent")
    maximum = analysis.get("max_volume_db")
    if maximum is not None and float(maximum) >= -0.1:
        warnings.append("peak level is close to 0 dB; listen for clipping before approval")
    return {**analysis, "technical_ok": not errors, "errors": errors, "warnings": warnings}


def voice_doctor(
    provider: str,
    services: dict[str, Any],
    *,
    service_url: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    client = create_tts_provider(provider, services, service_url=service_url)
    if hasattr(client, "doctor"):
        result = client.doctor()
    else:
        result = {"provider": provider, "health": client.health()}
    health = result.get("health") if isinstance(result.get("health"), dict) else {}
    contract_report: dict[str, Any] = {}
    contract_errors: list[str] = []
    if root is not None:
        workspace_type, _, contract = _workspace(root)
        audio = contract.get("audio") if isinstance(contract.get("audio"), dict) else {}
        default_provider = str(audio.get("tts_provider", "cosyvoice"))
        characters = _characters(contract)
        active = []
        for character in characters:
            voice = character.get("voice")
            if not isinstance(voice, dict):
                continue
            prefix = f"character {character.get('id')}.voice"
            contract_errors.extend(
                validate_voice_contract(
                    voice,
                    prefix=prefix,
                    default_provider=default_provider,
                    resolve_path=lambda value: _resolve_workspace_file(root, value),
                    require_identity=False,
                    require_approved=False,
                )
            )
            active.append(
                {
                    "character_id": character.get("id"),
                    "voice": canonical_voice_contract(voice, default_provider),
                }
            )
        contract_errors.extend(
            duplicate_voice_errors(
                characters,
                default_provider=default_provider,
                resolve_path=lambda value: _resolve_workspace_file(root, value),
                allow_shared=bool(audio.get("allow_shared_voices", False)),
            )
        )
        catalog = load_voice_catalog(root)
        catalog_errors = []
        for candidate in catalog["candidates"]:
            if not isinstance(candidate, dict) or not str(candidate.get("audio_path", "")).strip():
                catalog_errors.append("voice catalog contains a candidate without audio_path")
                continue
            try:
                path = _resolve_workspace_file(root, str(candidate["audio_path"]))
                if file_digest(path) != str(candidate.get("audio_sha256", "")):
                    catalog_errors.append(f"voice candidate {candidate.get('id')} audio hash changed")
            except SkillError as error:
                catalog_errors.append(str(error))
        contract_errors.extend(catalog_errors)
        contract_report = {"workspace": workspace_type, "active_voices": active, "catalog_candidates": len(catalog["candidates"])}
    return {"ok": health.get("ok") is not False and not contract_errors, **result, "contract": contract_report, "errors": contract_errors}


def list_provider_voices(
    provider: str,
    services: dict[str, Any],
    *,
    engine: str = "",
    service_url: str | None = None,
) -> dict[str, Any]:
    client = create_tts_provider(provider, services, service_url=service_url)
    return {"ok": True, **client.list_voices(engine=engine)}


def audition_voice(
    root: Path,
    *,
    character_id: str,
    provider: str,
    services: dict[str, Any],
    text: str,
    service_url: str | None = None,
    preset_voice_id: str = "",
    preset_engine: str = "",
    voice_id: str = "",
    model_size: str = "0.6B",
    seed: int = 42,
    instruct_text: str = "",
    candidate_id: str = "",
) -> dict[str, Any]:
    workspace_type, _, contract = _workspace(root)
    character = _character(contract, character_id)
    if not text.strip():
        raise SkillError("audition text is required")
    selected_id = candidate_id.strip() or f"{character_id}-{provider}-{preset_voice_id or voice_id}-{int(time.time())}"
    selected_id = re.sub(r"[^a-z0-9-]+", "-", selected_id.lower()).strip("-")
    if not SAFE_ID_RE.fullmatch(selected_id):
        raise SkillError("voice candidate id must use lowercase letters, digits, and hyphens")
    catalog = load_voice_catalog(root)
    if any(isinstance(item, dict) and item.get("id") == selected_id for item in catalog["candidates"]):
        raise SkillError(f"voice candidate already exists: {selected_id}")
    voice: dict[str, Any] = {
        "provider": provider,
        "voice_type": "preset",
        "voice_status": "auditioned",
        "model_size": model_size,
        "seed": seed,
    }
    if provider == "voicebox":
        if not preset_voice_id.strip():
            raise SkillError("Voicebox audition requires --preset-voice-id")
        voice.update(
            {
                "preset_voice_id": preset_voice_id.strip(),
                "preset_engine": preset_engine.strip() or "qwen_custom_voice",
            }
        )
        if voice["preset_engine"] == "qwen_custom_voice" and model_size in VOICEBOX_MODELS:
            model, revision, license_name = VOICEBOX_MODELS[model_size]
            voice.update({"model": model, "model_revision": revision, "source_license": license_name})
    elif provider == "cosyvoice":
        if not voice_id.strip():
            raise SkillError("CosyVoice audition requires --voice-id")
        voice["voice_id"] = voice_id.strip()
    else:
        raise SkillError("VoxCPM design auditions remain experimental and cannot yet be started by this command")
    if instruct_text.strip():
        voice["instruct_text"] = instruct_text.strip()
    client = create_tts_provider(provider, services, service_url=service_url)
    health = client.health()
    if health.get("ok") is False:
        raise SkillError(f"{provider} service is unavailable: {health.get('error', health)}")
    output = root / "assets" / "voice-auditions" / character_id / f"{selected_id}.wav"
    rendered = client.synthesize(
        text.strip(),
        output,
        voice=voice,
        reference_audio=None,
        language=str((contract.get("audio") or {}).get("language", "zh-CN")),
        profile_name=f"GVS {workspace_type} {character_id} {selected_id}",
    )
    qa = _audition_qa(output)
    if not qa["technical_ok"]:
        output.unlink(missing_ok=True)
        raise SkillError("voice audition failed technical QA: " + "; ".join(qa["errors"]))
    if str(rendered.get("provider_profile_id", "")).strip():
        voice["provider_profile_id"] = str(rendered["provider_profile_id"])
    relative_output = _relative_asset(root, output)
    candidate = {
        "id": selected_id,
        "character_id": character_id,
        "status": "auditioned",
        "provider": provider,
        "voice": voice,
        "audition_text": text.strip(),
        "audio_path": relative_output,
        "audio_sha256": file_digest(output),
        "license_basis": str(voice.get("source_license", "provider-preset")),
        "created_at": int(time.time()),
        "render": rendered,
        "audio_qa": qa,
    }
    catalog["candidates"].append(candidate)
    save_voice_catalog(root, catalog)
    return {"ok": True, "workspace": workspace_type, "candidate": candidate, "next": "Review the WAV, then run voice-approve or voice-reject."}


def import_voice_candidate(
    root: Path,
    *,
    character_id: str,
    source: Path,
    reference_text: str,
    consent: str,
    provider: str,
    license_note: str = "",
    candidate_id: str = "",
) -> dict[str, Any]:
    workspace_type, _, contract = _workspace(root)
    _character(contract, character_id)
    if not source.is_file():
        raise SkillError(f"voice import does not exist: {source}")
    if source.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
        raise SkillError("voice import must be WAV, MP3, M4A, FLAC, or OGG")
    if consent.strip().lower() not in VOICE_CONSENTS:
        raise SkillError("voice import consent must be synthetic, owned, or licensed")
    if consent.strip().lower() in {"owned", "licensed"} and not license_note.strip():
        raise SkillError("owned or licensed voice imports require a specific --license-note")
    if not reference_text.strip():
        raise SkillError("voice import requires the exact reference transcript")
    selected_id = candidate_id.strip() or f"{character_id}-{provider}-import-{int(time.time())}"
    selected_id = re.sub(r"[^a-z0-9-]+", "-", selected_id.lower()).strip("-")
    if not SAFE_ID_RE.fullmatch(selected_id):
        raise SkillError("voice candidate id must use lowercase letters, digits, and hyphens")
    catalog = load_voice_catalog(root)
    if any(isinstance(item, dict) and item.get("id") == selected_id for item in catalog["candidates"]):
        raise SkillError(f"voice candidate already exists: {selected_id}")
    output = root / "assets" / "voice-auditions" / character_id / f"{selected_id}{source.suffix.lower()}"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    qa = _audition_qa(output)
    if not qa["technical_ok"]:
        output.unlink(missing_ok=True)
        raise SkillError("voice import failed technical QA: " + "; ".join(qa["errors"]))
    candidate = {
        "id": selected_id,
        "character_id": character_id,
        "status": "auditioned",
        "provider": provider,
        "voice": {
            "provider": provider,
            "voice_type": "reference",
            "voice_status": "auditioned",
            "reference_audio": _relative_asset(root, output),
            "reference_text": reference_text.strip(),
            "consent": consent.strip().lower(),
            "source_license": license_note.strip() or "synthetic generated voice",
        },
        "audition_text": reference_text.strip(),
        "audio_path": _relative_asset(root, output),
        "audio_sha256": file_digest(output),
        "license_basis": license_note.strip() or "synthetic generated voice",
        "created_at": int(time.time()),
        "audio_qa": qa,
    }
    catalog["candidates"].append(candidate)
    save_voice_catalog(root, catalog)
    return {"ok": True, "workspace": workspace_type, "candidate": candidate}


def review_voice_candidate(
    root: Path,
    candidate_id: str,
    *,
    approve: bool,
    temporary_test: bool = False,
    approved_by: str = "user",
) -> dict[str, Any]:
    workspace_type, contract_path, contract = _workspace(root)
    catalog = load_voice_catalog(root)
    candidate = catalog_candidate(catalog, candidate_id)
    if str(candidate.get("status", "")) != "auditioned":
        raise SkillError(f"voice candidate {candidate_id} is already reviewed with status {candidate.get('status')}")
    audio_value = str(candidate.get("audio_path", "")).strip()
    audio_path = (root / audio_value).resolve()
    try:
        audio_path.relative_to(root.resolve())
    except ValueError as error:
        raise SkillError("voice candidate audio escapes the workspace") from error
    if not audio_path.is_file() or file_digest(audio_path) != str(candidate.get("audio_sha256", "")):
        raise SkillError("voice candidate audio is missing or changed after audition")
    character = _character(contract, str(candidate.get("character_id", "")))
    if approve:
        status = "temporary-test" if temporary_test else "approved"
        voice = dict(candidate.get("voice") or {})
        voice["voice_status"] = status
        voice["approved_at"] = int(time.time())
        voice["approved_by"] = approved_by.strip() or "user"
        errors = validate_voice_contract(
            voice,
            prefix=f"character {character.get('id')}.voice",
            default_provider=str((contract.get("audio") or {}).get("tts_provider", "cosyvoice")),
            resolve_path=lambda value: _resolve_workspace_file(root, value),
            require_approved=True,
            allow_temporary=temporary_test,
        )
        if errors:
            raise SkillError("voice approval failed: " + "; ".join(errors))
        character["voice"] = voice
        candidate["status"] = status
        candidate["voice"] = voice
    else:
        candidate["status"] = "rejected"
        candidate.setdefault("voice", {})["voice_status"] = "rejected"
    candidate["reviewed_at"] = int(time.time())
    atomic_write_json(contract_path, contract)
    save_voice_catalog(root, catalog)
    return {
        "ok": True,
        "workspace": workspace_type,
        "candidate_id": candidate_id,
        "character_id": character.get("id"),
        "status": candidate["status"],
        "next": "Run series-voice-sync before episode preflight." if workspace_type == "series" and approve else "",
    }


def _resolve_workspace_file(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise SkillError("voice paths must be relative to the workspace")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SkillError("voice path escapes the workspace") from error
    if not resolved.is_file():
        raise SkillError(f"voice file does not exist: {value}")
    return resolved


def voice_catalog_summary(root: Path) -> dict[str, Any]:
    workspace_type, _, contract = _workspace(root)
    catalog = load_voice_catalog(root)
    return {
        "ok": True,
        "workspace": workspace_type,
        "characters": [
            {"id": item.get("id"), "name": item.get("name"), "voice": item.get("voice", {})}
            for item in _characters(contract)
        ],
        "catalog": catalog,
    }
