#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from workflow_registry import DIRECTOR_MODES, GENRE_PACKS, PROJECT_TYPES


AUDIO_INTENTS = {"dialogue", "narration", "score-ambience", "effects-ambience", "intentional-silence"}
EXIT_BEHAVIORS = {"continue-action", "cut-on-action", "hold-reaction", "ending-hook"}
STRICT_DIRECTOR_MODES = {
    "cinematic-short",
    "dialogue-scene",
    "silent-cinema",
    "action-scene",
    "montage",
    "comedy-scene",
    "news-report",
}


def director_config(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("director") if isinstance(project.get("director"), dict) else {}
    mode = str(value.get("mode", "single-shot")).strip() or "single-shot"
    return {
        "mode": mode,
        "project_type": str(value.get("project_type", "single-clip")).strip() or "single-clip",
        "genre_packs": list(value.get("genre_packs", [])) if isinstance(value.get("genre_packs"), list) else [],
        "strict": bool(value.get("strict", mode in STRICT_DIRECTOR_MODES)),
        "default_exit_behavior": str(value.get("default_exit_behavior", "continue-action")).strip()
        or "continue-action",
    }


def shot_audio_intent(shot: dict[str, Any]) -> str:
    explicit = str(shot.get("audio_intent", "")).strip().lower()
    if explicit:
        return explicit
    dialogue = shot.get("dialogue") if isinstance(shot.get("dialogue"), list) else []
    if dialogue:
        return "dialogue"
    if str(shot.get("narration", "")).strip():
        return "narration"
    return "score-ambience"


def edit_window(shot: dict[str, Any]) -> tuple[float, float, float]:
    seconds = float(shot.get("seconds", 6))
    edit_in_value = shot.get("edit_in")
    edit_out_value = shot.get("edit_out")
    timeline_value = shot.get("timeline_duration")
    edit_in = float(edit_in_value) if edit_in_value not in (None, "") else 0.0
    edit_out = float(edit_out_value) if edit_out_value not in (None, "") else seconds
    timeline = float(timeline_value) if timeline_value not in (None, "") else edit_out - edit_in
    return edit_in, edit_out, timeline


def validate_director(project: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if project.get("director") is not None and not isinstance(project.get("director"), dict):
        errors.append("project.director must be an object")
    config = director_config(project)
    if config["mode"] not in DIRECTOR_MODES:
        errors.append("director.mode is unsupported")
    if config["project_type"] not in PROJECT_TYPES:
        errors.append("director.project_type is unsupported")
    if any(value not in GENRE_PACKS for value in config["genre_packs"]):
        errors.append("director.genre_packs contains an unsupported id")
    if config["default_exit_behavior"] not in EXIT_BEHAVIORS:
        errors.append("director.default_exit_behavior is unsupported")
    raw_director = project.get("director") if isinstance(project.get("director"), dict) else {}
    if raw_director.get("custom_direction") is not None and not isinstance(raw_director.get("custom_direction"), str):
        errors.append("director.custom_direction must be a string")
    beats = project.get("story_beats", [])
    if not isinstance(beats, list):
        errors.append("project.story_beats must be an array")
        beats = []
    beat_ids: set[str] = set()
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            errors.append(f"story_beats[{index}] must be an object")
            continue
        beat_id = str(beat.get("id", "")).strip()
        if not beat_id:
            errors.append(f"story_beats[{index}].id is required")
        elif beat_id in beat_ids:
            errors.append(f"duplicate story beat id: {beat_id}")
        beat_ids.add(beat_id)
        if not str(beat.get("visible_event", "")).strip():
            errors.append(f"story_beats[{index}].visible_event is required")
    for index, shot in enumerate(project.get("shots", [])):
        if not isinstance(shot, dict):
            continue
        prefix = f"shots[{index}]"
        intent = shot_audio_intent(shot)
        if intent not in AUDIO_INTENTS:
            errors.append(f"{prefix}.audio_intent is unsupported")
        dialogue = shot.get("dialogue") if isinstance(shot.get("dialogue"), list) else []
        if dialogue and intent != "dialogue":
            errors.append(f"{prefix}.audio_intent must be dialogue when dialogue lines are present")
        if intent == "dialogue" and not dialogue:
            errors.append(f"{prefix}.audio_intent dialogue requires at least one dialogue line")
        narration = str(shot.get("narration", "")).strip()
        if narration and not dialogue and intent != "narration":
            errors.append(f"{prefix}.audio_intent must be narration when narration is present without dialogue")
        if intent == "narration" and not narration:
            errors.append(f"{prefix}.audio_intent narration requires shot narration")
        exit_behavior = str(shot.get("exit_behavior", config["default_exit_behavior"])).strip()
        if exit_behavior not in EXIT_BEHAVIORS:
            errors.append(f"{prefix}.exit_behavior is unsupported")
        try:
            edit_in, edit_out, timeline = edit_window(shot)
            seconds = float(shot.get("seconds", 6))
            if edit_in < 0 or edit_out <= edit_in or edit_out > seconds + 0.001:
                errors.append(f"{prefix} must satisfy 0 <= edit_in < edit_out <= seconds")
            if timeline <= 0 or abs(timeline - (edit_out - edit_in)) > 0.05:
                errors.append(f"{prefix}.timeline_duration must equal edit_out - edit_in")
        except (TypeError, ValueError):
            errors.append(f"{prefix} edit_in, edit_out, and timeline_duration must be numbers")
        performance = shot.get("performance")
        if performance is not None and not isinstance(performance, dict):
            errors.append(f"{prefix}.performance must be an object")
        beat_id = str(shot.get("beat_id", "")).strip()
        if beat_id and beat_id not in beat_ids:
            errors.append(f"{prefix}.beat_id must reference project.story_beats")
    return errors


def director_gate(project: dict[str, Any]) -> dict[str, list[str]]:
    config = director_config(project)
    errors: list[str] = []
    warnings: list[str] = []
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    if not config["strict"] or len(shots) < 2:
        return {"errors": errors, "warnings": warnings}
    roles = [str(shot.get("shot_role", "")).strip() for shot in shots]
    dialogue_count = sum(bool(shot.get("dialogue")) for shot in shots)
    if not project.get("story_beats"):
        errors.append("strict director mode requires story_beats before paid generation")
    if dialogue_count == len(shots):
        errors.append("strict director mode blocks 100% dialogue-shot coverage")
    if len(set(value for value in roles if value)) < 2:
        errors.append("strict director mode requires at least two shot roles")
    if config["mode"] in {"cinematic-short", "dialogue-scene", "comedy-scene"}:
        establishing_roles = {"establishing", "wide", "over_shoulder"} if config["mode"] == "dialogue-scene" else {"establishing", "wide"}
        if not any(value in establishing_roles for value in roles):
            errors.append("narrative director mode requires an establishing or wide shot")
        if not any(value == "reaction" for value in roles):
            errors.append("narrative director mode requires a reaction shot")
    if config["mode"] == "action-scene" and not any(value in {"wide", "insert", "reaction"} for value in roles):
        errors.append("action-scene requires wide, insert, or reaction coverage")
    for index, shot in enumerate(shots[:-1]):
        ending = str(shot.get("ending_pose", "")).strip()
        exit_behavior = str(shot.get("exit_behavior", config["default_exit_behavior"])).strip()
        if ending and exit_behavior != "ending-hook":
            warnings.append(f"shots[{index}] has ending_pose before the final shot; prefer a cuttable continuing action")
    return {"errors": errors, "warnings": warnings}
