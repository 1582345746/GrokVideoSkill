#!/usr/bin/env python3
"""Compile one generation request without leaking the rest of the story.

The opt-in compiler has no lossy compact fallback: the prompt's cast, state,
speech timing, and cut constraints are one atomic contract. Oversize requests
must be split or rewritten before creating a paid task.
"""
from __future__ import annotations

import json
import math
from typing import Any

from gvs_common import SkillError


SCOPED_COMPILER = "scoped-v1"
DEFAULT_HARD_LIMIT = 4096
KINDS = {"image", "video", "character_master"}
VISUAL_STYLE_FIELDS = ("medium", "rendering", "palette", "lighting", "texture")
VIDEO_MODES = {"native-dialogue", "narration", "local-narration", "local-voice", "local-lipsync", "preserve", "mute"}


def uses_scoped_prompt_compiler(project: dict[str, Any]) -> bool:
    return project.get("prompt_compiler") == SCOPED_COMPILER


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _characters(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item for item in project.get("characters", [])
        if isinstance(item, dict) and _text(item.get("id"))
    }


def _ids(shot: dict[str, Any], key: str) -> list[str]:
    value = shot.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise SkillError(f"scoped prompt {key} must be an array of character ids")
    return list(dict.fromkeys(value))


def _identity(character_id: str, character: dict[str, Any], wardrobe: Any = None) -> str:
    value = f"{character_id} ({_text(character.get('name')) or character_id}): {_text(character.get('identity'))}"
    clothes = _text(wardrobe) or _text(character.get("wardrobe"))
    if clothes:
        value += f"; wardrobe={clothes}"
    return value


def _style(project: dict[str, Any], shot: dict[str, Any] | None) -> str:
    # Free-form style_bible can contain locations, cast and later plot events.
    # Reusable visual values belong to prompt_style, which has a small schema.
    values: dict[str, Any] = {}
    profile = project.get("visual_profile")
    if isinstance(profile, dict) and _text(profile.get("medium")) not in {"", "auto"}:
        values["medium"] = profile["medium"]
    for owner in (project, shot or {}):
        configured = owner.get("prompt_style", {})
        if not isinstance(configured, dict):
            raise SkillError("scoped prompt prompt_style must be an object")
        for key in VISUAL_STYLE_FIELDS:
            if _text(configured.get(key)):
                values[key] = configured[key]
    return "; ".join(f"{key}={_text(values[key])}" for key in VISUAL_STYLE_FIELDS if key in values)


def _visible_cast(project: dict[str, Any], shot: dict[str, Any]) -> tuple[list[str], list[str]]:
    characters = _characters(project)
    active = _ids(shot, "character_ids")
    background = _ids(shot, "background_character_ids")
    selected = list(dict.fromkeys(active + background))
    unknown = [item for item in selected if item not in characters]
    if unknown:
        raise SkillError("scoped prompt has unknown visible character ids: " + ", ".join(unknown))
    wardrobes = shot.get("wardrobe", {})
    if not isinstance(wardrobes, dict):
        raise SkillError("scoped prompt wardrobe must map character ids to clothing")
    descriptions = [_identity(item, characters[item], wardrobes.get(item)) for item in selected]
    return selected, descriptions


def _state_sections(shot: dict[str, Any], *, still: bool) -> list[str]:
    event = shot.get("event") if isinstance(shot.get("event"), dict) else {}
    state_in = event.get("requires", shot.get("state_in", {}))
    state_out = event.get("effects", shot.get("state_out", {}))
    scene, assets = shot.get("scene_state"), shot.get("asset_state")
    if "prompt_state" in shot:
        projection = shot["prompt_state"]
        if not isinstance(projection, dict):
            raise SkillError("scoped prompt prompt_state must be an object")
        if any(key not in {"scene", "assets"} for key in projection):
            raise SkillError("scoped prompt prompt_state only accepts scene and assets")
        for key in ("scene", "assets"):
            if key in projection and not isinstance(projection[key], dict):
                raise SkillError(f"scoped prompt prompt_state.{key} must be an object")
        # An explicit empty projection suppresses world locks, not event facts.
        scene, assets = projection.get("scene", {}), projection.get("assets", {})
    result = []
    for key, value in (("Start state", state_in), ("Scene locks", scene), ("Asset locks", assets)):
        if value:
            result.append(f"{key}: {_json(value)}")
    if not still and state_out:
        result.append("Event result state: " + _json(state_out))
    return result


def _line_window(line: dict[str, Any], shot: dict[str, Any], project: dict[str, Any]) -> tuple[float, float]:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    try:
        start, end = float(line["start"]), float(line["end"])
        seconds = float(shot.get("seconds", defaults.get("video_seconds", 6)))
    except (KeyError, TypeError, ValueError) as error:
        raise SkillError("scoped dialogue requires numeric start/end and generation seconds") from error
    if not all(math.isfinite(value) for value in (start, end, seconds)) or not 0 <= start < end <= seconds:
        raise SkillError("scoped dialogue timing must satisfy 0 <= start < end <= generation seconds")
    return start, end


def _speech(project: dict[str, Any], shot: dict[str, Any], visible: list[str]) -> str:
    audio = project.get("audio") if isinstance(project.get("audio"), dict) else {}
    mode = _text(audio.get("mode")) or "native-dialogue"
    if mode not in VIDEO_MODES:
        raise SkillError(f"scoped prompt unsupported audio mode: {mode}")
    raw_lines = shot.get("dialogue", [])
    if not isinstance(raw_lines, list) or any(not isinstance(line, dict) for line in raw_lines):
        raise SkillError("scoped dialogue must be an array of line objects")
    narration = _text(shot.get("narration"))
    if mode in {"narration", "local-narration"}:
        if raw_lines:
            raise SkillError("scoped narration cannot contain character dialogue")
        source = "Generate off-screen narration" if mode == "narration" else "Local post-production supplies off-screen narration"
        return f"{source}; every visible mouth stays closed. " + (f"Narrator audio only: {narration}" if mode == "narration" and narration else "No character speech.")
    if mode in {"mute", "preserve"}:
        if raw_lines:
            raise SkillError(f"scoped audio mode {mode} cannot request generated dialogue")
        return "No character speech or lip movement. " + ("Deliver silent video." if mode == "mute" else "Preserve scene sound only.")
    if not raw_lines:
        return "No speech; all visible mouths stay closed. Use only the declared ambience/effects."
    characters = _characters(project)
    speakers = {str(line.get("speaker", "")) for line in raw_lines}
    if len(speakers) != 1:
        raise SkillError("scoped native/local dialogue requires one speaker per shot; split the exchange into shots")
    result = []
    previous_end = 0.0
    for line in raw_lines:
        speaker = _text(line.get("speaker"))
        if speaker not in characters:
            raise SkillError("scoped dialogue speaker must reference a known character")
        on_screen = line.get("on_screen", True)
        if not isinstance(on_screen, bool):
            raise SkillError("scoped dialogue on_screen must be a boolean")
        if on_screen and speaker not in visible:
            raise SkillError(f"scoped dialogue speaker {speaker} is not visible; declare on_screen=false for a voice outside the frame")
        if not on_screen and speaker in visible:
            raise SkillError(f"scoped dialogue off-screen speaker {speaker} cannot also be in the visible cast")
        listener = _text(line.get("listener", shot.get("listener", "")))
        listener_position = _text(line.get("listener_position", shot.get("listener_position", "")))
        if listener and listener != "audience":
            if listener not in characters:
                raise SkillError(f"scoped dialogue listener {listener} must reference a known character")
            if listener not in visible and listener_position not in {"offscreen-left", "offscreen-right"}:
                raise SkillError(f"scoped dialogue listener {listener} needs an explicit offscreen-left/right position outside a single-speaker closeup")
        start, end = _line_window(line, shot, project)
        if start < previous_end:
            raise SkillError("scoped dialogue time windows cannot overlap")
        previous_end = end
        wording = _text(line.get("text"))
        if not wording:
            raise SkillError("scoped dialogue line text is required")
        voice = f"speaker={speaker} ({_text(characters[speaker].get('name')) or speaker}); "
        voice += "visible speaking face" if on_screen else "off-screen audio only, no visible speaking face"
        if listener:
            voice += f"; listener={listener}"
            if listener_position:
                voice += f" ({listener_position})"
        if mode == "native-dialogue":
            voice += f"; intended words={_json(wording)}"
        else:
            voice += "; speaking motion only; the final recorded voice is supplied locally"
        emotion = _text(line.get("emotion"))
        if emotion:
            voice += f"; delivery={emotion}"
        result.append(f"{start:.2f}-{end:.2f}s: {voice}.")
    speaker_id = next(iter(speakers))
    visible_speaker = any(line.get("on_screen", True) for line in raw_lines)
    mouth_rule = f"Only {speaker_id} moves their lips; every listener and background mouth stays closed." if visible_speaker else "Every visible mouth stays closed; the voice remains outside the frame."
    return (
        "One speaker, one continuous turn. Start the intended line at the declared start; finish within its window. "
        "Speech starts before unrelated gestures; no hesitation padding, repeated words, extra dialogue, or reaction pause. "
        + mouth_rule + "\n" + "\n".join(result)
    )


def _tail(shot: dict[str, Any]) -> str:
    exit_action = _text(shot.get("exit_action"))
    ending = _text(shot.get("exit_behavior")) or "cut-on-action"
    value = f"Edit exit={ending}."
    if exit_action:
        value += f" Cut while {exit_action} is still in progress."
    else:
        value += " Cut during the current motivated action."
    return value + " No sigh, farewell, nod to conclude, smile to conclude, freeze, final pose, or reset. Keep body/prop contact physically valid."


def compile_scoped_prompt(project: dict[str, Any], shot: dict[str, Any] | None = None, *, kind: str) -> str:
    """Compile the explicit current request and reject a lossy fallback."""
    if kind not in KINDS:
        raise SkillError("scoped prompt kind must be image, video, or character_master")
    sections = []
    visual = _style(project, shot)
    if visual:
        sections.append("Visual: " + visual)
    if kind == "character_master":
        master = project.get("character_master") if isinstance(project.get("character_master"), dict) else {}
        character_id = _text(master.get("character_id"))
        if character_id:
            character = _characters(project).get(character_id)
            if character is None:
                raise SkillError("scoped character master character_id must reference a known character")
            sections.append("Only this character: " + _identity(character_id, character))
        master_prompt = _text(master.get("prompt"))
        if not character_id and not master_prompt:
            raise SkillError("scoped character master requires character_id or an explicit standalone master prompt")
        characters = _characters(project)
        multi_sheet = str(master.get("mode", "single-sheet")) == "multi-sheet"
        if multi_sheet:
            if characters:
                sections.append("Distinct fictional cast, keep each identity separate: " + " | ".join(
                    _identity(cid, item) for cid, item in characters.items()
                ))
            character_count = max(2, len(characters))
            sections.append(
                f"Multi-character reference sheet: exactly {character_count} distinct non-overlapping slots, one full-body character per slot; preserve age, face, hair, body, wardrobe, and signature props. No one may replace another."
            )
        else:
            sections.append("One character only on a plain neutral background, front/side/back views of that same identity.")
        if master_prompt:
            sections.append("Master composition: " + master_prompt)
        sections.append("Plain neutral background, no crowd, location, story action, text, labels, captions, logo, or watermark.")
    else:
        if not isinstance(shot, dict):
            raise SkillError("scoped image/video prompt requires a shot")
        visible, descriptions = _visible_cast(project, shot)
        sections.append("Visible cast only: " + (" | ".join(descriptions) if descriptions else "none; no people or characters in the picture"))
        for key, label in (("location", "Location"), ("scene_id", "Scene"), ("time", "Time"), ("weather", "Weather"), ("lighting", "Lighting"), ("camera", "Camera composition"), ("eyeline", "Eyeline"), ("screen_direction", "Screen direction"), ("props", "Visible props")):
            if _text(shot.get(key)):
                sections.append(f"{label}: {_text(shot[key])}")
        sections.extend(_state_sections(shot, still=kind == "image"))
        if kind == "image":
            description = _text(shot.get("keyframe_description", shot.get("image_prompt")))
            if not description:
                raise SkillError("scoped image prompt requires a current keyframe description")
            sections.append("Capture only the opening still, before the event changes state: " + description)
        else:
            action = _text(shot.get("action_description", shot.get("video_prompt")))
            if not action:
                raise SkillError("scoped video prompt requires a current action description")
            sections.append("Current action only: " + action)
            if _text(shot.get("handoff_source")) == "reviewed-clip-exit":
                sections.append(
                    "Temporal handoff: the supplied reviewed-clip-exit reference is the exact previous shot exit. "
                    "Continue its motion, eyeline, screen direction, body contact, and prop position immediately; "
                    "do not reset into a new pose."
                )
            if _text(shot.get("camera_motion")):
                sections.append("Camera movement: " + _text(shot["camera_motion"]))
            if _text(shot.get("entry_action")):
                sections.append("Begin already in action: " + _text(shot["entry_action"]))
            sections.append(_speech(project, shot, visible))
            for key in ("environment_sound", "sound_effects"):
                if _text(shot.get(key)):
                    sections.append(f"{key}: {_text(shot[key])}")
            sections.append(_tail(shot))
        sections.append("One continuous physical camera view fills the entire canvas; coherent scene geometry, plausible anatomy and prop contact. No split-screen, panels, overlays, UI, logos, or watermark.")
        audio = project.get("audio") if isinstance(project.get("audio"), dict) else {}
        if kind == "video" and audio.get("subtitle_source") == "upstream":
            sections.append("Render only explicitly requested synchronized speech captions.")
        else:
            sections.append("No visible text, dialogue text, signs, captions, or subtitles; words are audio only.")
    prompt = "\n".join(sections)
    limits = project.get("limits") if isinstance(project.get("limits"), dict) else {}
    try:
        hard_limit = min(DEFAULT_HARD_LIMIT, int(limits.get("max_prompt_bytes", DEFAULT_HARD_LIMIT)))
    except (TypeError, ValueError) as error:
        raise SkillError("scoped prompt max_prompt_bytes must be a positive integer") from error
    if hard_limit <= 0:
        raise SkillError("scoped prompt max_prompt_bytes must be a positive integer")
    size = len(prompt.encode("utf-8"))
    if size > hard_limit:
        raise SkillError(f"scoped prompt is {size} UTF-8 bytes, above {hard_limit}; split or shorten this shot without dropping cast, state, speech timing, or edit-exit constraints")
    return prompt


def scoped_prompt_variants(project: dict[str, Any], shot: dict[str, Any] | None = None, *, kind: str) -> dict[str, str]:
    """Compatibility adapter: every variant preserves the exact contract."""
    prompt = compile_scoped_prompt(project, shot, kind=kind)
    return {name: prompt for name in ("full", "compact", "minimal")}
