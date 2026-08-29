#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from gvs_common import SkillError


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "assets" / "voice-components.json"


def _manifest() -> dict[str, Any]:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"voice component manifest is invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SkillError("unsupported voice component manifest")
    return value


def _command(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return result.returncode, (result.stdout or result.stderr).strip()


def _git_status(source: Path, expected_commit: str, expected_repository: str) -> dict[str, Any]:
    if not (source / ".git").is_dir():
        return {"exists": source.exists(), "git": False, "ready": False, "error": "source is not a Git checkout"}
    code, head = _command("git", "rev-parse", "HEAD", cwd=source)
    status_code, status = _command("git", "status", "--short", cwd=source)
    remote_code, remote = _command("git", "remote", "get-url", "origin", cwd=source)
    origin_matches = remote.rstrip("/").removesuffix(".git").lower() == expected_repository.rstrip("/").removesuffix(".git").lower()
    status_lines = [line for line in status.splitlines() if line.strip()]
    relevant_changes = [line for line in status_lines if line.strip() != "?? .vs/"]
    ready = code == 0 and status_code == 0 and head == expected_commit and origin_matches
    return {
        "exists": True,
        "git": code == 0,
        "head": head if code == 0 else "",
        "expected_commit": expected_commit,
        "pinned": head == expected_commit,
        "dirty": bool(status_lines) if status_code == 0 else None,
        "preserved_user_paths": [".vs/"] if any(line.strip() == "?? .vs/" for line in status_lines) else [],
        "relevant_changes": relevant_changes,
        "status": status,
        "origin": remote if remote_code == 0 else "",
        "origin_matches": origin_matches,
        "ready": ready,
    }


def _python_status(source: Path) -> dict[str, Any]:
    uv = shutil.which("uv")
    uv_version = _command(uv, "--version")[1] if uv else ""
    python = source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version = _command(str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))")[1] if python.is_file() else ""
    return {
        "uv": uv or "",
        "uv_version": uv_version,
        "runtime_python": str(python),
        "runtime_exists": python.is_file(),
        "runtime_version": version,
        "runtime_compatible": version.startswith("3.12."),
    }


def _gpu_status() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "error": "nvidia-smi is unavailable"}
    code, output = _command(
        executable,
        "--query-gpu=name,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    )
    if code != 0:
        return {"available": False, "error": output}
    rows = []
    for line in output.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) == 3:
            rows.append({"name": parts[0], "total_mib": int(parts[1]), "free_mib": int(parts[2])})
    return {"available": bool(rows), "gpus": rows}


def _model_status(models_root: Path, model: dict[str, Any]) -> dict[str, Any]:
    repo = str(model["repository"])
    cache = models_root / ("models--" + repo.replace("/", "--"))
    revision = ""
    main_ref = cache / "refs" / "main"
    if main_ref.is_file():
        revision = main_ref.read_text(encoding="utf-8", errors="replace").strip()
    snapshot = cache / "snapshots" / str(model["revision"])
    required = [str(item) for item in model.get("required_patterns", [])]
    missing = [item for item in required if not (snapshot / item).is_file() or (snapshot / item).stat().st_size <= 0]
    downloaded = snapshot.is_dir() and not missing
    return {
        **model,
        "cache": str(cache),
        "downloaded": downloaded,
        "missing_files": missing,
        "cached_main_revision": revision,
        "revision_matches": downloaded or revision == str(model["revision"]),
    }


def _service_health(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        return {"running": True, "health": payload}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return {"running": False, "error": type(error).__name__}


def voicebox_setup_plan(source: Path, models_root: Path, data_root: Path) -> dict[str, Any]:
    provider = _manifest()["providers"]["voicebox"]
    source = source.expanduser().resolve()
    models_root = models_root.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    source_status = _git_status(source, str(provider["commit"]), str(provider["repository"]))
    python_status = _python_status(source)
    models = [_model_status(models_root, item) for item in provider["models"]]
    disk_probe = models_root
    while not disk_probe.exists() and disk_probe != disk_probe.parent:
        disk_probe = disk_probe.parent
    try:
        disk = shutil.disk_usage(disk_probe)
        free_gb: float | None = round(disk.free / 1024**3, 2)
    except OSError:
        free_gb = None
    ready = bool(source_status.get("ready") and python_status["runtime_compatible"] and all(item["downloaded"] for item in models))
    return {
        "ok": True,
        "provider": "voicebox",
        "side_effects": "none",
        "source": source_status,
        "python": python_status,
        "gpu": _gpu_status(),
        "storage": {
            "models_root": str(models_root),
            "data_root": str(data_root),
            "free_gb": free_gb,
            "planned_download_gb": sum(float(item["estimated_size_gb"]) for item in models if not item["downloaded"]),
        },
        "models": models,
        "service": {"url": provider["service_url"], **_service_health(str(provider["service_url"]))},
        "service_environment": provider.get("service_environment", {}),
        "ready": ready,
        "approval_boundaries": [
            "create_or_repair_isolated_python_3.12_environment",
            "install_voicebox_backend_dependencies",
            "download_only_the_pinned_qwen_custom_voice_0.6b_model",
            "start_loopback_voicebox_backend",
            "generate_two_character_auditions",
        ],
        "codex_managed": True,
        "next": "After explicit approval, Codex can execute the listed boundaries in order; users do not need to run them manually.",
    }
