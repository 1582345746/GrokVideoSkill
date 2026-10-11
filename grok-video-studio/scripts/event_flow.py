"""Deterministic event and editorial contracts; never a semantic quality score.

This reducer verifies declared causality before spending. Only a pixel/audio
review can establish whether the provider actually realized the plan.
"""
from __future__ import annotations

import math
import re
from typing import Any

FUNCTIONS = {"goal", "obstacle", "choice", "discovery", "escalation", "reaction", "consequence", "reversal", "ending_hook"}
INTENTS = {"accuse", "deny", "warn", "request", "reveal", "threaten", "mislead", "decide", "payoff", "answer"}
PACE_DEFAULTS = {
    "max_shot_seconds": 3.5,
    "max_silent_seconds": 0.6,
    "max_dialogue_lead_seconds": 0.4,
    "max_dialogue_tail_seconds": 0.5,
    "max_no_change_seconds": 6.0,
    "max_function_repeat": 2,
    "max_visible_characters": 3,
    "max_speakers_per_shot": 1,
    "max_speech_units_per_second": 8.0,
}
FAST_DIALOGUE_PACE_DEFAULTS = {
    "max_timeline_silent_seconds": 0.8,
    "max_task_silent_seconds": 1.2,
}

# Narrative workflows need an explicit state-changing event chain before any
# paid request is allowed.  The exception list is intentionally small: a
# single shot, silent film, or montage can be valid without causality, while a
# project that presents itself as a narrative must opt into the reducer.
EVENT_FLOW_REQUIRED_WORKFLOWS = frozenset(
    {
        "short-drama",
        "dialogue-scene",
        "cinematic-short",
        "action-scene",
        "fast-continuous-event-dialogue",
    }
)
EVENT_FLOW_REQUIRED_DIRECTOR_MODES = frozenset(
    {"dialogue-scene", "cinematic-short", "action-scene"}
)
EVENT_FLOW_EXEMPT_WORKFLOWS = frozenset(
    {"single-shot", "silent", "silent-cinema", "montage"}
)
EVENT_FLOW_EXEMPT_DIRECTOR_MODES = frozenset(
    {"single-shot", "silent", "silent-cinema", "montage"}
)


def event_flow_policy(project: dict[str, Any]) -> dict[str, Any]:
    """Resolve whether this project must declare a causal event flow.

    Workflow IDs are the primary signal, but manually authored projects may
    only carry a director mode.  Explicit non-narrative modes remain exempt
    unless a narrative workflow was selected alongside them.
    """
    workflow = str(project.get("workflow", "")).strip()
    director = project.get("director") if isinstance(project.get("director"), dict) else {}
    mode = str(director.get("mode", "")).strip()
    required_by_workflow = workflow in EVENT_FLOW_REQUIRED_WORKFLOWS
    # A declared workflow is authoritative.  Director-mode fallback is only
    # for hand-authored projects that omit `workflow`; otherwise presets such
    # as character-consistent-story would unexpectedly inherit a narrative
    # event contract from their rendering mode.
    required_by_mode = not workflow and mode in EVENT_FLOW_REQUIRED_DIRECTOR_MODES
    explicit_exception = (
        workflow in EVENT_FLOW_EXEMPT_WORKFLOWS or mode in EVENT_FLOW_EXEMPT_DIRECTOR_MODES
    )
    required = required_by_workflow or required_by_mode
    # An explicitly narrative workflow cannot be downgraded by a copied or
    # stale director mode.  Otherwise a selected silent/montage mode is a
    # deliberate exception and remains compatible with legacy projects.
    if explicit_exception and not required_by_workflow:
        required = False
    if required:
        reason = "narrative workflow/director mode"
    elif explicit_exception:
        reason = "explicit non-narrative exception"
    else:
        reason = "non-narrative workflow"
    return {
        "required": required,
        "workflow": workflow or "unspecified",
        "director_mode": mode or "unspecified",
        "required_by_workflow": required_by_workflow,
        "required_by_director_mode": required_by_mode,
        "explicit_exception": explicit_exception,
        "reason": reason,
    }


def speech_units(text: str) -> int:
    """Count CJK syllables and Latin words, rather than whitespace in Mandarin."""
    return len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))


def _same(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _flat_state(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and k.strip() and isinstance(v, (str, int, float, bool))
        and not (isinstance(v, float) and not math.isfinite(v)) for k, v in value.items()
    )


def _dialogue_timeline(gaps: list[dict[str, Any]], total: float, enabled: bool,
                       pace: dict[str, Any]) -> dict[str, Any]:
    """Join no-dialogue fragments across editorial cuts, including lead/tail pads."""
    merged: list[dict[str, Any]] = []
    for gap in gaps:
        if merged and abs(merged[-1]["end"] - gap["start"]) < 0.001:
            merged[-1]["end"] = gap["end"]
            merged[-1]["parts"].append(gap)
        else:
            merged.append({"start": gap["start"], "end": gap["end"], "parts": [gap]})
    errors: list[str] = []
    for gap in merged:
        duration = gap["end"] - gap["start"]
        motivated = any(part["task_reasons"] for part in gap["parts"])
        limit = pace["max_task_silent_seconds"] if motivated else pace["max_timeline_silent_seconds"]
        unmotivated = sum(part["end"] - part["start"] for part in gap["parts"] if not part["task_reasons"])
        shot_ids = list(dict.fromkeys(part["shot_id"] for part in gap["parts"]))
        blocked = enabled and (duration > limit + 0.001 or unmotivated > pace["max_timeline_silent_seconds"] + 0.001)
        if enabled and duration > limit + 0.001:
            kind = "task insert" if motivated else "unmotivated no-dialogue"
            errors.append(
                f"dialogue timeline {' -> '.join(shot_ids)}: {kind} gap "
                f"{gap['start']:.2f}-{gap['end']:.2f}s ({duration:.2f}s) exceeds {limit}s"
            )
        elif blocked:
            errors.append(
                f"dialogue timeline {' -> '.join(shot_ids)}: unmotivated padding "
                f"{unmotivated:.2f}s within {gap['start']:.2f}-{gap['end']:.2f}s "
                f"exceeds {pace['max_timeline_silent_seconds']}s; a task insert cannot excuse padding"
            )
        gap.update({"start": round(gap["start"], 3), "end": round(gap["end"], 3),
                    "duration": round(duration, 3), "shot_ids": shot_ids,
                    "unmotivated_seconds": round(unmotivated, 3),
                    "task_motivated": motivated, "limit_seconds": limit, "blocked": blocked})
        for part in gap["parts"]:
            for key in ("start", "end", "source_start", "source_end"):
                part[key] = round(part[key], 3)
    return {
        "enabled": enabled, "evidence_kind": "declared-plan",
        "actual_speech_verified": False,
        "status": "blocked" if errors else "contract-valid" if enabled else "not-enabled",
        "errors": errors, "no_dialogue_intervals": merged,
        "no_dialogue_seconds": round(sum(gap["duration"] for gap in merged), 3),
        "longest_no_dialogue_seconds": max((gap["duration"] for gap in merged), default=0.0),
        "timeline_seconds": round(total, 3),
    }


def event_flow_report(project: dict[str, Any]) -> dict[str, Any]:
    raw = project.get("event_flow")
    policy = event_flow_policy(project)
    declared_required = isinstance(raw, dict) and raw.get("required") is True
    required = bool(policy["required"])
    fast_dialogue = project.get("workflow") == "fast-continuous-event-dialogue"
    errors: list[str] = []
    report: dict[str, Any] = {
        "required": required, "status": "not-enabled", "evidence_kind": "declared-plan",
        "declared_required": declared_required,
        "policy": policy,
        "semantic_quality": "unverified", "errors": errors, "shots": [],
    }
    if required and not declared_required:
        workflow_label = policy["workflow"]
        mode_label = policy["director_mode"]
        if workflow_label == "fast-continuous-event-dialogue":
            errors.append("fast-continuous-event-dialogue requires event_flow.required=true")
        elif policy["required_by_workflow"]:
            errors.append(f"{workflow_label} requires event_flow.required=true")
        else:
            errors.append(f"director.mode={mode_label} requires event_flow.required=true")
        report["status"] = "blocked"
    if raw is None:
        return report
    if not isinstance(raw, dict):
        errors.append("event_flow must be an object")
        return report
    if not isinstance(raw.get("required", False), bool):
        errors.append("event_flow.required must be a boolean")
    if required and raw.get("required") is not True:
        # The policy error above is the actionable message.  Do not continue
        # validating an intentionally disabled contract and produce a noisy
        # cascade of missing event fields.
        report["status"] = "blocked"
        return report
    if not required and not declared_required:
        return report
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        errors.append("event_flow.event_id is required")
    state = raw.get("initial_state", {})
    if not _flat_state(state) or not state:
        errors.append("event_flow.initial_state must be a nonempty flat scalar object")
        state = {}
    state = dict(state)
    pace_raw = raw.get("pace", {})
    if not isinstance(pace_raw, dict):
        errors.append("event_flow.pace must be an object")
        pace_raw = {}
    pace = dict(PACE_DEFAULTS)
    pace.update(FAST_DIALOGUE_PACE_DEFAULTS)
    defaults = {**PACE_DEFAULTS, **(FAST_DIALOGUE_PACE_DEFAULTS if fast_dialogue else {})}
    for key, default in defaults.items():
        value = pace_raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            errors.append(f"event_flow.pace.{key} must be a positive finite number")
        elif isinstance(default, int) and int(value) != value:
            errors.append(f"event_flow.pace.{key} must be an integer")
        else:
            pace[key] = value
    characters = {c.get("id") for c in project.get("characters", []) if isinstance(c, dict)}
    seen_lines: set[str] = set()
    previous_id = ""
    previous_function = ""
    active_beat = ""
    active_changes: dict[str, Any] = {}
    repeat = 0
    unchanged = 0.0
    total = 0.0
    speaking = 0.0
    longest = 0.0
    timeline_gaps: list[dict[str, Any]] = []
    shots = project.get("shots", [])
    if not isinstance(shots, list) or not shots:
        errors.append("event_flow requires shots")
        shots = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            errors.append(f"shots[{index}] must be an object")
            continue
        sid = str(shot.get("id", index))
        prefix = f"{sid} event flow"
        event = shot.get("event")
        if not isinstance(event, dict):
            errors.append(f"{prefix}: event is required")
            event = {}
        if event.get("event_id") != event_id:
            errors.append(f"{prefix}: must advance event_id {event_id!r}")
        function = event.get("function")
        if not isinstance(function, str) or function not in FUNCTIONS:
            errors.append(f"{prefix}: unsupported function {function!r}")
        mode = event.get("mode", "advance")
        if mode != "coverage":
            repeat = repeat + 1 if function == previous_function else 1
            previous_function = function
            if repeat > pace["max_function_repeat"]:
                errors.append(f"{prefix}: repeated function {function!r} exceeds pace limit")
        if index == 0:
            if not str(event.get("trigger", "")).strip():
                errors.append(f"{prefix}: first shot needs an observable trigger")
        elif event.get("cause_shot_id") != previous_id:
            errors.append(f"{prefix}: cause_shot_id must reference preceding shot {previous_id}")
        previous_id = sid
        for key in ("viewer_learns", "bridge_to_next"):
            if not isinstance(event.get(key), str) or not event[key].strip():
                errors.append(f"{prefix}: {key} is required")
        requires, effects = event.get("requires"), event.get("effects")
        if not _flat_state(requires) or not requires:
            errors.append(f"{prefix}: requires must name a nonempty input state")
            requires = {}
        if not _flat_state(effects):
            errors.append(f"{prefix}: effects must be a flat scalar object")
            effects = {}
        for key, value in requires.items():
            if key not in state or not _same(state[key], value):
                errors.append(f"{prefix}: input state mismatch for {key}; declared={value!r}, actual={state.get(key)!r}")
        before = dict(state)
        changed = {key: value for key, value in effects.items() if key not in state or not _same(state[key], value)}
        if not isinstance(mode, str) or mode not in {"advance", "coverage"}:
            errors.append(f"{prefix}: mode must be advance or coverage")
        if mode == "coverage":
            role = event.get("coverage_role")
            if not active_beat or event.get("beat_id") != active_beat or not isinstance(role, str) or role not in {"speaker", "listener", "object", "action"}:
                errors.append(f"{prefix}: coverage must reference the active advancing beat and a coverage_role")
            if changed:
                errors.append(f"{prefix}: coverage cannot introduce a new event state; use advance")
        elif not changed:
            errors.append(f"{prefix}: no state change; split out or remove this empty beat")
        else:
            active_beat = str(event.get("beat_id", sid))
            active_changes = changed
        state.update(effects)
        try:
            seconds = float(shot.get("seconds", 6))
            edit_in = float(shot.get("edit_in", 0) or 0)
            edit_out = float(shot.get("edit_out", seconds) or seconds)
            duration = edit_out - edit_in
            if not all(math.isfinite(v) for v in (seconds, edit_in, edit_out)) or not 0 <= edit_in < edit_out <= seconds:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{prefix}: invalid edit window")
            continue
        total += duration
        longest = max(longest, duration)
        if duration > pace["max_shot_seconds"] + 0.001:
            errors.append(f"{prefix}: edit duration {duration:.2f}s exceeds {pace['max_shot_seconds']}s")
        unchanged = 0.0 if changed else unchanged + duration
        if unchanged > pace["max_no_change_seconds"]:
            errors.append(f"{prefix}: no-change span {unchanged:.2f}s exceeds pace limit")
        visible = shot.get("character_ids", [])
        if not isinstance(visible, list):
            errors.append(f"{prefix}: character_ids must be a list of visible characters")
            visible = []
        if len(visible) > pace["max_visible_characters"]:
            errors.append(f"{prefix}: too many visible principal characters")
        lines = shot.get("dialogue", [])
        if not isinstance(lines, list):
            errors.append(f"{prefix}: dialogue must be an array")
            lines = []
        has_narration = bool(str(shot.get("narration", "")).strip()) or str(shot.get("audio_intent", "")).strip().lower() == "narration"
        speakers = {str(line.get("speaker")) for line in lines if isinstance(line, dict)}
        if len(speakers) > pace["max_speakers_per_shot"]:
            errors.append(f"{prefix}: split speaking turns into separate editorial shots")
        cursor = edit_in
        spoken_duration = 0.0
        speech_intervals: list[tuple[float, float]] = []
        for line in lines:
            if not isinstance(line, dict):
                errors.append(f"{prefix}: dialogue line must be an object")
                continue
            lid = str(line.get("id", "line"))
            lp = f"{sid}/{lid}"
            if not isinstance(line.get("on_screen", True), bool):
                errors.append(f"{lp}: on_screen must be a boolean")
            speaker, listener = line.get("speaker"), line.get("listener")
            if not isinstance(speaker, str) or not isinstance(listener, str) or speaker not in characters or listener not in characters or speaker == listener:
                errors.append(f"{lp}: distinct known speaker and listener are required")
            if line.get("on_screen", True) is not False and speaker not in visible:
                errors.append(f"{lp}: on-screen speaker must be visible in character_ids")
            source = line.get("audio_source")
            if line.get("on_screen", True) is False and (not isinstance(source, str) or source not in {"offscreen", "phone", "speaker"}):
                errors.append(f"{lp}: off-screen speech needs a motivated audio_source")
            if line.get("on_screen", True) is False and speaker in visible:
                errors.append(f"{lp}: off-screen speaker cannot also be in character_ids")
            if not isinstance(line.get("intent"), str) or line["intent"] not in INTENTS:
                errors.append(f"{lp}: dialogue intent is required")
            if not isinstance(line.get("new_information"), str) or not line["new_information"].strip():
                errors.append(f"{lp}: new_information is required")
            if not isinstance(line.get("effect_key"), str) or line["effect_key"] not in (active_changes if mode == "coverage" else changed):
                errors.append(f"{lp}: effect_key must name this shot's changed state")
            text = str(line.get("text", "")).strip()
            if not text:
                errors.append(f"{lp}: dialogue text is required")
            normalized = re.sub(r"[\W_]+", "", text)
            if normalized in seen_lines and not str(line.get("repeat_reason", "")).strip():
                errors.append(f"{lp}: repeated dialogue requires a motivated repeat_reason")
            seen_lines.add(normalized)
            try:
                start, end = float(line.get("start")), float(line.get("end"))
                if not all(math.isfinite(v) for v in (start, end)) or not edit_in <= start < end <= edit_out or start < cursor:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{lp}: dialogue must fit the retained edit window without overlap")
                continue
            if start - cursor > pace["max_silent_seconds"]:
                errors.append(f"{lp}: unmotivated silent gap {start - cursor:.2f}s")
            if cursor == edit_in and start - edit_in > pace["max_dialogue_lead_seconds"]:
                errors.append(f"{lp}: dialogue starts too late")
            if speech_units(text) / (end - start) > pace["max_speech_units_per_second"]:
                errors.append(f"{lp}: line is too long to remain intelligible in its window")
            spoken_duration += end - start
            cursor = end
            if text:
                speech_intervals.append((start, end))
        speaking += spoken_duration
        task_reasons: list[str] = []
        if mode == "coverage" and event.get("beat_id") == active_beat and event.get("coverage_role") in ("listener", "object", "action") and not changed:
            task_reasons.append(f"coverage:{event['coverage_role']}")
        if changed:
            task_reasons.append("state-effect")
        if any(isinstance(shot.get(key), str) and shot[key].strip() for key in ("entry_action", "exit_action")):
            task_reasons.append("visible-action")
        if lines:
            if edit_out - cursor > pace["max_dialogue_tail_seconds"]:
                errors.append(f"{prefix}: dead tail after dialogue {edit_out - cursor:.2f}s")
        elif not has_narration:
            limit = pace["max_task_silent_seconds"] if fast_dialogue and task_reasons else pace["max_silent_seconds"]
            if duration > limit + 0.001:
                errors.append(f"{prefix}: silent shot {duration:.2f}s exceeds {limit}s; carry motivated off-screen dialogue or shorten the insert")
        # Valid source windows remain source-relative; only this projection uses final timeline time.
        gap_cursor = edit_in
        silent_tasks = task_reasons if not speech_intervals else []
        for start, end in speech_intervals + [(edit_out, edit_out)]:
            if start > gap_cursor + 0.001:
                timeline_gaps.append({"shot_id": sid, "start": total - duration + gap_cursor - edit_in,
                                      "end": total - duration + start - edit_in,
                                      "source_start": gap_cursor, "source_end": start,
                                      "task_reasons": list(silent_tasks)})
            gap_cursor = max(gap_cursor, end)
        report["shots"].append({"id": sid, "function": function, "state_in": before, "state_out": dict(state), "changes": changed, "timeline_start": round(total - duration, 3), "timeline_duration": round(duration, 3), "declared_speech_seconds": round(spoken_duration, 3)})
    dialogue_timeline = _dialogue_timeline(timeline_gaps, total, fast_dialogue, pace)
    errors.extend(dialogue_timeline["errors"])
    report.update({
        "status": "contract-valid" if not errors else "blocked", "final_state": state, "pace": pace,
        "timeline_seconds": round(total, 3), "mean_edit_shot_seconds": round(total / max(1, len(shots)), 3),
        "longest_edit_shot_seconds": round(longest, 3),
        "declared_speech_ratio": round(speaking / total, 3) if total else 0.0,
        "dialogue_timeline": dialogue_timeline,
    })
    return report


def validate_event_flow(project: dict[str, Any]) -> list[str]:
    return event_flow_report(project)["errors"]
