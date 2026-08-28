#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from gvs_common import SkillError, atomic_write_json, load_settings, read_json
from media_client import QuickAIImageClient, save_image_bytes


SERIES_VERSION = 1
SERIES_STATE_VERSION = 1
EPISODE_ID_RE = re.compile(r"^ep-[0-9]{3,4}$")
ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
EPISODE_STATUSES = {"draft", "approved", "generating", "needs_review", "completed", "failed"}
MAX_PROMPT_CHARS = 4096
SAFE_PROMPT_CHARS = 3800


def series_file(root: Path) -> Path:
    return root / "series.json"


def series_state_file(root: Path) -> Path:
    return root / "series-state.json"


def resolve_series_path(root: Path, value: str, *, must_exist: bool = True, directory: bool = False) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise SkillError(f"series paths must be relative: {value}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise SkillError(f"series path escapes the project: {value}") from error
    exists = resolved.is_dir() if directory else resolved.is_file()
    if must_exist and not exists:
        kind = "directory" if directory else "file"
        raise SkillError(f"series {kind} does not exist: {value}")
    return resolved


def load_series(root: Path) -> dict[str, Any]:
    value = read_json(series_file(root))
    if value.get("version") != SERIES_VERSION:
        raise SkillError("series.json has an unsupported version")
    return value


def episode_records(series: dict[str, Any]) -> list[dict[str, Any]]:
    value = series.get("episodes", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def character_records(series: dict[str, Any]) -> list[dict[str, Any]]:
    value = series.get("characters", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def series_character_master_config(series: dict[str, Any], character: dict[str, Any]) -> dict[str, Any]:
    value = character.get("master") if isinstance(character.get("master"), dict) else {}
    default_enabled = (series.get("defaults") or {}).get("video_mode") == "image-to-video"
    enabled = bool(value.get("enabled", default_enabled))
    return {
        **value,
        "enabled": enabled,
        "generate": bool(value.get("generate", enabled)),
        "path": str(value.get("path", f"assets/character-masters/{character.get('id', 'character')}.png")),
        "source_references": value.get("source_references", []),
        "image_size": str(value.get("image_size", "1024x1024")),
        "image_quality": str(value.get("image_quality", "auto")),
    }


def fresh_series_state(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SERIES_STATE_VERSION,
        "updated_at": int(time.time()),
        "characters": {
            str(character.get("id", "")): {"status": "pending", "attempts": 0}
            for character in character_records(series)
            if str(character.get("id", ""))
        },
        "episodes": {
            str(episode.get("id", "")): {"status": "draft", "attempts": 0}
            for episode in episode_records(series)
            if str(episode.get("id", ""))
        },
    }


def load_series_state(root: Path, series: dict[str, Any] | None = None) -> dict[str, Any]:
    series = series or load_series(root)
    path = series_state_file(root)
    if not path.is_file():
        return fresh_series_state(series)
    value = read_json(path)
    if value.get("version") != SERIES_STATE_VERSION:
        raise SkillError("series-state.json has an unsupported version")
    characters = value.setdefault("characters", {})
    episodes = value.setdefault("episodes", {})
    if not isinstance(characters, dict) or not isinstance(episodes, dict):
        raise SkillError("series-state.json has an unsupported format")
    for character in character_records(series):
        characters.setdefault(str(character.get("id", "")), {"status": "pending", "attempts": 0})
    for episode in episode_records(series):
        episodes.setdefault(str(episode.get("id", "")), {"status": "draft", "attempts": 0})
    return value


def save_series_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = int(time.time())
    atomic_write_json(series_state_file(root), state)


def write_series_event(root: Path, event: dict[str, Any]) -> None:
    path = root / "logs" / "series-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def create_series_contract(
    root: Path,
    *,
    title: str,
    premise: str,
    episode_count: int,
    target_seconds: int,
    workflow: str,
    video_mode: str,
    video_provider: str,
    video_size: str,
    video_resolution: str,
    video_aspect_ratio: str,
) -> dict[str, Any]:
    root = root.resolve()
    if series_file(root).exists() or series_state_file(root).exists():
        raise SkillError(f"series already exists: {root}")
    if episode_count < 1 or episode_count > 100:
        raise SkillError("episode count must be from 1 to 100")
    if target_seconds < 1 or target_seconds > 750:
        raise SkillError("episode target duration must be from 1 to 750 seconds")
    for folder in ("assets/character-masters", "assets/references", "episodes", "logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    episodes = [
        {
            "id": f"ep-{number:03d}",
            "number": number,
            "title": "",
            "synopsis": "",
            "continuity_in": "",
            "intended_continuity_out": "",
            "character_states": {},
            "project": f"episodes/ep-{number:03d}",
        }
        for number in range(1, episode_count + 1)
    ]
    series = {
        "version": SERIES_VERSION,
        "id": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64] or "video-series",
        "title": title.strip(),
        "premise": premise.strip(),
        "season_arc": "",
        "style_bible": "",
        "locations": [],
        "props": [],
        "characters": [],
        "defaults": {
            "episode_target_seconds": target_seconds,
            "workflow": workflow,
            "video_mode": video_mode,
            "video_provider": video_provider,
            "video_size": video_size,
            "video_resolution": video_resolution,
            "video_aspect_ratio": video_aspect_ratio,
        },
        "limits": {"max_character_image_requests": 20, "max_episodes": 100},
        "episodes": episodes,
    }
    atomic_write_json(series_file(root), series)
    save_series_state(root, fresh_series_state(series))
    return series


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).lower().replace("-", "_")
            if name in {
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
            if _contains_secret(child):
                return True
    return isinstance(value, list) and any(_contains_secret(item) for item in value)


def validate_series(root: Path, series: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if series.get("version") != SERIES_VERSION:
        errors.append("series.version must be 1")
    for name in ("id", "title", "premise"):
        if not str(series.get(name, "")).strip():
            errors.append(f"series.{name} is required")
    if _contains_secret(series):
        errors.append("series contains a credential-like field; credentials must stay outside projects")
    defaults = series.get("defaults") if isinstance(series.get("defaults"), dict) else {}
    if defaults.get("video_mode") not in {"text-to-video", "image-to-video"}:
        errors.append("series.defaults.video_mode must be text-to-video or image-to-video")
    if defaults.get("video_provider") not in {"quickai", "quickainew"}:
        errors.append("series.defaults.video_provider must be quickai or quickainew")
    characters = series.get("characters", [])
    if not isinstance(characters, list):
        errors.append("series.characters must be an array")
        characters = []
    character_ids: set[str] = set()
    for index, character in enumerate(characters):
        prefix = f"characters[{index}]"
        if not isinstance(character, dict):
            errors.append(f"{prefix} must be an object")
            continue
        character_id = str(character.get("id", "")).strip()
        if not ASSET_ID_RE.fullmatch(character_id):
            errors.append(f"{prefix}.id must use lowercase letters, digits, and hyphens")
        elif character_id in character_ids:
            errors.append(f"duplicate character id: {character_id}")
        character_ids.add(character_id)
        for name in ("name", "identity"):
            if not str(character.get(name, "")).strip():
                errors.append(f"{prefix}.{name} is required")
        master = series_character_master_config(series, character)
        if bool(master["enabled"]):
            path_value = str(master.get("path", f"assets/character-masters/{character_id}.png")).strip()
            try:
                path = resolve_series_path(root, path_value, must_exist=False)
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    errors.append(f"{prefix}.master.path must be PNG, JPEG, or WebP")
            except SkillError as error:
                errors.append(str(error))
            if bool(master["generate"]) and not str(master.get("prompt", "")).strip():
                errors.append(f"{prefix}.master.prompt is required when generate is true")
            elif bool(master["generate"]):
                prompt = series_character_prompt(series, character)
                if len(prompt) > MAX_PROMPT_CHARS:
                    errors.append(
                        f"{prefix}.master composed prompt has {len(prompt)} characters; maximum is {MAX_PROMPT_CHARS}"
                    )
            sources = master.get("source_references", [])
            if not isinstance(sources, list) or not all(isinstance(item, str) and item.strip() for item in sources):
                errors.append(f"{prefix}.master.source_references must be an array of relative paths")
    episodes = series.get("episodes", [])
    if not isinstance(episodes, list) or not episodes:
        errors.append("series.episodes must be a non-empty array")
        return errors
    limits = series.get("limits") if isinstance(series.get("limits"), dict) else {}
    try:
        maximum = int(limits.get("max_episodes", 100))
    except (TypeError, ValueError):
        maximum = 100
        errors.append("series.limits.max_episodes must be a positive integer")
    if len(episodes) > maximum:
        errors.append(f"episode count {len(episodes)} exceeds limits.max_episodes {maximum}")
    episode_ids: set[str] = set()
    project_paths: set[str] = set()
    for index, episode in enumerate(episodes):
        prefix = f"episodes[{index}]"
        if not isinstance(episode, dict):
            errors.append(f"{prefix} must be an object")
            continue
        episode_id = str(episode.get("id", ""))
        if not EPISODE_ID_RE.fullmatch(episode_id):
            errors.append(f"{prefix}.id must use ep-001 format")
        elif episode_id in episode_ids:
            errors.append(f"duplicate episode id: {episode_id}")
        episode_ids.add(episode_id)
        if episode.get("number") != index + 1:
            errors.append(f"{prefix}.number must be {index + 1}")
        project_value = str(episode.get("project", "")).strip()
        if not project_value:
            errors.append(f"{prefix}.project is required")
        elif project_value in project_paths:
            errors.append(f"duplicate episode project path: {project_value}")
        project_paths.add(project_value)
        try:
            resolve_series_path(root, project_value, must_exist=False, directory=True)
        except SkillError as error:
            errors.append(str(error))
    return errors


def get_episode(series: dict[str, Any], episode_id: str) -> dict[str, Any]:
    for episode in episode_records(series):
        if episode.get("id") == episode_id:
            return episode
    raise SkillError(f"unknown episode id: {episode_id}")


def episode_contract_digest(root: Path, series: dict[str, Any], episode: dict[str, Any]) -> str:
    project_root = episode_root(root, episode)
    creative_series = {
        key: series.get(key)
        for key in ("version", "id", "title", "premise", "season_arc", "style_bible", "locations", "props", "characters", "defaults")
    }
    value = {
        "series": creative_series,
        "episode": episode,
        "project": read_json(project_root / "project.json"),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def episode_root(root: Path, episode: dict[str, Any], *, must_exist: bool = True) -> Path:
    return resolve_series_path(root, str(episode.get("project", "")), must_exist=must_exist, directory=True)


def canonical_character_bible(series: dict[str, Any]) -> str:
    lines = []
    for character in character_records(series):
        line = f"{character.get('name')} ({character.get('id')}): {str(character.get('identity', '')).strip()}"
        wardrobe = str(character.get("wardrobe", "")).strip()
        if wardrobe:
            line += f" Canonical wardrobe: {wardrobe}."
        lines.append(line)
    return "\n".join(lines)


def _same_file(source: Path, target: Path) -> bool:
    if not target.is_file() or source.stat().st_size != target.stat().st_size:
        return False
    return hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()


def sync_episode_contract(root: Path, series: dict[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    project_root = episode_root(root, episode)
    project_path = project_root / "project.json"
    project = read_json(project_path)
    state = load_series_state(root, series)
    episodes = episode_records(series)
    index = episodes.index(episode)
    prior_summary = ""
    if index > 0:
        previous_id = str(episodes[index - 1].get("id", ""))
        prior_summary = str((state.get("episodes", {}).get(previous_id) or {}).get("continuity_summary", "")).strip()
    project["series_context"] = {
        "series_id": series.get("id", ""),
        "series_title": series.get("title", ""),
        "episode_id": episode.get("id", ""),
        "episode_number": episode.get("number"),
        "continuity_in": str(episode.get("continuity_in", "")).strip(),
        "previous_episode_continuity": prior_summary,
        "intended_continuity_out": str(episode.get("intended_continuity_out", "")).strip(),
    }
    if str(episode.get("title", "")).strip():
        project["title"] = str(episode["title"]).strip()
    if str(episode.get("synopsis", "")).strip():
        project["topic"] = str(episode["synopsis"]).strip()
    project["character_bible"] = canonical_character_bible(series)
    if str(series.get("style_bible", "")).strip():
        project["style_bible"] = str(series["style_bible"]).strip()
    old_characters = {
        str(item.get("id", "")): item
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    character_states = episode.get("character_states") if isinstance(episode.get("character_states"), dict) else {}
    synced_characters: list[dict[str, Any]] = []
    for character in character_records(series):
        character_id = str(character.get("id", ""))
        existing = dict(old_characters.get(character_id, {}))
        episode_character = character_states.get(character_id) if isinstance(character_states.get(character_id), dict) else {}
        existing.update(
            {
                "id": character_id,
                "name": character.get("name", ""),
                "identity": character.get("identity", ""),
                "wardrobe": episode_character.get("wardrobe") or character.get("wardrobe", ""),
            }
        )
        references = [
            value
            for value in existing.get("references", [])
            if isinstance(value, str) and not value.startswith("assets/characters/")
        ]
        master = series_character_master_config(series, character)
        if bool(master["enabled"]):
            runtime = (state.get("characters", {}).get(character_id) or {}) if isinstance(state.get("characters"), dict) else {}
            source_value = str(
                runtime.get("path") or master.get("path", f"assets/character-masters/{character_id}.png")
            )
            source = resolve_series_path(root, source_value, must_exist=False)
            if source.is_file():
                target = project_root / "assets" / "characters" / f"{character_id}{source.suffix.lower()}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not _same_file(source, target):
                    shutil.copy2(source, target)
                references.insert(0, target.relative_to(project_root).as_posix())
        existing["references"] = list(dict.fromkeys(references))
        synced_characters.append(existing)
    project["characters"] = synced_characters
    master = project.get("character_master")
    if isinstance(master, dict):
        master["enabled"] = False
        master["generate"] = False
    for shot in project.get("shots", []):
        if isinstance(shot, dict):
            shot["use_character_master"] = False
    atomic_write_json(project_path, project)
    return project


def sync_all_episode_contracts(root: Path, series: dict[str, Any] | None = None) -> list[str]:
    series = series or load_series(root)
    synced = []
    for episode in episode_records(series):
        project_root = episode_root(root, episode, must_exist=False)
        if (project_root / "project.json").is_file():
            sync_episode_contract(root, series, episode)
            synced.append(str(episode.get("id", "")))
    return synced


def _episode_runtime(state: dict[str, Any], episode_id: str) -> dict[str, Any]:
    runtime = state.setdefault("episodes", {}).setdefault(episode_id, {"status": "draft", "attempts": 0})
    if runtime.get("status") not in EPISODE_STATUSES:
        raise SkillError(f"episode {episode_id} has unsupported runtime status")
    return runtime


def approve_episode(root: Path, episode_id: str) -> dict[str, Any]:
    series = load_series(root)
    state = load_series_state(root, series)
    episodes = episode_records(series)
    episode = get_episode(series, episode_id)
    if not str(episode.get("title", "")).strip() or not str(episode.get("synopsis", "")).strip():
        raise SkillError(f"{episode_id} requires a title and synopsis before approval")
    index = episodes.index(episode)
    blockers = [
        str(item.get("id", ""))
        for item in episodes[:index]
        if _episode_runtime(state, str(item.get("id", ""))).get("status") != "completed"
    ]
    if blockers:
        raise SkillError(f"complete earlier episode(s) before approving {episode_id}: {', '.join(blockers)}")
    runtime = _episode_runtime(state, episode_id)
    if runtime.get("status") not in {"draft", "approved"}:
        raise SkillError(f"{episode_id} cannot be approved from status {runtime.get('status')}")
    runtime.update({"status": "approved", "approved_at": int(time.time()), "error": ""})
    runtime["approved_contract_sha256"] = episode_contract_digest(root, series, episode)
    save_series_state(root, state)
    write_series_event(root, {"kind": "episode_approved", "episode_id": episode_id})
    return runtime


def select_next_episode(root: Path, *, for_run: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    series = load_series(root)
    state = load_series_state(root, series)
    episodes = episode_records(series)
    if for_run:
        blockers = []
        for episode in episodes:
            episode_id = str(episode.get("id", ""))
            status = str(_episode_runtime(state, episode_id).get("status", "draft"))
            if status in {"generating", "needs_review", "failed"}:
                blockers.append(f"{episode_id}:{status}")
        if blockers:
            raise SkillError("resolve the current episode before generating the next one: " + ", ".join(blockers))
        for episode in episodes:
            episode_id = str(episode.get("id", ""))
            if _episode_runtime(state, episode_id).get("status") == "approved":
                return episode, state
        raise SkillError("no approved episode is ready; preflight and approve the next draft first")
    for episode in episodes:
        episode_id = str(episode.get("id", ""))
        if _episode_runtime(state, episode_id).get("status") != "completed":
            return episode, state
    raise SkillError("all episodes are completed")


def begin_episode(root: Path, episode_id: str, *, allow_failed_retry: bool = False) -> dict[str, Any]:
    series = load_series(root)
    state = load_series_state(root, series)
    episode = get_episode(series, episode_id)
    runtime = _episode_runtime(state, episode_id)
    status = runtime.get("status")
    if status == "failed" and allow_failed_retry:
        pass
    elif status != "approved":
        raise SkillError(f"{episode_id} must be approved before generation; current status is {status}")
    approved_digest = str(runtime.get("approved_contract_sha256", "")).strip()
    current_digest = episode_contract_digest(root, series, episode)
    if approved_digest and approved_digest != current_digest:
        raise SkillError(
            f"{episode_id} creative contract changed after approval; run series-preflight and series-approve again before generation"
        )
    runtime.update(
        {
            "status": "generating",
            "attempts": int(runtime.get("attempts", 0)) + 1,
            "generation_started_at": int(time.time()),
            "error": "",
            "generation_contract_sha256": current_digest,
        }
    )
    save_series_state(root, state)
    write_series_event(root, {"kind": "episode_generation_started", "episode_id": episode_id})
    return runtime


def finish_episode_generation(
    root: Path,
    episode_id: str,
    *,
    final_path: Path,
    qa: dict[str, Any],
) -> dict[str, Any]:
    series = load_series(root)
    state = load_series_state(root, series)
    runtime = _episode_runtime(state, episode_id)
    runtime.update(
        {
            "status": "needs_review",
            "generation_finished_at": int(time.time()),
            "final": final_path.resolve().relative_to(root.resolve()).as_posix(),
            "technical_qa": bool(qa.get("technical_ok", qa.get("ok", False))),
            "qa": qa,
            "error": "",
        }
    )
    save_series_state(root, state)
    write_series_event(
        root,
        {"kind": "episode_generation_finished", "episode_id": episode_id, "technical_qa": runtime["technical_qa"]},
    )
    return runtime


def fail_episode_generation(root: Path, episode_id: str, error: Exception) -> None:
    series = load_series(root)
    state = load_series_state(root, series)
    runtime = _episode_runtime(state, episode_id)
    runtime.update({"status": "failed", "error": str(error)[:1000], "failed_at": int(time.time())})
    save_series_state(root, state)
    write_series_event(root, {"kind": "episode_generation_failed", "episode_id": episode_id, "error": str(error)[:1000]})


def accept_episode(
    root: Path,
    episode_id: str,
    *,
    continuity_summary: str,
    review_notes: str,
    accept_qa_warnings: bool,
) -> dict[str, Any]:
    summary = continuity_summary.strip()
    if not summary:
        raise SkillError("--continuity-summary is required so the next episode can inherit the reviewed end state")
    series = load_series(root)
    state = load_series_state(root, series)
    runtime = _episode_runtime(state, episode_id)
    if runtime.get("status") != "needs_review":
        raise SkillError(f"{episode_id} must be in needs_review before acceptance")
    if not runtime.get("technical_qa") and not accept_qa_warnings:
        raise SkillError("technical QA did not pass; fix it or explicitly use --accept-qa-warnings after manual review")
    runtime.update(
        {
            "status": "completed",
            "completed_at": int(time.time()),
            "continuity_summary": summary,
            "review_notes": review_notes.strip(),
            "manual_review_complete": True,
        }
    )
    save_series_state(root, state)
    write_series_event(root, {"kind": "episode_accepted", "episode_id": episode_id})
    return runtime


def series_status(root: Path) -> dict[str, Any]:
    series = load_series(root)
    state = load_series_state(root, series)
    episodes = []
    counts = {status: 0 for status in sorted(EPISODE_STATUSES)}
    for episode in episode_records(series):
        episode_id = str(episode.get("id", ""))
        runtime = _episode_runtime(state, episode_id)
        status = str(runtime.get("status", "draft"))
        counts[status] += 1
        episodes.append(
            {
                "id": episode_id,
                "number": episode.get("number"),
                "title": episode.get("title", ""),
                "status": status,
                "project": episode.get("project", ""),
                "final": runtime.get("final", ""),
                "technical_qa": runtime.get("technical_qa"),
            }
        )
    return {
        "series": str(root.resolve()),
        "title": series.get("title", ""),
        "video_mode": (series.get("defaults") or {}).get("video_mode"),
        "video_provider": (series.get("defaults") or {}).get("video_provider"),
        "counts": counts,
        "episodes": episodes,
        "characters": state.get("characters", {}),
    }


def series_context(root: Path, episode_id: str | None = None) -> dict[str, Any]:
    series = load_series(root)
    state = load_series_state(root, series)
    episode = get_episode(series, episode_id) if episode_id else select_next_episode(root)[0]
    project_root = episode_root(root, episode)
    planned = [
        {
            "id": item.get("id"),
            "number": item.get("number"),
            "title": item.get("title", ""),
            "synopsis": item.get("synopsis", ""),
            "status": _episode_runtime(state, str(item.get("id", ""))).get("status"),
        }
        for item in episode_records(series)
    ]
    previous = []
    for item in episode_records(series):
        if int(item.get("number", 0)) >= int(episode.get("number", 0)):
            break
        episode_runtime = _episode_runtime(state, str(item.get("id", "")))
        previous_root = episode_root(root, item)
        previous_project = read_json(previous_root / "project.json")
        qa_reports = ((episode_runtime.get("qa") or {}).get("reports") or [])
        review_artifacts = [
            {
                "kind": report.get("kind"),
                "id": report.get("id"),
                "ok": report.get("ok"),
                "errors": report.get("errors", []),
                "warnings": report.get("warnings", []),
                "review_frames": report.get("review_frames", []),
            }
            for report in qa_reports
            if isinstance(report, dict)
        ]
        previous.append(
            {
                "id": item.get("id"),
                "title": item.get("title", ""),
                "synopsis": item.get("synopsis", ""),
                "status": episode_runtime.get("status"),
                "continuity_summary": episode_runtime.get("continuity_summary", ""),
                "final": episode_runtime.get("final", ""),
                "project_path": str(previous_root / "project.json"),
                "story": previous_project.get("story", ""),
                "prompts": [
                    {
                        key: shot.get(key)
                        for key in (
                            "id",
                            "summary",
                            "scene_id",
                            "character_ids",
                            "continuity_notes",
                            "image_prompt",
                            "video_prompt",
                            "seconds",
                        )
                    }
                    for shot in previous_project.get("shots", [])
                    if isinstance(shot, dict)
                ],
                "review_artifacts": review_artifacts,
            }
        )
    return {
        "series": {
            key: series.get(key)
            for key in ("id", "title", "premise", "season_arc", "style_bible", "locations", "props", "characters", "defaults")
        },
        "planned_episodes": planned,
        "previous_episodes": previous,
        "current_episode": episode,
        "current_project": read_json(project_root / "project.json"),
        "current_project_path": str(project_root / "project.json"),
    }


def _file_signature(value: dict[str, Any], references: list[Path]) -> str:
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for path in references:
        digest.update(path.name.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def series_character_prompt(series: dict[str, Any], character: dict[str, Any]) -> str:
    master = series_character_master_config(series, character)
    sections = [
        "[CHARACTER IDENTITY]\n" + str(character.get("identity", "")).strip(),
        "[CANONICAL WARDROBE]\n" + str(character.get("wardrobe", "")).strip(),
        "[SINGLE-SHEET CHARACTER MASTER]\n" + str(master.get("prompt", "")).strip(),
    ]
    style = str(series.get("style_bible", "")).strip()
    if style:
        sections.append("[SERIES STYLE]\n" + style)
    return "\n\n".join(section for section in sections if section.split("\n", 1)[-1].strip())


def series_character_preflight(root: Path, series: dict[str, Any] | None = None) -> dict[str, Any]:
    series = series or load_series(root)
    state = load_series_state(root, series)
    prompts = []
    pending_requests = 0
    planned_requests = 0
    for character in character_records(series):
        character_id = str(character.get("id", ""))
        master = series_character_master_config(series, character)
        if not master["enabled"] or not master["generate"]:
            continue
        planned_requests += 1
        runtime = (state.get("characters", {}).get(character_id) or {})
        if runtime.get("status") != "completed":
            pending_requests += 1
        prompt = series_character_prompt(series, character)
        prompts.append(
            {
                "kind": "series_character_master",
                "id": character_id,
                "status": runtime.get("status", "pending"),
                "characters": len(prompt),
                "hard_limit": MAX_PROMPT_CHARS,
                "safe_limit": SAFE_PROMPT_CHARS,
                "remaining": MAX_PROMPT_CHARS - len(prompt),
                "within_hard_limit": len(prompt) <= MAX_PROMPT_CHARS,
            }
        )
    return {
        "planned_character_master_images": planned_requests,
        "pending_character_master_images": pending_requests,
        "prompts": prompts,
    }


def generate_series_characters(
    root: Path,
    *,
    character_ids: list[str] | None,
    retry_failed: bool,
    retry_reason: str,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if retry_failed and not retry_reason.strip():
        raise SkillError("--retry-reason is required with --retry-failed")
    series = load_series(root)
    errors = validate_series(root, series)
    if errors:
        raise SkillError("series validation failed: " + "; ".join(errors))
    state = load_series_state(root, series)
    settings = load_settings()
    if not settings.get("quickai_image_key"):
        raise SkillError("QuickAI image key is required for series character masters")
    client = QuickAIImageClient(
        settings["quickai_base_url"], settings["quickai_image_key"], settings["image_model"]
    )
    by_id = {str(item.get("id", "")): item for item in character_records(series)}
    requested = list(dict.fromkeys(character_ids or by_id.keys()))
    unknown = [character_id for character_id in requested if character_id not in by_id]
    if unknown:
        raise SkillError("unknown character id(s): " + ", ".join(unknown))
    limits = series.get("limits") if isinstance(series.get("limits"), dict) else {}
    maximum = int(limits.get("max_character_image_requests", 20))
    billable = sum(
        1
        for character_id in requested
        if bool(series_character_master_config(series, by_id[character_id])["enabled"])
        and bool(series_character_master_config(series, by_id[character_id])["generate"])
    )
    if billable > maximum:
        raise SkillError(f"character request count {billable} exceeds max_character_image_requests {maximum}")
    completed: list[str] = []
    skipped: list[str] = []
    for character_id in requested:
        character = by_id[character_id]
        master = series_character_master_config(series, character)
        runtime = state.setdefault("characters", {}).setdefault(character_id, {"status": "pending", "attempts": 0})
        if not bool(master["enabled"]):
            skipped.append(character_id)
            continue
        path_value = str(master.get("path", f"assets/character-masters/{character_id}.png"))
        target = resolve_series_path(root, path_value, must_exist=False)
        if not bool(master["generate"]):
            if not target.is_file():
                raise SkillError(f"external master is missing for {character_id}: {path_value}")
            runtime.update({"status": "completed", "path": target.relative_to(root).as_posix(), "source": "external"})
            save_series_state(root, state)
            skipped.append(character_id)
            continue
        references = [resolve_series_path(root, value) for value in master.get("source_references", [])]
        prompt = series_character_prompt(series, character)
        size = str(master.get("image_size", "1024x1024"))
        quality = str(master.get("image_quality", "auto"))
        current_signature = _file_signature(
            {"model": settings["image_model"], "prompt": prompt, "size": size, "quality": quality}, references
        )
        runtime_path = str(runtime.get("path", "")).strip()
        existing = resolve_series_path(root, runtime_path, must_exist=False) if runtime_path else target
        if runtime.get("status") == "completed" and existing.is_file() and runtime.get("signature") == current_signature:
            skipped.append(character_id)
            continue
        attempts = int(runtime.get("attempts", 0))
        if attempts > 0 and not retry_failed:
            raise SkillError(
                f"character master was already attempted for {character_id}; use --retry-failed with a reason to authorize another billable request"
            )
        runtime.update(
            {
                "status": "submitting",
                "attempts": attempts + 1,
                "signature": current_signature,
                "error": "",
                "retry_reason": retry_reason.strip() if attempts else "",
            }
        )
        save_series_state(root, state)
        write_series_event(root, {"kind": "character_master_create", "character_id": character_id, "attempt": attempts + 1})
        if on_progress:
            on_progress({"phase": "series_character", "character_id": character_id, "status": "submitting"})
        try:
            data = client.edit(prompt, references, size=size, quality=quality) if references else client.generate(
                prompt, size=size, quality=quality
            )
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
            save_series_state(root, state)
            completed.append(character_id)
            if on_progress:
                on_progress({"phase": "series_character", "character_id": character_id, "status": "completed"})
        except Exception as error:
            runtime.update({"status": "failed", "error": str(error)[:1000]})
            save_series_state(root, state)
            write_series_event(root, {"kind": "character_master_failed", "character_id": character_id, "error": str(error)[:1000]})
            raise
    synced = sync_all_episode_contracts(root, series)
    return {"completed": completed, "skipped": skipped, "synced_episodes": synced}
