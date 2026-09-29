#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from workflow_registry import DIRECTOR_MODES, GENRE_PACKS, PROJECT_TYPES


AUDIO_INTENTS = {"dialogue", "narration", "score-ambience", "effects-ambience", "intentional-silence"}
EXIT_BEHAVIORS = {"continue-action", "cut-on-action", "hold-reaction", "ending-hook"}
STORY_CONTRACT_FIELDS = ("goal", "obstacle", "decision", "consequence", "payoff")
NARRATIVE_CONTRACT_FIELDS = (
    "protagonist_goal",
    "audience_knows",
    "character_knows",
    "choice",
    "visible_consequence",
    "next_question",
)
NARRATIVE_CONTRACT_ALIASES = {
    "protagonist_goal": ("protagonist_goal", "main_character_goal", "goal"),
    "audience_knows": ("audience_knows", "audience_known", "audience_known_information"),
    "character_knows": ("character_knows", "character_known", "character_known_information"),
    "choice": ("choice", "decision", "chosen_action"),
    "visible_consequence": ("visible_consequence", "visible_result", "consequence", "result"),
    "next_question": ("next_question", "open_question", "what_next"),
}
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
        "boundary_policy": str(value.get("boundary_policy", "warn")).strip().lower() or "warn",
    }


def story_contract(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("story_contract") if isinstance(project.get("story_contract"), dict) else {}
    return {
        "required": bool(value.get("required", False)),
        **{field: str(value.get(field, "")).strip() for field in STORY_CONTRACT_FIELDS},
    }


def narrative_contract(project: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit v2 viewer-causality contract.

    The aliases keep projects authored against the earlier director contract
    readable while giving new projects one stable machine-facing schema.
    """
    value = project.get("narrative_contract_v2") if isinstance(project.get("narrative_contract_v2"), dict) else {}
    result: dict[str, Any] = {"required": bool(value.get("required", False))}
    for field in NARRATIVE_CONTRACT_FIELDS:
        result[field] = ""
        for alias in NARRATIVE_CONTRACT_ALIASES[field]:
            candidate = str(value.get(alias, "")).strip()
            if candidate:
                result[field] = candidate
                break
    return result


def _first_text(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def shot_narrative_contract(shot: dict[str, Any]) -> dict[str, str]:
    """Extract the four causal answers required for one narrative shot."""
    performance = shot.get("performance") if isinstance(shot.get("performance"), dict) else {}
    return {
        "main_event": _first_text(shot, ("main_event", "visible_event", "summary")),
        "motivation": _first_text(shot, ("motivation", "why_now", "intent"))
        or _first_text(performance, ("trigger", "decision")),
        "result": _first_text(shot, ("result", "visible_result", "outcome", "consequence"))
        or _first_text(shot, ("continuity_out", "exit_action")),
        "next_reason": _first_text(shot, ("next_reason", "why_next", "cut_motivation"))
        or _first_text(shot, ("continuity_out", "exit_action")),
    }


def narrative_contract_coverage(project: dict[str, Any]) -> dict[str, Any]:
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    fields = ("main_event", "motivation", "result", "next_reason")
    counts = {field: 0 for field in fields}
    for shot in shots:
        extracted = shot_narrative_contract(shot)
        for field in fields:
            counts[field] += int(bool(extracted[field]))
    denominator = max(1, len(shots))
    return {
        "shot_count": len(shots),
        "field_counts": counts,
        "coverage": {field: round(count / denominator, 3) for field, count in counts.items()},
        "complete_shots": sum(all(shot_narrative_contract(shot).values()) for shot in shots),
    }


def _flatten_state(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        result: dict[str, str] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten_state(child, path))
        return result
    if isinstance(value, list):
        result = {}
        for index, child in enumerate(value):
            result.update(_flatten_state(child, f"{prefix}[{index}]"))
        return result
    if value in (None, ""):
        return {}
    return {prefix: str(value)} if prefix else {}


def continuity_state_diff(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare declared scene and asset state at an edit boundary."""
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for name in ("scene_state", "asset_state"):
        before.update({f"{name}.{key}": value for key, value in _flatten_state(previous.get(name, {})).items()})
        after.update({f"{name}.{key}": value for key, value in _flatten_state(current.get(name, {})).items()})
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    removed = sorted(key for key in set(before) - set(after))
    added = sorted(key for key in set(after) - set(before))
    return {
        "changed": [{"field": key, "before": before[key], "after": after[key]} for key in changed],
        "removed": [{"field": key, "before": before[key]} for key in removed],
        "added": [{"field": key, "after": after[key]} for key in added],
        "has_diff": bool(changed or removed or added),
    }


def validate_narrative_contract(project: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    raw = project.get("narrative_contract_v2")
    if raw is not None and not isinstance(raw, dict):
        errors.append("project.narrative_contract_v2 must be an object")
        raw = {}
    if isinstance(raw, dict):
        if raw.get("required") is not None and not isinstance(raw.get("required"), bool):
            errors.append("narrative_contract_v2.required must be a boolean")
        for field in NARRATIVE_CONTRACT_FIELDS:
            aliases = NARRATIVE_CONTRACT_ALIASES[field]
            for alias in aliases:
                if alias in raw and raw[alias] is not None and not isinstance(raw[alias], str):
                    errors.append(f"narrative_contract_v2.{alias} must be a string")
    contract = narrative_contract(project)
    if not contract["required"]:
        return errors
    missing = [field for field in NARRATIVE_CONTRACT_FIELDS if not contract[field]]
    if missing:
        errors.append("narrative_contract_v2 is incomplete: fill " + ", ".join(missing))
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    for index, shot in enumerate(shots):
        extracted = shot_narrative_contract(shot)
        missing_shot = [field for field in ("main_event", "motivation", "result", "next_reason") if not extracted[field]]
        if missing_shot:
            errors.append(f"shots[{index}] narrative contract is incomplete: fill {', '.join(missing_shot)}")
        actions = shot.get("actions")
        if isinstance(actions, list) and len([item for item in actions if str(item).strip()]) > 2:
            errors.append(f"shots[{index}] contains too many concurrent actions; split the beat across shots")
        try:
            action_count = int(shot.get("action_count", 0))
        except (TypeError, ValueError):
            errors.append(f"shots[{index}].action_count must be an integer")
            action_count = 0
        if action_count > 2:
            errors.append(f"shots[{index}] action_count {action_count} exceeds the two-action shot limit")
    return errors


def story_clarity_score(project: dict[str, Any]) -> dict[str, Any]:
    """Score whether a viewer can follow the causal chain before generation."""
    contract = story_contract(project)
    v2_contract = narrative_contract(project)
    present = [field for field in STORY_CONTRACT_FIELDS if contract[field]]
    beats = [beat for beat in project.get("story_beats", []) if isinstance(beat, dict)]
    beat_fields = ("visible_event", "audience_effect", "consequence", "why_next")
    beat_points = sum(sum(bool(str(beat.get(field, "")).strip()) for field in beat_fields) for beat in beats)
    beat_max = max(1, len(beats) * len(beat_fields))
    narrative_coverage = narrative_contract_coverage(project)
    legacy_contract_ratio = len(present) / len(STORY_CONTRACT_FIELDS)
    v2_contract_ratio = sum(bool(v2_contract[field]) for field in NARRATIVE_CONTRACT_FIELDS) / len(NARRATIVE_CONTRACT_FIELDS)
    shot_items = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    event_shots = sum(bool(str(shot.get("summary", "")).strip()) for shot in shot_items)
    causal_shots = sum(
        bool(str(shot.get("continuity_out", "")).strip()) and bool(str(shot.get("continuity_in", "")).strip())
        for shot in shot_items[1:]
    )
    score = round(
        max(legacy_contract_ratio, v2_contract_ratio) * 50
        + max((beat_points / beat_max), narrative_coverage["complete_shots"] / max(1, len(shot_items))) * 30
        + (event_shots / max(1, len(shot_items))) * 10
        + (causal_shots / max(1, len(shot_items) - 1)) * 10,
        1,
    )
    missing = [field for field in STORY_CONTRACT_FIELDS if not contract[field]]
    return {
        "score": score,
        "threshold": 70.0,
        "required": contract["required"] or v2_contract["required"],
        "missing_contract_fields": missing if not v2_contract["required"] else [field for field in NARRATIVE_CONTRACT_FIELDS if not v2_contract[field]],
        "story_beat_count": len(beats),
        "shot_event_coverage": round(event_shots / max(1, len(shot_items)), 3),
        "causal_handoff_coverage": round(causal_shots / max(1, len(shot_items) - 1), 3) if len(shot_items) > 1 else 1.0,
        "narrative_contract": narrative_contract_coverage(project),
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
    if config["boundary_policy"] not in {"warn", "block"}:
        errors.append("director.boundary_policy must be warn or block")
    raw_story = project.get("story_contract")
    if raw_story is not None and not isinstance(raw_story, dict):
        errors.append("project.story_contract must be an object")
    elif isinstance(raw_story, dict):
        if raw_story.get("required") is not None and not isinstance(raw_story.get("required"), bool):
            errors.append("story_contract.required must be a boolean")
        for field in STORY_CONTRACT_FIELDS:
            if raw_story.get(field) is not None and not isinstance(raw_story.get(field), str):
                errors.append(f"story_contract.{field} must be a string")
    errors.extend(validate_narrative_contract(project))
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
    clarity = story_clarity_score(project)
    narrative_project = bool(story_contract(project)["required"]) or config["mode"] != "single-shot" or len(shots) >= 2
    if narrative_project and clarity["score"] < clarity["threshold"]:
        missing = ", ".join(clarity["missing_contract_fields"]) or "shot-level causal handoffs"
        message = f"story clarity score {clarity['score']:.1f}/100 is below {clarity['threshold']:.0f}; fill {missing}"
        if clarity["required"]:
            errors.append(message)
        else:
            warnings.append(message)
    if not config["strict"] or len(shots) < 2:
        # Even relaxed projects benefit from a visible one-beat contract.  A
        # missing answer is a warning until narrative_contract_v2.required is
        # enabled, so legacy single clips remain compatible.
        coverage = narrative_contract_coverage(project)
        if (
            coverage["shot_count"] > 1 or narrative_contract(project)["required"] or config["mode"] != "single-shot"
        ) and coverage["shot_count"] and coverage["complete_shots"] < coverage["shot_count"]:
            warnings.append(
                "some shots lack a complete main-event/motivation/result/next-reason contract; "
                "the viewer may not understand the causal transition"
            )
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
        next_shot = shots[index + 1]
        boundary_missing = []
        if not str(shot.get("exit_action", "")).strip():
            boundary_missing.append("exit_action")
        if not str(shot.get("continuity_out", "")).strip():
            boundary_missing.append("continuity_out")
        if not str(next_shot.get("entry_action", "")).strip():
            boundary_missing.append("next.entry_action")
        if not str(next_shot.get("continuity_in", "")).strip():
            boundary_missing.append("next.continuity_in")
        if boundary_missing:
            message = f"boundary after {shot.get('id', index)} lacks {', '.join(boundary_missing)}"
            if config["boundary_policy"] == "block":
                errors.append(message)
            else:
                warnings.append(message)
    coverage = narrative_contract_coverage(project)
    if coverage["shot_count"] and coverage["complete_shots"] < coverage["shot_count"]:
        warnings.append(
            f"narrative shot contract coverage is {coverage['complete_shots']}/{coverage['shot_count']}; "
            "fill main_event, motivation, result, and next_reason before paid generation"
        )
    return {"errors": errors, "warnings": warnings}
