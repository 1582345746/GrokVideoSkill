#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, project_state_lock, read_json
from media_tools import export_review_frames, postprocess_video, probe_media, quality_report
from chatcut_adapter import build_chatcut_contract, chatcut_capability_report


EDIT_PLAN_VERSION = 2
EDIT_STATE_VERSION = 2
EDIT_BACKENDS = {"auto", "native", "chatcut", "jianying-draft"}
TRANSITIONS = {
    "cut": "",
    "dissolve": "fade",
    "fade-black": "fadeblack",
    "wipe-left": "wipeleft",
    "slide-left": "slideleft",
}
FILTER_PRESETS = {
    "none": "",
    "cinematic": "eq=contrast=1.06:saturation=0.92:gamma=0.98,vignette=PI/5",
    "warm": "colorbalance=rs=.05:gs=.01:bs=-.04",
    "cool": "colorbalance=rs=-.04:gs=.01:bs=.05",
    "vivid": "eq=contrast=1.05:saturation=1.18",
    "monochrome": "hue=s=0",
    "denoise": "hqdn3d=1.5:1.5:6:6",
    "sharpen": "unsharp=5:5:0.65:5:5:0",
}


def edit_plan_path(root: Path) -> Path:
    return root / "edit-plan.json"


def edit_state_path(root: Path) -> Path:
    return root / "edit-state.json"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _json_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    raw = Path(str(value))
    if raw.is_absolute():
        raise SkillError(f"edit plan paths must be relative to the project: {value}")
    root = root.resolve()
    path = (root / raw).resolve()
    try:
        common = Path(os.path.commonpath([str(root), str(path)]))
    except ValueError as error:
        raise SkillError(f"edit plan path leaves the project: {value}") from error
    if common != root:
        raise SkillError(f"edit plan path leaves the project: {value}")
    if must_exist and not path.is_file():
        raise SkillError(f"edit input does not exist: {value}")
    return path


def editing_capabilities() -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {
        "native": {
            "available": bool(ffmpeg and ffprobe),
            "ffmpeg": ffmpeg or "not_found",
            "ffprobe": ffprobe or "not_found",
            "transitions": sorted(TRANSITIONS),
            "filters": sorted(FILTER_PRESETS),
            "per_shot_filters": True,
            "per_shot_speed": {"minimum": 0.25, "maximum": 4.0},
            "per_boundary_transitions": True,
            "mixed_cut_and_transition_boundaries": True,
            "loudness_normalization_lufs": {"minimum": -24, "maximum": -10},
            "preview_evidence": True,
            "edit_plan_versions": {"write": EDIT_PLAN_VERSION, "read": [1, EDIT_PLAN_VERSION]},
            "resumable": True,
            "output": "deliverables/final-edited.mp4",
            "preserves_clean_master": True,
        },
        "chatcut": chatcut_capability_report(),
        "jianying-draft": {
            "available": "experimental-export-only",
            "portable": False,
            "default_fallback": False,
            "environment": detect_jianying_environment(),
        },
    }


def detect_jianying_environment() -> dict[str, Any]:
    if os.name != "nt":
        return {"installed": False, "platform": os.name, "reason": "Jianying desktop draft probing is Windows-only."}
    local = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local) / "JianyingPro" if local else Path("__missing__")
    exe = root / "Apps" / "JianyingPro.exe"
    versions = []
    apps = root / "Apps"
    if apps.is_dir():
        versions = sorted(
            (item.name for item in apps.iterdir() if item.is_dir() and re.fullmatch(r"\d+(?:\.\d+){2,3}", item.name)),
            key=lambda value: tuple(int(part) for part in value.split(".")),
            reverse=True,
        )
    draft_root = root / "User Data" / "Projects" / "com.lveditor.draft"
    draft_format = "not_observed"
    if draft_root.is_dir():
        for path in sorted(draft_root.glob("*/draft_content.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            header = path.read_bytes()[:96].lstrip()
            if header.startswith((b"{", b"[")):
                draft_format = "plain_json"
            elif header and re.fullmatch(rb"[A-Za-z0-9+/=]+", header):
                draft_format = "opaque_base64-like_payload"
            else:
                draft_format = "opaque_or_unknown"
            break
    return {
        "installed": exe.is_file(),
        "platform": "windows",
        "version": versions[0] if versions else "unknown",
        "draft_format": draft_format,
        "actual_draft_export_supported": False,
        "reason": "The observed desktop draft payload is version-coupled and is not a portable public JSON contract.",
    }


def _runtime_clip(state: dict[str, Any], shot_id: str) -> str:
    runtime = ((state.get("shots") or {}).get(shot_id) or {}) if isinstance(state, dict) else {}
    video = runtime.get("video") if isinstance(runtime, dict) else {}
    if isinstance(video, dict) and video.get("status") == "completed" and str(video.get("path", "")).strip():
        return str(video["path"])
    return f"clips/{shot_id}.mp4"


def create_edit_plan(
    root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    *,
    backend: str = "auto",
    transition: str = "cut",
    transition_seconds: float = 0.0,
    filter_preset: str = "none",
    shot_filters: dict[str, str] | None = None,
    shot_speeds: dict[str, float] | None = None,
    boundary_transitions: dict[str, tuple[str, float]] | None = None,
    normalize_lufs: float | None = -16.0,
) -> dict[str, Any]:
    if backend not in EDIT_BACKENDS:
        raise SkillError("editing backend is unsupported")
    if transition not in TRANSITIONS:
        raise SkillError("editing transition is unsupported")
    if filter_preset not in FILTER_PRESETS:
        raise SkillError("editing filter preset is unsupported")
    shot_filters = dict(shot_filters or {})
    shot_speeds = dict(shot_speeds or {})
    boundary_transitions = dict(boundary_transitions or {})
    if any(value not in set(FILTER_PRESETS) - {"none"} for value in shot_filters.values()):
        raise SkillError("one or more per-shot filters are unsupported")
    if any(float(value) < 0.25 or float(value) > 4.0 for value in shot_speeds.values()):
        raise SkillError("per-shot speed must be from 0.25 to 4.0")
    if normalize_lufs is not None and (float(normalize_lufs) < -24 or float(normalize_lufs) > -10):
        raise SkillError("normalize_lufs must be from -24 to -10")
    if transition == "cut":
        transition_seconds = 0.0
    elif transition_seconds <= 0 or transition_seconds > 2.0:
        raise SkillError("non-cut transition seconds must be greater than zero and no more than 2")
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    inputs: list[dict[str, Any]] = []
    for shot in shots:
        seconds = float(shot.get("seconds", 6))
        edit_in = float(shot.get("edit_in") or 0.0)
        raw_edit_out = shot.get("edit_out")
        edit_out = float(raw_edit_out) if raw_edit_out not in (None, "") else None
        shot_id = str(shot.get("id", ""))
        inputs.append(
            {
                "id": shot_id,
                "path": _runtime_clip(state, shot_id),
                "edit_in": round(edit_in, 3),
                "edit_out": round(edit_out, 3) if edit_out is not None else None,
                "generation_seconds": round(seconds, 3),
                "speed": round(float(shot_speeds.get(shot_id, 1.0)), 4),
                "filters": [] if shot_id not in shot_filters else [shot_filters[shot_id]],
            }
        )
    known_ids = {str(item["id"]) for item in inputs}
    unknown_shots = (set(shot_filters) | set(shot_speeds)) - known_ids
    if unknown_shots:
        raise SkillError("per-shot edit override references unknown input: " + ", ".join(sorted(unknown_shots)))
    unknown_boundaries = set(boundary_transitions) - {str(item["id"]) for item in inputs[:-1]}
    if unknown_boundaries:
        raise SkillError("boundary transition references an unknown or final input: " + ", ".join(sorted(unknown_boundaries)))
    for after, (kind, seconds) in boundary_transitions.items():
        if kind not in TRANSITIONS:
            raise SkillError(f"boundary transition after {after} is unsupported")
        if (kind == "cut" and abs(float(seconds)) > 0.001) or (
            kind != "cut" and (float(seconds) <= 0 or float(seconds) > 2.0)
        ):
            raise SkillError(f"boundary transition duration after {after} is invalid")
    post = project.get("postproduction") if isinstance(project.get("postproduction"), dict) else {}
    plan = {
        "version": EDIT_PLAN_VERSION,
        "backend": {
            "requested": backend,
            "selected": "native" if backend == "auto" else backend,
            "selection_reason": (
                "Standalone CLI cannot discover task-scoped ChatCut MCP tools; native is the deterministic auto fallback."
                if backend == "auto"
                else "Explicit user selection."
            ),
        },
        "timeline": {
            "target_size": str((project.get("defaults") or {}).get("video_size") or "auto"),
            "fps": 30,
            "inputs": inputs,
            "transitions": [
                {
                    "after": inputs[index]["id"],
                    "type": boundary_transitions.get(inputs[index]["id"], (transition, transition_seconds))[0],
                    "duration": round(
                        float(boundary_transitions.get(inputs[index]["id"], (transition, transition_seconds))[1]), 3
                    ),
                    "reason": (
                        "explicit per-boundary edit-plan choice"
                        if inputs[index]["id"] in boundary_transitions
                        else "explicit global edit-plan choice"
                    ),
                }
                for index in range(max(0, len(inputs) - 1))
            ],
        },
        "filters": [] if filter_preset == "none" else [{"preset": filter_preset, "scope": "all"}],
        "overlays": [],
        "audio_mix": {
            "preserve_source": True,
            "music": str(post.get("music", "")).strip(),
            "voice": str(post.get("voice", "")).strip(),
            "normalize_lufs": float(normalize_lufs) if normalize_lufs is not None else None,
        },
        "subtitles": {
            "path": str(post.get("subtitles", "")).strip(),
            "style": "clean",
            "burn": bool(str(post.get("subtitles", "")).strip()),
        },
        "finishing": {"fade_seconds": float(post.get("fade_seconds") or 0.0)},
        "preview_evidence": {
            "enabled": True,
            "directory": "deliverables/edit-preview",
            "include_boundaries": True,
            "maximum_frames": 9,
        },
        "deliveries": {
            "clean_master": "deliverables/final.mp4",
            "edited_master": "deliverables/final-edited.mp4",
        },
    }
    errors = validate_edit_plan(root, plan, require_inputs=False)
    if errors:
        raise SkillError("invalid edit plan: " + "; ".join(errors))
    atomic_write_json(edit_plan_path(root), plan)
    return plan


def load_edit_plan(root: Path) -> dict[str, Any]:
    return migrate_edit_plan(read_json(edit_plan_path(root)))


def migrate_edit_plan(plan: dict[str, Any]) -> dict[str, Any]:
    version = plan.get("version")
    if version == EDIT_PLAN_VERSION:
        return copy.deepcopy(plan)
    if version != 1:
        raise SkillError(f"edit plan version {version} is unsupported")
    migrated = copy.deepcopy(plan)
    migrated["version"] = EDIT_PLAN_VERSION
    timeline = migrated.get("timeline") if isinstance(migrated.get("timeline"), dict) else {}
    for item in timeline.get("inputs", []):
        if isinstance(item, dict):
            item.setdefault("speed", 1.0)
            item.setdefault("filters", [])
    migrated.setdefault(
        "preview_evidence",
        {"enabled": True, "directory": "deliverables/edit-preview", "include_boundaries": True, "maximum_frames": 9},
    )
    migrated["migration"] = {
        "from_version": 1,
        "to_version": EDIT_PLAN_VERSION,
        "semantics": "global filters, speed=1, original boundary transitions, and -16 LUFS normalization preserved",
    }
    return migrated


def validate_edit_plan(root: Path, plan: dict[str, Any], *, require_inputs: bool) -> list[str]:
    errors: list[str] = []
    try:
        plan = migrate_edit_plan(plan)
    except SkillError as error:
        return [str(error)]
    backend = plan.get("backend") if isinstance(plan.get("backend"), dict) else {}
    if backend.get("requested") not in EDIT_BACKENDS or backend.get("selected") not in EDIT_BACKENDS - {"auto"}:
        errors.append("edit-plan.backend requested or selected value is unsupported")
    timeline = plan.get("timeline") if isinstance(plan.get("timeline"), dict) else {}
    inputs = timeline.get("inputs") if isinstance(timeline.get("inputs"), list) else []
    if not inputs:
        errors.append("edit-plan.timeline.inputs must be a non-empty array")
    seen: set[str] = set()
    durations: list[float | None] = []
    for index, item in enumerate(inputs):
        prefix = f"edit-plan.timeline.inputs[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            durations.append(None)
            continue
        identifier = str(item.get("id", "")).strip()
        if not identifier or identifier in seen:
            errors.append(f"{prefix}.id must be non-empty and unique")
        seen.add(identifier)
        input_path: Path | None = None
        try:
            input_path = _project_path(root, str(item.get("path", "")), must_exist=require_inputs)
        except SkillError as error:
            errors.append(str(error))
        try:
            edit_in = float(item.get("edit_in", 0))
            edit_out = item.get("edit_out")
            parsed_out = float(edit_out) if edit_out not in (None, "") else None
            if edit_in < 0 or (parsed_out is not None and parsed_out <= edit_in):
                raise ValueError
            speed = float(item.get("speed", 1.0))
            if speed < 0.25 or speed > 4.0:
                raise ValueError
            if parsed_out is None and require_inputs and input_path is not None:
                parsed_out = float(probe_media(input_path)["duration"])
                if parsed_out <= edit_in:
                    raise ValueError
            durations.append(None if parsed_out is None else (parsed_out - edit_in) / speed)
        except (TypeError, ValueError):
            errors.append(f"{prefix} must satisfy 0 <= edit_in < edit_out and speed must be from 0.25 to 4.0")
            durations.append(None)
        filters = item.get("filters") if isinstance(item.get("filters"), list) else None
        if filters is None or any(value not in set(FILTER_PRESETS) - {"none"} for value in filters):
            errors.append(f"{prefix}.filters must contain supported non-none presets")
    transitions = timeline.get("transitions") if isinstance(timeline.get("transitions"), list) else []
    if len(transitions) != max(0, len(inputs) - 1):
        errors.append("edit-plan.timeline.transitions must contain one entry per input boundary")
    for index, item in enumerate(transitions):
        prefix = f"edit-plan.timeline.transitions[{index}]"
        if not isinstance(item, dict) or item.get("type") not in TRANSITIONS:
            errors.append(f"{prefix}.type is unsupported")
            continue
        expected_after = str(inputs[index].get("id", "")) if index < len(inputs) and isinstance(inputs[index], dict) else ""
        if item.get("after") != expected_after:
            errors.append(f"{prefix}.after must equal {expected_after}")
        transition_type = str(item["type"])
        try:
            duration = float(item.get("duration", 0))
        except (TypeError, ValueError):
            errors.append(f"{prefix}.duration must be numeric")
            continue
        if transition_type == "cut" and abs(duration) > 0.001:
            errors.append(f"{prefix}.duration must be zero for cut")
        if transition_type != "cut" and (duration <= 0 or duration > 2.0):
            errors.append(f"{prefix}.duration must be greater than zero and no more than 2")
        neighboring = durations[index : index + 2]
        if transition_type != "cut" and all(value is not None for value in neighboring):
            if duration >= min(float(value) for value in neighboring):
                errors.append(f"{prefix}.duration must be shorter than both adjacent clips")
    raw_filters = plan.get("filters") if isinstance(plan.get("filters"), list) else []
    for index, item in enumerate(raw_filters):
        if not isinstance(item, dict) or item.get("preset") not in set(FILTER_PRESETS) - {"none"}:
            errors.append(f"edit-plan.filters[{index}].preset is unsupported")
        elif item.get("scope", "all") != "all":
            errors.append(f"edit-plan.filters[{index}].scope must be all; use timeline input filters for one shot")
    audio = plan.get("audio_mix") if isinstance(plan.get("audio_mix"), dict) else {}
    normalize_lufs = audio.get("normalize_lufs")
    try:
        if normalize_lufs is not None and (float(normalize_lufs) < -24 or float(normalize_lufs) > -10):
            raise ValueError
    except (TypeError, ValueError):
        errors.append("edit-plan.audio_mix.normalize_lufs must be null or from -24 to -10")
    preview = plan.get("preview_evidence") if isinstance(plan.get("preview_evidence"), dict) else {}
    if not isinstance(preview.get("enabled", True), bool) or not isinstance(preview.get("include_boundaries", True), bool):
        errors.append("edit-plan.preview_evidence flags must be booleans")
    try:
        maximum_frames = int(preview.get("maximum_frames", 9))
        if maximum_frames < 1 or maximum_frames > 9:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("edit-plan.preview_evidence.maximum_frames must be from 1 to 9")
    try:
        preview_path = _project_path(root, str(preview.get("directory", "deliverables/edit-preview")))
        if preview_path == root.resolve():
            errors.append("edit-plan.preview_evidence.directory must be a non-empty project subdirectory")
    except SkillError as error:
        errors.append(str(error))
    deliveries = plan.get("deliveries") if isinstance(plan.get("deliveries"), dict) else {}
    try:
        clean = _project_path(root, str(deliveries.get("clean_master", "")))
        edited = _project_path(root, str(deliveries.get("edited_master", "")))
        if clean == edited:
            errors.append("edited_master must not overwrite clean_master")
        if edited.suffix.lower() != ".mp4":
            errors.append("edited_master must end with .mp4")
    except SkillError as error:
        errors.append(str(error))
    for section, field in (("audio_mix", "music"), ("audio_mix", "voice"), ("subtitles", "path")):
        value = plan.get(section) if isinstance(plan.get(section), dict) else {}
        path_value = str(value.get(field, "")).strip()
        if path_value:
            try:
                _project_path(root, path_value, must_exist=require_inputs)
            except SkillError as error:
                errors.append(str(error))
    return errors


def _run(command: list[str], action: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise SkillError(f"ffmpeg failed while {action}: {detail}")


def _target_dimensions(plan: dict[str, Any], media: list[dict[str, Any]]) -> tuple[int, int]:
    value = str((plan.get("timeline") or {}).get("target_size") or "auto")
    if value == "auto":
        return int(media[0]["width"]), int(media[0]["height"])
    try:
        width, height = (int(item) for item in value.split("x", 1))
    except (TypeError, ValueError) as error:
        raise SkillError("edit target_size must be WIDTHxHEIGHT or auto") from error
    if width < 2 or height < 2 or width % 2 or height % 2:
        raise SkillError("edit target dimensions must be positive even integers")
    return width, height


def _atempo_chain(speed: float) -> str:
    values: list[float] = []
    remaining = float(speed)
    while remaining < 0.5 - 1e-9:
        values.append(0.5)
        remaining /= 0.5
    while remaining > 2.0 + 1e-9:
        values.append(2.0)
        remaining /= 2.0
    values.append(remaining)
    return ",".join(f"atempo={value:.6f}" for value in values)


def _filter_chain(plan: dict[str, Any], item: dict[str, Any], width: int, height: int) -> str:
    values = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
    ]
    for global_filter in plan.get("filters", []):
        preset = str(global_filter.get("preset", "")) if isinstance(global_filter, dict) else ""
        expression = FILTER_PRESETS.get(preset, "")
        if expression:
            values.append(expression)
    for preset in item.get("filters", []):
        expression = FILTER_PRESETS.get(str(preset), "")
        if expression:
            values.append(expression)
    speed = float(item.get("speed", 1.0))
    if abs(speed - 1.0) > 1e-9:
        values.append(f"setpts=PTS/{speed:.6f}")
    values.extend(["fps=30", "format=yuv420p"])
    return ",".join(values)


def _normalize_inputs(
    root: Path,
    plan: dict[str, Any],
    signature: str,
    state: dict[str, Any],
) -> tuple[list[Path], list[dict[str, Any]]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required for native editing")
    inputs = list((plan.get("timeline") or {}).get("inputs") or [])
    sources = [_project_path(root, str(item["path"]), must_exist=True) for item in inputs]
    probes = [probe_media(path) for path in sources]
    width, height = _target_dimensions(plan, probes)
    cache = root / ".gvs-edit-cache" / signature[:16]
    cache.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    segment_media: list[dict[str, Any]] = []
    for index, (item, source, probe) in enumerate(zip(inputs, sources, probes), 1):
        output = cache / f"segment-{index:03d}.mp4"
        edit_in = float(item.get("edit_in") or 0.0)
        edit_out_value = item.get("edit_out")
        edit_out = float(edit_out_value) if edit_out_value not in (None, "") else float(probe["duration"])
        if edit_out > float(probe["duration"]) + 0.15:
            raise SkillError(f"edit window for {item['id']} exceeds the source duration")
        source_duration = edit_out - edit_in
        speed = float(item.get("speed", 1.0))
        duration = source_duration / speed
        reusable = False
        if output.is_file():
            try:
                cached = probe_media(output)
                reusable = abs(float(cached["duration"]) - duration) <= max(0.12, duration * 0.03)
            except SkillError:
                reusable = False
        if not reusable:
            command = [ffmpeg, "-y", "-ss", f"{edit_in:.3f}", "-t", f"{source_duration:.3f}", "-i", str(source)]
            audio_index = 0
            if not probe["has_audio"]:
                command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
                audio_index = 1
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    f"{audio_index}:a:0",
                    "-vf",
                    _filter_chain(plan, item, width, height),
                    "-af",
                    f"{_atempo_chain(speed)},apad",
                    "-t",
                    f"{duration:.3f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
            _run(command, f"normalizing edit input {index}")
        current = probe_media(output)
        segments.append(output)
        segment_media.append(current)
        state.setdefault("stages", {})[str(item["id"])] = {
            "status": "completed",
            "path": str(output.relative_to(root).as_posix()),
            "duration": current["duration"],
            "speed": speed,
            "filters": list(item.get("filters", [])),
            "updated_at": int(time.time()),
        }
        atomic_write_json(edit_state_path(root), state)
    return segments, segment_media


def _assemble_timeline(
    segments: list[Path],
    segment_media: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    output: Path,
) -> float:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required for native editing")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not transitions or all(item.get("type") == "cut" for item in transitions):
        with tempfile.TemporaryDirectory(prefix=".gvs-edit-concat-", dir=str(output.parent)) as temp_name:
            concat = Path(temp_name) / "concat.txt"
            concat.write_text(
                "\n".join(f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in segments) + "\n",
                encoding="utf-8",
            )
            _run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)],
                "assembling hard-cut timeline",
            )
        return sum(float(item["duration"]) for item in segment_media)

    if any(item.get("type") == "cut" for item in transitions):
        groups: list[list[Path]] = [[segments[0]]]
        group_media_items: list[list[dict[str, Any]]] = [[segment_media[0]]]
        group_transitions: list[dict[str, Any]] = []
        for index, transition in enumerate(transitions, 1):
            if transition.get("type") == "cut":
                groups[-1].append(segments[index])
                group_media_items[-1].append(segment_media[index])
            else:
                group_transitions.append(transition)
                groups.append([segments[index]])
                group_media_items.append([segment_media[index]])
        with tempfile.TemporaryDirectory(prefix=".gvs-edit-groups-", dir=str(output.parent)) as temp_name:
            grouped_segments: list[Path] = []
            grouped_media: list[dict[str, Any]] = []
            for index, (group, media_items) in enumerate(zip(groups, group_media_items), 1):
                if len(group) == 1:
                    grouped_segments.append(group[0])
                    grouped_media.append(media_items[0])
                    continue
                grouped = Path(temp_name) / f"group-{index:03d}.mp4"
                _assemble_timeline(
                    group,
                    media_items,
                    [{"type": "cut", "duration": 0.0} for _ in range(len(group) - 1)],
                    grouped,
                )
                grouped_segments.append(grouped)
                grouped_media.append(probe_media(grouped))
            return _assemble_timeline(grouped_segments, grouped_media, group_transitions, output)

    command = [ffmpeg, "-y"]
    for segment in segments:
        command.extend(["-i", str(segment)])
    filters: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"
    accumulated = float(segment_media[0]["duration"])
    for index, transition in enumerate(transitions, 1):
        duration = float(transition["duration"])
        next_video = f"v{index}"
        next_audio = f"a{index}"
        if transition.get("type") == "cut":
            filters.append(
                f"[{video_label}][{audio_label}][{index}:v][{index}:a]concat=n=2:v=1:a=1[{next_video}][{next_audio}]"
            )
            accumulated += float(segment_media[index]["duration"])
        else:
            offset = accumulated - duration
            filters.append(
                f"[{video_label}][{index}:v]xfade=transition={TRANSITIONS[str(transition['type'])]}:duration={duration:.3f}:offset={offset:.3f}[{next_video}]"
            )
            filters.append(f"[{audio_label}][{index}:a]acrossfade=d={duration:.3f}:c1=tri:c2=tri[{next_audio}]")
            accumulated += float(segment_media[index]["duration"]) - duration
        video_label = next_video
        audio_label = next_audio
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{video_label}]",
            "-map",
            f"[{audio_label}]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run(command, "rendering transition timeline")
    return accumulated


def _input_signature(root: Path, plan: dict[str, Any]) -> str:
    plan = migrate_edit_plan(plan)
    inputs = list((plan.get("timeline") or {}).get("inputs") or [])
    material = {"plan": plan, "inputs": []}
    for item in inputs:
        path = _project_path(root, str(item["path"]), must_exist=True)
        material["inputs"].append({"path": str(item["path"]), "bytes": path.stat().st_size, "sha256": _digest(path)})
    return _json_digest(material)


def _preview_times(
    segment_media: list[dict[str, Any]], transitions: list[dict[str, Any]], *, maximum_frames: int
) -> list[float]:
    if not segment_media:
        return []
    accumulated = float(segment_media[0]["duration"])
    boundaries: list[float] = []
    for index, transition in enumerate(transitions, 1):
        duration = float(transition.get("duration", 0.0))
        boundaries.append(accumulated - duration / 2.0)
        accumulated += float(segment_media[index]["duration"]) - duration
    values = [min(0.05, max(0.0, accumulated / 2.0)), *boundaries, max(0.0, accumulated - 0.05)]
    deduplicated: list[float] = []
    for value in values:
        if not deduplicated or abs(value - deduplicated[-1]) > 0.02:
            deduplicated.append(value)
    if len(deduplicated) <= maximum_frames:
        return deduplicated
    if maximum_frames == 1:
        return [deduplicated[len(deduplicated) // 2]]
    selected = [round(index * (len(deduplicated) - 1) / (maximum_frames - 1)) for index in range(maximum_frames)]
    return [deduplicated[index] for index in selected]


def render_native_edit(root: Path, plan: dict[str, Any], *, resume: bool = True) -> dict[str, Any]:
    root = root.resolve()
    plan = migrate_edit_plan(plan)
    errors = validate_edit_plan(root, plan, require_inputs=True)
    if errors:
        raise SkillError("invalid edit plan: " + "; ".join(errors))
    backend = str((plan.get("backend") or {}).get("selected", ""))
    if backend != "native":
        raise SkillError(f"edit plan selected {backend}; native renderer only accepts backend=native")
    with project_state_lock(root):
        signature = _input_signature(root, plan)
        output = _project_path(root, str((plan.get("deliveries") or {})["edited_master"]))
        previous = read_json(edit_state_path(root)) if edit_state_path(root).is_file() else {}
        if (
            resume
            and previous.get("status") == "completed"
            and previous.get("signature") == signature
            and output.is_file()
            and previous.get("output", {}).get("sha256") == _digest(output)
        ):
            return {
                "status": "completed",
                "resumed": True,
                **previous.get("output", {}),
                "qa": previous.get("qa", {}),
                "preview_evidence": previous.get("preview_evidence", []),
            }
        state: dict[str, Any] = {
            "version": EDIT_STATE_VERSION,
            "status": "rendering",
            "signature": signature,
            "started_at": int(time.time()),
            "stages": previous.get("stages", {}) if previous.get("signature") == signature else {},
            "output": {},
        }
        atomic_write_json(edit_state_path(root), state)
        try:
            segments, media = _normalize_inputs(root, plan, signature, state)
            output.parent.mkdir(parents=True, exist_ok=True)
            timeline_output = output.with_name(output.stem + ".timeline.mp4")
            expected_duration = _assemble_timeline(
                segments,
                media,
                list((plan.get("timeline") or {}).get("transitions") or []),
                timeline_output,
            )
            audio = plan.get("audio_mix") if isinstance(plan.get("audio_mix"), dict) else {}
            subtitles = plan.get("subtitles") if isinstance(plan.get("subtitles"), dict) else {}
            finishing = plan.get("finishing") if isinstance(plan.get("finishing"), dict) else {}
            normalize_lufs = audio.get("normalize_lufs")
            music_value = str(audio.get("music", "")).strip()
            voice_value = str(audio.get("voice", "")).strip()
            subtitle_value = str(subtitles.get("path", "")).strip() if subtitles.get("burn") else ""
            needs_finish = bool(
                music_value
                or voice_value
                or subtitle_value
                or float(finishing.get("fade_seconds") or 0.0)
                or normalize_lufs is not None
            )
            if needs_finish:
                postprocess_video(
                    timeline_output,
                    output,
                    music=_project_path(root, music_value, must_exist=True) if music_value else None,
                    voice=_project_path(root, voice_value, must_exist=True) if voice_value else None,
                    subtitles=_project_path(root, subtitle_value, must_exist=True) if subtitle_value else None,
                    subtitle_style=str(subtitles.get("style", "clean")),
                    fade_seconds=float(finishing.get("fade_seconds") or 0.0),
                    normalize_lufs=float(normalize_lufs) if normalize_lufs is not None else None,
                    normalize_audio=normalize_lufs is not None,
                )
                timeline_output.unlink(missing_ok=True)
            else:
                timeline_output.replace(output)
            probe = probe_media(output)
            expected_size = str((plan.get("timeline") or {}).get("target_size") or "auto")
            qa = quality_report(output, expected_size=expected_size, expected_duration=expected_duration)
            preview_config = plan.get("preview_evidence") if isinstance(plan.get("preview_evidence"), dict) else {}
            preview_frames: list[dict[str, Any]] = []
            if preview_config.get("enabled", True):
                preview_root = _project_path(root, str(preview_config.get("directory", "deliverables/edit-preview")))
                maximum_frames = int(preview_config.get("maximum_frames", 9))
                transitions = list((plan.get("timeline") or {}).get("transitions") or [])
                if preview_config.get("include_boundaries", True):
                    times = _preview_times(media, transitions, maximum_frames=maximum_frames)
                    exported = export_review_frames(output, preview_root, stem="final-edited", at_seconds=times)
                else:
                    exported = export_review_frames(output, preview_root, stem="final-edited", count=min(5, maximum_frames))
                preview_frames = [
                    {
                        **frame,
                        "path": Path(str(frame["path"])).relative_to(root).as_posix(),
                        "sha256": _digest(Path(str(frame["path"]))),
                    }
                    for frame in exported
                ]
            state.update(
                {
                    "status": "completed",
                    "completed_at": int(time.time()),
                    "output": {
                        "path": str(output.relative_to(root).as_posix()),
                        "bytes": output.stat().st_size,
                        "sha256": _digest(output),
                        "media": probe,
                    },
                    "qa": qa,
                    "preview_evidence": preview_frames,
                }
            )
            atomic_write_json(edit_state_path(root), state)
            return {
                "status": "completed",
                "resumed": False,
                **state["output"],
                "qa": qa,
                "preview_evidence": preview_frames,
            }
        except Exception as error:
            state.update({"status": "failed", "failed_at": int(time.time()), "error": str(error)})
            atomic_write_json(edit_state_path(root), state)
            raise


def export_edit_handoff(root: Path, plan: dict[str, Any], *, backend: str) -> dict[str, Any]:
    root = root.resolve()
    plan = migrate_edit_plan(plan)
    if backend not in {"chatcut", "jianying-draft"}:
        raise SkillError("handoff backend must be chatcut or jianying-draft")
    errors = validate_edit_plan(root, plan, require_inputs=True)
    if errors:
        raise SkillError("invalid edit plan: " + "; ".join(errors))
    handoff_plan = copy.deepcopy(plan)
    handoff_plan["backend"] = {
        "requested": backend,
        "selected": backend,
        "selection_reason": "Explicit edit-handoff export selection.",
    }
    inputs = list((plan.get("timeline") or {}).get("inputs") or [])
    manifest = []
    for item in inputs:
        source = _project_path(root, str(item["path"]), must_exist=True)
        manifest.append(
            {
                "id": str(item["id"]),
                "project_path": str(item["path"]),
                "bytes": source.stat().st_size,
                "sha256": _digest(source),
                "media": probe_media(source),
                "edit_in": item.get("edit_in", 0.0),
                "edit_out": item.get("edit_out"),
            }
        )
    if backend == "chatcut":
        installation = chatcut_capability_report()
        # Hash the project plan as stored on disk. The backend-specific copy is
        # only an execution view; receipts must still bind to the user's plan.
        contract = build_chatcut_contract(plan, manifest, installation=installation)
        packet = {
            "version": 1,
            "backend": "chatcut",
            "status": "handoff_ready",
            "capability_gate": {
                "required": "mcp__chatcut__ tools loaded and authorized in the current Codex task",
                "verified_by_cli": False,
                "plugin_installed": bool(installation["plugin"]["installed"]),
                "mcp_configured": bool(installation["mcp"]["configured"]),
                "task_tools_visible": bool(installation["runtime"]["task_tools_visible"]),
                "reason": "MCP tools are task-scoped and cannot be enumerated by this standalone process.",
            },
            "project_relative_root": ".",
            "edit_plan": handoff_plan,
            "media_manifest": manifest,
            "required_receipt": [
                "remote_project_id",
                "remote_timeline_id",
                "rendered_asset",
                "output_sha256",
                "source_plan_sha256",
                "unmapped_features",
                "verification",
                "tool_trace",
            ],
            "adapter": {
                "name": "grok-video-studio-chatcut",
                "version": contract["adapter_version"],
                "source_plan_sha256": contract["source_plan_sha256"],
            },
            "installation": installation,
            "integration_contract": contract,
        }
        output = root / "deliverables" / "chatcut-handoff.json"
        atomic_write_json(output, packet)
        return {"backend": backend, "status": "handoff_ready", "path": str(output), "packet": packet}

    bundle = root / "deliverables" / "jianying-handoff"
    media_root = bundle / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    relink = []
    bundled_manifest = []
    for index, (item, entry) in enumerate(zip(inputs, manifest), 1):
        source = _project_path(root, str(item["path"]), must_exist=True)
        target = media_root / f"{index:03d}-{str(item['id'])}{source.suffix.lower()}"
        shutil.copy2(source, target)
        bundle_path = target.relative_to(bundle).as_posix()
        bundled_manifest.append({**entry, "bundle_path": bundle_path})
        relink.append({"project_path": str(item["path"]), "bundle_path": bundle_path})
    compatibility = {
        "version": 1,
        "backend": "jianying-draft",
        "status": "experimental_handoff_only",
        "actual_draft": False,
        "portable_import_guaranteed": False,
        "environment": detect_jianying_environment(),
        "unsupported": [
            "draft_content.json generation",
            "draft_meta_info.json generation",
            "Jianying material/effect identifiers",
            "automatic installation into a user's draft directory",
        ],
        "reason": "Observed Jianying 11.2 draft payloads are opaque Base64-like data rather than a stable public JSON schema.",
        "next": "Import the bundled media manually or use the native MP4 delivery; enable a real exporter only after version-specific reversible fixture tests.",
    }
    atomic_write_json(bundle / "edit-plan.json", handoff_plan)
    atomic_write_json(bundle / "media-manifest.json", {"version": 1, "media": bundled_manifest})
    atomic_write_json(bundle / "relink-map.json", {"version": 1, "paths": relink})
    atomic_write_json(bundle / "compatibility.json", compatibility)
    return {
        "backend": backend,
        "status": "experimental_handoff_only",
        "path": str(bundle),
        "actual_draft": False,
        "compatibility": compatibility,
        "media_count": len(bundled_manifest),
    }
