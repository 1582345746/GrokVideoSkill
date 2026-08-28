#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from gvs_common import (
    APIError,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_QUICKAI_URL,
    DEFAULT_QUICKAINEW_URL,
    DEFAULT_VIDEO_MODEL,
    SkillError,
    assert_mp4,
    atomic_write_json,
    config_path,
    load_settings,
    normalize_base_url,
    print_json,
    save_settings,
)
from media_client import QuickAIImageClient, QuickAINewVideoClient, QuickAIVideoClient, VIDEO_RESOLUTIONS, save_image_bytes
from media_tools import export_review_frames, extract_cover, postprocess_video, quality_report
from provider_contracts import task_progress


SKILL_VERSION = "1.4.0"
PROJECT_VERSION = 1
STATE_VERSION = 1
MAX_VIDEO_SECONDS = 15
HARD_PROMPT_CHARS = 4096
SAFE_PROMPT_CHARS = 3800
MAX_CREDENTIAL_PAYLOAD_CHARS = 32768
SHOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SIZE_RE = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")
ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "2:3", "3:2"}
VIDEO_AUDIO_POLICIES = {"preserve", "mute"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TERMINAL_FAILURES = {"failed", "submission_unknown"}
SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = SKILL_ROOT / "assets" / "workflow-templates"


def project_file(root: Path) -> Path:
    return root / "project.json"


def state_file(root: Path) -> Path:
    return root / "state.json"


def load_workflows() -> dict[str, dict[str, Any]]:
    workflows: dict[str, dict[str, Any]] = {}
    for path in sorted(WORKFLOW_ROOT.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SkillError(f"invalid workflow template {path.name} at line {error.lineno}") from error
        if not isinstance(value, dict):
            raise SkillError(f"workflow template root must be an object: {path.name}")
        workflow_id = str(value.get("id", "")).strip()
        title = str(value.get("title", "")).strip()
        if not SHOT_ID_RE.fullmatch(workflow_id) or not title:
            raise SkillError(f"workflow template requires a valid id and title: {path.name}")
        if workflow_id in workflows:
            raise SkillError(f"duplicate workflow id: {workflow_id}")
        workflows[workflow_id] = value
    if not workflows:
        raise SkillError(f"no workflow templates found: {WORKFLOW_ROOT}")
    return workflows


def workflow_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": workflow_id,
            "title": workflow["title"],
            "summary": workflow.get("summary", ""),
            "character_master": bool(workflow.get("character_master", False)),
            "preferred_clip_seconds": int(workflow.get("preferred_clip_seconds", 6)),
        }
        for workflow_id, workflow in load_workflows().items()
    ]


def get_workflow(workflow_id: str) -> dict[str, Any]:
    workflows = load_workflows()
    try:
        return workflows[workflow_id]
    except KeyError as error:
        raise SkillError(f"unknown workflow '{workflow_id}'; run capabilities to list available workflows") from error


def load_project(root: Path) -> dict[str, Any]:
    path = project_file(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SkillError(f"project does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise SkillError(f"invalid project JSON at line {error.lineno}") from error
    if not isinstance(value, dict):
        raise SkillError("project JSON root must be an object")
    return value


def fresh_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": int(time.time()),
        "character_master": {"status": "pending", "attempts": 0},
        "shots": {},
        "deliverables": {},
        "budget_usage": {"image_attempts": 0, "video_attempts": 0, "estimated_cost": 0.0},
    }


def load_state(root: Path) -> dict[str, Any]:
    path = state_file(root)
    if not path.exists():
        return fresh_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SkillError(f"invalid state JSON at line {error.lineno}") from error
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION or not isinstance(value.get("shots"), dict):
        raise SkillError("state file has an unsupported format")
    value.setdefault("character_master", {"status": "pending", "attempts": 0})
    value.setdefault("deliverables", {})
    value.setdefault("budget_usage", {"image_attempts": 0, "video_attempts": 0, "estimated_cost": 0.0})
    return value


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = int(time.time())
    atomic_write_json(state_file(root), state)


def resolve_project_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise SkillError(f"project paths must be relative: {value}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise SkillError(f"project path escapes the project: {value}") from error
    if must_exist and not resolved.is_file():
        raise SkillError(f"reference file does not exist: {value}")
    return resolved


def _known_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"api_key", "quickai_key", "quickainew_key", "authorization", "secret"}:
                return True
            if _known_secret_field(child):
                return True
    if isinstance(value, list):
        return any(_known_secret_field(item) for item in value)
    return False


def shot_value(project: dict[str, Any], shot: dict[str, Any], name: str, default: Any) -> Any:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    default_name = "video_seconds" if name == "seconds" else name
    return shot.get(name, defaults.get(default_name, default))


def video_mode(project: dict[str, Any]) -> str:
    value = str(project.get("video_mode", "")).strip()
    if value:
        return value
    return "text-to-video" if project.get("workflow") == "text-to-video" else "image-to-video"


def video_provider(project: dict[str, Any], settings: dict[str, Any] | None = None) -> str:
    value = str(project.get("video_provider", "")).strip()
    if value:
        return value
    return str((settings or {}).get("default_video_provider", "quickainew" if video_mode(project) == "image-to-video" else "quickai"))


def video_resolution(project: dict[str, Any], shot: dict[str, Any]) -> str:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    return str(shot.get("video_resolution", defaults.get("video_resolution", "480p"))).strip()


def video_aspect_ratio(project: dict[str, Any], shot: dict[str, Any]) -> str:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    return str(shot.get("video_aspect_ratio", defaults.get("video_aspect_ratio", "16:9"))).strip()


def allow_ui_elements(project: dict[str, Any], shot: dict[str, Any] | None = None) -> bool:
    if shot is not None and "allow_ui_elements" in shot:
        return bool(shot.get("allow_ui_elements"))
    return bool(project.get("allow_ui_elements", False))


def audio_policy(project: dict[str, Any]) -> str:
    defaults = project.get("defaults") if isinstance(project.get("defaults"), dict) else {}
    return str(defaults.get("audio_policy", "preserve")).strip().lower()


def character_master_config(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("character_master")
    return value if isinstance(value, dict) else {}


def project_characters(project: dict[str, Any]) -> list[dict[str, Any]]:
    value = project.get("characters", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def budget_config(project: dict[str, Any]) -> dict[str, Any]:
    value = project.get("budget")
    return value if isinstance(value, dict) else {}


def _budget_number(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if allow_none and value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise SkillError(f"budget.{name} must be a non-negative number") from error
    if result < 0:
        raise SkillError(f"budget.{name} must be a non-negative number")
    return result


def estimated_project_cost(project: dict[str, Any], *, image_requests: int, video_requests: int) -> dict[str, Any]:
    budget = budget_config(project)
    image_rate = _budget_number(budget.get("image_request", 0), "image_request") or 0.0
    video_rate = _budget_number(budget.get("video_request", 0), "video_request") or 0.0
    maximum = _budget_number(budget.get("max_estimated_cost"), "max_estimated_cost", allow_none=True)
    estimated = round(image_requests * image_rate + video_requests * video_rate, 6)
    return {
        "currency": str(budget.get("currency") or "CNY"),
        "image_request": image_rate,
        "video_request": video_rate,
        "max_estimated_cost": maximum,
        "estimated_cost": estimated,
        "within_budget": maximum is None or estimated <= maximum,
    }


def record_budget_attempt(state: dict[str, Any], project: dict[str, Any], kind: str) -> None:
    if kind not in {"image", "video"}:
        raise SkillError(f"unsupported budget attempt kind: {kind}")
    budget = budget_config(project)
    rate = _budget_number(budget.get(f"{kind}_request", 0), f"{kind}_request") or 0.0
    maximum = _budget_number(budget.get("max_estimated_cost"), "max_estimated_cost", allow_none=True)
    usage = state.setdefault("budget_usage", {"image_attempts": 0, "video_attempts": 0, "estimated_cost": 0.0})
    next_cost = round(float(usage.get("estimated_cost", 0)) + rate, 6)
    if maximum is not None and next_cost > maximum:
        raise SkillError(
            f"budget gate blocked the next {kind} request: estimated attempted cost {next_cost} exceeds {maximum} {budget.get('currency', 'CNY')}"
        )
    usage[f"{kind}_attempts"] = int(usage.get(f"{kind}_attempts", 0)) + 1
    usage["estimated_cost"] = next_cost
    usage["currency"] = str(budget.get("currency") or "CNY")


def require_retry_reason(retry_failed: bool, retry_reason: str) -> str:
    reason = retry_reason.strip()
    if retry_failed and not reason:
        raise SkillError("--retry-reason is required with --retry-failed to preserve a duplicate-billing audit trail")
    return reason


def archive_runtime_attempt(runtime: dict[str, Any], reason: str) -> None:
    snapshot = {
        "at": int(time.time()),
        "reason": reason,
        "status": runtime.get("status", ""),
        "attempts": runtime.get("attempts", 0),
        "task_id": runtime.get("task_id", ""),
        "path": runtime.get("path", ""),
        "error": runtime.get("error", ""),
    }
    history = runtime.setdefault("history", [])
    if isinstance(history, list):
        history.append(snapshot)
        del history[:-20]


def emit_progress(enabled: bool, **event: Any) -> None:
    if enabled:
        print(json.dumps({"gvs_progress": True, "at": int(time.time()), **event}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)


def portable_qa(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "path"}


def _positive_limit(limits: dict[str, Any], name: str, fallback: int, errors: list[str]) -> int:
    try:
        value = int(limits.get(name, fallback))
    except (TypeError, ValueError):
        errors.append(f"limits.{name} must be a positive integer")
        return fallback
    if value < 1:
        errors.append(f"limits.{name} must be a positive integer")
        return fallback
    return value


def _reference_errors(root: Path, references: Any, prefix: str, max_references: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(references, list) or not all(isinstance(item, str) and item.strip() for item in references):
        return [f"{prefix} must be an array of relative paths"]
    if len(references) > max_references:
        errors.append(f"{prefix} exceeds max_reference_images {max_references}")
    for reference in references:
        try:
            path = resolve_project_path(root, reference)
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                errors.append(f"{prefix} has unsupported image type: {reference}")
        except SkillError as error:
            errors.append(str(error))
    return errors


def validate_project(root: Path, project: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if project.get("version") != PROJECT_VERSION:
        errors.append("project.version must be 1")
    for field in ("title", "topic", "story"):
        if not str(project.get(field, "")).strip():
            errors.append(f"project.{field} is required")
    if _known_secret_field(project):
        errors.append("project contains a credential-like field; credentials must stay outside the project")
    mode = video_mode(project)
    provider = str(project.get("video_provider", "")).strip()
    if mode not in {"text-to-video", "image-to-video"}:
        errors.append("project.video_mode must be text-to-video or image-to-video")
    if provider and provider not in {"quickai", "quickainew"}:
        errors.append("project.video_provider must be quickai or quickainew")
    if project.get("allow_ui_elements") is not None and not isinstance(project.get("allow_ui_elements"), bool):
        errors.append("project.allow_ui_elements must be a boolean")
    if audio_policy(project) not in VIDEO_AUDIO_POLICIES:
        errors.append("defaults.audio_policy must be preserve or mute")
    shots = project.get("shots")
    if not isinstance(shots, list) or not shots:
        errors.append("project.shots must be a non-empty array")
        return errors
    limits = project.get("limits") if isinstance(project.get("limits"), dict) else {}
    max_images = _positive_limit(limits, "max_image_requests", 12, errors)
    max_videos = _positive_limit(limits, "max_video_requests", 8, errors)
    max_seconds = _positive_limit(limits, "max_total_video_seconds", 60, errors)
    max_references = _positive_limit(limits, "max_reference_images", 9, errors)
    max_prompt_chars = _positive_limit(limits, "max_prompt_chars", HARD_PROMPT_CHARS, errors)
    if max_prompt_chars > HARD_PROMPT_CHARS:
        errors.append(f"limits.max_prompt_chars cannot exceed provider limit {HARD_PROMPT_CHARS}")
        max_prompt_chars = HARD_PROMPT_CHARS

    raw_characters = project.get("characters", [])
    character_ids: set[str] = set()
    if not isinstance(raw_characters, list):
        errors.append("project.characters must be an array")
        raw_characters = []
    for index, character in enumerate(raw_characters):
        prefix = f"characters[{index}]"
        if not isinstance(character, dict):
            errors.append(f"{prefix} must be an object")
            continue
        character_id = str(character.get("id", "")).strip()
        if not SHOT_ID_RE.fullmatch(character_id):
            errors.append(f"{prefix}.id must use lowercase letters, digits, and hyphens")
        elif character_id in character_ids:
            errors.append(f"duplicate character id: {character_id}")
        character_ids.add(character_id)
        if not str(character.get("name", "")).strip():
            errors.append(f"{prefix}.name is required")
        if not str(character.get("identity", "")).strip():
            errors.append(f"{prefix}.identity is required")
        errors.extend(_reference_errors(root, character.get("references", []), f"{prefix}.references", max_references))

    master = character_master_config(project)
    master_enabled = bool(master.get("enabled", False))
    master_generate = bool(master.get("generate", False))
    if project.get("character_master") is not None and not isinstance(project.get("character_master"), dict):
        errors.append("project.character_master must be an object")
    if master_enabled:
        if str(master.get("mode", "single-sheet")) != "single-sheet":
            errors.append("character_master.mode must be single-sheet")
        master_path = str(master.get("path", "")).strip()
        if not master_path:
            errors.append("character_master.path is required when enabled")
        elif not master_generate:
            try:
                path = resolve_project_path(root, master_path)
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    errors.append("character_master.path must be PNG, JPEG, or WebP")
            except SkillError as error:
                errors.append(str(error))
        else:
            try:
                resolve_project_path(root, master_path, must_exist=False)
            except SkillError as error:
                errors.append(str(error))
            if not str(master.get("prompt", "")).strip():
                errors.append("character_master.prompt is required when generate is true")
            else:
                prompt = composed_character_prompt(project)
                if len(prompt) > max_prompt_chars:
                    errors.append(f"character_master composed prompt has {len(prompt)} characters; maximum is {max_prompt_chars}")
        errors.extend(_reference_errors(root, master.get("source_references", []), "character_master.source_references", max_references))

    if len(shots) > max_videos:
        errors.append(f"shot count {len(shots)} exceeds max_video_requests {max_videos}")
    generated_images = sum(1 for shot in shots if isinstance(shot, dict) and bool(shot.get("generate_image", True))) + int(master_enabled and master_generate)
    if generated_images > max_images:
        errors.append(f"generated image count {generated_images} exceeds max_image_requests {max_images}")
    if project.get("budget") is not None and not isinstance(project.get("budget"), dict):
        errors.append("project.budget must be an object")
    else:
        try:
            cost = estimated_project_cost(project, image_requests=generated_images, video_requests=len(shots))
            if not cost["within_budget"]:
                errors.append(
                    f"estimated project cost {cost['estimated_cost']} exceeds budget.max_estimated_cost {cost['max_estimated_cost']} {cost['currency']}"
                )
        except SkillError as error:
            errors.append(str(error))
    seen: set[str] = set()
    total_seconds = 0
    for index, raw_shot in enumerate(shots, 1):
        prefix = f"shots[{index - 1}]"
        if not isinstance(raw_shot, dict):
            errors.append(f"{prefix} must be an object")
            continue
        shot_id = str(raw_shot.get("id", "")).strip()
        if not SHOT_ID_RE.fullmatch(shot_id):
            errors.append(f"{prefix}.id must use lowercase letters, digits, and hyphens")
        elif shot_id in seen:
            errors.append(f"duplicate shot id: {shot_id}")
        seen.add(shot_id)
        if bool(raw_shot.get("generate_image", True)) and not str(raw_shot.get("image_prompt", "")).strip():
            errors.append(f"{prefix}.image_prompt is required when generate_image is true")
        if not str(raw_shot.get("video_prompt", "")).strip():
            errors.append(f"{prefix}.video_prompt is required")
        if bool(raw_shot.get("use_character_master", False)) and not master_enabled:
            errors.append(f"{prefix}.use_character_master requires project.character_master.enabled")
        shot_character_ids = raw_shot.get("character_ids", [])
        if not isinstance(shot_character_ids, list) or not all(isinstance(value, str) and value.strip() for value in shot_character_ids):
            errors.append(f"{prefix}.character_ids must be an array of character ids")
        else:
            unknown_characters = [value for value in shot_character_ids if value not in character_ids]
            if unknown_characters:
                errors.append(f"{prefix}.character_ids has unknown ids: {', '.join(unknown_characters)}")
        scene_id = str(raw_shot.get("scene_id", "")).strip()
        if scene_id and not SHOT_ID_RE.fullmatch(scene_id):
            errors.append(f"{prefix}.scene_id must use lowercase letters, digits, and hyphens")
        if raw_shot.get("wardrobe") is not None and not isinstance(raw_shot.get("wardrobe"), dict):
            errors.append(f"{prefix}.wardrobe must map character ids to wardrobe descriptions")
        if raw_shot.get("allow_ui_elements") is not None and not isinstance(raw_shot.get("allow_ui_elements"), bool):
            errors.append(f"{prefix}.allow_ui_elements must be a boolean")
        if str(raw_shot.get("image_prompt", "")).strip():
            image_prompt = composed_image_prompt(project, raw_shot)
            if len(image_prompt) > max_prompt_chars:
                errors.append(f"{prefix} composed image prompt has {len(image_prompt)} characters; maximum is {max_prompt_chars}")
        if str(raw_shot.get("video_prompt", "")).strip():
            video_prompt = composed_video_prompt(project, raw_shot)
            if len(video_prompt) > max_prompt_chars:
                errors.append(f"{prefix} composed video prompt has {len(video_prompt)} characters; maximum is {max_prompt_chars}")
        seconds = shot_value(project, raw_shot, "seconds", 6)
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1 or seconds > MAX_VIDEO_SECONDS:
            errors.append(f"{prefix}.seconds must be an integer from 1 to {MAX_VIDEO_SECONDS}")
        else:
            total_seconds += seconds
        resolution = video_resolution(project, raw_shot)
        if resolution not in VIDEO_RESOLUTIONS:
            errors.append(f"{prefix}.video_resolution must be one of 480p, 720p, 1080p")
        aspect_ratio = video_aspect_ratio(project, raw_shot)
        if aspect_ratio not in ASPECT_RATIOS:
            errors.append(f"{prefix}.video_aspect_ratio must be one of {', '.join(sorted(ASPECT_RATIOS))}")
        for size_name, fallback in (("image_size", "1024x1024"), ("video_size", "1280x720")):
            size = str(shot_value(project, raw_shot, size_name, fallback))
            if size != "auto" and not SIZE_RE.fullmatch(size):
                errors.append(f"{prefix}.{size_name} must be WIDTHxHEIGHT or auto")
        for ref_name in ("image_references", "video_references"):
            references = raw_shot.get(ref_name, [])
            errors.extend(_reference_errors(root, references, f"{prefix}.{ref_name}", max_references))
    if total_seconds > max_seconds:
        errors.append(f"total video seconds {total_seconds} exceeds max_total_video_seconds {max_seconds}")
    return errors


def require_valid_project(root: Path) -> dict[str, Any]:
    project = load_project(root)
    errors = validate_project(root, project)
    if errors:
        raise SkillError("project validation failed: " + "; ".join(errors))
    return project


def selected_shots(project: dict[str, Any], shot_ids: list[str] | None) -> list[dict[str, Any]]:
    shots = [shot for shot in project["shots"] if isinstance(shot, dict)]
    if not shot_ids:
        return shots
    requested = list(dict.fromkeys(shot_ids))
    by_id = {str(shot["id"]): shot for shot in shots}
    unknown = [shot_id for shot_id in requested if shot_id not in by_id]
    if unknown:
        raise SkillError("unknown shot id(s): " + ", ".join(unknown))
    return [by_id[shot_id] for shot_id in requested]


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def signature(value: dict[str, Any], references: list[Path]) -> str:
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for path in references:
        digest.update(path.name.encode("utf-8"))
        digest.update(file_digest(path).encode("ascii"))
    return digest.hexdigest()


def composed_character_prompt(project: dict[str, Any]) -> str:
    master = character_master_config(project)
    sections = []
    character = str(project.get("character_bible", "")).strip()
    style = str(project.get("style_bible", "")).strip()
    if character:
        sections.append("[CHARACTER IDENTITY]\n" + character)
    sections.append("[SINGLE-SHEET CHARACTER MASTER]\n" + str(master.get("prompt", "")).strip())
    if style:
        sections.append("[STYLE]\n" + style)
    return "\n\n".join(sections)


def structured_shot_context(project: dict[str, Any], shot: dict[str, Any]) -> str:
    requested = shot.get("character_ids", []) if isinstance(shot.get("character_ids", []), list) else []
    by_id = {str(item.get("id", "")): item for item in project_characters(project)}
    wardrobe_overrides = shot.get("wardrobe") if isinstance(shot.get("wardrobe"), dict) else {}
    lines: list[str] = []
    for character_id in requested:
        character = by_id.get(str(character_id))
        if not character:
            continue
        wardrobe = str(wardrobe_overrides.get(character_id) or character.get("wardrobe") or "").strip()
        identity = str(character.get("identity") or "").strip()
        line = f"{character.get('name')} ({character_id}): {identity}"
        if wardrobe:
            line += f" Wardrobe: {wardrobe}."
        lines.append(line)
    scene_id = str(shot.get("scene_id", "")).strip()
    continuity = str(shot.get("continuity_notes", "")).strip()
    if scene_id:
        lines.append(f"Scene: {scene_id}.")
    if continuity:
        lines.append(f"Continuity: {continuity}")
    return "\n".join(lines)


def composed_image_prompt(project: dict[str, Any], shot: dict[str, Any]) -> str:
    sections = []
    character = str(project.get("character_bible", "")).strip()
    style = str(project.get("style_bible", "")).strip()
    if character:
        sections.append("[CHARACTER BIBLE]\n" + character)
    if style:
        sections.append("[STYLE BIBLE]\n" + style)
    structured = structured_shot_context(project, shot)
    if structured:
        sections.append("[SHOT CONTINUITY]\n" + structured)
    if not allow_ui_elements(project, shot):
        sections.append(
            "[CLEAN FRAME POLICY]\n"
            "Clean filmed scene. No app UI, controls, overlays, text, logos, watermarks, captions, counters, comments, or stickers anywhere in frame."
        )
    sections.append("[SHOT KEYFRAME]\n" + str(shot["image_prompt"]).strip())
    return "\n\n".join(sections)


def composed_video_prompt(project: dict[str, Any], shot: dict[str, Any]) -> str:
    sections = []
    character = str(project.get("character_bible", "")).strip()
    style = str(project.get("style_bible", "")).strip()
    if character:
        sections.append(
            "[IDENTITY LOCK]\n"
            "Same character every shot; preserve face, hair, body, clothes, props, and colors.\n" + character
        )
    if style:
        sections.append("[STYLE LOCK]\n" + style)
    structured = structured_shot_context(project, shot)
    if structured:
        sections.append("[SHOT CONTINUITY]\n" + structured)
    if not allow_ui_elements(project, shot):
        sections.append(
            "[CLEAN FRAME POLICY]\n"
            "Clean cinematic frame. No app UI, controls, overlays, text, logos, watermarks, captions, counters, comments, or stickers anywhere in frame."
        )
    sections.append("[SHOT MOTION]\n" + str(shot["video_prompt"]).strip())
    return "\n\n".join(sections)


def preflight_report(project: dict[str, Any]) -> dict[str, Any]:
    master = character_master_config(project)
    limits = project.get("limits") if isinstance(project.get("limits"), dict) else {}
    try:
        prompt_limit = min(int(limits.get("max_prompt_chars", HARD_PROMPT_CHARS)), HARD_PROMPT_CHARS)
    except (TypeError, ValueError):
        prompt_limit = HARD_PROMPT_CHARS
    prompts: list[dict[str, Any]] = []
    warnings: list[str] = []
    if bool(master.get("enabled", False)) and bool(master.get("generate", False)) and str(master.get("prompt", "")).strip():
        value = composed_character_prompt(project)
        characters = len(value)
        prompts.append(
            {
                "kind": "character_master",
                "id": "character-master",
                "characters": characters,
                "hard_limit": prompt_limit,
                "safe_limit": SAFE_PROMPT_CHARS,
                "remaining": prompt_limit - characters,
                "within_hard_limit": characters <= prompt_limit,
            }
        )
    total_seconds = 0
    image_requests = int(bool(master.get("enabled", False)) and bool(master.get("generate", False)))
    for shot in project.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("id", ""))
        if bool(shot.get("generate_image", True)):
            image_requests += 1
        if str(shot.get("image_prompt", "")).strip():
            value = composed_image_prompt(project, shot)
            characters = len(value)
            prompts.append(
                {
                    "kind": "image",
                    "id": shot_id,
                    "characters": characters,
                    "hard_limit": prompt_limit,
                    "safe_limit": SAFE_PROMPT_CHARS,
                    "remaining": prompt_limit - characters,
                    "within_hard_limit": characters <= prompt_limit,
                }
            )
        if str(shot.get("video_prompt", "")).strip():
            value = composed_video_prompt(project, shot)
            characters = len(value)
            prompts.append(
                {
                    "kind": "video",
                    "id": shot_id,
                    "characters": characters,
                    "hard_limit": prompt_limit,
                    "safe_limit": SAFE_PROMPT_CHARS,
                    "remaining": prompt_limit - characters,
                    "within_hard_limit": characters <= prompt_limit,
                }
            )
        seconds = shot_value(project, shot, "seconds", 6)
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            total_seconds += seconds
        if bool(shot.get("use_character_master", False)) and shot.get("video_references"):
            warnings.append(f"{shot_id}: explicit video_references bypass the generated keyframe; do not send a multi-view master sheet directly to video")
    for item in prompts:
        if item["characters"] > SAFE_PROMPT_CHARS:
            warnings.append(
                f"{item['kind']} prompt {item['id']} uses {item['characters']} characters; keep it at or below {SAFE_PROMPT_CHARS} for provider headroom"
            )
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    if video_mode(project) == "text-to-video" and len(shots) > 1 and (str(project.get("character_bible", "")).strip() or project_characters(project)):
        warnings.append(
            "multi-shot text-to-video identity continuity is prompt-only; use image-to-video with a character master and per-shot keyframes when strict identity is required"
        )
    video_requests = len(shots)
    try:
        cost = estimated_project_cost(project, image_requests=image_requests, video_requests=video_requests)
    except SkillError as error:
        cost = {"error": str(error), "within_budget": False}
    return {
        "workflow": project.get("workflow", "general-video"),
        "requests": {
            "character_master_images": int(bool(master.get("enabled", False)) and bool(master.get("generate", False))),
            "shot_images": image_requests - int(bool(master.get("enabled", False)) and bool(master.get("generate", False))),
            "total_images": image_requests,
            "videos": video_requests,
        },
        "total_video_seconds": total_seconds,
        "max_clip_seconds": MAX_VIDEO_SECONDS,
        "prompt_hard_limit": prompt_limit,
        "prompt_safe_limit": SAFE_PROMPT_CHARS,
        "prompts": prompts,
        "budget": cost,
        "warnings": warnings,
    }


def audit_project(root: Path, project: dict[str, Any]) -> dict[str, Any]:
    errors = validate_project(root, project)
    warnings: list[str] = []
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    characters = {str(item.get("id")): item for item in project_characters(project)}
    previous: dict[str, Any] | None = None
    for shot in shots:
        shot_id = str(shot.get("id", ""))
        character_ids = shot.get("character_ids", []) if isinstance(shot.get("character_ids", []), list) else []
        if characters and not character_ids:
            warnings.append(f"{shot_id}: no character_ids selected; structured identity locks will not be added")
        wardrobe = shot.get("wardrobe") if isinstance(shot.get("wardrobe"), dict) else {}
        for character_id, description in wardrobe.items():
            canonical = str((characters.get(str(character_id)) or {}).get("wardrobe") or "").strip()
            if canonical and str(description).strip() != canonical and not bool(shot.get("continuity_change", False)):
                warnings.append(f"{shot_id}: wardrobe for {character_id} differs from the character bible without continuity_change=true")
        if previous:
            same_scene = str(previous.get("scene_id", "")) and previous.get("scene_id") == shot.get("scene_id")
            shared_characters = set(previous.get("character_ids", [])) & set(character_ids)
            if (same_scene or shared_characters) and not str(shot.get("continuity_notes", "")).strip():
                warnings.append(f"{shot_id}: adjacent scene/character continuity has no continuity_notes")
        previous = shot
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "manual_review_required": [
            "character identity and wardrobe across adjacent shots",
            "screen direction, eyeline, prop placement, and scene lighting",
            "hands, limbs, facial anatomy, and motion naturalness",
            "clean frame: no unintended app UI, captions, logos, watermarks, or overlay text",
        ],
    }


def shot_state(state: dict[str, Any], shot_id: str) -> dict[str, Any]:
    shots = state.setdefault("shots", {})
    value = shots.setdefault(shot_id, {"image": {"status": "pending", "attempts": 0}, "video": {"status": "pending", "attempts": 0}})
    value.setdefault("image", {"status": "pending", "attempts": 0})
    value.setdefault("video", {"status": "pending", "attempts": 0})
    return value


def write_event(root: Path, event: dict[str, Any]) -> None:
    path = root / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def clients(*, require_secrets: bool = True) -> tuple[QuickAIImageClient, QuickAINewVideoClient, dict[str, Any]]:
    settings = load_settings(require_secrets=require_secrets)
    return (
        QuickAIImageClient(settings["quickai_base_url"], settings["quickai_key"], settings["image_model"]),
        QuickAINewVideoClient(settings["quickainew_base_url"], settings["quickainew_key"], settings["video_model"]),
        settings,
    )


def select_video_client(settings: dict[str, Any], provider: str) -> QuickAIVideoClient | QuickAINewVideoClient:
    if provider == "quickai":
        if not settings.get("quickai_key"):
            raise SkillError("QuickAI key is required for video_provider=quickai")
        return QuickAIVideoClient(settings["quickai_base_url"], settings["quickai_key"], settings["video_model"])
    if provider == "quickainew":
        if not settings.get("quickainew_key"):
            raise SkillError("QuickAI New key is required for video_provider=quickainew")
        return QuickAINewVideoClient(settings["quickainew_base_url"], settings["quickainew_key"], settings["video_model"])
    raise SkillError("video_provider must be quickai or quickainew")


def resolved_character_master(root: Path, project: dict[str, Any], state: dict[str, Any]) -> Path:
    master = character_master_config(project)
    if not bool(master.get("enabled", False)):
        raise SkillError("character master is not enabled")
    runtime = state.setdefault("character_master", {"status": "pending", "attempts": 0})
    runtime_path = str(runtime.get("path", "")).strip()
    if runtime.get("status") == "completed" and runtime_path:
        return resolve_project_path(root, runtime_path)
    if not bool(master.get("generate", False)):
        return resolve_project_path(root, str(master.get("path", "")))
    raise SkillError("generated character master is missing; run generate-character first")


def generate_character_master(root: Path, *, retry_failed: bool, retry_reason: str = "", progress: bool = False) -> dict[str, Any]:
    reason = require_retry_reason(retry_failed, retry_reason)
    project = require_valid_project(root)
    state = load_state(root)
    master = character_master_config(project)
    runtime = state.setdefault("character_master", {"status": "pending", "attempts": 0})
    if not bool(master.get("enabled", False)):
        return {"status": "disabled", "skipped": True}
    if not bool(master.get("generate", False)):
        path = resolve_project_path(root, str(master["path"]))
        runtime.update({"status": "completed", "path": path.relative_to(root).as_posix(), "source": "external", "error": ""})
        save_state(root, state)
        return {"status": "completed", "path": str(path), "source": "external", "skipped": True}

    image_client, _, settings = clients()
    if not settings.get("quickai_key"):
        raise SkillError("QuickAI key is required for image generation")
    references = [resolve_project_path(root, value) for value in master.get("source_references", [])]
    prompt = composed_character_prompt(project)
    size = str(master.get("image_size") or project.get("defaults", {}).get("image_size") or "1024x1024")
    quality = str(master.get("image_quality") or project.get("defaults", {}).get("image_quality") or "auto")
    current_signature = signature({"model": settings["image_model"], "prompt": prompt, "size": size, "quality": quality}, references)
    existing_path = str(runtime.get("path", ""))
    existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
    if runtime.get("status") == "completed" and existing and existing.is_file() and runtime.get("signature") == current_signature:
        return {"status": "completed", "path": str(existing), "source": "generated", "skipped": True}
    attempts = int(runtime.get("attempts", 0))
    if attempts > 0 and not retry_failed:
        raise SkillError("character master creation was already attempted; inspect state.json and use --retry-failed to authorize another billable request")
    if attempts > 0:
        archive_runtime_attempt(runtime, reason)
        write_event(root, {"kind": "character_master_retry_authorized", "reason": reason, "previous_status": runtime.get("status", "")})
    record_budget_attempt(state, project, "image")
    runtime.update({"status": "submitting", "attempts": attempts + 1, "signature": current_signature, "error": ""})
    save_state(root, state)
    write_event(root, {"kind": "character_master_create", "attempt": runtime["attempts"]})
    emit_progress(progress, phase="character_master_create", status="submitting", attempt=runtime["attempts"])
    try:
        data = image_client.edit(prompt, references, size=size, quality=quality) if references else image_client.generate(prompt, size=size, quality=quality)
        target = resolve_project_path(root, str(master["path"]), must_exist=False)
        output = save_image_bytes(data, target.with_suffix(""))
        runtime.update(
            {
                "status": "completed",
                "path": output.relative_to(root).as_posix(),
                "source": "generated",
                "bytes": output.stat().st_size,
                "error": "",
            }
        )
        save_state(root, state)
        write_event(root, {"kind": "character_master_completed", "bytes": output.stat().st_size})
        emit_progress(progress, phase="character_master_create", status="completed", bytes=output.stat().st_size)
        return {"status": "completed", "path": str(output), "source": "generated", "skipped": False}
    except Exception as error:
        runtime.update({"status": "failed", "error": str(error)[:1000]})
        save_state(root, state)
        write_event(root, {"kind": "character_master_failed", "error": str(error)[:1000]})
        raise


def generate_images(
    root: Path,
    *,
    retry_failed: bool,
    retry_reason: str = "",
    progress: bool = False,
    shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    reason = require_retry_reason(retry_failed, retry_reason)
    project = require_valid_project(root)
    state = load_state(root)
    image_client, _, settings = clients()
    if not settings.get("quickai_key"):
        raise SkillError("QuickAI key is required for image generation")
    completed: list[str] = []
    skipped: list[str] = []
    for shot in selected_shots(project, shot_ids):
        shot_id = str(shot["id"])
        if not bool(shot.get("generate_image", True)):
            skipped.append(shot_id)
            continue
        references = [resolve_project_path(root, value) for value in shot.get("image_references", [])]
        if bool(shot.get("use_character_master", False)):
            master_path = resolved_character_master(root, project, state)
            if master_path not in references:
                references.insert(0, master_path)
        prompt = composed_image_prompt(project, shot)
        size = str(shot_value(project, shot, "image_size", "1024x1024"))
        quality = str(shot_value(project, shot, "image_quality", "auto"))
        current_signature = signature({"model": settings["image_model"], "prompt": prompt, "size": size, "quality": quality}, references)
        runtime = shot_state(state, shot_id)["image"]
        existing_path = str(runtime.get("path", ""))
        existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
        if runtime.get("status") == "completed" and existing and existing.is_file() and runtime.get("signature") == current_signature:
            skipped.append(shot_id)
            continue
        if int(runtime.get("attempts", 0)) > 0 and not retry_failed:
            raise SkillError(f"image create was already attempted for {shot_id}; inspect state.json and use --retry-failed to authorize another billable request")
        if int(runtime.get("attempts", 0)) > 0:
            archive_runtime_attempt(runtime, reason)
            write_event(root, {"kind": "image_retry_authorized", "shot_id": shot_id, "reason": reason, "previous_status": runtime.get("status", "")})
        record_budget_attempt(state, project, "image")
        runtime.update({"status": "submitting", "attempts": int(runtime.get("attempts", 0)) + 1, "signature": current_signature, "error": ""})
        save_state(root, state)
        write_event(root, {"kind": "image_create", "shot_id": shot_id, "attempt": runtime["attempts"]})
        emit_progress(progress, phase="image_create", shot_id=shot_id, status="submitting", attempt=runtime["attempts"])
        try:
            data = image_client.edit(prompt, references, size=size, quality=quality) if references else image_client.generate(prompt, size=size, quality=quality)
            output = save_image_bytes(data, root / "assets" / "keyframes" / shot_id)
            runtime.update({"status": "completed", "path": output.relative_to(root).as_posix(), "bytes": output.stat().st_size, "error": ""})
            completed.append(shot_id)
            save_state(root, state)
            emit_progress(progress, phase="image_create", shot_id=shot_id, status="completed", bytes=output.stat().st_size)
        except Exception as error:
            runtime.update({"status": "failed", "error": str(error)[:1000]})
            save_state(root, state)
            write_event(root, {"kind": "image_failed", "shot_id": shot_id, "error": str(error)[:1000]})
            raise
    return {"completed": completed, "skipped": skipped}


def video_references(root: Path, shot: dict[str, Any], runtime: dict[str, Any]) -> list[Path]:
    explicit = [resolve_project_path(root, value) for value in shot.get("video_references", [])]
    if explicit:
        return explicit
    image = runtime.get("image", {})
    if image.get("status") == "completed" and image.get("path"):
        return [resolve_project_path(root, str(image["path"]))]
    if bool(shot.get("generate_image", True)):
        raise SkillError(f"generated keyframe is missing for {shot['id']}")
    return []


def generate_videos(
    root: Path,
    *,
    retry_failed: bool,
    retry_reason: str = "",
    progress: bool = False,
    poll_timeout: int,
    shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    reason = require_retry_reason(retry_failed, retry_reason)
    project = require_valid_project(root)
    state = load_state(root)
    _, _, settings = clients()
    mode = video_mode(project)
    provider = video_provider(project, settings)
    if mode not in {"text-to-video", "image-to-video"}:
        raise SkillError("video_mode must be text-to-video or image-to-video")
    video_client = select_video_client(settings, provider)
    completed: list[str] = []
    skipped: list[str] = []
    for shot in selected_shots(project, shot_ids):
        shot_id = str(shot["id"])
        runtime = shot_state(state, shot_id)
        video = runtime["video"]
        references = video_references(root, shot, runtime) if mode == "image-to-video" else []
        prompt = composed_video_prompt(project, shot)
        seconds = int(shot_value(project, shot, "seconds", 6))
        size = str(shot_value(project, shot, "video_size", "1280x720"))
        resolution = video_resolution(project, shot)
        aspect_ratio = video_aspect_ratio(project, shot)
        current_signature = signature(
            {"mode": mode, "provider": provider, "model": settings["video_model"], "prompt": prompt, "seconds": seconds, "size": size, "resolution": resolution, "aspect_ratio": aspect_ratio},
            references,
        )
        existing_path = str(video.get("path", ""))
        existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
        if video.get("status") == "completed" and existing and existing.is_file() and video.get("signature") == current_signature:
            assert_mp4(existing)
            skipped.append(shot_id)
            continue
        task_id = str(video.get("task_id", "")).strip()
        if task_id and video.get("signature") and video.get("signature") != current_signature:
            raise SkillError(f"{shot_id} changed after task creation; restore the original shot or start a new project to avoid mixing task state")
        archived_for_retry = False
        if task_id and video.get("status") == "failed":
            if not retry_failed:
                raise SkillError(
                    f"video task failed for {shot_id} ({task_id}); inspect the provider and use --retry-failed only to authorize a new billable task"
                )
            archive_runtime_attempt(video, reason)
            archived_for_retry = True
            write_event(root, {"kind": "video_retry_authorized", "shot_id": shot_id, "previous_task_id": task_id, "reason": reason})
            video["previous_task_id"] = task_id
            video["task_id"] = ""
            task_id = ""
        if not task_id:
            attempts = int(video.get("attempts", 0))
            if attempts > 0 and not retry_failed:
                raise SkillError(f"video create was already attempted for {shot_id}; inspect state.json and use --retry-failed only if duplicate billing is acceptable")
            if attempts > 0 and not archived_for_retry:
                archive_runtime_attempt(video, reason)
                write_event(
                    root,
                    {"kind": "video_retry_authorized", "shot_id": shot_id, "previous_task_id": video.get("task_id", ""), "reason": reason},
                )
            record_budget_attempt(state, project, "video")
            video.update(
                {
                    "status": "submitting",
                    "attempts": attempts + 1,
                    "signature": current_signature,
                    "mode": mode,
                    "provider": provider,
                    "model": settings["video_model"],
                    "seconds": seconds,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "reference_count": len(references),
                    "error": "",
                    "create_attempted_at": int(time.time()),
                }
            )
            save_state(root, state)
            write_event(root, {"kind": "video_create", "shot_id": shot_id, "attempt": video["attempts"]})
            emit_progress(progress, phase="video_create", shot_id=shot_id, status="submitting", attempt=video["attempts"])
            try:
                task_id = video_client.create(
                    prompt,
                    seconds=seconds,
                    size=size,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    references=references,
                )
            except Exception as error:
                status = "failed" if isinstance(error, APIError) and error.status in {400, 401, 403, 404, 409, 422} else "submission_unknown"
                video.update({"status": status, "error": str(error)[:1000]})
                save_state(root, state)
                write_event(root, {"kind": "video_create_failed", "shot_id": shot_id, "status": status, "error": str(error)[:1000]})
                raise
            video.update({"status": "queued", "task_id": task_id, "error": ""})
            save_state(root, state)
            emit_progress(progress, phase="video_create", shot_id=shot_id, status="queued", task_id=task_id)

        def on_status(status: str, payload: dict[str, Any]) -> None:
            video["status"] = status
            video["last_polled_at"] = int(time.time())
            provider_progress = task_progress(payload)
            if provider_progress is not None:
                video["progress"] = provider_progress
            save_state(root, state)
            emit_progress(
                progress,
                phase="video_poll",
                shot_id=shot_id,
                task_id=task_id,
                status=status,
                progress=provider_progress,
            )

        try:
            status_payload = video_client.poll(task_id, timeout_seconds=poll_timeout, on_status=on_status)
            output = root / "clips" / f"{shot_id}.mp4"
            video_client.download(task_id, status_payload, output)
            try:
                qa = quality_report(output, expected_size=size, expected_duration=seconds)
            except SkillError as error:
                qa = {"ok": False, "errors": [str(error)], "warnings": [], "manual_review_required": []}
            video.update(
                {
                    "status": "completed",
                    "path": output.relative_to(root).as_posix(),
                    "bytes": output.stat().st_size,
                    "progress": 100.0,
                    "qa": portable_qa(qa),
                    "error": "",
                }
            )
            save_state(root, state)
            write_event(root, {"kind": "video_completed", "shot_id": shot_id, "task_id": task_id, "bytes": output.stat().st_size})
            emit_progress(progress, phase="video_download", shot_id=shot_id, task_id=task_id, status="completed", bytes=output.stat().st_size)
            completed.append(shot_id)
        except TimeoutError as error:
            video.update({"status": "poll_timeout", "error": str(error)[:1000]})
            save_state(root, state)
            raise SkillError(str(error)) from error
        except Exception as error:
            if video.get("status") not in {"poll_timeout", "submission_unknown"}:
                video.update({"status": "failed", "error": str(error)[:1000]})
            save_state(root, state)
            write_event(root, {"kind": "video_failed", "shot_id": shot_id, "task_id": task_id, "error": str(error)[:1000]})
            raise
    return {"completed": completed, "skipped": skipped}


def probe_media(path: Path) -> dict[str, Any]:
    assert_mp4(path)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("ffprobe is required for media validation")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SkillError(f"ffprobe could not read {path.name}: {result.stderr.strip()[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SkillError(f"ffprobe returned invalid JSON for {path.name}") from error
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    if not video:
        raise SkillError(f"media has no video stream: {path}")
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as error:
        raise SkillError(f"invalid media metadata: {path}") from error
    if duration <= 0 or width <= 0 or height <= 0:
        raise SkillError(f"media duration or dimensions are invalid: {path}")
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "has_audio": any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams),
    }


def _run_ffmpeg(command: list[str], action: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SkillError(f"ffmpeg failed while {action}: {detail}")


def assemble_clips(clips: list[Path], output: Path, *, target_size: str, audio_policy: str = "preserve") -> dict[str, Any]:
    if not clips:
        raise SkillError("at least one clip is required")
    if audio_policy not in {"preserve", "mute"}:
        raise SkillError("audio policy must be preserve or mute")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to assemble clips")
    inputs = [probe_media(path) for path in clips]
    if target_size == "auto":
        width, height = inputs[0]["width"], inputs[0]["height"]
    else:
        if not SIZE_RE.fullmatch(target_size):
            raise SkillError("assembly target size must be WIDTHxHEIGHT or auto")
        width, height = (int(value) for value in target_size.split("x", 1))
    if width % 2 or height % 2:
        raise SkillError("assembly target width and height must be even for H.264")
    output = output.resolve()
    if output.suffix.lower() != ".mp4":
        raise SkillError("assembled output filename must end with .mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gvs-assemble-", dir=str(output.parent)) as temp_name:
        temp_root = Path(temp_name)
        normalized: list[Path] = []
        filter_value = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps=30"
        )
        for index, clip in enumerate(clips, 1):
            segment = temp_root / f"segment-{index:03d}.mp4"
            command = [ffmpeg, "-y", "-i", str(clip)]
            source_audio_index = 0
            if audio_policy == "preserve" and not inputs[index - 1]["has_audio"]:
                command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
                source_audio_index = 1
            command.extend(["-map", "0:v:0", "-vf", filter_value, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"])
            if audio_policy == "preserve":
                command.extend(["-map", f"{source_audio_index}:a:0", "-af", "apad", "-t", f"{inputs[index - 1]['duration']:.3f}", "-c:a", "aac", "-ar", "48000", "-ac", "2"])
            else:
                command.append("-an")
            command.extend(["-movflags", "+faststart", str(segment)])
            _run_ffmpeg(command, f"normalizing clip {index}")
            probe_media(segment)
            normalized.append(segment)
        concat = temp_root / "concat.txt"
        concat.write_text(
            "\n".join(f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in normalized) + "\n",
            encoding="utf-8",
        )
        _run_ffmpeg(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)],
            "concatenating normalized clips",
        )
    media = probe_media(output)
    expected_duration = sum(float(item["duration"]) for item in inputs)
    if media["width"] != width or media["height"] != height:
        raise SkillError("assembled video dimensions do not match the requested target")
    if media["codec"] != "h264" or media["pixel_format"] != "yuv420p":
        raise SkillError("assembled video is not H.264 yuv420p")
    if audio_policy == "preserve" and not media["has_audio"]:
        raise SkillError("assembled video did not preserve or synthesize an audio track")
    if audio_policy == "mute" and media["has_audio"]:
        raise SkillError("assembled video contains audio despite mute policy")
    if media["duration"] < expected_duration * 0.85:
        raise SkillError("assembled video duration is unexpectedly shorter than its input clips")
    return {"path": str(output), "bytes": output.stat().st_size, **media, "clip_count": len(clips), "audio_policy": audio_policy}


def assemble(root: Path) -> tuple[Path, dict[str, Any]]:
    project = require_valid_project(root)
    state = load_state(root)
    clips: list[Path] = []
    for shot in project["shots"]:
        video = shot_state(state, str(shot["id"]))["video"]
        if video.get("status") != "completed" or not video.get("path"):
            raise SkillError(f"video is not complete for {shot['id']}")
        clips.append(resolve_project_path(root, str(video["path"])))
    output = root / "deliverables" / "final.mp4"
    target_size = str(project.get("defaults", {}).get("video_size") or "auto")
    media = assemble_clips(clips, output, target_size=target_size, audio_policy=audio_policy(project))
    qa = quality_report(output, expected_size=target_size, expected_duration=sum(probe_media(path)["duration"] for path in clips))
    state.setdefault("deliverables", {})["final"] = {
        "path": output.relative_to(root).as_posix(),
        "bytes": output.stat().st_size,
        "updated_at": int(time.time()),
        "media": {key: value for key, value in media.items() if key not in {"path", "bytes"}},
        "qa": portable_qa(qa),
    }
    save_state(root, state)
    return output, media


def qa_project(root: Path) -> dict[str, Any]:
    project = require_valid_project(root)
    state = load_state(root)
    reports: list[dict[str, Any]] = []
    review_root = root / "deliverables" / "qa-frames"
    for shot in project["shots"]:
        shot_id = str(shot["id"])
        runtime = shot_state(state, shot_id)["video"]
        if runtime.get("status") != "completed" or not runtime.get("path"):
            reports.append({"kind": "clip", "id": shot_id, "ok": False, "errors": ["video is not completed"], "warnings": []})
            continue
        path = resolve_project_path(root, str(runtime["path"]))
        report = quality_report(
            path,
            expected_size=str(shot_value(project, shot, "video_size", "1280x720")),
            expected_duration=float(shot_value(project, shot, "seconds", 6)),
        )
        review_frames = export_review_frames(path, review_root, stem=shot_id)
        report["review_frames"] = [
            {**frame, "path": Path(str(frame["path"])).relative_to(root).as_posix()} for frame in review_frames
        ]
        runtime["qa"] = portable_qa(report)
        reports.append({"kind": "clip", "id": shot_id, **report})
    final = state.get("deliverables", {}).get("final", {})
    if isinstance(final, dict) and final.get("path"):
        final_path = resolve_project_path(root, str(final["path"]))
        report = quality_report(
            final_path,
            expected_size=str(project.get("defaults", {}).get("video_size") or "auto"),
            expected_duration=float(preflight_report(project)["total_video_seconds"]),
        )
        expected_audio_policy = audio_policy(project)
        if expected_audio_policy == "preserve" and not report["media"]["has_audio"]:
            report["errors"].append("final delivery has no audio track while defaults.audio_policy is preserve")
            report["ok"] = False
        elif expected_audio_policy == "mute" and report["media"]["has_audio"]:
            report["errors"].append("final delivery contains audio while defaults.audio_policy is mute")
            report["ok"] = False
        review_frames = export_review_frames(final_path, review_root, stem="final")
        report["review_frames"] = [
            {**frame, "path": Path(str(frame["path"])).relative_to(root).as_posix()} for frame in review_frames
        ]
        final["qa"] = portable_qa(report)
        reports.append({"kind": "deliverable", "id": "final", **report})
    save_state(root, state)
    technical_ok = all(bool(item.get("ok")) for item in reports) if reports else False
    return {
        "ok": technical_ok,
        "technical_ok": technical_ok,
        "reports": reports,
        "project_audit": audit_project(root, project),
        "visual_review_required": True,
        "manual_review_complete": False,
    }


def plan_shot_durations(
    workflow: dict[str, Any],
    *,
    shot_count: int | None,
    seconds: int | None,
    target_seconds: int | None,
) -> list[int]:
    preferred = seconds or int(workflow.get("preferred_clip_seconds", 6))
    if preferred < 1 or preferred > MAX_VIDEO_SECONDS:
        raise SkillError(f"clip seconds must be from 1 to {MAX_VIDEO_SECONDS}")
    if target_seconds is None:
        count = shot_count or int(workflow.get("default_shots", 1))
        if count < 1 or count > 50:
            raise SkillError("shot count must be from 1 to 50")
        return [preferred] * count
    if target_seconds < 1 or target_seconds > 50 * MAX_VIDEO_SECONDS:
        raise SkillError(f"target duration must be from 1 to {50 * MAX_VIDEO_SECONDS} seconds")
    count = shot_count or max(1, (target_seconds + preferred - 1) // preferred)
    if count < 1 or count > 50:
        raise SkillError("shot count must be from 1 to 50")
    if target_seconds < count or target_seconds > count * MAX_VIDEO_SECONDS:
        raise SkillError(f"target duration {target_seconds} cannot be distributed across {count} shots of 1 to {MAX_VIDEO_SECONDS} seconds")
    base, remainder = divmod(target_seconds, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def init_project(
    root: Path,
    title: str,
    topic: str,
    workflow_id: str,
    shot_durations: list[int],
    video_size: str,
    video_mode_value: str,
    video_provider_value: str,
    video_resolution_value: str,
    video_aspect_ratio_value: str,
) -> Path:
    root = root.resolve()
    path = project_file(root)
    if path.exists():
        raise SkillError(f"project already exists: {path}")
    workflow = get_workflow(workflow_id)
    for folder in ("assets/references", "assets/keyframes", "clips", "deliverables", "logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    shot_defaults = workflow.get("shot_defaults") if isinstance(workflow.get("shot_defaults"), dict) else {}
    shots = [
        {
            "id": f"shot-{index:03d}",
            "summary": "",
            "scene_id": "",
            "character_ids": [],
            "continuity_notes": "",
            "image_prompt": "",
            "video_prompt": "",
            "generate_image": False if video_mode_value == "text-to-video" else bool(shot_defaults.get("generate_image", True)),
            "use_character_master": False if video_mode_value == "text-to-video" else bool(shot_defaults.get("use_character_master", False)),
            "image_references": [],
            "video_references": [],
            "video_resolution": video_resolution_value,
            "video_aspect_ratio": video_aspect_ratio_value,
            "seconds": shot_durations[index - 1],
        }
        for index in range(1, len(shot_durations) + 1)
    ]
    uses_master = bool(workflow.get("character_master", False))
    project = {
        "version": PROJECT_VERSION,
        "title": title.strip(),
        "topic": topic.strip(),
        "workflow": workflow_id,
        "workflow_title": workflow["title"],
        "workflow_guidance": workflow.get("guidance", {}),
        "video_mode": video_mode_value,
        "video_provider": video_provider_value,
        "target_duration_seconds": sum(shot_durations),
        "story": "",
        "character_bible": "",
        "style_bible": "",
        "characters": [],
        "character_master": {
            "enabled": uses_master,
            "mode": "single-sheet",
            "generate": uses_master,
            "path": "assets/references/character-master.png",
            "prompt": "",
            "source_references": [],
            "image_size": "1024x1024",
            "image_quality": "auto"
        },
        "defaults": {
            "image_size": "1024x1024",
            "image_quality": "auto",
            "video_size": video_size,
            "video_seconds": shot_durations[0],
            "video_resolution": video_resolution_value,
            "video_aspect_ratio": video_aspect_ratio_value,
            "audio_policy": "preserve",
        },
        "allow_ui_elements": False,
        "limits": {
            "max_image_requests": max(12, len(shot_durations) + int(uses_master)),
            "max_video_requests": max(8, len(shot_durations)),
            "max_total_video_seconds": max(60, sum(shot_durations)),
            "max_reference_images": 9,
            "max_prompt_chars": HARD_PROMPT_CHARS,
        },
        "budget": {
            "currency": "CNY",
            "image_request": 0.0,
            "video_request": 0.0,
            "max_estimated_cost": None,
        },
        "postproduction": {
            "music": "",
            "voice": "",
            "subtitles": "",
            "fade_seconds": 0.0,
            "cover_at_seconds": 0.5,
        },
        "shots": shots,
    }
    atomic_write_json(path, project)
    save_state(root, fresh_state())
    return path


def doctor() -> tuple[int, dict[str, Any]]:
    image_client, video_client, settings = clients()
    result: dict[str, Any] = {
        "ok": True,
        "skill_version": SKILL_VERSION,
        "config": str(config_path()),
        "providers": {},
        "ffmpeg": shutil.which("ffmpeg") or "not_found",
        "ffprobe": shutil.which("ffprobe") or "not_found",
    }
    if result["ffmpeg"] == "not_found" or result["ffprobe"] == "not_found":
        result["ok"] = False
    checks = []
    if settings.get("quickai_key"):
        checks.append(("quickai", image_client.list_models, image_client))
    else:
        result["providers"]["quickai"] = {"ok": False, "configured_model": settings["image_model"], "skipped": True, "reason": "key_not_configured"}
    if settings.get("quickainew_key"):
        checks.append(("quickainew", video_client.list_models, video_client))
    else:
        result["providers"]["quickainew"] = {"ok": False, "configured_model": settings["video_model"], "skipped": True, "reason": "key_not_configured"}
    for name, operation, client in checks:
        started = time.perf_counter()
        try:
            models = operation()
            image_present = settings["image_model"] in models if name == "quickai" else None
            video_present = settings["video_model"] in models if name == settings.get("default_video_provider") else None
            configured_models = [settings["image_model"]] if name == "quickai" else []
            if name == settings.get("default_video_provider"):
                configured_models.append(settings["video_model"])
            present = video_present if name == settings.get("default_video_provider") else True
            result["providers"][name] = {
                "ok": present,
                "configured_model": settings["video_model"] if name == settings.get("default_video_provider") else settings["image_model"],
                "configured_models": configured_models,
                "models": models,
                "model_present": present,
                "image_model_present": image_present,
                "video_model_present": video_present,
                "model_count": len(models),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "circuit": client.health_snapshot(),
            }
            if not present:
                result["ok"] = False
        except Exception as error:
            client = image_client if name == "quickai" else video_client
            result["providers"][name] = {
                "ok": False,
                "configured_model": settings["video_model"] if name == settings.get("default_video_provider") else settings["image_model"],
                "configured_models": [settings["image_model"]] if name == "quickai" else [settings["video_model"]],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(error)[:1000],
                "circuit": client.health_snapshot(),
            }
            if name == settings.get("default_video_provider") or name == "quickai" and settings.get("quickai_key") or name == "quickainew" and settings.get("quickainew_key"):
                result["ok"] = False
    return (0 if result["ok"] else 1), result


def read_credentials_payload() -> tuple[str, str]:
    prompt = "Credential payload JSON: "
    raw = getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline(MAX_CREDENTIAL_PAYLOAD_CHARS + 1)
    if len(raw) > MAX_CREDENTIAL_PAYLOAD_CHARS:
        raise SkillError("credential payload is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SkillError("credential payload must be one JSON object") from error
    if not isinstance(payload, dict):
        raise SkillError("credential payload must be one JSON object")
    quickai_key = payload.get("quickai_key")
    quickainew_key = payload.get("quickainew_key")
    if quickai_key is None:
        quickai_key = ""
    if quickainew_key is None:
        quickainew_key = ""
    if not isinstance(quickai_key, str) or not isinstance(quickainew_key, str):
        raise SkillError("credential payload fields quickai_key and quickainew_key must be strings when provided")
    return quickai_key.strip(), quickainew_key.strip()


def configure(args: argparse.Namespace) -> dict[str, Any]:
    if args.credentials_stdin and args.environment_only:
        raise SkillError("--credentials-stdin cannot be combined with --environment-only because the supplied keys would not persist")
    if args.credentials_stdin:
        quickai_key, quickainew_key = read_credentials_payload()
        credential_source = "agent-stdin"
    else:
        quickai_key = os.environ.get("GVS_QUICKAI_KEY", "").strip()
        quickainew_key = os.environ.get("GVS_QUICKAINEW_KEY", "").strip()
        if not quickai_key:
            quickai_key = getpass.getpass("QuickAI image key: ").strip()
        if not quickainew_key:
            quickainew_key = getpass.getpass("QuickAI New video key: ").strip()
        credential_source = "environment-or-interactive"
    if not quickai_key and not quickainew_key:
        raise SkillError("at least one provider key is required")
    config = {
        "quickai_base_url": normalize_base_url(args.quickai_base_url),
        "quickainew_base_url": normalize_base_url(args.quickainew_base_url),
        "image_model": args.image_model.strip(),
        "video_model": args.video_model.strip(),
        "default_video_provider": args.video_provider.strip(),
    }
    connection: dict[str, Any] = {"quickai": "not_tested", "quickainew": "not_tested"}
    if not args.skip_test:
        connection = {"quickai": "not_configured", "quickainew": "not_configured"}
        if quickai_key:
            image_models = QuickAIImageClient(config["quickai_base_url"], quickai_key, config["image_model"]).list_models()
            if config["image_model"] not in image_models:
                raise SkillError(f"configured image model is not advertised by QuickAI: {config['image_model']}")
            connection["quickai"] = "ok"
        if quickainew_key:
            video_models = QuickAINewVideoClient(config["quickainew_base_url"], quickainew_key, config["video_model"]).list_models()
            if config["video_model"] not in video_models:
                raise SkillError(f"configured video model is not advertised by QuickAI New: {config['video_model']}")
            connection["quickainew"] = "ok"
    save_settings(config, quickai_key, quickainew_key, store_secrets=not args.environment_only)
    return {
        "configured": str(config_path()),
        "secret_provider": "environment" if args.environment_only else "windows-dpapi",
        "credential_source": credential_source,
        "connection": connection,
    }


def status_summary(root: Path) -> dict[str, Any]:
    project = load_project(root)
    state = load_state(root)
    shots = []
    for shot in project.get("shots", []):
        if not isinstance(shot, dict):
            continue
        runtime = shot_state(state, str(shot.get("id", "")))
        shots.append(
            {
                "id": shot.get("id"),
                "image": {key: runtime["image"].get(key) for key in ("status", "path", "attempts", "error") if runtime["image"].get(key) not in (None, "")},
                "video": {
                    key: runtime["video"].get(key)
                    for key in ("status", "task_id", "path", "attempts", "progress", "mode", "provider", "model", "seconds", "resolution", "aspect_ratio", "reference_count", "qa", "history", "error")
                    if runtime["video"].get(key) not in (None, "")
                },
            }
        )
    master = state.get("character_master", {})
    return {
        "project": str(root.resolve()),
        "title": project.get("title", ""),
        "workflow": project.get("workflow", "general-video"),
        "video_mode": video_mode(project),
        "video_provider": str(project.get("video_provider", "")) or None,
        "character_master": {key: master.get(key) for key in ("status", "path", "source", "attempts", "error") if master.get(key) not in (None, "")},
        "shots": shots,
        "deliverables": state.get("deliverables", {}),
        "budget_usage": state.get("budget_usage", {}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create resumable QuickAI and Grok video projects.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("version", help="Print the installed Skill version.")
    commands.add_parser("capabilities", help="List editable workflow titles for conversational selection.")
    describe = commands.add_parser("describe", help="Show one workflow's planning and prompt guidance.")
    describe.add_argument("workflow")

    setup = commands.add_parser("configure", help="Securely configure direct provider credentials.")
    setup.add_argument("--quickai-base-url", default=DEFAULT_QUICKAI_URL)
    setup.add_argument("--quickainew-base-url", default=DEFAULT_QUICKAINEW_URL)
    setup.add_argument("--image-model", default=DEFAULT_IMAGE_MODEL)
    setup.add_argument("--video-model", default=DEFAULT_VIDEO_MODEL)
    setup.add_argument("--video-provider", choices=("quickai", "quickainew"), default="quickai")
    setup.add_argument("--environment-only", action="store_true", help="Do not persist secrets; require environment variables at runtime.")
    setup.add_argument(
        "--credentials-stdin",
        action="store_true",
        help="Read one non-echoed JSON credential payload from stdin for Codex-managed installation.",
    )
    setup.add_argument("--skip-test", action="store_true")

    commands.add_parser("doctor", help="Check credentials, model routing, and ffmpeg without generating media.")

    init = commands.add_parser("init", help="Create a project contract and durable state.")
    init.add_argument("project", type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--topic", required=True)
    init.add_argument("--workflow", default="general-video")
    init.add_argument("--shots", type=int)
    init.add_argument("--target-seconds", type=int)
    init.add_argument("--video-size", default="1280x720")
    init.add_argument("--mode", choices=("text-to-video", "image-to-video"))
    init.add_argument("--video-provider", choices=("quickai", "quickainew"))
    init.add_argument("--video-resolution", choices=tuple(sorted(VIDEO_RESOLUTIONS)), default="480p")
    init.add_argument("--aspect-ratio", choices=tuple(sorted(ASPECT_RATIOS)), default="16:9")
    init.add_argument("--seconds", type=int)

    for name in ("validate", "preflight", "status", "assemble", "audit"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)

    qa = commands.add_parser("qa", help="Run technical media QA and emit the required human review checklist.")
    qa.add_argument("project", type=Path)
    qa.add_argument("--strict", action="store_true", help="Return a failing exit code when technical QA fails.")

    character = commands.add_parser("generate-character")
    character.add_argument("project", type=Path)
    character.add_argument("--retry-failed", action="store_true")
    character.add_argument("--retry-reason", default="")
    character.add_argument("--progress", action="store_true")

    images = commands.add_parser("generate-images")
    images.add_argument("project", type=Path)
    images.add_argument("--retry-failed", action="store_true")
    images.add_argument("--retry-reason", default="")
    images.add_argument("--progress", action="store_true")
    images.add_argument("--shot", action="append", help="Generate only this shot id; repeat for multiple shots.")

    for name in ("generate-videos", "resume"):
        videos = commands.add_parser(name)
        videos.add_argument("project", type=Path)
        videos.add_argument("--retry-failed", action="store_true")
        videos.add_argument("--retry-reason", default="")
        videos.add_argument("--progress", action="store_true", help="Write JSONL progress events to stderr.")
        videos.add_argument("--poll-timeout", type=int, default=1800)
        videos.add_argument("--shot", action="append", help="Process only this shot id; repeat for multiple shots.")

    run = commands.add_parser("run")
    run.add_argument("project", type=Path)
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--retry-reason", default="")
    run.add_argument("--progress", action="store_true", help="Write JSONL progress events to stderr.")
    run.add_argument("--poll-timeout", type=int, default=1800)
    run.add_argument("--no-assemble", action="store_true")
    run.add_argument("--shot", action="append", help="Process only this shot id; partial runs do not auto-assemble.")

    assemble_files = commands.add_parser("assemble-files", help="Normalize and assemble existing MP4 clips in the supplied order.")
    assemble_files.add_argument("output", type=Path)
    assemble_files.add_argument("clips", type=Path, nargs="+")
    assemble_files.add_argument("--target-size", default="auto")
    assemble_files.add_argument("--audio-policy", choices=("preserve", "mute"), default="preserve")

    postprocess = commands.add_parser("postprocess", help="Add optional music, voice, burned subtitles, and fades to an MP4.")
    postprocess.add_argument("input", type=Path)
    postprocess.add_argument("output", type=Path)
    postprocess.add_argument("--music", type=Path)
    postprocess.add_argument("--voice", type=Path)
    postprocess.add_argument("--subtitles", type=Path)
    postprocess.add_argument("--fade-seconds", type=float, default=0.0)

    cover = commands.add_parser("cover", help="Export a representative frame as a JPG or PNG cover.")
    cover.add_argument("input", type=Path)
    cover.add_argument("output", type=Path)
    cover.add_argument("--at-seconds", type=float, default=0.5)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "version":
            print_json({"ok": True, "version": SKILL_VERSION})
            return 0
        if args.command == "capabilities":
            print_json({"ok": True, "version": SKILL_VERSION, "workflows": workflow_catalog(), "selection": "Reply with a workflow id or title."})
            return 0
        if args.command == "describe":
            print_json({"ok": True, "workflow": get_workflow(args.workflow)})
            return 0
        if args.command == "configure":
            print_json({"ok": True, **configure(args)})
            return 0
        if args.command == "doctor":
            code, result = doctor()
            print_json(result)
            return code
        if args.command == "init":
            workflow = get_workflow(args.workflow)
            durations = plan_shot_durations(
                workflow,
                shot_count=args.shots,
                seconds=args.seconds,
                target_seconds=args.target_seconds,
            )
            if args.video_size != "auto" and not SIZE_RE.fullmatch(args.video_size):
                raise SkillError("--video-size must be WIDTHxHEIGHT or auto")
            selected_mode = args.mode or ("text-to-video" if args.workflow == "text-to-video" else "image-to-video")
            selected_provider = args.video_provider or ("quickai" if selected_mode == "text-to-video" else "quickainew")
            path = init_project(
                args.project,
                args.title,
                args.topic,
                args.workflow,
                durations,
                args.video_size,
                selected_mode,
                selected_provider,
                args.video_resolution,
                args.aspect_ratio,
            )
            print_json({"ok": True, "project": str(path), "workflow": args.workflow, "shot_seconds": durations, "target_seconds": sum(durations)})
            return 0
        if args.command == "assemble-files":
            clips = [path.resolve() for path in args.clips]
            for clip in clips:
                if not clip.is_file():
                    raise SkillError(f"clip does not exist: {clip}")
            media = assemble_clips(clips, args.output.resolve(), target_size=args.target_size, audio_policy=args.audio_policy)
            print_json({"ok": True, "output": str(args.output.resolve()), "media": media})
            return 0
        if args.command == "postprocess":
            input_path = args.input.resolve()
            if not input_path.is_file():
                raise SkillError(f"input video does not exist: {input_path}")
            media = postprocess_video(
                input_path,
                args.output.resolve(),
                music=args.music.resolve() if args.music else None,
                voice=args.voice.resolve() if args.voice else None,
                subtitles=args.subtitles.resolve() if args.subtitles else None,
                fade_seconds=args.fade_seconds,
            )
            print_json({"ok": True, "output": str(args.output.resolve()), "media": media})
            return 0
        if args.command == "cover":
            input_path = args.input.resolve()
            if not input_path.is_file():
                raise SkillError(f"input video does not exist: {input_path}")
            result = extract_cover(input_path, args.output.resolve(), at_seconds=args.at_seconds)
            print_json({"ok": True, "cover": result})
            return 0
        root = args.project.resolve()
        if args.command in {"validate", "preflight"}:
            project = load_project(root)
            errors = validate_project(root, project)
            print_json(
                {
                    "ok": not errors,
                    "project": str(root),
                    "errors": errors,
                    "preflight": preflight_report(project),
                    "audit": audit_project(root, project),
                }
            )
            return 0 if not errors else 1
        if args.command == "status":
            print_json({"ok": True, **status_summary(root)})
            return 0
        if args.command == "audit":
            result = audit_project(root, load_project(root))
            print_json({"project": str(root), **result})
            return 0 if result["ok"] else 1
        if args.command == "qa":
            result = qa_project(root)
            print_json({"project": str(root), **result})
            return 0 if result["ok"] or not args.strict else 1
        if args.command == "generate-character":
            print_json(
                {
                    "ok": True,
                    "character_master": generate_character_master(
                        root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                    ),
                }
            )
            return 0
        if args.command == "generate-images":
            print_json(
                {
                    "ok": True,
                    "images": generate_images(
                        root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                        shot_ids=args.shot,
                    ),
                }
            )
            return 0
        if args.command in {"generate-videos", "resume"}:
            print_json(
                {
                    "ok": True,
                    "videos": generate_videos(
                        root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                        poll_timeout=args.poll_timeout,
                        shot_ids=args.shot,
                    ),
                }
            )
            return 0
        if args.command == "assemble":
            output, media = assemble(root)
            print_json({"ok": True, "output": str(output), "media": media})
            return 0
        if args.command == "run":
            character_result = generate_character_master(
                root,
                retry_failed=args.retry_failed,
                retry_reason=args.retry_reason,
                progress=args.progress,
            )
            image_result = generate_images(
                root,
                retry_failed=args.retry_failed,
                retry_reason=args.retry_reason,
                progress=args.progress,
                shot_ids=args.shot,
            )
            video_result = generate_videos(
                root,
                retry_failed=args.retry_failed,
                retry_reason=args.retry_reason,
                progress=args.progress,
                poll_timeout=args.poll_timeout,
                shot_ids=args.shot,
            )
            assembled = None if args.no_assemble or args.shot else assemble(root)
            print_json(
                {
                    "ok": True,
                    "character_master": character_result,
                    "images": image_result,
                    "videos": video_result,
                    "output": str(assembled[0]) if assembled else "",
                    "media": assembled[1] if assembled else {},
                }
            )
            return 0
        raise SkillError("unknown command")
    except (OSError, ValueError, SkillError, APIError, subprocess.SubprocessError) as error:
        print_json({"ok": False, "error": str(error)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
