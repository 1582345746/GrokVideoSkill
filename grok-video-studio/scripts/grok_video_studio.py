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
import uuid
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
    atomic_write_bytes,
    atomic_write_json,
    configure_utf8_stdio,
    config_path,
    load_settings,
    locked_project_state,
    normalize_base_url,
    print_json,
    read_json,
    redact,
    save_settings,
)
from component_manager import (
    component_plan,
    component_status,
    install_component_sources,
    load_component_settings,
    save_component_settings,
    setup_component_runtimes,
    start_components,
    stop_components,
)
from install_profiles import INSTALL_PROFILE_IDS, install_profile_plan, install_profile_settings_path, load_install_profile, save_install_profile
from dialogue_workflow import (
    DIALOGUE_MODES,
    SUBTITLE_SOURCES,
    audio_config,
    dialogue_preflight,
    dialogue_prompt,
    dialogue_subtitle_cues,
    render_local_dialogue,
    validate_dialogue,
)
from media_client import (
    QuickAIImageClient,
    QuickAINewVideoClient,
    QuickAIVideoClient,
    VIDEO_RESOLUTIONS,
    image_reference_report,
    save_image_bytes,
)
from media_tools import SUBTITLE_STYLES, export_review_frames, extract_cover, postprocess_video, quality_report
from news_workflow import create_news_contract, load_news_contract, news_context, validate_news_contract
from provider_contracts import (
    PROVIDER_CAPABILITIES,
    REPAIRABLE_INPUT_CATEGORIES,
    ProviderTaskFailedError,
    allows_automatic_failover,
    classify_provider_error,
    task_progress,
)
from series_workflow import (
    accept_episode,
    approve_episode,
    begin_episode,
    create_series_contract,
    episode_records,
    episode_root,
    fail_episode_generation,
    finish_episode_generation,
    generate_series_characters,
    get_episode,
    load_series,
    load_series_state,
    select_next_episode,
    series_context,
    series_character_master_config,
    series_character_preflight,
    series_status,
    sync_all_episode_contracts,
    sync_approved_episode_voices,
    sync_episode_contract,
    validate_series,
)
from voice_workflow import (
    audition_voice,
    import_voice_candidate,
    list_provider_voices,
    review_voice_candidate,
    voice_catalog_summary,
    voice_doctor,
)
from voice_setup import voicebox_setup_plan


SKILL_VERSION = "2.0.1"
PROJECT_VERSION = 1
STATE_VERSION = 1
MAX_VIDEO_SECONDS = 15
HARD_PROMPT_BYTES = 4096
SAFE_PROMPT_BYTES = 3800
# Compatibility aliases for external callers that imported the v1 names.
HARD_PROMPT_CHARS = HARD_PROMPT_BYTES
SAFE_PROMPT_CHARS = SAFE_PROMPT_BYTES
MAX_CREDENTIAL_PAYLOAD_CHARS = 32768
SHOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SIZE_RE = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")
ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "2:3", "3:2"}
VIDEO_AUDIO_POLICIES = {"preserve", "mute"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SHOT_ROLES = {
    "establishing",
    "wide",
    "medium",
    "closeup",
    "over_shoulder",
    "insert",
    "reaction",
    "transition",
    "ending_hook",
}
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
            if normalized in {
                "api_key",
                "quickai_key",
                "quickainew_key",
                "quickai_image_key",
                "quickai_video_key",
                "quickainew_video_key",
                "authorization",
                "secret",
            }:
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
    return str((settings or {}).get("default_video_provider", "quickai"))


def video_provider_policy(project: dict[str, Any]) -> str:
    value = str(project.get("video_provider_policy", "")).strip().lower()
    if value:
        return value
    return "fixed" if video_provider(project) == "quickainew" else "automatic"


def configured_default_video_provider() -> str:
    path = config_path()
    if not path.is_file():
        return "quickai"
    try:
        value = str(read_json(path).get("default_video_provider", "quickai")).strip().lower()
    except SkillError:
        return "quickai"
    return value if value in PROVIDER_CAPABILITIES else "quickai"


def resolve_video_provider_options(provider: str | None, policy: str | None) -> tuple[str, str]:
    selected_provider = str(provider or configured_default_video_provider()).strip().lower()
    selected_policy = str(policy or "").strip().lower()
    if not selected_policy:
        selected_policy = "fixed" if provider or selected_provider == "quickainew" else "automatic"
    return selected_provider, selected_policy


def provider_capability_report(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for provider, capabilities in PROVIDER_CAPABILITIES.items():
        video_configured = bool(settings.get(capabilities.credential_role))
        value = {
            "title": capabilities.title,
            "text_to_image": capabilities.text_to_image,
            "text_to_video": capabilities.text_to_video,
            "image_to_video": capabilities.image_to_video,
            "video_reference": capabilities.video_reference,
            "video_edit": capabilities.video_edit,
            "video_extend": capabilities.video_extend,
            "audio_generation": capabilities.audio_generation,
            "preset_voice_reference": capabilities.preset_voice_reference,
            "audio_file_reference": capabilities.audio_file_reference,
            "priority": capabilities.priority,
            "video_configured": video_configured,
        }
        if provider == "quickai":
            value["image_configured"] = bool(settings.get("quickai_image_key"))
        report[provider] = value
    return report


def video_provider_candidates(project: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    preferred = video_provider(project, settings)
    policy = video_provider_policy(project)
    mode = video_mode(project)
    capability_name = "text_to_video" if mode == "text-to-video" else "image_to_video"
    if preferred not in PROVIDER_CAPABILITIES:
        raise SkillError("video_provider must be quickai or quickainew")
    if not getattr(PROVIDER_CAPABILITIES[preferred], capability_name):
        raise SkillError(f"{PROVIDER_CAPABILITIES[preferred].title} does not support {mode}")

    if policy == "fixed":
        credential_role = PROVIDER_CAPABILITIES[preferred].credential_role
        if settings.get(credential_role):
            return [preferred]
        raise SkillError(f"{PROVIDER_CAPABILITIES[preferred].title} 视频 Key 未配置，无法执行固定提供方视频任务")
    if policy != "automatic":
        raise SkillError("video_provider_policy must be automatic or fixed")

    if preferred == "quickainew":
        if settings.get("quickainew_video_key"):
            return ["quickainew"]
        raise SkillError("QuickAI New 视频 Key 未配置，无法执行明确指定的 QuickAI New 视频任务")

    candidates = []
    if settings.get("quickai_video_key"):
        candidates.append("quickai")
    if settings.get("quickainew_video_key"):
        candidates.append("quickainew")
    if not candidates:
        raise SkillError("未配置可用的视频 Key：请配置 QuickAI 视频 Key 或 QuickAI New 视频 Key")
    return candidates


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
    audio = project.get("audio") if isinstance(project.get("audio"), dict) else {}
    if str(audio.get("mode", "")).strip().lower() == "mute":
        return "mute"
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


def max_total_attempts(project: dict[str, Any]) -> int:
    value = project.get("retry_policy") if isinstance(project.get("retry_policy"), dict) else {}
    try:
        attempts = int(value.get("max_total_attempts", 3))
    except (TypeError, ValueError) as error:
        raise SkillError("retry_policy.max_total_attempts must be a positive integer") from error
    if attempts < 1:
        raise SkillError("retry_policy.max_total_attempts must be a positive integer")
    return attempts


def project_prompt_limit(project: dict[str, Any]) -> int:
    limits = project.get("limits") if isinstance(project.get("limits"), dict) else {}
    raw = limits.get("max_prompt_bytes", limits.get("max_prompt_chars", HARD_PROMPT_BYTES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return HARD_PROMPT_BYTES
    return max(1, min(value, HARD_PROMPT_BYTES))


def next_prompt_version(variants: dict[str, str], current: str, *, hard_limit: int = HARD_PROMPT_BYTES) -> str:
    order = ("full", "compact", "minimal")
    try:
        index = order.index(current)
    except ValueError:
        index = -1
    for candidate in order[index + 1:]:
        if prompt_bytes(variants.get(candidate, "")) <= hard_limit:
            return candidate
    return "minimal"


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
        "request_id": runtime.get("request_id", ""),
        "attempt_id": runtime.get("attempt_id", ""),
        "provider": runtime.get("provider", ""),
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


def _unsupported_reference_errors(root: Path, references: Any, prefix: str, max_references: int) -> list[str]:
    """Reject non-image media instead of silently treating it as an I2V image."""
    errors = _reference_errors(root, references, prefix, max_references)
    if not isinstance(references, list):
        return errors
    for reference in references:
        try:
            path = resolve_project_path(root, reference)
        except SkillError:
            continue
        suffix = path.suffix.lower()
        if suffix in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
            errors.append(f"{prefix} does not support video reference {reference}; use video-edit or video-extend when implemented")
        elif suffix in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}:
            errors.append(f"{prefix} does not support audio reference {reference}; use preset_voice_reference or audio_file_reference when implemented")
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
    policy = str(project.get("video_provider_policy", "automatic" if provider != "quickainew" else "fixed")).strip()
    if policy not in {"automatic", "fixed"}:
        errors.append("project.video_provider_policy must be automatic or fixed")
    if project.get("allow_ui_elements") is not None and not isinstance(project.get("allow_ui_elements"), bool):
        errors.append("project.allow_ui_elements must be a boolean")
    if audio_policy(project) not in VIDEO_AUDIO_POLICIES:
        errors.append("defaults.audio_policy must be preserve or mute")
    errors.extend(validate_dialogue(root, project))
    shots = project.get("shots")
    if not isinstance(shots, list) or not shots:
        errors.append("project.shots must be a non-empty array")
        return errors
    limits = project.get("limits") if isinstance(project.get("limits"), dict) else {}
    try:
        total_attempts = max_total_attempts(project)
    except SkillError as error:
        total_attempts = 3
        errors.append(str(error))
    retry_policy = project.get("retry_policy") if isinstance(project.get("retry_policy"), dict) else {}
    try:
        max_retries = int(retry_policy.get("max_retries", total_attempts - 1))
        if max_retries < 0:
            raise ValueError
    except (TypeError, ValueError):
        max_retries = total_attempts - 1
        errors.append("retry_policy.max_retries must be a non-negative integer")
    if max_retries + 1 > total_attempts:
        errors.append("retry_policy.max_total_attempts must be at least max_retries + 1 (three retries require four total attempts)")
    max_images = _positive_limit(limits, "max_image_requests", 12, errors)
    max_videos = _positive_limit(limits, "max_video_requests", 8, errors)
    max_seconds = _positive_limit(limits, "max_total_video_seconds", 60, errors)
    max_references = _positive_limit(limits, "max_reference_images", 9, errors)
    # v1 projects may still carry max_prompt_chars. It is accepted as a
    # migration alias, but the value is enforced against UTF-8 bytes in v2.
    prompt_limit_key = "max_prompt_bytes" if "max_prompt_bytes" in limits else "max_prompt_chars"
    max_prompt_bytes = _positive_limit(limits, prompt_limit_key, HARD_PROMPT_BYTES, errors)
    if max_prompt_bytes > HARD_PROMPT_BYTES:
        errors.append(f"limits.{prompt_limit_key} cannot exceed provider limit {HARD_PROMPT_BYTES} UTF-8 bytes")
        max_prompt_bytes = HARD_PROMPT_BYTES

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
                try:
                    select_prompt_variant(prompt_variants(project, kind="character_master"), hard_limit=max_prompt_bytes)
                except SkillError as error:
                    errors.append(f"character_master prompt: {error}")
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
        shot_role = str(raw_shot.get("shot_role", "")).strip().lower()
        if shot_role and shot_role not in SHOT_ROLES:
            errors.append(f"{prefix}.shot_role must be one of {', '.join(sorted(SHOT_ROLES))}")
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
        if raw_shot.get("narration") is not None and not isinstance(raw_shot.get("narration"), str):
            errors.append(f"{prefix}.narration must be a string")
        if raw_shot.get("subtitle") is not None and not isinstance(raw_shot.get("subtitle"), str):
            errors.append(f"{prefix}.subtitle must be a string")
        for field in ("location", "time", "weather", "lighting", "camera", "camera_motion", "environment_motion", "ending_pose", "audio_notes"):
            if raw_shot.get(field) is not None and not isinstance(raw_shot.get(field), (str, list)):
                errors.append(f"{prefix}.{field} must be a string or array")
        if raw_shot.get("props") is not None and not isinstance(raw_shot.get("props"), (str, list)):
            errors.append(f"{prefix}.props must be a string or array")
        if raw_shot.get("environment_sound") is not None and not isinstance(raw_shot.get("environment_sound"), (str, list)):
            errors.append(f"{prefix}.environment_sound must be a string or array")
        if raw_shot.get("sound_effects") is not None and not isinstance(raw_shot.get("sound_effects"), (str, list)):
            errors.append(f"{prefix}.sound_effects must be a string or array")
        subtitle_items = raw_shot.get("subtitles")
        if subtitle_items is not None:
            if raw_shot.get("subtitle"):
                errors.append(f"{prefix} cannot use both subtitle and subtitles")
            if not isinstance(subtitle_items, list):
                errors.append(f"{prefix}.subtitles must be an array")
            else:
                previous_end = 0.0
                shot_seconds = shot_value(project, raw_shot, "seconds", 6)
                for cue_index, cue in enumerate(subtitle_items):
                    cue_prefix = f"{prefix}.subtitles[{cue_index}]"
                    if not isinstance(cue, dict):
                        errors.append(f"{cue_prefix} must be an object")
                        continue
                    if not str(cue.get("text", "")).strip():
                        errors.append(f"{cue_prefix}.text is required")
                    try:
                        start = float(cue.get("start"))
                        end = float(cue.get("end"))
                    except (TypeError, ValueError):
                        errors.append(f"{cue_prefix}.start and end must be seconds")
                        continue
                    if start < 0 or end <= start or not isinstance(shot_seconds, int) or end > shot_seconds:
                        errors.append(f"{cue_prefix} must satisfy 0 <= start < end <= shot seconds")
                    if start < previous_end:
                        errors.append(f"{cue_prefix} overlaps the previous subtitle cue")
                    previous_end = max(previous_end, end)
        if str(raw_shot.get("image_prompt", "")).strip():
            try:
                select_prompt_variant(prompt_variants(project, raw_shot, kind="image"), hard_limit=max_prompt_bytes)
            except SkillError as error:
                errors.append(f"{prefix} image prompt: {error}")
        if str(raw_shot.get("video_prompt", "")).strip():
            try:
                select_prompt_variant(prompt_variants(project, raw_shot, kind="video"), hard_limit=max_prompt_bytes)
            except SkillError as error:
                errors.append(f"{prefix} video prompt: {error}")
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
            errors.extend(_unsupported_reference_errors(root, references, f"{prefix}.{ref_name}", max_references))
        if bool(raw_shot.get("generate_image", True)) and isinstance(shot_character_ids, list):
            by_character_id = {
                str(character.get("id", "")): character for character in raw_characters if isinstance(character, dict)
            }
            combined = [
                str(value)
                for character_id in shot_character_ids
                for value in (
                    by_character_id.get(str(character_id), {}).get("references", [])
                    if isinstance(by_character_id.get(str(character_id), {}).get("references", []), list)
                    else []
                )
            ]
            image_references = raw_shot.get("image_references", [])
            combined.extend(str(value) for value in image_references if isinstance(image_references, list))
            if bool(raw_shot.get("use_character_master", False)):
                combined.append(str(master.get("path", "")))
            if len(dict.fromkeys(value for value in combined if value)) > max_references:
                errors.append(f"{prefix} combined character and image references exceed max_reference_images {max_references}")
    if total_seconds > max_seconds:
        errors.append(f"total video seconds {total_seconds} exceeds max_total_video_seconds {max_seconds}")
    if project.get("workflow") == "news-video":
        errors.extend(validate_news_contract(root, project))
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
    for key, label in (
        ("shot_role", "Shot role"),
        ("location", "Location"),
        ("time", "Time"),
        ("weather", "Weather"),
        ("lighting", "Lighting"),
        ("props", "Props"),
        ("camera", "Camera"),
        ("camera_motion", "Camera motion"),
        ("environment_motion", "Environment motion"),
        ("ending_pose", "Ending pose"),
    ):
        value = shot.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if str(value or "").strip() and not (key == "scene_id"):
            lines.append(f"{label}: {str(value).strip()}")
    if continuity:
        lines.append(f"Continuity: {continuity}")
    return "\n".join(lines)


def episode_continuity_context(project: dict[str, Any]) -> str:
    value = project.get("series_context") if isinstance(project.get("series_context"), dict) else {}
    sections = []
    continuity_in = str(value.get("continuity_in", "")).strip()
    previous = str(value.get("previous_episode_continuity", "")).strip()
    if previous:
        sections.append("Reviewed previous episode end state: " + previous)
    if continuity_in:
        sections.append("Required current episode starting state: " + continuity_in)
    return "\n".join(sections)


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
    episode_continuity = episode_continuity_context(project)
    if episode_continuity:
        sections.append("[EPISODE CONTINUITY]\n" + episode_continuity)
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
    structured = structured_shot_context(project, shot)
    requested_characters = shot.get("character_ids", []) if isinstance(shot.get("character_ids", []), list) else []
    if character:
        identity_lock = "Same character every shot; preserve face, hair, body, clothes, props, and colors."
        if not requested_characters or not project_characters(project):
            identity_lock += "\n" + character
        sections.append("[IDENTITY LOCK]\n" + identity_lock)
    if style:
        sections.append("[STYLE LOCK]\n" + style)
    if structured:
        sections.append("[SHOT CONTINUITY]\n" + structured)
    episode_continuity = episode_continuity_context(project)
    if episode_continuity:
        sections.append("[EPISODE CONTINUITY]\n" + episode_continuity)
    if not allow_ui_elements(project, shot):
        sections.append(
            "[CLEAN FRAME POLICY]\n"
            "Clean cinematic frame. No app UI, controls, overlays, text, logos, watermarks, captions, counters, comments, or stickers anywhere in frame."
        )
    spoken = dialogue_prompt(project, shot)
    if spoken:
        sections.append("[DIALOGUE]\n" + spoken)
    sound_parts = []
    for key, label in (("environment_sound", "Environment sound"), ("sound_effects", "Sound effects"), ("audio_notes", "Audio design")):
        value = shot.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if str(value or "").strip():
            sound_parts.append(f"{label}: {str(value).strip()}")
    if sound_parts and audio_config(project)["mode"] == "native-dialogue":
        sections.append("[AUDIO DESIGN]\n" + "\n".join(sound_parts))
    sections.append("[SHOT MOTION]\n" + str(shot["video_prompt"]).strip())
    return "\n\n".join(sections)


def prompt_bytes(value: str) -> int:
    return len(str(value).encode("utf-8"))


def _compact_character_identity(project: dict[str, Any], shot: dict[str, Any] | None = None) -> str:
    requested = set()
    if isinstance(shot, dict) and isinstance(shot.get("character_ids"), list):
        requested = {str(item) for item in shot["character_ids"]}
    characters = project_characters(project)
    selected = [item for item in characters if not requested or str(item.get("id", "")) in requested]
    if selected:
        values = []
        for item in selected:
            identity = str(item.get("identity", "")).strip()
            wardrobe = str(item.get("wardrobe", "")).strip()
            suffix = f"; wardrobe: {wardrobe}" if wardrobe else ""
            if identity:
                values.append(f"{item.get('name', item.get('id', 'character'))}: {identity}{suffix}")
        if values:
            return " | ".join(values)
    return str(project.get("character_bible", "")).strip()


def _compact_shot_facts(project: dict[str, Any], shot: dict[str, Any], *, kind: str) -> list[str]:
    facts: list[str] = []
    identity = _compact_character_identity(project, shot)
    if identity:
        facts.append("Identity: " + identity)
    for key, label in (
        ("scene_id", "Location"),
        ("location", "Location"),
        ("time", "Time"),
        ("weather", "Weather"),
        ("lighting", "Lighting"),
        ("props", "Props"),
        ("shot_role", "Shot role"),
        ("camera", "Camera"),
        ("camera_motion", "Camera motion"),
        ("environment_motion", "Environment motion"),
        ("ending_pose", "Ending pose"),
        ("continuity_notes", "Continuity"),
    ):
        value = shot.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if str(value or "").strip():
            facts.append(f"{label}: {str(value).strip()}")
    source_key = "image_prompt" if kind == "image" else "video_prompt"
    if str(shot.get("summary", "")).strip():
        facts.append("Action: " + str(shot["summary"]).strip())
    if str(shot.get(source_key, "")).strip():
        facts.append(("Keyframe: " if kind == "image" else "Motion: ") + str(shot[source_key]).strip())
    spoken = dialogue_prompt(project, shot) if kind == "video" else ""
    if spoken:
        facts.append("Dialogue and sound: " + spoken)
    return facts


def prompt_variants(project: dict[str, Any], shot: dict[str, Any] | None = None, *, kind: str) -> dict[str, str]:
    """Build deterministic full/compact/minimal prompts without truncating text."""
    if kind not in {"image", "video", "character_master"}:
        raise SkillError("prompt kind must be image, video, or character_master")
    if kind == "character_master":
        full = composed_character_prompt(project)
        identity = _compact_character_identity(project)
        master = character_master_config(project)
        compact = "\n\n".join(item for item in (
            "[CHARACTER IDENTITY]\n" + identity if identity else "",
            "[MASTER]\n" + str(master.get("prompt", "")).strip(),
            "Clean single-sheet turnaround, one person only, consistent face, hair, clothing, body proportions, and signature props.",
        ) if item)
        minimal = "One clean single-sheet character master: " + (identity or str(master.get("prompt", "")).strip())
        return {"full": full, "compact": compact, "minimal": minimal}
    if shot is None:
        raise SkillError("shot is required for image and video prompts")
    full = composed_image_prompt(project, shot) if kind == "image" else composed_video_prompt(project, shot)
    facts = _compact_shot_facts(project, shot, kind=kind)
    compact = "\n".join(facts)
    # A minimal retry keeps the fields that determine continuity and intent;
    # it never takes an arbitrary character slice that could drop identity or
    # the actual action.
    def first_fact(*prefixes: str) -> str:
        return next((item for item in facts if item.startswith(prefixes)), "")

    mandatory = [
        first_fact("Identity:"),
        first_fact("Location:"),
        first_fact("Action:", "Motion:", "Keyframe:"),
        first_fact("Dialogue and sound:"),
        first_fact("Ending pose:"),
        first_fact("Camera:", "Camera motion:"),
    ]
    mandatory = [item for item in mandatory if item]
    if not mandatory:
        mandatory = ["Character identity, location, core action, and composition remain consistent."]
    minimal = " ".join(dict.fromkeys(item for item in mandatory if item.strip()))
    if kind == "video":
        minimal += " One continuous motion; preserve the ending pose; clean frame with no captions, logos, watermarks, or UI."
    return {"full": full, "compact": compact, "minimal": minimal}


def select_prompt_variant(variants: dict[str, str], *, hard_limit: int = HARD_PROMPT_BYTES, safe_limit: int = SAFE_PROMPT_BYTES, preferred: str = "") -> tuple[str, str]:
    order = [name for name in ("full", "compact", "minimal") if name in variants]
    if preferred in order:
        order = order[order.index(preferred):]
    for name in order:
        value = variants.get(name, "")
        if prompt_bytes(value) <= safe_limit:
            return name, value
    minimal = variants.get("minimal", "")
    if prompt_bytes(minimal) <= hard_limit:
        return "minimal", minimal
    raise SkillError(f"minimum prompt exceeds {hard_limit} UTF-8 bytes; shorten the character, location, action, dialogue, or ending pose")


def prompt_budget_entry(kind: str, identifier: str, variants: dict[str, str], *, hard_limit: int) -> dict[str, Any]:
    selection_error = ""
    try:
        selected, _ = select_prompt_variant(variants, hard_limit=hard_limit)
    except SkillError as error:
        # Preflight must remain a report even when the smallest version cannot
        # fit; generation/validation will still block the request.
        selected = "minimal" if "minimal" in variants else next(iter(variants), "")
        selection_error = str(error)
    values = {
        name: {"characters": len(value), "utf8_bytes": prompt_bytes(value), "text": value}
        for name, value in variants.items()
    }
    return {
        "kind": kind,
        "id": identifier,
        "versions": values,
        "selected_version": selected,
        "characters": values[selected]["characters"],
        "utf8_bytes": values[selected]["utf8_bytes"],
        "hard_limit": hard_limit,
        "safe_limit": SAFE_PROMPT_BYTES,
        "remaining": hard_limit - values[selected]["utf8_bytes"],
        "within_hard_limit": values[selected]["utf8_bytes"] <= hard_limit,
        "compression_needed": selected != "full",
        "compression_suggestion": (
            f"use {selected} version; remove season-wide and unrelated scene detail while retaining identity, location, action, dialogue, and ending pose"
            if selected != "full"
            else "full prompt fits the safe provider budget"
        ),
        "selection_error": selection_error,
    }


def shot_character_references(root: Path, project: dict[str, Any], shot: dict[str, Any]) -> list[Path]:
    requested = shot.get("character_ids", []) if isinstance(shot.get("character_ids", []), list) else []
    by_id = {str(item.get("id", "")): item for item in project_characters(project)}
    references: list[Path] = []
    for character_id in requested:
        character = by_id.get(str(character_id))
        if not character:
            continue
        for value in character.get("references", []):
            path = resolve_project_path(root, str(value))
            if path not in references:
                references.append(path)
    return references


def preflight_report(project: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    master = character_master_config(project)
    prompt_limit = project_prompt_limit(project)
    prompts: list[dict[str, Any]] = []
    references_report: list[dict[str, Any]] = []
    warnings: list[str] = []
    state_for_references = load_state(root) if root is not None else None
    if bool(master.get("enabled", False)) and bool(master.get("generate", False)) and str(master.get("prompt", "")).strip():
        prompts.append(prompt_budget_entry("character_master", "character-master", prompt_variants(project, kind="character_master"), hard_limit=prompt_limit))
    total_seconds = 0
    image_requests = int(bool(master.get("enabled", False)) and bool(master.get("generate", False)))
    for shot in project.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("id", ""))
        if bool(shot.get("generate_image", True)):
            image_requests += 1
            image_size = str(shot_value(project, shot, "image_size", "1024x1024"))
            aspect_ratio = video_aspect_ratio(project, shot)
            if image_size != "auto" and SIZE_RE.fullmatch(image_size) and aspect_ratio in ASPECT_RATIOS:
                width, height = (int(item) for item in image_size.split("x", 1))
                expected = image_size_for_aspect_ratio(aspect_ratio)
                expected_width, expected_height = (int(item) for item in expected.split("x", 1))
                orientation_matches = (width > height) == (expected_width > expected_height) and (width < height) == (
                    expected_width < expected_height
                )
                if not orientation_matches:
                    warnings.append(
                        f"{shot_id}: image_size {image_size} orientation does not match video aspect ratio {aspect_ratio}; "
                        f"use {expected} to reduce keyframe cropping and composition drift"
                    )
        video_size = str(shot_value(project, shot, "video_size", "1280x720"))
        aspect_ratio = video_aspect_ratio(project, shot)
        if video_size != "auto" and SIZE_RE.fullmatch(video_size) and aspect_ratio in ASPECT_RATIOS:
            width, height = (int(item) for item in video_size.split("x", 1))
            ratio_width, ratio_height = (int(item) for item in aspect_ratio.split(":", 1))
            orientation_matches = (width > height) == (ratio_width > ratio_height) and (width < height) == (
                ratio_width < ratio_height
            )
            if not orientation_matches:
                warnings.append(
                    f"{shot_id}: video_size {video_size} orientation does not match video aspect ratio {aspect_ratio}; "
                    "QuickAI New will omit the conflicting size and use resolution plus aspect_ratio"
                )
        if str(shot.get("image_prompt", "")).strip():
            prompts.append(prompt_budget_entry("image", shot_id, prompt_variants(project, shot, kind="image"), hard_limit=prompt_limit))
        if str(shot.get("video_prompt", "")).strip():
            prompts.append(prompt_budget_entry("video", shot_id, prompt_variants(project, shot, kind="video"), hard_limit=prompt_limit))
        seconds = shot_value(project, shot, "seconds", 6)
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            total_seconds += seconds
        if bool(shot.get("use_character_master", False)) and shot.get("video_references"):
            warnings.append(f"{shot_id}: explicit video_references bypass the generated keyframe; do not send a multi-view master sheet directly to video")
        if root is not None and video_mode(project) == "image-to-video":
            reference_values = shot.get("video_references", []) if isinstance(shot.get("video_references", []), list) else []
            if not reference_values and isinstance(state_for_references, dict):
                runtime = (state_for_references.get("shots") or {}).get(shot_id, {})
                image_runtime = runtime.get("image") if isinstance(runtime, dict) else {}
                if isinstance(image_runtime, dict) and image_runtime.get("status") == "completed" and image_runtime.get("path"):
                    reference_values = [str(image_runtime["path"])]
            for reference_value in reference_values:
                try:
                    reference_path = resolve_project_path(root, str(reference_value))
                except (SkillError, TypeError):
                    continue
                reference_report = image_reference_report(reference_path, aspect_ratio)
                references_report.append({**reference_report, "shot": shot_id, "path": str(reference_value)})
                for warning in reference_report.get("warnings", []):
                    warnings.append(f"{shot_id}: {reference_value}: {warning}")
                for error in reference_report.get("errors", []):
                    warnings.append(f"{shot_id}: {reference_value}: {error}")
    for item in prompts:
        prompt_size = int(item["utf8_bytes"])
        full_size = int(item["versions"].get("full", {}).get("utf8_bytes", prompt_size))
        if item.get("selection_error"):
            warnings.append(f"{item['kind']} prompt {item['id']} cannot fit the {prompt_limit}-byte hard limit: {item['selection_error']}")
        if item.get("compression_needed"):
            warnings.append(
                f"{item['kind']} prompt {item['id']} compressed from {full_size} to {prompt_size} UTF-8 bytes ({item['selected_version']}) for provider headroom; {item['compression_suggestion']}"
            )
        elif prompt_size > SAFE_PROMPT_BYTES:
            warnings.append(
                f"{item['kind']} prompt {item['id']} uses {prompt_size} UTF-8 bytes; selected {item['selected_version']} prompt and keep it at or below {SAFE_PROMPT_BYTES} for provider headroom"
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
        "prompt_hard_limit_bytes": prompt_limit,
        "prompt_safe_limit_bytes": SAFE_PROMPT_BYTES,
        # Keep the v1 names in machine output for clients that have not yet
        # migrated; both values refer to UTF-8 byte limits in v2.
        "prompt_hard_limit": prompt_limit,
        "prompt_safe_limit": SAFE_PROMPT_BYTES,
        "prompts": prompts,
        "references": references_report,
        "budget": cost,
        "warnings": warnings,
        "dialogue": dialogue_preflight(project),
    }


def audit_project(root: Path, project: dict[str, Any]) -> dict[str, Any]:
    errors = validate_project(root, project)
    warnings: list[str] = []
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    role_counts = {role: 0 for role in sorted(SHOT_ROLES)}
    dialogue_shots = 0
    non_dialogue_shots = 0
    action_shots = 0
    characters = {str(item.get("id")): item for item in project_characters(project)}
    previous: dict[str, Any] | None = None
    for shot in shots:
        shot_id = str(shot.get("id", ""))
        role = str(shot.get("shot_role", "")).strip().lower()
        if role in role_counts:
            role_counts[role] += 1
        has_dialogue = bool(shot.get("dialogue") or shot.get("narration") or shot.get("subtitle"))
        dialogue_shots += int(has_dialogue)
        non_dialogue_shots += int(not has_dialogue)
        action_shots += int(role in {"medium", "wide", "over_shoulder", "insert"})
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
    cinematic_workflow = project.get("workflow") in {"character-consistent-story", "episodic-series"} or bool(project.get("series_context"))
    if cinematic_workflow:
        required_roles = ("establishing", "reaction", "ending_hook")
        for role in required_roles:
            if shots and role_counts[role] == 0:
                warnings.append(f"shot coverage is missing {role}; add a dedicated cinematic beat")
        if shots and non_dialogue_shots == 0:
            warnings.append("shot coverage has no non-dialogue visual beat")
        if shots and action_shots == 0:
            warnings.append("shot coverage has no action-oriented beat")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "coverage": {
            "shot_count": len(shots),
            "role_counts": role_counts,
            "dialogue_shots": dialogue_shots,
            "non_dialogue_shots": non_dialogue_shots,
            "action_shots": action_shots,
            "dialogue_ratio": round(dialogue_shots / len(shots), 3) if shots else 0.0,
        },
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
        QuickAIImageClient(settings["quickai_base_url"], settings["quickai_image_key"], settings["image_model"]),
        QuickAINewVideoClient(settings["quickainew_base_url"], settings["quickainew_video_key"], settings["video_model"]),
        settings,
    )


def select_video_client(settings: dict[str, Any], provider: str) -> QuickAIVideoClient | QuickAINewVideoClient:
    if provider == "quickai":
        if not settings.get("quickai_video_key"):
            raise SkillError("QuickAI video key is required for video_provider=quickai")
        return QuickAIVideoClient(settings["quickai_base_url"], settings["quickai_video_key"], settings["video_model"])
    if provider == "quickainew":
        if not settings.get("quickainew_video_key"):
            raise SkillError("QuickAI New video key is required for video_provider=quickainew")
        return QuickAINewVideoClient(settings["quickainew_base_url"], settings["quickainew_video_key"], settings["video_model"])
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


@locked_project_state
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
        runtime.update(
            {
                "status": "completed",
                "path": path.relative_to(root).as_posix(),
                "source": "external",
                "sha256": file_digest(path),
                "locked": True,
                "error": "",
            }
        )
        save_state(root, state)
        return {"status": "completed", "path": str(path), "source": "external", "skipped": True}

    image_client, _, settings = clients()
    if not settings.get("quickai_image_key"):
        raise SkillError("QuickAI image key is required for image generation")
    references = [resolve_project_path(root, value) for value in master.get("source_references", [])]
    prompt_variants_value = prompt_variants(project, kind="character_master")
    prompt_version, prompt = select_prompt_variant(prompt_variants_value, preferred=str(runtime.get("prompt_version_override", "")))
    size = str(master.get("image_size") or project.get("defaults", {}).get("image_size") or "1024x1024")
    quality = str(master.get("image_quality") or project.get("defaults", {}).get("image_quality") or "auto")
    current_signature = signature({"model": settings["image_model"], "prompt": prompt, "size": size, "quality": quality}, references)
    existing_path = str(runtime.get("path", ""))
    existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
    if runtime.get("status") == "completed" and existing and existing.is_file() and runtime.get("signature") == current_signature:
        return {"status": "completed", "path": str(existing), "source": "generated", "skipped": True}
    attempts = int(runtime.get("attempts", 0))
    if attempts >= max_total_attempts(project) and runtime.get("status") != "completed":
        raise SkillError(f"character master reached max_total_attempts={max_total_attempts(project)}; inspect state and create a new authorized run")
    if attempts > 0 and not retry_failed:
        raise SkillError("character master creation was already attempted; inspect state.json and use --retry-failed to authorize another billable request")
    if attempts > 0:
        archive_runtime_attempt(runtime, reason)
        write_event(root, {"kind": "character_master_retry_authorized", "reason": reason, "previous_status": runtime.get("status", "")})
    record_budget_attempt(state, project, "image")
    runtime.update(
        {
            "status": "submitting",
            "attempts": attempts + 1,
            "request_id": uuid.uuid4().hex,
            "attempt_id": uuid.uuid4().hex,
            "signature": current_signature,
            "provider": "quickai",
            "model": settings["image_model"],
            "prompt": prompt,
            "prompt_original": prompt_variants_value["full"],
            "prompt_versions": prompt_variants_value,
            "prompt_version": prompt_version,
            "prompt_utf8_bytes": prompt_bytes(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "error": "",
        }
    )
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
                "sha256": file_digest(output),
                "generated_at": int(time.time()),
                "locked": False,
                "error": "",
            }
        )
        save_state(root, state)
        write_event(root, {"kind": "character_master_completed", "bytes": output.stat().st_size})
        emit_progress(progress, phase="character_master_create", status="completed", bytes=output.stat().st_size)
        return {"status": "completed", "path": str(output), "source": "generated", "skipped": False}
    except Exception as error:
        category = classify_provider_error(error, phase="create", task_known=False)
        runtime.update({"status": "failed", "error": str(error)[:1000], "error_category": category})
        save_state(root, state)
        write_event(root, {"kind": "character_master_failed", "error": str(error)[:1000]})
        if category in REPAIRABLE_INPUT_CATEGORIES and runtime["attempts"] < max_total_attempts(project):
            runtime["prompt_version_override"] = next_prompt_version(
                prompt_variants_value, prompt_version, hard_limit=project_prompt_limit(project)
            )
            save_state(root, state)
            return generate_character_master(root, retry_failed=True, retry_reason=f"automatic prompt repair: {category}", progress=progress)
        raise


@locked_project_state
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
    shots = selected_shots(project, shot_ids)
    skipped = [str(shot["id"]) for shot in shots if not bool(shot.get("generate_image", True))]
    generating = [shot for shot in shots if bool(shot.get("generate_image", True))]
    if not generating:
        return {"completed": [], "skipped": skipped}
    image_client, _, settings = clients()
    if not settings.get("quickai_image_key"):
        raise SkillError("QuickAI image key is required for image generation")
    completed: list[str] = []
    for shot in generating:
        shot_id = str(shot["id"])
        references = shot_character_references(root, project, shot)
        for value in shot.get("image_references", []):
            path = resolve_project_path(root, value)
            if path not in references:
                references.append(path)
        if bool(shot.get("use_character_master", False)):
            master_path = resolved_character_master(root, project, state)
            if master_path not in references:
                references.insert(0, master_path)
        runtime = shot_state(state, shot_id)["image"]
        prompt_variants_value = prompt_variants(project, shot, kind="image")
        prompt_version, prompt = select_prompt_variant(prompt_variants_value, preferred=str(runtime.get("prompt_version_override", "")))
        size = str(shot_value(project, shot, "image_size", "1024x1024"))
        quality = str(shot_value(project, shot, "image_quality", "auto"))
        current_signature = signature({"model": settings["image_model"], "prompt": prompt, "size": size, "quality": quality}, references)
        existing_path = str(runtime.get("path", ""))
        existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
        if runtime.get("status") == "completed" and existing and existing.is_file() and runtime.get("signature") == current_signature:
            skipped.append(shot_id)
            continue
        if int(runtime.get("attempts", 0)) >= max_total_attempts(project):
            raise SkillError(f"image generation for {shot_id} reached max_total_attempts={max_total_attempts(project)}")
        if int(runtime.get("attempts", 0)) > 0 and not retry_failed:
            raise SkillError(f"image create was already attempted for {shot_id}; inspect state.json and use --retry-failed to authorize another billable request")
        if int(runtime.get("attempts", 0)) > 0:
            archive_runtime_attempt(runtime, reason)
            write_event(root, {"kind": "image_retry_authorized", "shot_id": shot_id, "reason": reason, "previous_status": runtime.get("status", "")})
        record_budget_attempt(state, project, "image")
        runtime.update(
            {
                "status": "submitting",
                "attempts": int(runtime.get("attempts", 0)) + 1,
                "request_id": uuid.uuid4().hex,
                "attempt_id": uuid.uuid4().hex,
                "signature": current_signature,
                "provider": "quickai",
                "model": settings["image_model"],
                "prompt": prompt,
                "prompt_original": prompt_variants_value["full"],
                "prompt_versions": prompt_variants_value,
                "prompt_version": prompt_version,
                "prompt_utf8_bytes": prompt_bytes(prompt),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "reference_sha256": [file_digest(path) for path in references],
                "error": "",
            }
        )
        save_state(root, state)
        write_event(root, {"kind": "image_create", "shot_id": shot_id, "attempt": runtime["attempts"]})
        emit_progress(progress, phase="image_create", shot_id=shot_id, status="submitting", attempt=runtime["attempts"])
        try:
            data = image_client.edit(prompt, references, size=size, quality=quality) if references else image_client.generate(prompt, size=size, quality=quality)
            output = save_image_bytes(data, root / "assets" / "keyframes" / shot_id)
            runtime.update(
                {
                    "status": "completed",
                    "path": output.relative_to(root).as_posix(),
                    "bytes": output.stat().st_size,
                    "sha256": file_digest(output),
                    "generated_at": int(time.time()),
                    "locked": False,
                    "error": "",
                }
            )
            completed.append(shot_id)
            save_state(root, state)
            emit_progress(progress, phase="image_create", shot_id=shot_id, status="completed", bytes=output.stat().st_size)
        except Exception as error:
            category = classify_provider_error(error, phase="create", task_known=False)
            runtime.update({"status": "failed", "error": str(error)[:1000], "error_category": category})
            save_state(root, state)
            write_event(root, {"kind": "image_failed", "shot_id": shot_id, "error": str(error)[:1000]})
            if category in REPAIRABLE_INPUT_CATEGORIES and runtime["attempts"] < max_total_attempts(project):
                runtime["prompt_version_override"] = next_prompt_version(
                    prompt_variants_value, prompt_version, hard_limit=project_prompt_limit(project)
                )
                save_state(root, state)
                return generate_images(root, retry_failed=True, retry_reason=f"automatic prompt repair: {category}", progress=progress, shot_ids=shot_ids)
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


def sanitized_provider_error(error: Exception, settings: dict[str, Any]) -> str:
    return redact(
        str(error)[:1000],
        [
            str(settings.get("quickai_image_key", "")),
            str(settings.get("quickai_video_key", "")),
            str(settings.get("quickainew_video_key", "")),
        ],
    )


def update_provider_attempt(video: dict[str, Any], *, attempt_id: str, **values: Any) -> None:
    attempts = video.setdefault("provider_attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        video["provider_attempts"] = attempts
    record = next((item for item in reversed(attempts) if isinstance(item, dict) and item.get("attempt_id") == attempt_id), None)
    if record is None:
        record = {"attempt_id": attempt_id}
        attempts.append(record)
    record.update(values)
    del attempts[:-20]


def recoverable_known_task_lookup(video: dict[str, Any]) -> bool:
    category = str(video.get("error_category", "")).strip()
    message = str(video.get("error", "")).lower()
    if category == "task_lookup_transient":
        return True
    # v2.0.0 originally classified a known-task lookup 404 as an unsupported
    # capability. Preserve those task IDs and migrate them on the next resume.
    return category == "capability_unsupported" and "http 404" in message and "not found" in message


@locked_project_state
def generate_videos(
    root: Path,
    *,
    retry_failed: bool,
    retry_reason: str = "",
    progress: bool = False,
    poll_timeout: int,
    shot_ids: list[str] | None = None,
    replace_lost_task: bool = False,
) -> dict[str, Any]:
    reason = require_retry_reason(retry_failed, retry_reason)
    if replace_lost_task and not retry_failed:
        raise SkillError("--replace-lost-task requires --retry-failed and --retry-reason because it may create a replacement paid task")
    if replace_lost_task and not shot_ids:
        raise SkillError("--replace-lost-task requires --shot so unrelated pending shots cannot be submitted")
    project = require_valid_project(root)
    state = load_state(root)
    _, _, settings = clients()
    mode = video_mode(project)
    candidates = video_provider_candidates(project, settings)
    generate_audio = bool(audio_config(project)["generate_audio"])
    if mode not in {"text-to-video", "image-to-video"}:
        raise SkillError("video_mode must be text-to-video or image-to-video")
    completed: list[str] = []
    skipped: list[str] = []
    final_providers: dict[str, str] = {}
    for shot in selected_shots(project, shot_ids):
        shot_id = str(shot["id"])
        runtime = shot_state(state, shot_id)
        video = runtime["video"]
        references = video_references(root, shot, runtime) if mode == "image-to-video" else []
        prompt_variants_value = prompt_variants(project, shot, kind="video")
        prompt_version, prompt = select_prompt_variant(prompt_variants_value, preferred=str(video.get("prompt_version_override", "")))
        seconds = int(shot_value(project, shot, "seconds", 6))
        size = "auto" if bool(video.get("omit_size", False)) else str(shot_value(project, shot, "video_size", "1280x720"))
        resolution = video_resolution(project, shot)
        aspect_ratio = video_aspect_ratio(project, shot)
        def provider_signature(provider_name: str) -> str:
            return signature(
                {"mode": mode, "provider": provider_name, "model": settings["video_model"], "prompt": prompt, "seconds": seconds, "size": size, "resolution": resolution, "aspect_ratio": aspect_ratio, "generate_audio": generate_audio},
                references,
            )

        stored_provider = str(video.get("provider", "")).strip()
        current_signature = provider_signature(stored_provider or candidates[0])
        existing_path = str(video.get("path", ""))
        existing = resolve_project_path(root, existing_path, must_exist=False) if existing_path else None
        if video.get("status") == "completed" and existing and existing.is_file() and video.get("signature") == current_signature:
            assert_mp4(existing)
            skipped.append(shot_id)
            final_providers[shot_id] = stored_provider
            continue
        request_id = str(video.get("request_id", "")).strip() or uuid.uuid4().hex
        video["request_id"] = request_id
        task_id = str(video.get("task_id", "")).strip()
        if replace_lost_task and not task_id:
            raise SkillError(f"{shot_id} has no known task ID to verify or replace")
        active_provider = stored_provider or candidates[0]
        if active_provider not in candidates:
            candidates_for_shot = [active_provider]
        else:
            candidates_for_shot = candidates
        changed_failed_retry = video.get("status") == "failed" and retry_failed
        if (
            task_id
            and video.get("signature")
            and video.get("signature") != provider_signature(active_provider)
            and not changed_failed_retry
        ):
            raise SkillError(f"{shot_id} changed after task creation; restore the original shot or start a new project to avoid mixing task state")
        automatic_failover = False
        if replace_lost_task and task_id:
            if str(video.get("status", "")) not in {"poll_timeout", "failed"} or str(video.get("error_category", "")) not in {
                "task_timeout",
                "task_lookup_transient",
            }:
                raise SkillError(
                    f"{shot_id} is not an unresolved lost-task candidate; use ordinary resume or the confirmed terminal-failure retry path"
                )
            verification_client = select_video_client(settings, active_provider)
            try:
                status, _ = verification_client.query(task_id)
            except APIError as error:
                if error.status != 404:
                    raise SkillError(f"lost-task verification was inconclusive for {shot_id}: {sanitized_provider_error(error, settings)}") from error
            else:
                raise SkillError(f"{shot_id} task {task_id} is still queryable with status {status}; resume it instead of replacing it")

            recovery_output = root / "clips" / f"{shot_id}.mp4"
            try:
                verification_client.download(task_id, {}, recovery_output)
            except SkillError as error:
                if "HTTP 404" not in str(error):
                    raise SkillError(f"lost-task content verification was inconclusive for {shot_id}: {error}") from error
            else:
                try:
                    qa = quality_report(recovery_output, expected_size=size, expected_duration=seconds)
                except SkillError as error:
                    qa = {"ok": False, "errors": [str(error)], "warnings": [], "manual_review_required": []}
                video.update(
                    {
                        "status": "completed",
                        "path": recovery_output.relative_to(root).as_posix(),
                        "bytes": recovery_output.stat().st_size,
                        "sha256": file_digest(recovery_output),
                        "media": qa.get("media", {}),
                        "progress": 100.0,
                        "qa": portable_qa(qa),
                        "final_provider": active_provider,
                        "error": "",
                        "error_category": "",
                    }
                )
                update_provider_attempt(
                    video,
                    attempt_id=str(video.get("attempt_id", "")).strip() or uuid.uuid4().hex,
                    status="completed",
                    finished_at=int(time.time()),
                    bytes=recovery_output.stat().st_size,
                )
                save_state(root, state)
                write_event(
                    root,
                    {
                        "kind": "video_content_recovered",
                        "shot_id": shot_id,
                        "request_id": request_id,
                        "provider": active_provider,
                        "task_id": task_id,
                        "bytes": recovery_output.stat().st_size,
                    },
                )
                completed.append(shot_id)
                final_providers[shot_id] = active_provider
                continue

            previous_task_id = task_id
            archive_runtime_attempt(video, f"confirmed lost upstream task: {reason}")
            video.update(
                {
                    "status": "failed",
                    "previous_task_id": previous_task_id,
                    "task_id": "",
                    "error": "provider status and content endpoints both returned HTTP 404",
                    "error_category": "provider_task_failed",
                }
            )
            task_id = ""
            save_state(root, state)
            write_event(
                root,
                {
                    "kind": "video_lost_task_replacement_authorized",
                    "shot_id": shot_id,
                    "request_id": request_id,
                    "provider": active_provider,
                    "previous_task_id": previous_task_id,
                    "reason": reason,
                },
            )
        if task_id and video.get("status") == "failed" and recoverable_known_task_lookup(video):
            video.update({"status": "queued", "error": "", "error_category": "task_lookup_transient"})
            update_provider_attempt(
                video,
                attempt_id=str(video.get("attempt_id", "")).strip() or uuid.uuid4().hex,
                status="queued",
                resumed_at=int(time.time()),
            )
            save_state(root, state)
            write_event(
                root,
                {
                    "kind": "video_known_task_resumed",
                    "shot_id": shot_id,
                    "request_id": request_id,
                    "provider": active_provider,
                    "task_id": task_id,
                    "reason": "recoverable task lookup failure",
                },
            )
        if task_id and video.get("status") == "failed":
            next_provider = str(video.get("failover_next_provider", "")).strip()
            if next_provider and next_provider in candidates_for_shot:
                archive_runtime_attempt(video, "automatic provider failover")
                automatic_failover = True
                active_provider = next_provider
            elif retry_failed:
                archive_runtime_attempt(video, reason)
                write_event(
                    root,
                    {"kind": "video_retry_authorized", "shot_id": shot_id, "previous_task_id": video.get("task_id", ""), "reason": reason},
                )
            else:
                raise SkillError(
                    f"video task failed for {shot_id} ({task_id}); inspect the provider and use --retry-failed only to authorize a new billable task"
                )
            video["previous_task_id"] = task_id
            video["task_id"] = ""
            video.pop("failover_next_provider", None)
            task_id = ""

        while True:
            provider_index = candidates_for_shot.index(active_provider) if active_provider in candidates_for_shot else 0
            video_client = select_video_client(settings, active_provider)
            if not task_id:
                attempts = int(video.get("attempts", 0))
                if attempts >= max_total_attempts(project):
                    raise SkillError(f"video generation for {shot_id} reached max_total_attempts={max_total_attempts(project)}")
                if attempts > 0 and not retry_failed and not automatic_failover:
                    raise SkillError(f"video create was already attempted for {shot_id}; inspect state.json and use --retry-failed only if duplicate billing is acceptable")
                if attempts > 0 and retry_failed and not automatic_failover and video.get("status") != "failed":
                    archive_runtime_attempt(video, reason)
                record_budget_attempt(state, project, "video")
                attempt_id = uuid.uuid4().hex
                video.update(
                    {
                        "status": "submitting",
                        "attempts": attempts + 1,
                        "attempt_id": attempt_id,
                        "signature": provider_signature(active_provider),
                        "mode": mode,
                        "prompt": prompt,
                        "prompt_original": prompt_variants_value["full"],
                        "prompt_versions": prompt_variants_value,
                        "prompt_version": prompt_version,
                        "prompt_utf8_bytes": prompt_bytes(prompt),
                        "provider": active_provider,
                        "model": settings["video_model"],
                        "seconds": seconds,
                        "resolution": resolution,
                        "aspect_ratio": aspect_ratio,
                        "reference_count": len(references),
                        "generate_audio": generate_audio,
                        "error": "",
                        "error_category": "",
                        "create_attempted_at": int(time.time()),
                    }
                )
                update_provider_attempt(
                    video,
                    attempt_id=attempt_id,
                    request_id=request_id,
                    provider=active_provider,
                    task_id="",
                    status="submitting",
                    created_at=int(time.time()),
                )
                save_state(root, state)
                write_event(root, {"kind": "video_create", "shot_id": shot_id, "request_id": request_id, "attempt_id": attempt_id, "provider": active_provider, "attempt": video["attempts"]})
                emit_progress(progress, phase="video_create", shot_id=shot_id, provider=active_provider, status="submitting", attempt=video["attempts"])
                try:
                    task_id = video_client.create(
                        prompt,
                        seconds=seconds,
                        size=size,
                        resolution=resolution,
                        aspect_ratio=aspect_ratio,
                        generate_audio=generate_audio,
                        references=references,
                    )
                except Exception as error:
                    category = classify_provider_error(error, phase="create", task_known=False)
                    message = sanitized_provider_error(error, settings)
                    status = "submission_unknown" if category == "submission_unknown" else "failed"
                    video.update({"status": status, "error": message, "error_category": category})
                    update_provider_attempt(video, attempt_id=attempt_id, status=status, error=message, error_category=category, finished_at=int(time.time()))
                    save_state(root, state)
                    write_event(root, {"kind": "video_create_failed", "shot_id": shot_id, "request_id": request_id, "attempt_id": attempt_id, "provider": active_provider, "status": status, "error_category": category, "error": message})
                    if category in REPAIRABLE_INPUT_CATEGORIES and video["attempts"] < max_total_attempts(project):
                        if category == "size_conflict":
                            video["omit_size"] = True
                        video["prompt_version_override"] = next_prompt_version(
                            prompt_variants_value, prompt_version, hard_limit=project_prompt_limit(project)
                        )
                        save_state(root, state)
                        return generate_videos(root, retry_failed=True, retry_reason=f"automatic parameter/prompt repair: {category}", progress=progress, poll_timeout=poll_timeout, shot_ids=shot_ids)
                    can_failover = provider_index + 1 < len(candidates_for_shot) and allows_automatic_failover(error, phase="create", task_known=False)
                    if not can_failover:
                        raise SkillError(message) from error
                    next_provider = candidates_for_shot[provider_index + 1]
                    archive_runtime_attempt(video, "automatic provider failover")
                    write_event(root, {"kind": "video_provider_failover", "shot_id": shot_id, "request_id": request_id, "from_provider": active_provider, "to_provider": next_provider, "error_category": category})
                    emit_progress(progress, phase="video_failover", shot_id=shot_id, from_provider=active_provider, to_provider=next_provider, status="switching")
                    active_provider = next_provider
                    automatic_failover = True
                    continue
                video.update({"status": "queued", "task_id": task_id, "error": "", "error_category": ""})
                update_provider_attempt(video, attempt_id=attempt_id, task_id=task_id, status="queued")
                save_state(root, state)
                emit_progress(progress, phase="video_create", shot_id=shot_id, provider=active_provider, status="queued", task_id=task_id)
            else:
                attempt_id = str(video.get("attempt_id", "")).strip() or uuid.uuid4().hex
                video["attempt_id"] = attempt_id
                update_provider_attempt(video, attempt_id=attempt_id, request_id=request_id, provider=active_provider, task_id=task_id, status=str(video.get("status", "queued")))

            def on_status(status: str, payload: dict[str, Any]) -> None:
                video["status"] = status
                video["last_polled_at"] = int(time.time())
                provider_progress = task_progress(payload)
                if provider_progress is not None:
                    video["progress"] = provider_progress
                update_provider_attempt(video, attempt_id=attempt_id, status=status, progress=provider_progress)
                save_state(root, state)
                emit_progress(progress, phase="video_poll", shot_id=shot_id, provider=active_provider, task_id=task_id, status=status, progress=provider_progress)

            upstream_completed = False
            try:
                status_payload = video_client.poll(task_id, timeout_seconds=poll_timeout, on_status=on_status)
                upstream_completed = True
                output = root / "clips" / f"{shot_id}.mp4"
                video_client.download(task_id, status_payload, output)
                try:
                    qa = quality_report(output, expected_size=size, expected_duration=seconds)
                except SkillError as error:
                    qa = {"ok": False, "errors": [str(error)], "warnings": [], "manual_review_required": []}
                video.update({"status": "completed", "path": output.relative_to(root).as_posix(), "bytes": output.stat().st_size, "sha256": file_digest(output), "media": qa.get("media", {}), "progress": 100.0, "qa": portable_qa(qa), "final_provider": active_provider, "error": "", "error_category": ""})
                update_provider_attempt(video, attempt_id=attempt_id, status="completed", finished_at=int(time.time()), bytes=output.stat().st_size)
                save_state(root, state)
                write_event(root, {"kind": "video_completed", "shot_id": shot_id, "request_id": request_id, "attempt_id": attempt_id, "provider": active_provider, "task_id": task_id, "bytes": output.stat().st_size})
                emit_progress(progress, phase="video_download", shot_id=shot_id, provider=active_provider, task_id=task_id, status="completed", bytes=output.stat().st_size)
                completed.append(shot_id)
                final_providers[shot_id] = active_provider
                break
            except TimeoutError as error:
                message = sanitized_provider_error(error, settings)
                video.update({"status": "poll_timeout", "error": message, "error_category": "task_timeout"})
                update_provider_attempt(video, attempt_id=attempt_id, status="poll_timeout", error=message, error_category="task_timeout")
                save_state(root, state)
                raise SkillError(message) from error
            except Exception as error:
                category = classify_provider_error(error, phase="task", task_known=True)
                message = sanitized_provider_error(error, settings)
                if upstream_completed:
                    video.update({"status": "local_processing_failed", "error": message, "error_category": "local_processing"})
                    update_provider_attempt(video, attempt_id=attempt_id, status="local_processing_failed", error=message, error_category="local_processing")
                    save_state(root, state)
                    write_event(root, {"kind": "video_local_processing_failed", "shot_id": shot_id, "request_id": request_id, "attempt_id": attempt_id, "provider": active_provider, "task_id": task_id, "error_category": "local_processing", "error": message})
                    raise SkillError(message) from error
                video.update({"status": "failed", "error": message, "error_category": category})
                update_provider_attempt(video, attempt_id=attempt_id, status="failed", error=message, error_category=category, finished_at=int(time.time()))
                can_failover = provider_index + 1 < len(candidates_for_shot) and allows_automatic_failover(error, phase="task", task_known=True)
                if can_failover:
                    next_provider = candidates_for_shot[provider_index + 1]
                    video["failover_next_provider"] = next_provider
                save_state(root, state)
                write_event(root, {"kind": "video_failed", "shot_id": shot_id, "request_id": request_id, "attempt_id": attempt_id, "provider": active_provider, "task_id": task_id, "error_category": category, "error": message})
                if not can_failover:
                    raise SkillError(message) from error
                next_provider = candidates_for_shot[provider_index + 1]
                archive_runtime_attempt(video, "automatic provider failover")
                write_event(root, {"kind": "video_provider_failover", "shot_id": shot_id, "request_id": request_id, "from_provider": active_provider, "to_provider": next_provider, "error_category": category})
                emit_progress(progress, phase="video_failover", shot_id=shot_id, from_provider=active_provider, to_provider=next_provider, status="switching")
                video["previous_task_id"] = task_id
                video["task_id"] = ""
                video.pop("failover_next_provider", None)
                task_id = ""
                active_provider = next_provider
                automatic_failover = True
                continue
    return {"completed": completed, "skipped": skipped, "final_providers": final_providers}


def media_frame_rate(value: Any) -> float:
    text = str(value or "").strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return round(float(numerator) / float(denominator), 3) if float(denominator) else 0.0
        return round(float(text), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path: Path) -> dict[str, Any]:
    assert_mp4(path)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("ffprobe is required for media validation")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), None)
    subtitles = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "subtitle"]
    audio_metadata: dict[str, Any] = {}
    if audio:
        tags = audio.get("tags") if isinstance(audio.get("tags"), dict) else {}
        try:
            sample_rate = int(audio.get("sample_rate") or 0)
            channels = int(audio.get("channels") or 0)
            audio_duration = float(audio.get("duration") or duration)
        except (TypeError, ValueError):
            sample_rate, channels, audio_duration = 0, 0, duration
        audio_metadata = {
            "codec": str(audio.get("codec_name") or ""),
            "sample_rate": sample_rate,
            "channels": channels,
            "duration": round(audio_duration, 3),
            "language": str(tags.get("language") or ""),
        }
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "frame_rate": media_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "has_audio": bool(audio),
        "audio": audio_metadata,
        "has_subtitles": bool(subtitles),
        "subtitle_streams": [
            {
                "codec": str(item.get("codec_name") or ""),
                "language": str((item.get("tags") or {}).get("language") or "") if isinstance(item.get("tags"), dict) else "",
                "title": str((item.get("tags") or {}).get("title") or "") if isinstance(item.get("tags"), dict) else "",
            }
            for item in subtitles
        ],
    }


def _run_ffmpeg(command: list[str], action: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
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
    return {"path": str(output), "bytes": output.stat().st_size, "sha256": file_digest(output), **media, "clip_count": len(clips), "audio_policy": audio_policy}


@locked_project_state
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


@locked_project_state
def qa_project(root: Path) -> dict[str, Any]:
    project = require_valid_project(root)
    dialogue = dialogue_preflight(project)
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
        if report["media"].get("has_subtitles") and audio_config(project).get("subtitle_source") == "none":
            report["errors"].append("clean delivery contains an embedded subtitle stream while audio.subtitle_source is none")
            report["ok"] = False
        if dialogue["mode"] == "native-dialogue" and dialogue["line_count"] and not report["media"].get("has_audio"):
            report["errors"].append("native-dialogue shot has no audio track")
            report["ok"] = False
        if dialogue["mode"] == "native-dialogue" and dialogue["line_count"]:
            report.setdefault("blocking_review_items", []).append(
                "inspect rendered pixels for model-baked dialogue, captions, or other text; any detected text blocks clean delivery"
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
            expected_duration=float(preflight_report(project, root)["total_video_seconds"]),
        )
        expected_audio_policy = audio_policy(project)
        if expected_audio_policy == "preserve" and not report["media"]["has_audio"]:
            report["errors"].append("final delivery has no audio track while defaults.audio_policy is preserve")
            report["ok"] = False
        elif expected_audio_policy == "mute" and report["media"]["has_audio"]:
            report["errors"].append("final delivery contains audio while defaults.audio_policy is mute")
            report["ok"] = False
        if report["media"].get("has_subtitles") and audio_config(project).get("subtitle_source") == "none":
            report["errors"].append("clean final contains an embedded subtitle stream while audio.subtitle_source is none")
            report["ok"] = False
        if dialogue["mode"] == "native-dialogue" and dialogue["line_count"] and not report["media"].get("has_audio"):
            report["errors"].append("native-dialogue final has no audio track")
            report["ok"] = False
        if dialogue["mode"] == "native-dialogue" and dialogue["line_count"]:
            report.setdefault("blocking_review_items", []).append(
                "inspect rendered pixels for model-baked dialogue, captions, or other text; any detected text blocks clean delivery"
            )
        review_frames = export_review_frames(final_path, review_root, stem="final")
        report["review_frames"] = [
            {**frame, "path": Path(str(frame["path"])).relative_to(root).as_posix()} for frame in review_frames
        ]
        final["qa"] = portable_qa(report)
        reports.append({"kind": "deliverable", "id": "final", **report})
    save_state(root, state)
    technical_ok = all(bool(item.get("ok")) for item in reports) if reports else False
    dialogue_review = {
        **dialogue,
        "manual_checks": (
            [
                "listen for exact approved wording and intelligibility",
                "verify mouth timing and speaker identity",
                "reject unintended model-baked captions; use the clean local subtitle derivative instead",
            ]
            if dialogue["mode"] == "native-dialogue" and dialogue["line_count"]
            else []
        ),
    }
    return {
        "ok": technical_ok,
        "technical_ok": technical_ok,
        "reports": reports,
        "project_audit": audit_project(root, project),
        "dialogue_review": dialogue_review,
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


def image_size_for_aspect_ratio(aspect_ratio: str) -> str:
    if aspect_ratio in {"16:9", "4:3", "3:2"}:
        return "1536x1024"
    if aspect_ratio in {"9:16", "3:4", "2:3"}:
        return "1024x1536"
    return "1024x1024"


def init_project(
    root: Path,
    title: str,
    topic: str,
    workflow_id: str,
    shot_durations: list[int],
    video_size: str,
    video_mode_value: str,
    video_provider_value: str,
    video_provider_policy_value: str,
    video_resolution_value: str,
    video_aspect_ratio_value: str,
    audio_mode_value: str = "native-dialogue",
    subtitle_source_value: str = "none",
) -> Path:
    root = root.resolve()
    path = project_file(root)
    if path.exists():
        raise SkillError(f"project already exists: {path}")
    workflow = get_workflow(workflow_id)
    for folder in ("assets/references", "assets/keyframes", "assets/voices", "assets/dialogue", "clips", "deliverables", "logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    shot_defaults = workflow.get("shot_defaults") if isinstance(workflow.get("shot_defaults"), dict) else {}
    shots = [
        {
            "id": f"shot-{index:03d}",
            "summary": "",
            "shot_role": "medium",
            "scene_id": "",
            "character_ids": [],
            "continuity_notes": "",
            "narration": "",
            "subtitle": "",
            "dialogue": [],
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
        "video_provider_policy": video_provider_policy_value,
        "target_duration_seconds": sum(shot_durations),
        "story": "",
        "character_bible": "",
        "style_bible": "",
        "characters": [],
        "audio": {
            "mode": audio_mode_value,
            "language": "zh-CN",
            "generate_audio": audio_mode_value == "native-dialogue",
            "preserve_source_audio": True,
            "duck_source_audio": True,
            "subtitle_source": subtitle_source_value,
        },
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
            "image_size": image_size_for_aspect_ratio(video_aspect_ratio_value),
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
            "max_prompt_bytes": HARD_PROMPT_BYTES,
        },
        "budget": {
            "currency": "CNY",
            "image_request": 0.0,
            "video_request": 0.0,
            "max_estimated_cost": None,
        },
        "retry_policy": {
            "max_total_attempts": 3,
            "max_retries": 2,
            "counts_provider_failover": True,
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


MODEL_DISPLAY_SUFFIXES = ("（按次）", "（按量）", "（计费）")


def provider_model_match(configured_model: str, models: list[str]) -> str:
    if configured_model in models:
        return configured_model
    for candidate in models:
        normalized = candidate.strip()
        for suffix in MODEL_DISPLAY_SUFFIXES:
            if normalized.endswith(suffix) and normalized[: -len(suffix)].strip() == configured_model:
                return candidate
    return ""


def doctor() -> tuple[int, dict[str, Any]]:
    settings = load_settings()
    result: dict[str, Any] = {
        "ok": True,
        "skill_version": SKILL_VERSION,
        "config": str(config_path()),
        "providers": {},
        "capabilities": provider_capability_report(settings),
        "diagnostics_zh": [],
        "ffmpeg": shutil.which("ffmpeg") or "not_found",
        "ffprobe": shutil.which("ffprobe") or "not_found",
    }
    if result["ffmpeg"] == "not_found" or result["ffprobe"] == "not_found":
        result["ok"] = False
    checks = [
        (
            "quickai_image",
            settings.get("quickai_image_key", ""),
            settings["image_model"],
            QuickAIImageClient(settings["quickai_base_url"], settings["quickai_image_key"], settings["image_model"]),
            False,
        ),
        (
            "quickai_video",
            settings.get("quickai_video_key", ""),
            settings["video_model"],
            QuickAIVideoClient(settings["quickai_base_url"], settings["quickai_video_key"], settings["video_model"]),
            False,
        ),
        (
            "quickainew_video",
            settings.get("quickainew_video_key", ""),
            settings["video_model"],
            QuickAINewVideoClient(
                settings["quickainew_base_url"], settings["quickainew_video_key"], settings["video_model"]
            ),
            False,
        ),
    ]
    for name, key, model, client, required in checks:
        if not key:
            result["providers"][name] = {
                "ok": not required,
                "configured_model": model,
                "skipped": True,
                "required": required,
                "reason": "key_not_configured",
            }
            if required:
                result["ok"] = False
            continue
        started = time.perf_counter()
        try:
            models = client.list_models()
            matched_model = provider_model_match(model, models)
            present = bool(matched_model)
            result["providers"][name] = {
                "ok": present,
                "configured_model": model,
                "models": models,
                "model_present": present,
                "matched_model": matched_model,
                "required": required,
                "model_count": len(models),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "circuit": client.health_snapshot(),
            }
            if not present:
                result["ok"] = False
        except Exception as error:
            result["providers"][name] = {
                "ok": False,
                "configured_model": model,
                "required": required,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(error)[:1000],
                "circuit": client.health_snapshot(),
            }
            result["ok"] = False
    if not settings.get("quickai_image_key"):
        result["diagnostics_zh"].append("QuickAI 生图 Key 未配置：生图会在付费请求前停止，且不会切换到 QuickAI New。")
    if not settings.get("quickai_video_key") and not settings.get("quickainew_video_key"):
        result["diagnostics_zh"].append("QuickAI 与 QuickAI New 视频 Key 均未配置：当前不能生成视频。")
    elif not settings.get("quickainew_video_key"):
        result["diagnostics_zh"].append("QuickAI New 视频 Key 未配置：QuickAI 视频失败时不会进入备用提供方。")
    return (0 if result["ok"] else 1), result


def read_credentials_payload() -> tuple[str, str, str]:
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
    for name in ("quickai_key", "quickainew_key", "quickai_image_key", "quickai_video_key", "quickainew_video_key"):
        if name in payload and not isinstance(payload[name], str):
            raise SkillError(f"credential payload field {name} must be a string when provided")
    legacy_quickai = str(payload.get("quickai_key", "")).strip()
    legacy_quickainew = str(payload.get("quickainew_key", "")).strip()
    image_key = str(payload["quickai_image_key"]).strip() if "quickai_image_key" in payload else legacy_quickai
    video_key = str(payload["quickai_video_key"]).strip() if "quickai_video_key" in payload else legacy_quickai
    new_video_key = (
        str(payload["quickainew_video_key"]).strip() if "quickainew_video_key" in payload else legacy_quickainew
    )
    return image_key, video_key, new_video_key


def configure(args: argparse.Namespace) -> dict[str, Any]:
    if args.credentials_stdin and args.environment_only:
        raise SkillError("--credentials-stdin cannot be combined with --environment-only because the supplied keys would not persist")
    if args.credentials_stdin:
        quickai_image_key, quickai_video_key, quickainew_video_key = read_credentials_payload()
        credential_source = "agent-stdin"
    else:
        legacy_quickai = os.environ.get("GVS_QUICKAI_KEY", "").strip()
        legacy_quickainew = os.environ.get("GVS_QUICKAINEW_KEY", "").strip()
        quickai_image_key = (
            os.environ.get("GVS_QUICKAI_IMAGE_KEY", "").strip()
            or os.environ.get("QUICKAI_IMAGE_API_KEY", "").strip()
            or legacy_quickai
        )
        quickai_video_key = (
            os.environ.get("GVS_QUICKAI_VIDEO_KEY", "").strip()
            or os.environ.get("QUICKAI_VIDEO_API_KEY", "").strip()
            or legacy_quickai
        )
        quickainew_video_key = (
            os.environ.get("GVS_QUICKAINEW_VIDEO_KEY", "").strip()
            or os.environ.get("QUICKAI_NEW_VIDEO_API_KEY", "").strip()
            or legacy_quickainew
        )
        if not quickai_image_key:
            quickai_image_key = getpass.getpass("QuickAI image key (optional): ").strip()
        if not quickai_video_key:
            quickai_video_key = getpass.getpass("QuickAI video key for T2V/I2V (optional): ").strip()
        if not quickainew_video_key:
            quickainew_video_key = getpass.getpass("QuickAI New video key (optional): ").strip()
        credential_source = "environment-or-interactive"
    if not quickai_image_key and not quickai_video_key and not quickainew_video_key:
        raise SkillError("at least one provider key is required")
    config = {
        "quickai_base_url": normalize_base_url(args.quickai_base_url),
        "quickainew_base_url": normalize_base_url(args.quickainew_base_url),
        "image_model": args.image_model.strip(),
        "video_model": args.video_model.strip(),
        "default_video_provider": (
            args.video_provider
            or ("quickai" if quickai_video_key else "quickainew" if quickainew_video_key else "quickai")
        ),
    }
    connection: dict[str, Any] = {
        "quickai_image": "not_tested",
        "quickai_video": "not_tested",
        "quickainew_video": "not_tested",
    }
    if not args.skip_test:
        connection = {"quickai_image": "not_configured", "quickai_video": "not_configured", "quickainew_video": "not_configured"}
        if quickai_image_key:
            image_models = QuickAIImageClient(config["quickai_base_url"], quickai_image_key, config["image_model"]).list_models()
            if config["image_model"] not in image_models:
                raise SkillError(f"configured image model is not advertised by QuickAI: {config['image_model']}")
            connection["quickai_image"] = "ok"
        if quickai_video_key:
            video_models = QuickAIVideoClient(config["quickai_base_url"], quickai_video_key, config["video_model"]).list_models()
            if config["video_model"] not in video_models:
                raise SkillError(f"configured video model is not advertised by QuickAI: {config['video_model']}")
            connection["quickai_video"] = "ok"
        if quickainew_video_key:
            video_models = QuickAINewVideoClient(config["quickainew_base_url"], quickainew_video_key, config["video_model"]).list_models()
            if config["video_model"] not in video_models:
                raise SkillError(f"configured video model is not advertised by QuickAI New: {config['video_model']}")
            connection["quickainew_video"] = "ok"
    save_settings(
        config,
        quickai_image_key,
        quickai_video_key,
        quickainew_video_key,
        store_secrets=not args.environment_only,
    )
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
                "image": {
                    key: runtime["image"].get(key)
                    for key in ("status", "request_id", "attempt_id", "path", "sha256", "locked", "provider", "model", "prompt_sha256", "reference_sha256", "generated_at", "attempts", "history", "review_status", "review_notes", "reviewed_at", "error_category", "error")
                    if runtime["image"].get(key) not in (None, "")
                },
                "video": {
                    key: runtime["video"].get(key)
                    for key in ("status", "request_id", "attempt_id", "task_id", "path", "sha256", "media", "attempts", "progress", "mode", "provider", "final_provider", "provider_attempts", "model", "seconds", "resolution", "aspect_ratio", "reference_count", "qa", "history", "review_status", "review_notes", "reviewed_at", "error_category", "error")
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
        "video_provider_policy": video_provider_policy(project),
        "character_master": {key: master.get(key) for key in ("status", "path", "source", "attempts", "error") if master.get(key) not in (None, "")},
        "shots": shots,
        "deliverables": state.get("deliverables", {}),
        "budget_usage": state.get("budget_usage", {}),
    }


@locked_project_state
def review_shot_asset(root: Path, shot_id: str, *, kind: str, decision: str, notes: str) -> dict[str, Any]:
    project = require_valid_project(root)
    known = {str(shot.get("id", "")) for shot in project.get("shots", []) if isinstance(shot, dict)}
    if shot_id not in known:
        raise SkillError(f"unknown shot id: {shot_id}")
    state = load_state(root)
    runtime = shot_state(state, shot_id)[kind]
    if runtime.get("status") != "completed" or not runtime.get("path"):
        raise SkillError(f"{kind} for {shot_id} must be completed before review")
    path = resolve_project_path(root, str(runtime["path"]))
    digest = file_digest(path)
    if runtime.get("sha256") and runtime.get("sha256") != digest:
        raise SkillError(f"{kind} asset changed after generation; review is blocked")
    archive_runtime_attempt(runtime, f"user review: {decision}")
    runtime.update(
        {
            "review_status": "approved" if decision == "approve" else "rejected",
            "review_notes": notes.strip(),
            "reviewed_at": int(time.time()),
            "reviewed_sha256": digest,
        }
    )
    if decision == "approve":
        if kind == "image":
            runtime["locked"] = True
    else:
        runtime.update(
            {
                "status": "failed",
                "locked": False,
                "error": "asset rejected by user review",
                "error_category": "user_rejected",
            }
        )
    save_state(root, state)
    write_event(root, {"kind": f"{kind}_reviewed", "shot_id": shot_id, "decision": decision, "sha256": digest})
    if decision == "approve":
        next_action = "approved image is hash-locked" if kind == "image" else "approved video review is recorded"
    else:
        next_action = "use --retry-failed with --retry-reason to authorize a replacement paid request"
    return {
        "shot_id": shot_id,
        "kind": kind,
        "decision": decision,
        "path": str(path),
        "sha256": digest,
        "locked": bool(runtime.get("locked", False)),
        "next": next_action,
    }


def compact_series_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        key: runtime.get(key)
        for key in (
            "status",
            "attempts",
            "approved_at",
            "generation_started_at",
            "generation_finished_at",
            "completed_at",
            "final",
            "technical_qa",
            "continuity_summary",
            "review_notes",
            "manual_review_complete",
            "error",
        )
        if runtime.get(key) not in (None, "")
    }


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def subtitle_cues(root: Path, project: dict[str, Any]) -> list[dict[str, Any]]:
    news_narration: dict[str, str] = {}
    if project.get("workflow") == "news-video":
        try:
            news = load_news_contract(root)
            news_narration = {
                str(item.get("shot_id", "")): str(item.get("narration", "")).strip()
                for item in news.get("script_segments", [])
                if isinstance(item, dict)
            }
        except SkillError:
            news_narration = {}
    cues: list[dict[str, Any]] = []
    dialogue_by_shot: dict[str, list[dict[str, Any]]] = {}
    for cue in dialogue_subtitle_cues(project):
        dialogue_by_shot.setdefault(str(cue["shot_id"]), []).append(cue)
    offset = 0.0
    for shot in project.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("id", ""))
        duration = float(shot_value(project, shot, "seconds", 6))
        items = shot.get("subtitles") if isinstance(shot.get("subtitles"), list) else []
        if dialogue_by_shot.get(shot_id):
            cues.extend(dialogue_by_shot[shot_id])
        elif items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                cues.append(
                    {
                        "shot_id": shot_id,
                        "start": offset + float(item["start"]),
                        "end": offset + float(item["end"]),
                        "text": str(item["text"]).strip(),
                    }
                )
        else:
            text = (
                str(shot.get("subtitle", "")).strip()
                or str(shot.get("narration", "")).strip()
                or news_narration.get(shot_id, "")
            )
            if text:
                padding = min(0.15, duration * 0.08)
                cues.append(
                    {
                        "shot_id": shot_id,
                        "start": offset + padding,
                        "end": offset + duration - padding,
                        "text": text,
                    }
                )
        offset += duration
    return cues


def export_subtitles(root: Path, project: dict[str, Any], output: Path) -> dict[str, Any]:
    cues = subtitle_cues(root, project)
    if not cues:
        raise SkillError("project has no subtitle, narration, or news narration text")
    if output.suffix.lower() != ".srt":
        raise SkillError("subtitle output must end with .srt")
    blocks = []
    for index, cue in enumerate(cues, 1):
        text = str(cue["text"]).replace("\r\n", "\n").replace("\r", "\n").replace("-->", "->").strip()
        blocks.append(
            f"{index}\n{_srt_timestamp(float(cue['start']))} --> {_srt_timestamp(float(cue['end']))}\n{text}"
        )
    atomic_write_bytes(output.resolve(), ("\n\n".join(blocks) + "\n").encode("utf-8"))
    return {"path": str(output.resolve()), "cue_count": len(cues), "cues": cues}


def resolve_subtitle_source(project: dict[str, Any], requested: str = "auto") -> str:
    selected = str(requested or "auto").strip().lower()
    if selected == "auto":
        selected = audio_config(project).get("subtitle_source", "project")
    if selected not in SUBTITLE_SOURCES:
        raise SkillError("subtitle source must be auto, upstream, project, or none")
    return selected


def resolve_project_audio_options(
    audio_mode: str | None,
    subtitle_source: str | None,
    install_profile: str | None,
) -> tuple[str, str]:
    """Resolve explicit project options, optionally inheriting an install profile."""
    selected_mode = str(audio_mode or "").strip().lower()
    selected_source = str(subtitle_source or "").strip().lower()
    selected_profile = str(install_profile or "").strip()
    # An installer-selected profile is the default for new projects. Without a
    # profile, v2 projects use upstream dialogue and no generated subtitle.
    if not selected_profile and install_profile_settings_path().is_file():
        selected_profile = str(load_install_profile().get("profile", "")).strip()
    if selected_profile:
        plan = install_profile_plan(selected_profile)
        if not selected_mode:
            selected_mode = str(plan["audio_mode"])
        if not selected_source:
            selected_source = str(plan["subtitle_source"])
    return selected_mode or "native-dialogue", selected_source or "none"


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
    setup.add_argument("--video-provider", choices=("quickai", "quickainew"))
    setup.add_argument("--environment-only", action="store_true", help="Do not persist secrets; require environment variables at runtime.")
    setup.add_argument(
        "--credentials-stdin",
        action="store_true",
        help="Read one non-echoed JSON credential payload from stdin for Codex-managed installation.",
    )
    setup.add_argument("--skip-test", action="store_true")

    commands.add_parser("doctor", help="Check credentials, model routing, and ffmpeg without generating media.")

    install_plan = commands.add_parser(
        "install-plan", help="Show a side-effect-free capability installation plan and dependency checks."
    )
    # Aliases such as full-dialogue are resolved by install_profiles.py so old
    # automation can keep using the command after the profile rename.
    install_plan.add_argument("--profile", default="upstream-dialogue")
    install_configure = commands.add_parser(
        "install-configure", help="Persist the selected capability profile without storing provider secrets."
    )
    install_configure.add_argument("--profile", required=True)

    components_plan = commands.add_parser("components-plan", help="Show optional local-service requirements without changing the machine.")
    components_plan.add_argument("--profile", choices=("core", "native-dialogue", "local-voice", "full-dialogue"), required=True)
    components_configure = commands.add_parser("components-configure", help="Save the user-approved optional component profile and locations.")
    components_configure.add_argument("--profile", choices=("core", "native-dialogue", "local-voice", "full-dialogue"), required=True)
    components_configure.add_argument("--source-root", type=Path)
    components_configure.add_argument("--models-root", type=Path)
    components_configure.add_argument("--cosyvoice-url")
    components_configure.add_argument("--musetalk-url")
    components_configure.add_argument("--voicebox-url")
    components_configure.add_argument("--voxcpm-url")
    components_install = commands.add_parser("components-install", help="Download pinned optional component sources after explicit approval.")
    components_install.add_argument("--profile", choices=("core", "native-dialogue", "local-voice", "full-dialogue"), required=True)
    components_install.add_argument("--accept-downloads", action="store_true")
    components_doctor = commands.add_parser("components-doctor", help="Check optional source pins and localhost services.")
    components_doctor.add_argument("--profile", choices=("core", "native-dialogue", "local-voice", "full-dialogue"))
    components_doctor.add_argument("--component", choices=("cosyvoice", "musetalk", "all"))
    components_setup = commands.add_parser("components-setup", help="Build isolated Docker runtimes and optionally download model weights.")
    components_setup.add_argument("--profile", choices=("local-voice", "full-dialogue"), required=True)
    components_setup.add_argument("--accept-downloads", action="store_true")
    components_setup.add_argument("--include-models", action="store_true")
    components_start = commands.add_parser("components-start", help="Start user-approved localhost AI media services.")
    components_start.add_argument("--profile", choices=("local-voice", "full-dialogue"), required=True)
    components_start.add_argument("--component", choices=("cosyvoice", "musetalk", "all"))
    components_stop = commands.add_parser("components-stop", help="Stop and remove only Grok Video Studio managed service containers.")
    components_stop.add_argument("--profile", choices=("local-voice", "full-dialogue"), required=True)
    components_stop.add_argument("--component", choices=("cosyvoice", "musetalk", "all"))

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
    init.add_argument("--video-provider-policy", choices=("automatic", "fixed"))
    init.add_argument("--video-resolution", choices=tuple(sorted(VIDEO_RESOLUTIONS)), default="480p")
    init.add_argument("--aspect-ratio", choices=tuple(sorted(ASPECT_RATIOS)), default="16:9")
    init.add_argument("--audio-mode", choices=tuple(sorted(DIALOGUE_MODES)))
    init.add_argument("--subtitle-source", choices=tuple(sorted(SUBTITLE_SOURCES)))
    init.add_argument("--install-profile", help="Inherit audio and subtitle defaults from a saved installation profile.")
    init.add_argument("--seconds", type=int)

    series_init = commands.add_parser("series-init", help="Create a series contract and standard episode projects.")
    series_init.add_argument("series", type=Path)
    series_init.add_argument("--title", required=True)
    series_init.add_argument("--premise", required=True)
    series_init.add_argument("--episodes", type=int, default=20)
    series_init.add_argument("--episode-seconds", type=int, default=90)
    series_init.add_argument("--workflow", default="character-consistent-story")
    series_init.add_argument("--video-size", default="1280x720")
    series_init.add_argument("--mode", choices=("text-to-video", "image-to-video"), default="image-to-video")
    series_init.add_argument("--video-provider", choices=("quickai", "quickainew"))
    series_init.add_argument("--video-provider-policy", choices=("automatic", "fixed"))
    series_init.add_argument("--video-resolution", choices=tuple(sorted(VIDEO_RESOLUTIONS)), default="480p")
    series_init.add_argument("--aspect-ratio", choices=tuple(sorted(ASPECT_RATIOS)), default="16:9")
    series_init.add_argument("--audio-mode", choices=tuple(sorted(DIALOGUE_MODES)))
    series_init.add_argument("--subtitle-source", choices=tuple(sorted(SUBTITLE_SOURCES)))
    series_init.add_argument("--install-profile", help="Inherit audio and subtitle defaults from a saved installation profile.")
    series_init.add_argument("--clip-seconds", type=int)

    for name in ("series-validate", "series-preflight", "series-status", "series-next", "series-sync", "series-voice-sync"):
        command = commands.add_parser(name)
        command.add_argument("series", type=Path)
        if name == "series-preflight":
            command.add_argument("--episode")

    context = commands.add_parser("series-context", help="Export compact season history and the current episode contract.")
    context.add_argument("series", type=Path)
    context.add_argument("--episode")

    series_approve = commands.add_parser("series-approve", help="Approve one reviewed episode contract for paid generation.")
    series_approve.add_argument("series", type=Path)
    series_approve.add_argument("episode")

    series_characters = commands.add_parser("series-generate-characters", help="Generate reusable series-level character masters.")
    series_characters.add_argument("series", type=Path)
    series_characters.add_argument("--character", action="append")
    series_characters.add_argument("--retry-failed", action="store_true")
    series_characters.add_argument("--retry-reason", default="")
    series_characters.add_argument("--progress", action="store_true")

    series_run = commands.add_parser("series-run", help="Generate one approved episode and stop at the visual review gate.")
    series_run.add_argument("series", type=Path)
    series_run.add_argument("--episode")
    series_run.add_argument("--next", action="store_true")
    series_run.add_argument("--retry-failed", action="store_true")
    series_run.add_argument("--retry-reason", default="")
    series_run.add_argument("--progress", action="store_true")
    series_run.add_argument("--poll-timeout", type=int, default=1800)

    series_accept = commands.add_parser("series-accept", help="Accept a reviewed episode and record its actual continuity state.")
    series_accept.add_argument("series", type=Path)
    series_accept.add_argument("episode")
    series_accept.add_argument("--continuity-summary", required=True)
    series_accept.add_argument("--review-notes", default="")
    series_accept.add_argument("--accept-qa-warnings", action="store_true")

    news_init = commands.add_parser("news-init", help="Create a sourced-news contract and standard video project.")
    news_init.add_argument("project", type=Path)
    news_init.add_argument("--title", required=True)
    news_init.add_argument("--topic", required=True)
    news_init.add_argument("--region", default="global")
    news_init.add_argument("--language", default="zh-CN")
    news_init.add_argument("--window-hours", type=int, default=24)
    news_init.add_argument("--target-seconds", type=int, default=60)
    news_init.add_argument("--shots", type=int)
    news_init.add_argument("--clip-seconds", type=int)
    news_init.add_argument("--video-size", default="1280x720")
    news_init.add_argument("--mode", choices=("text-to-video", "image-to-video"), default="text-to-video")
    news_init.add_argument("--video-provider", choices=("quickai", "quickainew"))
    news_init.add_argument("--video-provider-policy", choices=("automatic", "fixed"))
    news_init.add_argument("--video-resolution", choices=tuple(sorted(VIDEO_RESOLUTIONS)), default="480p")
    news_init.add_argument("--aspect-ratio", choices=tuple(sorted(ASPECT_RATIOS)), default="16:9")
    news_init.add_argument("--audio-mode", choices=tuple(sorted(DIALOGUE_MODES)))
    news_init.add_argument("--subtitle-source", choices=tuple(sorted(SUBTITLE_SOURCES)))
    news_init.add_argument("--install-profile", help="Inherit audio and subtitle defaults from a saved installation profile.")

    for name in ("news-validate", "news-context"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)

    for name in ("validate", "preflight", "status", "assemble", "audit"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)

    review_shot = commands.add_parser("review-shot", help="Approve or reject one completed shot image/video asset.")
    review_shot.add_argument("project", type=Path)
    review_shot.add_argument("shot")
    review_shot.add_argument("--kind", choices=("image", "video"), required=True)
    review_shot.add_argument("--decision", choices=("approve", "reject"), required=True)
    review_shot.add_argument("--notes", default="")

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
        videos.add_argument(
            "--replace-lost-task",
            action="store_true",
            help="Verify a timed-out known task through status and content endpoints before authorizing one replacement create.",
        )

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
    postprocess.add_argument("--subtitle-style", choices=tuple(SUBTITLE_STYLES), default="clean")
    postprocess.add_argument("--fade-seconds", type=float, default=0.0)

    subtitles = commands.add_parser("subtitles", help="Export deterministic SRT and optionally burn a separate subtitled MP4.")
    subtitles.add_argument("project", type=Path)
    subtitles.add_argument("--output-srt", type=Path)
    subtitles.add_argument("--burn", action="store_true")
    subtitles.add_argument("--source-video", type=Path)
    subtitles.add_argument("--output-video", type=Path)
    subtitles.add_argument("--style", choices=tuple(SUBTITLE_STYLES), default="clean")
    subtitles.add_argument(
        "--source",
        choices=("auto", "upstream", "project", "none"),
        default="auto",
        help="Select upstream pixels, deterministic project cues, or no subtitle delivery.",
    )
    subtitles.add_argument(
        "--confirm-source-clean",
        action="store_true",
        help="Allow subtitle burning for native-dialogue only after visual review confirms the source has no baked captions.",
    )

    dialogue = commands.add_parser("dialogue-render", help="Render local TTS dialogue, deterministic timing, mixing, and optional lip sync.")
    dialogue.add_argument("project", type=Path)
    dialogue.add_argument("--source-video", type=Path)
    dialogue.add_argument("--output-video", type=Path)
    dialogue.add_argument("--cosyvoice-url")
    dialogue.add_argument("--voicebox-url")
    dialogue.add_argument("--tts-provider", choices=("cosyvoice", "voicebox", "voxcpm"))
    dialogue.add_argument("--musetalk-url")
    dialogue.add_argument("--force", action="store_true")
    dialogue.add_argument("--burn-subtitles", action="store_true")
    dialogue.add_argument("--subtitle-style", choices=tuple(SUBTITLE_STYLES), default="clean")
    dialogue.add_argument(
        "--subtitle-source",
        choices=("auto", "upstream", "project", "none"),
        default="auto",
        help="Select subtitle source for the optional SRT/burned delivery.",
    )

    voice_catalog = commands.add_parser("voice-catalog", help="Show character voice contracts and recorded audition candidates.")
    voice_catalog.add_argument("workspace", type=Path)
    voice_doctor_parser = commands.add_parser("voice-doctor", help="Check one localhost TTS provider without downloading models.")
    voice_doctor_parser.add_argument("--provider", choices=("cosyvoice", "voicebox", "voxcpm"), required=True)
    voice_doctor_parser.add_argument("--service-url")
    voice_doctor_parser.add_argument("--workspace", type=Path)
    voice_list = commands.add_parser("voice-list", help="List provider voices without generating audio.")
    voice_list.add_argument("--provider", choices=("cosyvoice", "voicebox", "voxcpm"), required=True)
    voice_list.add_argument("--engine", default="")
    voice_list.add_argument("--service-url")
    voice_audition = commands.add_parser("voice-audition", help="Generate one review-only character voice audition.")
    voice_audition.add_argument("workspace", type=Path)
    voice_audition.add_argument("character")
    voice_audition.add_argument("--provider", choices=("cosyvoice", "voicebox", "voxcpm"), required=True)
    voice_audition.add_argument("--text", required=True)
    voice_audition.add_argument("--service-url")
    voice_audition.add_argument("--preset-voice-id", default="")
    voice_audition.add_argument("--engine", default="")
    voice_audition.add_argument("--voice-id", default="")
    voice_audition.add_argument("--model-size", choices=("0.6B", "1.7B", "1B", "3B"), default="0.6B")
    voice_audition.add_argument("--seed", type=int, default=42)
    voice_audition.add_argument("--instruct", default="")
    voice_audition.add_argument("--candidate-id", default="")
    voice_import = commands.add_parser("voice-import", help="Import a rights-cleared reference voice as a review candidate.")
    voice_import.add_argument("workspace", type=Path)
    voice_import.add_argument("character")
    voice_import.add_argument("source", type=Path)
    voice_import.add_argument("--provider", choices=("cosyvoice", "voxcpm"), required=True)
    voice_import.add_argument("--reference-text", required=True)
    voice_import.add_argument("--consent", choices=("synthetic", "owned", "licensed"), required=True)
    voice_import.add_argument("--license-note", default="")
    voice_import.add_argument("--candidate-id", default="")
    voice_approve = commands.add_parser("voice-approve", help="Approve a reviewed audition for its character contract.")
    voice_approve.add_argument("workspace", type=Path)
    voice_approve.add_argument("candidate")
    voice_approve.add_argument("--temporary-test", action="store_true")
    voice_approve.add_argument("--approved-by", default="user")
    voice_reject = commands.add_parser("voice-reject", help="Reject a reviewed audition without changing the active voice.")
    voice_reject.add_argument("workspace", type=Path)
    voice_reject.add_argument("candidate")
    voicebox_plan = commands.add_parser("voicebox-setup-plan", help="Inspect a Voicebox/Qwen setup without changing the machine.")
    voicebox_plan.add_argument("--source", type=Path, required=True)
    voicebox_plan.add_argument("--models-root", type=Path, required=True)
    voicebox_plan.add_argument("--data-root", type=Path, required=True)

    cover = commands.add_parser("cover", help="Export a representative frame as a JPG or PNG cover.")
    cover.add_argument("input", type=Path)
    cover.add_argument("output", type=Path)
    cover.add_argument("--at-seconds", type=float, default=0.5)
    return parser


def main() -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args()
    try:
        if args.command == "version":
            print_json({"ok": True, "version": SKILL_VERSION})
            return 0
        if args.command == "capabilities":
            install_profile = load_install_profile()
            try:
                provider_settings = load_settings(require_secrets=False)
            except SkillError:
                provider_settings = {
                    "quickai_image_key": "",
                    "quickai_video_key": "",
                    "quickainew_video_key": "",
                }
            print_json(
                {
                    "ok": True,
                    "version": SKILL_VERSION,
                    "installation": install_profile,
                    "video_provider_default": configured_default_video_provider(),
                    "video_provider_selection": {
                        "unspecified": "quickai with safe quickainew fallback",
                        "explicit_quickai": "fixed quickai when --video-provider quickai is supplied",
                        "explicit_quickainew": "fixed quickainew when --video-provider quickainew is supplied",
                    },
                    "providers": provider_capability_report(provider_settings),
                    "product_routes": [
                        {
                            "id": "text-to-video",
                            "command": "init",
                            "use_for": "one standalone video generated directly from prompts",
                        },
                        {
                            "id": "image-to-video",
                            "command": "init",
                            "use_for": "one standalone video animated from supplied images or generated keyframes",
                        },
                        {
                            "id": "episodic-series",
                            "command": "series-init",
                            "use_for": "multiple ordered episodes with shared canon, review gates, and continuity state",
                        },
                        {
                            "id": "news-video",
                            "command": "news-init",
                            "use_for": "current hot-news research with sourced claims before standard video generation",
                        },
                    ],
                    "workflows": workflow_catalog(),
                    "audio_routes": [
                        {"id": "preserve", "requires": [], "use_for": "keep provider or supplied source audio"},
                        {"id": "native-dialogue", "requires": [], "use_for": "ask the video provider for synchronized spoken dialogue"},
                        {"id": "local-voice", "requires": ["approved-tts-provider"], "use_for": "deterministic multi-provider TTS, subtitles, and FFmpeg mixing"},
                        {"id": "local-lipsync", "requires": ["approved-tts-provider", "musetalk"], "use_for": "local TTS plus character mouth synchronization"},
                    ],
                    "tts_providers": [
                        {"id": "voicebox", "status": "supported", "voices": ["Qwen CustomVoice", "Kokoro"]},
                        {"id": "cosyvoice", "status": "supported", "voices": ["preset", "rights-cleared reference"]},
                        {"id": "voxcpm", "status": "experimental", "voices": ["designed master", "rights-cleared reference"]},
                    ],
                    "selection": "Choose one product route, then select an internal workflow preset when needed.",
                }
            )
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
        if args.command == "install-plan":
            print_json({"ok": True, **install_profile_plan(args.profile)})
            return 0
        if args.command == "install-configure":
            settings = save_install_profile(args.profile)
            print_json({"ok": True, "settings": settings, "path": str(install_profile_settings_path())})
            return 0
        if args.command == "components-plan":
            print_json({"ok": True, **component_plan(args.profile)})
            return 0
        if args.command == "components-configure":
            settings = save_component_settings(
                profile=args.profile,
                source_root=args.source_root,
                models_root=args.models_root,
                cosyvoice_url=args.cosyvoice_url,
                musetalk_url=args.musetalk_url,
                voicebox_url=args.voicebox_url,
                voxcpm_url=args.voxcpm_url,
            )
            print_json({"ok": True, "settings": settings, "plan": component_plan(args.profile, settings)})
            return 0
        if args.command == "components-install":
            print_json({"ok": True, **install_component_sources(args.profile, accept_downloads=args.accept_downloads)})
            return 0
        if args.command == "components-doctor":
            result = component_status(args.profile, args.component)
            print_json(result)
            return 0 if result["ok"] else 1
        if args.command == "components-setup":
            print_json(
                {
                    "ok": True,
                    **setup_component_runtimes(
                        args.profile,
                        accept_downloads=args.accept_downloads,
                        include_models=args.include_models,
                    ),
                }
            )
            return 0
        if args.command == "components-start":
            print_json({"ok": True, **start_components(args.profile, args.component)})
            return 0
        if args.command == "components-stop":
            print_json({"ok": True, **stop_components(args.profile, args.component)})
            return 0
        if args.command == "voice-catalog":
            print_json(voice_catalog_summary(args.workspace.resolve()))
            return 0
        if args.command == "voicebox-setup-plan":
            print_json(voicebox_setup_plan(args.source, args.models_root, args.data_root))
            return 0
        if args.command in {"voice-doctor", "voice-list", "voice-audition"}:
            component_settings = load_component_settings()
            services = component_settings.get("services") if isinstance(component_settings.get("services"), dict) else {}
            if args.command == "voice-doctor":
                result = voice_doctor(
                    args.provider,
                    services,
                    service_url=args.service_url,
                    root=args.workspace.resolve() if args.workspace else None,
                )
                print_json(result)
                return 0 if result["ok"] else 1
            if args.command == "voice-list":
                print_json(list_provider_voices(args.provider, services, engine=args.engine, service_url=args.service_url))
                return 0
            print_json(
                audition_voice(
                    args.workspace.resolve(),
                    character_id=args.character,
                    provider=args.provider,
                    services=services,
                    text=args.text,
                    service_url=args.service_url,
                    preset_voice_id=args.preset_voice_id,
                    preset_engine=args.engine,
                    voice_id=args.voice_id,
                    model_size=args.model_size,
                    seed=args.seed,
                    instruct_text=args.instruct,
                    candidate_id=args.candidate_id,
                )
            )
            return 0
        if args.command == "voice-import":
            print_json(
                import_voice_candidate(
                    args.workspace.resolve(),
                    character_id=args.character,
                    source=args.source.resolve(),
                    reference_text=args.reference_text,
                    consent=args.consent,
                    provider=args.provider,
                    license_note=args.license_note,
                    candidate_id=args.candidate_id,
                )
            )
            return 0
        if args.command in {"voice-approve", "voice-reject"}:
            print_json(
                review_voice_candidate(
                    args.workspace.resolve(),
                    args.candidate,
                    approve=args.command == "voice-approve",
                    temporary_test=bool(getattr(args, "temporary_test", False)),
                    approved_by=str(getattr(args, "approved_by", "user")),
                )
            )
            return 0
        if args.command == "series-init":
            workflow = get_workflow(args.workflow)
            selected_audio_mode, selected_subtitle_source = resolve_project_audio_options(
                args.audio_mode, args.subtitle_source, args.install_profile
            )
            durations = plan_shot_durations(
                workflow,
                shot_count=None,
                seconds=args.clip_seconds,
                target_seconds=args.episode_seconds,
            )
            if args.video_size != "auto" and not SIZE_RE.fullmatch(args.video_size):
                raise SkillError("--video-size must be WIDTHxHEIGHT or auto")
            selected_provider, selected_provider_policy = resolve_video_provider_options(
                args.video_provider, args.video_provider_policy
            )
            root = args.series.resolve()
            series = create_series_contract(
                root,
                title=args.title,
                premise=args.premise,
                episode_count=args.episodes,
                target_seconds=args.episode_seconds,
                workflow=args.workflow,
                video_mode=args.mode,
                video_provider=selected_provider,
                video_provider_policy=selected_provider_policy,
                video_size=args.video_size,
                video_resolution=args.video_resolution,
                video_aspect_ratio=args.aspect_ratio,
                audio_mode=selected_audio_mode,
                subtitle_source=selected_subtitle_source,
            )
            for episode in episode_records(series):
                project_root = root / str(episode["project"])
                init_project(
                    project_root,
                    f"{args.title} {episode['id']}",
                    f"Episode {episode['number']} plan",
                    args.workflow,
                    durations,
                    args.video_size,
                    args.mode,
                    selected_provider,
                    selected_provider_policy,
                    args.video_resolution,
                    args.aspect_ratio,
                    selected_audio_mode,
                    selected_subtitle_source,
                )
            synced = sync_all_episode_contracts(root, series)
            print_json(
                {
                    "ok": True,
                    "series": str(root),
                    "series_contract": str(root / "series.json"),
                    "episode_count": len(series["episodes"]),
                    "episode_target_seconds": args.episode_seconds,
                    "shot_seconds": durations,
                    "video_provider": selected_provider,
                    "video_provider_policy": selected_provider_policy,
                    "synced_episodes": synced,
                    "next": "Fill the season plan and episode project prompts, then run series-preflight and series-approve ep-001.",
                }
            )
            return 0
        if args.command.startswith("series-"):
            root = args.series.resolve()
            series = load_series(root)
            series_errors = validate_series(root, series)
            if args.command == "series-status":
                print_json({"ok": not series_errors, "errors": series_errors, **series_status(root)})
                return 0 if not series_errors else 1
            if args.command == "series-context":
                print_json({"ok": not series_errors, "errors": series_errors, **series_context(root, args.episode)})
                return 0 if not series_errors else 1
            if args.command == "series-next":
                episode, state = select_next_episode(root)
                episode_id = str(episode.get("id", ""))
                runtime = state["episodes"][episode_id]
                action = {
                    "draft": "fill_prompts_then_preflight_and_approve",
                    "approved": "run_episode",
                    "generating": "resume_episode",
                    "needs_review": "review_frames_then_accept",
                    "failed": "inspect_state_then_explicitly_retry",
                }.get(str(runtime.get("status")), "inspect")
                print_json(
                    {
                        "ok": not series_errors,
                        "errors": series_errors,
                        "episode": episode,
                        "runtime": compact_series_runtime(runtime),
                        "next_action": action,
                    }
                )
                return 0 if not series_errors else 1
            if args.command in {"series-sync", "series-voice-sync"}:
                if series_errors:
                    raise SkillError("series validation failed: " + "; ".join(series_errors))
                synced = (
                    sync_approved_episode_voices(root, series)
                    if args.command == "series-voice-sync"
                    else sync_all_episode_contracts(root, series)
                )
                print_json({"ok": True, "synced_episodes": synced, "approved_voices_only": args.command == "series-voice-sync"})
                return 0
            if args.command in {"series-validate", "series-preflight"}:
                requested = [get_episode(series, args.episode)] if args.command == "series-preflight" and args.episode else episode_records(series)
                episodes = []
                all_ok = not series_errors
                character_preflight = series_character_preflight(root, series)
                for episode in requested:
                    episode_id = str(episode.get("id", ""))
                    try:
                        if args.command == "series-preflight":
                            sync_episode_contract(root, series, episode)
                        project_root = episode_root(root, episode)
                        project = load_project(project_root)
                        errors = validate_project(project_root, project)
                        result: dict[str, Any] = {
                            "id": episode_id,
                            "project": str(project_root),
                            "ok": not errors,
                            "errors": errors,
                        }
                        if args.command == "series-preflight":
                            result["preflight"] = preflight_report(project, project_root)
                            result["audit"] = audit_project(project_root, project)
                    except SkillError as error:
                        result = {"id": episode_id, "ok": False, "errors": [str(error)]}
                    all_ok = all_ok and bool(result["ok"])
                    episodes.append(result)
                totals: dict[str, Any] = {}
                if args.command == "series-preflight":
                    episode_images = sum(
                        int(((item.get("preflight") or {}).get("requests") or {}).get("total_images", 0))
                        for item in episodes
                    )
                    episode_videos = sum(
                        int(((item.get("preflight") or {}).get("requests") or {}).get("videos", 0))
                        for item in episodes
                    )
                    totals = {
                        "pending_series_character_images": character_preflight["pending_character_master_images"],
                        "episode_images": episode_images,
                        "episode_videos": episode_videos,
                        "total_pending_images": character_preflight["pending_character_master_images"] + episode_images,
                    }
                print_json(
                    {
                        "ok": all_ok,
                        "series": str(root),
                        "series_errors": series_errors,
                        "character_preflight": character_preflight,
                        "request_totals": totals,
                        "episodes": episodes,
                    }
                )
                return 0 if all_ok else 1
            if args.command == "series-approve":
                if series_errors:
                    raise SkillError("series validation failed: " + "; ".join(series_errors))
                episode = get_episode(series, args.episode)
                project_root = episode_root(root, episode)
                sync_episode_contract(root, series, episode)
                project = load_project(project_root)
                project_errors = validate_project(project_root, project)
                if project_errors:
                    raise SkillError("episode project validation failed: " + "; ".join(project_errors))
                print_json(
                    {
                        "ok": True,
                        "episode": args.episode,
                        "runtime": compact_series_runtime(approve_episode(root, args.episode)),
                    }
                )
                return 0
            if args.command == "series-generate-characters":
                if series_errors:
                    raise SkillError("series validation failed: " + "; ".join(series_errors))

                def series_progress(event: dict[str, Any]) -> None:
                    emit_progress(args.progress, **event)

                result = generate_series_characters(
                    root,
                    character_ids=args.character,
                    retry_failed=args.retry_failed,
                    retry_reason=args.retry_reason,
                    on_progress=series_progress,
                )
                print_json({"ok": True, "characters": result})
                return 0
            if args.command == "series-accept":
                runtime = accept_episode(
                    root,
                    args.episode,
                    continuity_summary=args.continuity_summary,
                    review_notes=args.review_notes,
                    accept_qa_warnings=args.accept_qa_warnings,
                )
                print_json({"ok": True, "episode": args.episode, "runtime": compact_series_runtime(runtime)})
                return 0
            if args.command == "series-run":
                if args.episode and args.next:
                    raise SkillError("use either --episode or --next, not both")
                if series_errors:
                    raise SkillError("series validation failed: " + "; ".join(series_errors))
                if args.episode:
                    episode = get_episode(series, args.episode)
                else:
                    episode = select_next_episode(root, for_run=True)[0]
                episode_id = str(episode.get("id", ""))
                project_root = episode_root(root, episode)
                sync_episode_contract(root, series, episode)
                project = require_valid_project(project_root)
                series_state = load_series_state(root, series)
                if video_mode(project) == "image-to-video":
                    used_characters = {
                        str(character_id)
                        for shot in project.get("shots", [])
                        if isinstance(shot, dict)
                        for character_id in shot.get("character_ids", [])
                    }
                    by_id = {str(item.get("id", "")): item for item in series.get("characters", []) if isinstance(item, dict)}
                    missing_masters = []
                    for character_id in sorted(used_characters):
                        character = by_id.get(character_id)
                        master = series_character_master_config(series, character) if character else {"enabled": False}
                        if bool(master["enabled"]):
                            runtime = (series_state.get("characters", {}).get(character_id) or {})
                            if runtime.get("status") != "completed":
                                missing_masters.append(character_id)
                    if missing_masters:
                        raise SkillError(
                            "generate or register series character masters before image-to-video: " + ", ".join(missing_masters)
                        )
                begin_episode(root, episode_id, allow_failed_retry=args.retry_failed)
                try:
                    character_result = generate_character_master(
                        project_root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                    )
                    image_result = generate_images(
                        project_root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                    )
                    video_result = generate_videos(
                        project_root,
                        retry_failed=args.retry_failed,
                        retry_reason=args.retry_reason,
                        progress=args.progress,
                        poll_timeout=args.poll_timeout,
                    )
                    output, media = assemble(project_root)
                    qa = qa_project(project_root)
                    runtime = finish_episode_generation(root, episode_id, final_path=output, qa=qa)
                except Exception as error:
                    fail_episode_generation(root, episode_id, error)
                    raise
                print_json(
                    {
                        "ok": True,
                        "episode": episode_id,
                        "status": "needs_review",
                        "character_master": character_result,
                        "images": image_result,
                        "videos": video_result,
                        "output": str(output),
                        "media": media,
                        "qa": qa,
                        "runtime": compact_series_runtime(runtime),
                    }
                )
                return 0
        if args.command == "news-init":
            workflow = get_workflow("news-video")
            selected_audio_mode, selected_subtitle_source = resolve_project_audio_options(
                args.audio_mode, args.subtitle_source, args.install_profile
            )
            durations = plan_shot_durations(
                workflow,
                shot_count=args.shots,
                seconds=args.clip_seconds,
                target_seconds=args.target_seconds,
            )
            if args.video_size != "auto" and not SIZE_RE.fullmatch(args.video_size):
                raise SkillError("--video-size must be WIDTHxHEIGHT or auto")
            provider, provider_policy = resolve_video_provider_options(
                args.video_provider, args.video_provider_policy
            )
            root = args.project.resolve()
            path = init_project(
                root,
                args.title,
                args.topic,
                "news-video",
                durations,
                args.video_size,
                args.mode,
                provider,
                provider_policy,
                args.video_resolution,
                args.aspect_ratio,
                selected_audio_mode,
                selected_subtitle_source,
            )
            package = create_news_contract(
                root,
                topic=args.topic,
                region=args.region,
                language=args.language,
                window_hours=args.window_hours,
            )
            print_json(
                {
                    "ok": True,
                    "project": str(path),
                    "news_contract": str(root / "news.json"),
                    "video_mode": args.mode,
                    "video_provider": provider,
                    "video_provider_policy": provider_policy,
                    "shot_seconds": durations,
                    "research_status": package["editorial"]["status"],
                    "next": "Codex must browse current sources, fill sourced claims and script segments, then run news-validate.",
                }
            )
            return 0
        if args.command in {"news-validate", "news-context"}:
            root = args.project.resolve()
            context = news_context(root)
            if args.command == "news-context":
                print_json({"ok": True, **context})
                return 0
            errors = validate_project(root, context["project"])
            print_json(
                {
                    "ok": not errors,
                    "project": str(root),
                    "errors": errors,
                    "preflight": preflight_report(context["project"], root),
                    "audit": audit_project(root, context["project"]),
                    "news": context["news"],
                }
            )
            return 0 if not errors else 1
        if args.command == "init":
            workflow = get_workflow(args.workflow)
            selected_audio_mode, selected_subtitle_source = resolve_project_audio_options(
                args.audio_mode, args.subtitle_source, args.install_profile
            )
            durations = plan_shot_durations(
                workflow,
                shot_count=args.shots,
                seconds=args.seconds,
                target_seconds=args.target_seconds,
            )
            if args.video_size != "auto" and not SIZE_RE.fullmatch(args.video_size):
                raise SkillError("--video-size must be WIDTHxHEIGHT or auto")
            selected_mode = args.mode or ("text-to-video" if args.workflow == "text-to-video" else "image-to-video")
            selected_provider, selected_provider_policy = resolve_video_provider_options(
                args.video_provider, args.video_provider_policy
            )
            path = init_project(
                args.project,
                args.title,
                args.topic,
                args.workflow,
                durations,
                args.video_size,
                selected_mode,
                selected_provider,
                selected_provider_policy,
                args.video_resolution,
                args.aspect_ratio,
                selected_audio_mode,
                selected_subtitle_source,
            )
            print_json(
                {
                    "ok": True,
                    "project": str(path),
                    "workflow": args.workflow,
                    "video_provider": selected_provider,
                    "video_provider_policy": selected_provider_policy,
                    "shot_seconds": durations,
                    "target_seconds": sum(durations),
                }
            )
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
                subtitle_style=args.subtitle_style,
                fade_seconds=args.fade_seconds,
            )
            print_json({"ok": True, "output": str(args.output.resolve()), "media": media})
            return 0
        if args.command == "subtitles":
            root = args.project.resolve()
            project = require_valid_project(root)
            if (
                args.burn
                and audio_config(project)["mode"] == "native-dialogue"
                and dialogue_preflight(project)["line_count"]
                and not args.confirm_source_clean
            ):
                raise SkillError(
                    "native-dialogue providers may bake captions into pixels; inspect the source video first, then re-run with "
                    "--confirm-source-clean only when the source is visually caption-free"
                )
            subtitle_source = resolve_subtitle_source(project, args.source)
            if args.output_video and not args.burn:
                raise SkillError("--output-video requires --burn")
            if args.burn and subtitle_source != "project":
                raise SkillError(
                    f"subtitle source {subtitle_source!r} has no local SRT to burn; use --source project for a deterministic subtitle copy"
                )
            subtitle_result: dict[str, Any] = {"source": subtitle_source, "preserved": subtitle_source == "upstream"}
            if subtitle_source == "project":
                srt_output = args.output_srt.resolve() if args.output_srt else root / "deliverables" / "subtitles.srt"
                subtitle_result.update(export_subtitles(root, project, srt_output))
            elif args.output_srt:
                raise SkillError("--output-srt is only valid with --source project")
            video_result: dict[str, Any] | None = None
            if args.burn:
                source = args.source_video.resolve() if args.source_video else root / "deliverables" / "final.mp4"
                output = args.output_video.resolve() if args.output_video else root / "deliverables" / "final-subtitled.mp4"
                if not source.is_file():
                    raise SkillError(f"subtitle source video does not exist: {source}")
                if source == output:
                    raise SkillError("subtitled output must differ from the clean source video")
                video_result = postprocess_video(source, output, subtitles=srt_output, subtitle_style=args.style)
            print_json({"ok": True, "subtitles": subtitle_result, "burned_video": video_result or {}})
            return 0
        if args.command == "dialogue-render":
            root = args.project.resolve()
            project = require_valid_project(root)
            subtitle_source = resolve_subtitle_source(project, args.subtitle_source)
            if args.burn_subtitles and subtitle_source != "project":
                raise SkillError(
                    f"subtitle source {subtitle_source!r} has no local SRT to burn; use --subtitle-source project for a deterministic subtitle copy"
                )
            source = args.source_video.resolve() if args.source_video else root / "deliverables" / "final.mp4"
            output = args.output_video.resolve() if args.output_video else root / "deliverables" / "final-dialogue.mp4"
            if source == output:
                raise SkillError("dialogue output must differ from the clean source video")
            result = render_local_dialogue(
                root,
                project,
                source_video=source,
                output_video=output,
                service_url=args.cosyvoice_url,
                voicebox_url=args.voicebox_url,
                tts_provider=args.tts_provider,
                musetalk_url=args.musetalk_url,
                force=args.force,
            )
            subtitles_result: dict[str, Any] = {"source": subtitle_source, "preserved": subtitle_source == "upstream"}
            if subtitle_source == "project":
                subtitles_result.update(export_subtitles(root, project, root / "deliverables" / "dialogue.srt"))
            burned: dict[str, Any] = {}
            if args.burn_subtitles:
                delivery = Path(str((result.get("lipsync") or {}).get("path") or output))
                burned_path = delivery.with_name(delivery.stem + "-subtitled.mp4")
                burned = postprocess_video(
                    delivery,
                    burned_path,
                    subtitles=Path(subtitles_result["path"]),
                    subtitle_style=args.subtitle_style,
                )
            print_json({"ok": True, "dialogue": result, "subtitles": subtitles_result, "burned_video": burned})
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
                    "preflight": preflight_report(project, root),
                    "audit": audit_project(root, project),
                }
            )
            return 0 if not errors else 1
        if args.command == "status":
            print_json({"ok": True, **status_summary(root)})
            return 0
        if args.command == "review-shot":
            print_json(
                {
                    "ok": True,
                    "review": review_shot_asset(
                        root,
                        args.shot,
                        kind=args.kind,
                        decision=args.decision,
                        notes=args.notes,
                    ),
                }
            )
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
                        replace_lost_task=args.replace_lost_task,
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
