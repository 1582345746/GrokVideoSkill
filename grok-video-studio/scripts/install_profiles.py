#!/usr/bin/env python3
"""Machine-readable installation capability profiles.

This module is deliberately side-effect free. The installer and Codex can use
its plan to explain dependencies before any checkout, model download, or
service start is attempted.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, config_dir


SKILL_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PROFILE_PATH = SKILL_ROOT / "assets" / "install-profiles.json"
INSTALL_PROFILE_VERSION = 1
INSTALL_PROFILE_IDS = ("basic", "upstream-dialogue", "precise-subtitles", "precise-voice", "lip-sync")
PROFILE_SETTINGS_VERSION = 1


def load_install_manifest() -> dict[str, Any]:
    try:
        value = json.loads(INSTALL_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"install profile manifest is invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != INSTALL_PROFILE_VERSION:
        raise SkillError("unsupported install profile manifest")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise SkillError("install profile manifest has no profiles")
    aliases = value.get("aliases", {})
    if not isinstance(aliases, dict):
        raise SkillError("install profile aliases must be an object")
    for profile_id in INSTALL_PROFILE_IDS:
        if profile_id not in profiles:
            raise SkillError(f"install profile manifest is missing {profile_id}")
    return value


def resolve_install_profile(profile: str) -> tuple[str, dict[str, Any]]:
    manifest = load_install_manifest()
    requested = str(profile or "").strip().lower()
    canonical = str(manifest.get("aliases", {}).get(requested, requested))
    value = manifest["profiles"].get(canonical)
    if not isinstance(value, dict):
        choices = ", ".join(INSTALL_PROFILE_IDS)
        raise SkillError(f"unknown install profile {requested!r}; choose one of {choices}")
    return canonical, value


def _dependency_status(dependency: str) -> dict[str, Any]:
    executable = {"ffmpeg": "ffmpeg", "docker": "docker", "nvidia-gpu": "nvidia-smi"}.get(dependency)
    installed = bool(executable and shutil.which(executable))
    methods = {
        "ffmpeg": "Install FFmpeg and ffprobe, or expose them on PATH.",
        "docker": "Install Docker Desktop with the WSL2 backend.",
        "nvidia-gpu": "Install a compatible NVIDIA driver and enable Docker GPU passthrough.",
    }
    return {
        "id": dependency,
        "installed": installed,
        "required": True,
        "install_hint": methods.get(dependency, "Install the dependency before using this profile."),
    }


def install_profile_plan(profile: str = "basic") -> dict[str, Any]:
    canonical, value = resolve_install_profile(profile)
    dependencies = [_dependency_status(str(item)) for item in value.get("dependencies", [])]
    missing = [item["id"] for item in dependencies if not item["installed"]]
    component_profile = str(value.get("component_profile", "core"))
    requires_downloads = component_profile in {"local-voice", "full-dialogue"}
    return {
        "requested_profile": str(profile),
        "profile": canonical,
        "title": str(value.get("title", canonical)),
        "description": str(value.get("description", "")),
        "component_profile": component_profile,
        "audio_mode": str(value.get("audio_mode", "preserve")),
        "subtitle_source": str(value.get("subtitle_source", "upstream")),
        "key_roles": [str(item) for item in value.get("key_roles", [])],
        "dependencies": dependencies,
        "missing_dependencies": missing,
        "model_download_gb": float(value.get("model_download_gb", 0)),
        "gpu_required": bool(value.get("gpu_required", False)),
        "requires_component_downloads": requires_downloads,
        "consent_required": requires_downloads,
        "actions": [
            "copy_skill",
            "configure_provider_keys",
            "run_provider_and_media_diagnostics",
            *(["install_pinned_component_sources", "build_runtime", "download_pinned_models"] if requires_downloads else []),
        ],
        "side_effects": "none",
    }


def install_profile_settings_path() -> Path:
    return config_dir() / "install-profile.json"


def save_install_profile(profile: str) -> dict[str, Any]:
    plan = install_profile_plan(profile)
    value = {
        "version": PROFILE_SETTINGS_VERSION,
        "profile": plan["profile"],
        "component_profile": plan["component_profile"],
        "audio_mode": plan["audio_mode"],
        "subtitle_source": plan["subtitle_source"],
    }
    atomic_write_json(install_profile_settings_path(), value)
    return value


def load_install_profile() -> dict[str, Any]:
    path = install_profile_settings_path()
    if not path.is_file():
        return {"version": PROFILE_SETTINGS_VERSION, "profile": "basic", "component_profile": "core"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"install profile settings are invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != PROFILE_SETTINGS_VERSION:
        raise SkillError("unsupported install profile settings")
    return value
