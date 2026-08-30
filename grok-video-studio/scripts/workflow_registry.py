#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from gvs_common import SkillError


ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DIRECTOR_MODES = {
    "single-shot",
    "cinematic-short",
    "dialogue-scene",
    "silent-cinema",
    "action-scene",
    "montage",
    "product-ad",
    "performance",
    "comedy-scene",
    "news-report",
}
PROJECT_TYPES = {"single-clip", "cinematic-short", "episodic-series", "sourced-news"}
GENRE_PACKS = {
    "historical",
    "wuxia",
    "sci-fi",
    "family",
    "romance",
    "comedy",
    "disaster",
    "rural",
    "suspense",
}


def load_genre_packs(root: Path) -> dict[str, dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        value = _read_template(path, source="builtin-genre")
        pack_id = str(value.get("id", "")).strip()
        if pack_id not in GENRE_PACKS:
            raise SkillError(f"unsupported genre pack id in {path.name}: {pack_id}")
        if pack_id in packs:
            raise SkillError(f"duplicate genre pack id: {pack_id}")
        for field in ("title", "visual", "performance", "camera", "audio", "avoid"):
            if not str(value.get(field, "")).strip():
                raise SkillError(f"genre pack {pack_id} requires {field}")
        packs[pack_id] = value
    missing = sorted(GENRE_PACKS - set(packs))
    if missing:
        raise SkillError("missing built-in genre packs: " + ", ".join(missing))
    return packs


def default_custom_workflow_root() -> Path:
    configured = os.environ.get("GVS_WORKFLOW_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "GrokVideoSkill" / "workflows").resolve()


def _read_template(path: Path, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SkillError(f"workflow template does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise SkillError(f"invalid workflow template {path.name} at line {error.lineno}") from error
    if not isinstance(value, dict):
        raise SkillError(f"workflow template root must be an object: {path.name}")
    value = deepcopy(value)
    value["_source"] = source
    value["_path"] = str(path.resolve())
    return value


def _merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def workflow_errors(workflow: dict[str, Any], *, expected_id: str | None = None) -> list[str]:
    errors: list[str] = []
    workflow_id = str(workflow.get("id", "")).strip()
    if not ID_RE.fullmatch(workflow_id):
        errors.append("id must use lowercase letters, digits, and hyphens")
    if expected_id and workflow_id != expected_id:
        errors.append(f"resolved id must remain {expected_id}")
    if not str(workflow.get("title", "")).strip():
        errors.append("title is required")
    for field, minimum, maximum in (
        ("default_shots", 1, 24),
        ("preferred_clip_seconds", 1, 15),
    ):
        try:
            value = int(workflow.get(field, 0))
        except (TypeError, ValueError):
            value = 0
        if value < minimum or value > maximum:
            errors.append(f"{field} must be an integer from {minimum} to {maximum}")
    if str(workflow.get("project_type", "single-clip")) not in PROJECT_TYPES:
        errors.append("project_type is unsupported")
    if str(workflow.get("director_mode", "single-shot")) not in DIRECTOR_MODES:
        errors.append("director_mode is unsupported")
    genre_packs = workflow.get("genre_packs", [])
    if not isinstance(genre_packs, list) or not all(str(value) in GENRE_PACKS for value in genre_packs):
        errors.append("genre_packs must contain supported genre pack ids")
    routes = workflow.get("routes", ["text-to-video", "image-to-video"])
    if not isinstance(routes, list) or not routes or not all(value in {"text-to-video", "image-to-video"} for value in routes):
        errors.append("routes must contain text-to-video and/or image-to-video")
    if not isinstance(workflow.get("guidance", {}), dict):
        errors.append("guidance must be an object")
    if not isinstance(workflow.get("shot_defaults", {}), dict):
        errors.append("shot_defaults must be an object")
    return errors


def load_workflows(builtin_root: Path, custom_root: Path | None = None) -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    for path in sorted(builtin_root.glob("*.json")):
        value = _read_template(path, source="builtin")
        workflow_id = str(value.get("id", "")).strip()
        if workflow_id in raw:
            raise SkillError(f"duplicate built-in workflow id: {workflow_id}")
        raw[workflow_id] = value
    selected_custom_root = custom_root or default_custom_workflow_root()
    if selected_custom_root.is_dir():
        for path in sorted(selected_custom_root.glob("*.json")):
            value = _read_template(path, source="custom")
            workflow_id = str(value.get("id", "")).strip()
            if workflow_id in raw:
                raise SkillError(
                    f"custom workflow '{workflow_id}' conflicts with an existing workflow; use a new id and extends"
                )
            raw[workflow_id] = value
    if not raw:
        raise SkillError(f"no workflow templates found: {builtin_root}")

    resolved: dict[str, dict[str, Any]] = {}
    active: list[str] = []

    def resolve(workflow_id: str) -> dict[str, Any]:
        if workflow_id in resolved:
            return resolved[workflow_id]
        if workflow_id in active:
            raise SkillError("workflow inheritance cycle: " + " -> ".join([*active, workflow_id]))
        if workflow_id not in raw:
            raise SkillError(f"workflow extends unknown id: {workflow_id}")
        active.append(workflow_id)
        current = raw[workflow_id]
        parent_id = str(current.get("extends", "")).strip()
        value = _merge(resolve(parent_id), current) if parent_id else deepcopy(current)
        active.pop()
        errors = workflow_errors(value, expected_id=workflow_id)
        if errors:
            raise SkillError(f"invalid workflow '{workflow_id}': " + "; ".join(errors))
        resolved[workflow_id] = value
        return value

    for workflow_id in raw:
        resolve(workflow_id)
    return resolved


def validate_workflow_file(path: Path, builtin_root: Path) -> dict[str, Any]:
    candidate = _read_template(path.resolve(), source="candidate")
    workflow_id = str(candidate.get("id", "")).strip()
    parent_id = str(candidate.get("extends", "")).strip()
    if parent_id:
        builtins = load_workflows(builtin_root, custom_root=Path("__gvs_no_custom_workflows__"))
        if parent_id not in builtins:
            raise SkillError(f"candidate extends unknown built-in workflow: {parent_id}")
        candidate = _merge(builtins[parent_id], candidate)
    errors = workflow_errors(candidate, expected_id=workflow_id)
    return {
        "ok": not errors,
        "id": workflow_id,
        "path": str(path.resolve()),
        "errors": errors,
        "resolved": candidate if not errors else None,
    }
