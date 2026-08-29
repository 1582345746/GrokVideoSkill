#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from audio_client import MuseTalkClient
from component_manager import load_component_settings
from gvs_common import SkillError, atomic_write_json
from media_tools import mix_dialogue_track, probe_media, render_dialogue_track, replace_audio_track
from tts_providers import create_tts_provider
from voice_contracts import canonical_voice_contract, duplicate_voice_errors, file_digest, validate_voice_contract


DIALOGUE_MODES = {"preserve", "mute", "native-dialogue", "local-voice", "local-lipsync"}
SUBTITLE_SOURCES = {"upstream", "project", "none"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def audio_config(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("audio") if isinstance(project.get("audio"), dict) else {}
    mode = str(value.get("mode", "preserve")).strip().lower()
    subtitle_source = str(value.get("subtitle_source", "project")).strip().lower() or "project"
    return {
        "mode": mode,
        "subtitle_source": subtitle_source,
        "language": str(value.get("language", "zh-CN")).strip() or "zh-CN",
        "generate_audio": bool(value.get("generate_audio", mode == "native-dialogue")),
        "preserve_source_audio": bool(value.get("preserve_source_audio", True)),
        "duck_source_audio": bool(value.get("duck_source_audio", True)),
        "tts_provider": str(value.get("tts_provider", "cosyvoice")).strip().lower() or "cosyvoice",
        "allow_temporary_voices": bool(value.get("allow_temporary_voices", False)),
        "allow_shared_voices": bool(value.get("allow_shared_voices", False)),
    }


def project_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise SkillError("dialogue asset paths must be relative to the project")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SkillError("dialogue asset path escapes the project") from error
    if must_exist and not resolved.is_file():
        raise SkillError(f"dialogue asset does not exist: {value}")
    return resolved


def _seconds(project: dict[str, Any], shot: dict[str, Any]) -> float:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    try:
        return float(shot.get("seconds", defaults.get("video_seconds", 6)))
    except (TypeError, ValueError):
        return -1.0


def validate_dialogue(root: Path, project: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    config = audio_config(project)
    if project.get("audio") is not None and not isinstance(project.get("audio"), dict):
        errors.append("project.audio must be an object")
    if config["mode"] not in DIALOGUE_MODES:
        errors.append("audio.mode must be preserve, mute, native-dialogue, local-voice, or local-lipsync")
    if config["subtitle_source"] not in SUBTITLE_SOURCES:
        errors.append("audio.subtitle_source must be upstream, project, or none")
    if config["tts_provider"] not in {"cosyvoice", "voicebox", "voxcpm"}:
        errors.append("audio.tts_provider must be cosyvoice, voicebox, or voxcpm")
    if config["mode"] == "native-dialogue" and not config["generate_audio"]:
        errors.append("audio.generate_audio must be true for native-dialogue")
    if config["mode"] in {"mute", "local-voice", "local-lipsync"} and config["generate_audio"]:
        errors.append(f"audio.generate_audio must be false for {config['mode']}")
    characters = {
        str(item.get("id", "")): item
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    used_speakers: set[str] = set()
    line_ids: set[str] = set()
    for shot_index, shot in enumerate(project.get("shots", [])):
        if not isinstance(shot, dict):
            continue
        dialogue = shot.get("dialogue")
        if dialogue is None:
            continue
        prefix = f"shots[{shot_index}].dialogue"
        if not isinstance(dialogue, list):
            errors.append(f"{prefix} must be an array")
            continue
        if dialogue and (shot.get("subtitle") or shot.get("subtitles")):
            errors.append(f"shots[{shot_index}] must use dialogue as the single subtitle source when dialogue is present")
        previous_end = 0.0
        shot_seconds = _seconds(project, shot)
        for line_index, line in enumerate(dialogue):
            line_prefix = f"{prefix}[{line_index}]"
            if not isinstance(line, dict):
                errors.append(f"{line_prefix} must be an object")
                continue
            line_id = str(line.get("id", "")).strip()
            if not ID_RE.fullmatch(line_id):
                errors.append(f"{line_prefix}.id must use lowercase letters, digits, and hyphens")
            elif line_id in line_ids:
                errors.append(f"duplicate dialogue id: {line_id}")
            line_ids.add(line_id)
            speaker = str(line.get("speaker", "")).strip()
            if speaker not in characters:
                errors.append(f"{line_prefix}.speaker must reference a known character")
            else:
                used_speakers.add(speaker)
            text = str(line.get("text", "")).strip()
            if not text:
                errors.append(f"{line_prefix}.text is required")
            elif len(text) > 500:
                errors.append(f"{line_prefix}.text cannot exceed 500 characters")
            try:
                start = float(line.get("start"))
                end = float(line.get("end"))
            except (TypeError, ValueError):
                errors.append(f"{line_prefix}.start and end must be seconds")
                continue
            if start < 0 or end <= start or end > shot_seconds:
                errors.append(f"{line_prefix} must satisfy 0 <= start < end <= shot seconds")
            if start < previous_end:
                errors.append(f"{line_prefix} overlaps the previous dialogue line")
            previous_end = max(previous_end, end)
            for field in ("subtitle", "lip_sync"):
                if line.get(field) is not None and not isinstance(line.get(field), bool):
                    errors.append(f"{line_prefix}.{field} must be a boolean")
    if config["mode"] in {"local-voice", "local-lipsync"}:
        used_characters: list[dict[str, Any]] = []
        for speaker in sorted(used_speakers):
            character = characters[speaker]
            used_characters.append(character)
            voice = character.get("voice")
            prefix = f"character {speaker}.voice"
            if not isinstance(voice, dict):
                errors.append(f"{prefix} is required for local dialogue")
                continue
            errors.extend(
                validate_voice_contract(
                    voice,
                    prefix=prefix,
                    default_provider=config["tts_provider"],
                    resolve_path=lambda value: project_path(root, value),
                    require_identity=True,
                    require_approved=True,
                    allow_temporary=config["allow_temporary_voices"],
                )
            )
        errors.extend(
            duplicate_voice_errors(
                used_characters,
                default_provider=config["tts_provider"],
                resolve_path=lambda value: project_path(root, value),
                allow_shared=config["allow_shared_voices"],
            )
        )
    return errors


def dialogue_lines(project: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    offset = 0.0
    for shot in project.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("id", ""))
        for line in shot.get("dialogue", []) if isinstance(shot.get("dialogue"), list) else []:
            if not isinstance(line, dict):
                continue
            lines.append(
                {
                    **line,
                    "shot_id": shot_id,
                    "global_start": offset + float(line["start"]),
                    "global_end": offset + float(line["end"]),
                }
            )
        offset += _seconds(project, shot)
    return lines


def dialogue_subtitle_cues(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "shot_id": line["shot_id"],
            "line_id": str(line.get("id", "")),
            "speaker": str(line.get("speaker", "")),
            "start": line["global_start"],
            "end": line["global_end"],
            "text": str(line.get("text", "")).strip(),
        }
        for line in dialogue_lines(project)
        if bool(line.get("subtitle", True)) and str(line.get("text", "")).strip()
    ]


def dialogue_prompt(project: dict[str, Any], shot: dict[str, Any]) -> str:
    lines = shot.get("dialogue", []) if isinstance(shot.get("dialogue"), list) else []
    if not lines:
        return ""
    characters = {
        str(item.get("id", "")): str(item.get("name", item.get("id", "")))
        for item in project.get("characters", [])
        if isinstance(item, dict)
    }
    config = audio_config(project)
    rendered = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        speaker_id = str(line.get("speaker", ""))
        window = f"{float(line.get('start', 0)):.2f}-{float(line.get('end', 0)):.2f}s"
        speaker = characters.get(speaker_id, speaker_id)
        if config["mode"] == "native-dialogue":
            rendered.append(f"{window}, {speaker} says exactly: {str(line.get('text', '')).strip()}")
        else:
            emotion = str(line.get("emotion", "natural")).strip() or "natural"
            rendered.append(f"{window}, {speaker} performs natural {emotion} speaking motion; do not show any legible words")
    if config["mode"] == "native-dialogue":
        policy = "Generate synchronized spoken audio for the exact dialogue. Do not render the words as on-screen text."
    else:
        policy = "Show the active speaker naturally talking. Keep the frame text-free; local post-production supplies the final voice and subtitles."
    return policy + "\n" + "\n".join(rendered)


def dialogue_preflight(project: dict[str, Any]) -> dict[str, Any]:
    lines = dialogue_lines(project)
    warnings = []
    items = []
    for line in lines:
        duration = float(line["global_end"]) - float(line["global_start"])
        compact_chars = len(re.sub(r"[\s\W_]+", "", str(line.get("text", "")), flags=re.UNICODE))
        density = compact_chars / duration if duration else 999.0
        if density > 6.0:
            warnings.append(f"{line.get('id')} may be too dense for natural speech ({density:.1f} chars/s)")
        items.append(
            {
                "id": line.get("id"),
                "shot_id": line.get("shot_id"),
                "speaker": line.get("speaker"),
                "start": line["global_start"],
                "end": line["global_end"],
                "characters": compact_chars,
                "characters_per_second": round(density, 2),
            }
        )
    return {"mode": audio_config(project)["mode"], "line_count": len(lines), "lines": items, "warnings": warnings}


def _load_state(root: Path) -> dict[str, Any]:
    path = root / "dialogue-state.json"
    if not path.is_file():
        return {"version": 1, "lines": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"dialogue state is invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("lines"), dict):
        raise SkillError("unsupported dialogue state")
    return value


def render_local_dialogue(
    root: Path,
    project: dict[str, Any],
    *,
    source_video: Path,
    output_video: Path,
    service_url: str | None = None,
    voicebox_url: str | None = None,
    tts_provider: str | None = None,
    musetalk_url: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    errors = validate_dialogue(root, project)
    if errors:
        raise SkillError("dialogue validation failed: " + "; ".join(errors))
    config = audio_config(project)
    if config["mode"] not in {"local-voice", "local-lipsync"}:
        raise SkillError("dialogue render requires audio.mode local-voice or local-lipsync")
    if not source_video.is_file():
        raise SkillError(f"dialogue source video does not exist: {source_video}")
    settings = load_component_settings()
    services = settings.get("services") if isinstance(settings.get("services"), dict) else {}
    service_overrides = {"cosyvoice": service_url, "voicebox": voicebox_url}
    providers: dict[str, Any] = {}
    health_by_provider: dict[str, dict[str, Any]] = {}
    state = _load_state(root)
    state_path = root / "dialogue-state.json"
    characters = {str(item.get("id", "")): item for item in project.get("characters", []) if isinstance(item, dict)}
    rendered: list[dict[str, Any]] = []
    for line in dialogue_lines(project):
        line_id = str(line["id"])
        character = characters[str(line["speaker"])]
        selected_provider = (str(character["voice"].get("provider", "")) or tts_provider or config["tts_provider"]).strip().lower()
        voice = canonical_voice_contract(character["voice"], selected_provider)
        reference_value = str(voice.get("reference_audio", "")).strip()
        reference = project_path(root, reference_value) if reference_value else None
        voice_id = str(voice.get("voice_id", "")).strip()
        signature_value = {
            "text": str(line["text"]),
            "speaker": str(line["speaker"]),
            "provider": selected_provider,
            "voice": voice,
            "emotion": str(line.get("emotion", "")),
        }
        digest = hashlib.sha256(json.dumps(signature_value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if reference:
            digest.update(file_digest(reference).encode("ascii"))
        current_signature = digest.hexdigest()
        output = root / "assets" / "dialogue" / str(line["shot_id"]) / f"{line_id}.wav"
        runtime = state["lines"].get(line_id, {})
        if not force and runtime.get("signature") == current_signature and output.is_file():
            result = dict(runtime)
            result["skipped"] = True
        else:
            if selected_provider not in providers:
                providers[selected_provider] = create_tts_provider(
                    selected_provider,
                    services,
                    service_url=service_overrides.get(selected_provider),
                )
            tts = providers[selected_provider]
            if selected_provider not in health_by_provider:
                health_by_provider[selected_provider] = tts.health()
            health = health_by_provider[selected_provider]
            available = health.get("ok") is not False
            if not available:
                raise SkillError(
                    f"{selected_provider} service is unavailable and dialogue line {line_id} is not cached: {health.get('error', health)}"
                )
            available_speakers = health.get("speakers")
            if selected_provider == "cosyvoice" and reference is None and isinstance(available_speakers, list) and voice_id not in available_speakers:
                raise SkillError(
                    f"CosyVoice model does not provide voice_id {voice_id!r}; add a consented reference_audio or choose an available speaker"
                )
            result = tts.synthesize(
                str(line["text"]),
                output,
                voice=voice,
                reference_audio=reference,
                language=config["language"],
                emotion=str(line.get("emotion", "")),
                profile_name=f"GVS {character.get('id', line['speaker'])}",
            )
            result.update(
                {
                    "signature": current_signature,
                    "speaker": line["speaker"],
                    "shot_id": line["shot_id"],
                    "provider": selected_provider,
                }
            )
            state["lines"][line_id] = result
            atomic_write_json(state_path, state)
        rendered.append({**result, "id": line_id, "start": line["global_start"], "end": line["global_end"], "path": str(output.resolve())})
    source_media = probe_media(source_video)
    track = root / "deliverables" / "dialogue-track.wav"
    track_result = render_dialogue_track(rendered, track, duration=source_media["duration"])
    mixed_result = mix_dialogue_track(
        source_video,
        track,
        output_video,
        preserve_source_audio=config["preserve_source_audio"],
        duck_source_audio=config["duck_source_audio"],
    )
    lipsync_result: dict[str, Any] = {}
    if config["mode"] == "local-lipsync":
        lip_url = musetalk_url or str(services.get("musetalk", "http://127.0.0.1:9881"))
        client = MuseTalkClient(lip_url)
        health = client.health()
        if health.get("ok") is False:
            raise SkillError(f"MuseTalk service is unavailable: {health.get('error', health)}")
        lipsync_raw = output_video.with_name(output_video.stem + "-lipsync-raw.mp4")
        client.render(output_video, track, lipsync_raw)
        lipsync_output = output_video.with_name(output_video.stem + "-lipsynced.mp4")
        lipsync_result = replace_audio_track(lipsync_raw, output_video, lipsync_output)
    return {
        "mode": config["mode"],
        "providers": sorted({str(item.get("provider", config["tts_provider"])) for item in rendered}),
        "line_count": len(rendered),
        "rendered": rendered,
        "track": track_result,
        "video": mixed_result,
        "lipsync": lipsync_result,
        "state": str(state_path.resolve()),
    }
