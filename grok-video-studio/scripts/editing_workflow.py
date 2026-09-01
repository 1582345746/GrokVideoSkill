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
from media_tools import postprocess_video, probe_media, quality_report


EDIT_PLAN_VERSION = 1
EDIT_STATE_VERSION = 1
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
            "resumable": True,
            "output": "deliverables/final-edited.mp4",
            "preserves_clean_master": True,
        },
        "chatcut": {
            "available": "task-tool-discovery-required",
            "detection": "Use ChatCut only when mcp__chatcut__ tools are loaded and authorized in the current Codex task.",
            "standalone_cli_can_detect": False,
        },
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
) -> dict[str, Any]:
    if backend not in EDIT_BACKENDS:
        raise SkillError("editing backend is unsupported")
    if transition not in TRANSITIONS:
        raise SkillError("editing transition is unsupported")
    if filter_preset not in FILTER_PRESETS:
        raise SkillError("editing filter preset is unsupported")
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
        inputs.append(
            {
                "id": str(shot.get("id", "")),
                "path": _runtime_clip(state, str(shot.get("id", ""))),
                "edit_in": round(edit_in, 3),
                "edit_out": round(edit_out, 3) if edit_out is not None else None,
                "generation_seconds": round(seconds, 3),
            }
        )
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
                    "type": transition,
                    "duration": round(float(transition_seconds), 3),
                    "reason": "explicit global edit-plan choice",
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
            "normalize_lufs": -16,
        },
        "subtitles": {
            "path": str(post.get("subtitles", "")).strip(),
            "style": "clean",
            "burn": bool(str(post.get("subtitles", "")).strip()),
        },
        "finishing": {"fade_seconds": float(post.get("fade_seconds") or 0.0)},
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
    return read_json(edit_plan_path(root))


def validate_edit_plan(root: Path, plan: dict[str, Any], *, require_inputs: bool) -> list[str]:
    errors: list[str] = []
    if plan.get("version") != EDIT_PLAN_VERSION:
        errors.append(f"edit-plan.version must be {EDIT_PLAN_VERSION}")
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
        try:
            _project_path(root, str(item.get("path", "")), must_exist=require_inputs)
        except SkillError as error:
            errors.append(str(error))
        try:
            edit_in = float(item.get("edit_in", 0))
            edit_out = item.get("edit_out")
            parsed_out = float(edit_out) if edit_out not in (None, "") else None
            if edit_in < 0 or (parsed_out is not None and parsed_out <= edit_in):
                raise ValueError
            durations.append(None if parsed_out is None else parsed_out - edit_in)
        except (TypeError, ValueError):
            errors.append(f"{prefix} must satisfy 0 <= edit_in < edit_out")
            durations.append(None)
    transitions = timeline.get("transitions") if isinstance(timeline.get("transitions"), list) else []
    if len(transitions) != max(0, len(inputs) - 1):
        errors.append("edit-plan.timeline.transitions must contain one entry per input boundary")
    transition_types: set[str] = set()
    for index, item in enumerate(transitions):
        prefix = f"edit-plan.timeline.transitions[{index}]"
        if not isinstance(item, dict) or item.get("type") not in TRANSITIONS:
            errors.append(f"{prefix}.type is unsupported")
            continue
        transition_type = str(item["type"])
        transition_types.add(transition_type)
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
    if "cut" in transition_types and len(transition_types) > 1:
        errors.append("native edit v1 cannot mix cut and xfade boundaries in one timeline")
    raw_filters = plan.get("filters") if isinstance(plan.get("filters"), list) else []
    for index, item in enumerate(raw_filters):
        if not isinstance(item, dict) or item.get("preset") not in set(FILTER_PRESETS) - {"none"}:
            errors.append(f"edit-plan.filters[{index}].preset is unsupported")
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


def _filter_chain(plan: dict[str, Any], width: int, height: int) -> str:
    values = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "fps=30",
        "format=yuv420p",
    ]
    for item in plan.get("filters", []):
        preset = str(item.get("preset", "")) if isinstance(item, dict) else ""
        expression = FILTER_PRESETS.get(preset, "")
        if expression:
            values.append(expression)
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
        duration = edit_out - edit_in
        reusable = False
        if output.is_file():
            try:
                cached = probe_media(output)
                reusable = abs(float(cached["duration"]) - duration) <= max(0.12, duration * 0.03)
            except SkillError:
                reusable = False
        if not reusable:
            command = [ffmpeg, "-y", "-ss", f"{edit_in:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
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
                    _filter_chain(plan, width, height),
                    "-af",
                    "apad",
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

    command = [ffmpeg, "-y"]
    for segment in segments:
        command.extend(["-i", str(segment)])
    filters: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"
    accumulated = float(segment_media[0]["duration"])
    for index, transition in enumerate(transitions, 1):
        duration = float(transition["duration"])
        offset = accumulated - duration
        next_video = f"v{index}"
        next_audio = f"a{index}"
        filters.append(
            f"[{video_label}][{index}:v]xfade=transition={TRANSITIONS[str(transition['type'])]}:duration={duration:.3f}:offset={offset:.3f}[{next_video}]"
        )
        filters.append(f"[{audio_label}][{index}:a]acrossfade=d={duration:.3f}:c1=tri:c2=tri[{next_audio}]")
        video_label = next_video
        audio_label = next_audio
        accumulated += float(segment_media[index]["duration"]) - duration
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
    inputs = list((plan.get("timeline") or {}).get("inputs") or [])
    material = {"plan": plan, "inputs": []}
    for item in inputs:
        path = _project_path(root, str(item["path"]), must_exist=True)
        material["inputs"].append({"path": str(item["path"]), "bytes": path.stat().st_size, "sha256": _digest(path)})
    return _json_digest(material)


def render_native_edit(root: Path, plan: dict[str, Any], *, resume: bool = True) -> dict[str, Any]:
    root = root.resolve()
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
            return {"status": "completed", "resumed": True, **previous.get("output", {}), "qa": previous.get("qa", {})}
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
            music_value = str(audio.get("music", "")).strip()
            voice_value = str(audio.get("voice", "")).strip()
            subtitle_value = str(subtitles.get("path", "")).strip() if subtitles.get("burn") else ""
            needs_finish = bool(music_value or voice_value or subtitle_value or float(finishing.get("fade_seconds") or 0.0))
            if needs_finish:
                postprocess_video(
                    timeline_output,
                    output,
                    music=_project_path(root, music_value, must_exist=True) if music_value else None,
                    voice=_project_path(root, voice_value, must_exist=True) if voice_value else None,
                    subtitles=_project_path(root, subtitle_value, must_exist=True) if subtitle_value else None,
                    subtitle_style=str(subtitles.get("style", "clean")),
                    fade_seconds=float(finishing.get("fade_seconds") or 0.0),
                )
                timeline_output.unlink(missing_ok=True)
            else:
                timeline_output.replace(output)
            probe = probe_media(output)
            expected_size = str((plan.get("timeline") or {}).get("target_size") or "auto")
            qa = quality_report(output, expected_size=expected_size, expected_duration=expected_duration)
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
                }
            )
            atomic_write_json(edit_state_path(root), state)
            return {"status": "completed", "resumed": False, **state["output"], "qa": qa}
        except Exception as error:
            state.update({"status": "failed", "failed_at": int(time.time()), "error": str(error)})
            atomic_write_json(edit_state_path(root), state)
            raise


def export_edit_handoff(root: Path, plan: dict[str, Any], *, backend: str) -> dict[str, Any]:
    root = root.resolve()
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
        packet = {
            "version": 1,
            "backend": "chatcut",
            "status": "handoff_ready",
            "capability_gate": {
                "required": "mcp__chatcut__ tools loaded and authorized in the current Codex task",
                "verified_by_cli": False,
                "reason": "MCP tools are task-scoped and cannot be enumerated by this standalone process.",
            },
            "project_relative_root": ".",
            "edit_plan": handoff_plan,
            "media_manifest": manifest,
            "required_receipt": ["remote_project_id", "rendered_asset", "output_sha256", "unmapped_features"],
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
