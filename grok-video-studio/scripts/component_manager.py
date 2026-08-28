#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, config_dir, normalize_base_url


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_ROOT / "assets" / "components.json"
COMPONENT_SETTINGS_VERSION = 1
MODEL_STATE_VERSION = 1
MODEL_SAFETY_MARGIN_BYTES = 512 * 1024 * 1024


def load_manifest() -> dict[str, Any]:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"component manifest is invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SkillError("unsupported component manifest")
    return value


def component_settings_path() -> Path:
    return config_dir() / "components.json"


def model_state_path(models_root: Path) -> Path:
    return models_root.resolve() / ".gvs-model-state.json"


def load_model_state(models_root: Path) -> dict[str, Any]:
    path = model_state_path(models_root)
    if not path.is_file():
        return {"version": MODEL_STATE_VERSION, "models": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"model state is invalid: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != MODEL_STATE_VERSION
        or not isinstance(value.get("models"), dict)
    ):
        raise SkillError("unsupported model state")
    return value


def _model_key(component: str, model: dict[str, Any]) -> str:
    return f"{component}:{model.get('repository', '')}@{model.get('revision', '')}"


def _required_model_patterns(model: dict[str, Any]) -> list[str]:
    patterns = model.get("required_patterns") or model.get("allow_patterns") or []
    if not isinstance(patterns, list) or not all(isinstance(item, str) and item for item in patterns):
        raise SkillError(f"model required_patterns are invalid for {model.get('repository', '')}")
    if not patterns:
        raise SkillError(f"model has no required file patterns: {model.get('repository', '')}")
    return list(dict.fromkeys(patterns))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _model_file_records(target: Path, model: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]], int]:
    missing: list[str] = []
    records: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for pattern in _required_model_patterns(model):
        path = target / pattern
        if not path.is_file():
            missing.append(pattern)
            continue
        size = path.stat().st_size
        if size <= 0:
            missing.append(pattern)
            continue
        records[pattern] = {"bytes": size, "sha256": _sha256_file(path)}
        total_bytes += size
    return missing, records, total_bytes


def _model_files_ready(target: Path, model: dict[str, Any]) -> tuple[bool, list[str], int]:
    missing: list[str] = []
    total_bytes = 0
    for pattern in _required_model_patterns(model):
        path = target / pattern
        if not path.is_file():
            missing.append(pattern)
            continue
        size = path.stat().st_size
        if size <= 0:
            missing.append(pattern)
            continue
        total_bytes += size
    return not missing, missing, total_bytes


def _record_sizes_match_files(target: Path, model: dict[str, Any], record: dict[str, Any]) -> bool:
    files = record.get("files")
    if not isinstance(files, dict):
        return False
    for pattern in _required_model_patterns(model):
        entry = files.get(pattern)
        path = target / pattern
        if not isinstance(entry, dict) or not path.is_file():
            return False
        try:
            expected_size = int(entry.get("bytes", -1))
        except (TypeError, ValueError):
            return False
        if path.stat().st_size != expected_size:
            return False
    return True


def _record_matches_files(target: Path, model: dict[str, Any], record: dict[str, Any]) -> bool:
    files = record.get("files")
    if not isinstance(files, dict):
        return False
    required = _required_model_patterns(model)
    if any(pattern not in files for pattern in required):
        return False
    for pattern in required:
        entry = files.get(pattern)
        path = target / pattern
        if not isinstance(entry, dict) or not path.is_file():
            return False
        try:
            expected_size = int(entry.get("bytes", -1))
        except (TypeError, ValueError):
            return False
        if path.stat().st_size != expected_size or _sha256_file(path) != str(entry.get("sha256", "")):
            return False
    return True


def _recorded_files_unchanged(target: Path, record: dict[str, Any]) -> bool:
    files = record.get("files")
    if not isinstance(files, dict) or not files:
        return False
    resolved_target = target.resolve()
    for pattern, entry in files.items():
        if not isinstance(pattern, str) or not isinstance(entry, dict):
            return False
        path = (target / pattern).resolve()
        try:
            path.relative_to(resolved_target)
            expected_size = int(entry.get("bytes", -1))
        except (ValueError, TypeError):
            return False
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        if _sha256_file(path) != str(entry.get("sha256", "")):
            return False
    return True


def _ensure_model_disk_space(root: Path, model: dict[str, Any], existing_bytes: int) -> None:
    estimate_gb = float(model.get("estimated_size_gb", 0))
    if estimate_gb <= 0:
        return
    required = max(0, int(estimate_gb * 1024**3) - existing_bytes) + MODEL_SAFETY_MARGIN_BYTES
    usage = shutil.disk_usage(root)
    if usage.free < required:
        free_gb = usage.free / 1024**3
        needed_gb = required / 1024**3
        raise SkillError(
            f"insufficient disk space for {model.get('repository', '')}: {free_gb:.1f} GB free, "
            f"approximately {needed_gb:.1f} GB required including safety margin"
        )


def default_component_root() -> Path:
    override = os.environ.get("GVS_COMPONENT_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else (config_dir() / "components").resolve()


def default_component_settings() -> dict[str, Any]:
    manifest = load_manifest()
    root = default_component_root()
    components = manifest["components"]
    return {
        "version": COMPONENT_SETTINGS_VERSION,
        "profile": "core",
        "source_root": str(root / "src"),
        "models_root": str(root / "models"),
        "services": {name: data["service_url"] for name, data in components.items()},
    }


def load_component_settings() -> dict[str, Any]:
    path = component_settings_path()
    if not path.is_file():
        return default_component_settings()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"component settings are invalid: {error}") from error
    if not isinstance(value, dict) or value.get("version") != COMPONENT_SETTINGS_VERSION:
        raise SkillError("unsupported component settings version")
    return value


def save_component_settings(
    *,
    profile: str,
    source_root: Path | None = None,
    models_root: Path | None = None,
    cosyvoice_url: str | None = None,
    musetalk_url: str | None = None,
) -> dict[str, Any]:
    manifest = load_manifest()
    if profile not in manifest["profiles"]:
        raise SkillError("component profile must be core, native-dialogue, local-voice, or full-dialogue")
    settings = load_component_settings()
    settings["profile"] = profile
    if source_root is not None:
        settings["source_root"] = str(source_root.expanduser().resolve())
    if models_root is not None:
        settings["models_root"] = str(models_root.expanduser().resolve())
    services = settings.setdefault("services", {})
    for name, value in (("cosyvoice", cosyvoice_url), ("musetalk", musetalk_url)):
        if value is not None:
            normalized = normalize_base_url(value)
            hostname = urllib.parse.urlsplit(normalized).hostname
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise SkillError(f"{name} service must bind to a loopback URL")
            services[name] = normalized
    atomic_write_json(component_settings_path(), settings)
    return settings


def component_plan(profile: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    if profile not in manifest["profiles"]:
        raise SkillError("unknown component profile")
    settings = settings or load_component_settings()
    source_root = Path(str(settings["source_root"]))
    required = []
    model_download_gb = 0.0
    required_model_files = 0
    for name in manifest["profiles"][profile]:
        item = dict(manifest["components"][name])
        item.update({"id": name, "source": str(source_root / name), "service_url": settings["services"].get(name, item["service_url"])})
        for model in item.get("models", []):
            model_download_gb += float(model.get("estimated_size_gb", 0))
            required_model_files += len(model.get("required_patterns") or model.get("allow_patterns") or [])
        required.append(item)
    return {
        "profile": profile,
        "requires_local_services": bool(required),
        "components": required,
        "source_root": str(source_root),
        "models_root": str(settings["models_root"]),
        "model_download_gb": round(model_download_gb, 1),
        "required_model_files": required_model_files,
        "consent_required": bool(required),
        "note": "Source checkout is small; dependencies and model weights are installed only by an explicit service setup step.",
    }


def _git_output(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SkillError(f"git command failed: {detail}")
    return result.stdout.strip()


def _remove_staging_checkout(path: Path) -> None:
    def clear_readonly(function: Any, failing_path: str, _: Any) -> None:
        os.chmod(failing_path, stat.S_IWRITE)
        function(failing_path)

    shutil.rmtree(path, onerror=clear_readonly)


def _checkout_component(name: str, component: dict[str, Any], target: Path) -> dict[str, Any]:
    repository = str(component["repository"])
    commit = str(component["commit"])
    if target.exists():
        if not (target / ".git").is_dir():
            raise SkillError(f"component target exists but is not a git checkout: {target}")
        if _git_output("status", "--porcelain", cwd=target):
            raise SkillError(f"component checkout has local changes and was not modified: {target}")
        remote = _git_output("remote", "get-url", "origin", cwd=target)
        if remote.rstrip("/").lower() != repository.rstrip("/").lower():
            raise SkillError(f"component checkout uses a different origin: {target}")
        previous = _git_output("rev-parse", "HEAD", cwd=target)
        _git_output("fetch", "--depth", "1", "origin", commit, cwd=target)
        try:
            _git_output("checkout", "--detach", commit, cwd=target)
            if component.get("submodules"):
                _git_output("submodule", "update", "--init", "--recursive", "--depth", "1", cwd=target)
        except Exception as error:
            try:
                _git_output("checkout", "--detach", previous, cwd=target)
                if component.get("submodules"):
                    _git_output("submodule", "update", "--init", "--recursive", cwd=target)
            except Exception as rollback_error:
                raise SkillError(f"component update failed and rollback failed for {name}: {rollback_error}") from error
            raise
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".gvs-{name}-partial-", dir=str(target.parent)))
        try:
            _git_output("clone", "--filter=blob:none", "--no-checkout", repository, str(staging))
            _git_output("fetch", "--depth", "1", "origin", commit, cwd=staging)
            _git_output("checkout", "--detach", commit, cwd=staging)
            if component.get("submodules"):
                _git_output("submodule", "update", "--init", "--recursive", "--depth", "1", cwd=staging)
            staging.replace(target)
        finally:
            if staging.exists():
                _remove_staging_checkout(staging)
    actual = _git_output("rev-parse", "HEAD", cwd=target)
    if actual != commit:
        raise SkillError(f"component checkout did not reach pinned commit: {name}")
    return {"id": name, "source": str(target), "commit": actual, "status": "source-ready"}


def install_component_sources(profile: str, *, accept_downloads: bool) -> dict[str, Any]:
    if not accept_downloads:
        raise SkillError("component downloads require --accept-downloads after the user approves the selected profile")
    if not shutil.which("git"):
        raise SkillError("git is required to install optional component sources")
    settings = load_component_settings()
    settings["profile"] = profile
    atomic_write_json(component_settings_path(), settings)
    plan = component_plan(profile, settings)
    results = []
    manifest = load_manifest()
    for item in plan["components"]:
        results.append(_checkout_component(item["id"], manifest["components"][item["id"]], Path(item["source"])))
    return {"profile": profile, "installed": results, "next": "Run components doctor, then use the service setup commands documented for this profile."}


def _docker(*args: str, timeout: int = 14400, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise SkillError(f"docker command failed: {detail}")
    return result


def _download_component_models(
    name: str,
    component: dict[str, Any],
    *,
    image: str,
    models_root: Path,
) -> list[dict[str, Any]]:
    python = "/opt/conda/envs/cosyvoice/bin/python" if name == "cosyvoice" else "python3.10"
    root = models_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = load_model_state(root)
    state_models = state.setdefault("models", {})
    downloaded: list[dict[str, Any]] = []
    for model in component.get("models", []):
        repository = str(model["repository"])
        revision = str(model["revision"])
        key = _model_key(name, model)
        target = (root / str(model["destination"])).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SkillError(f"model destination escapes the configured models root: {target}") from error
        target.mkdir(parents=True, exist_ok=True)
        required_patterns = _required_model_patterns(model)
        record = state_models.get(key)
        if isinstance(record, dict) and record.get("revision") == revision and _record_matches_files(target, model, record):
            downloaded.append(
                {
                    "repository": repository,
                    "revision": revision,
                    "destination": str(target),
                    "bytes": int(record.get("total_bytes", 0)),
                    "status": "reused",
                }
            )
            continue
        ready, missing, existing_bytes = _model_files_ready(target, model)
        # A complete directory without state can be adopted. If state exists
        # but its digests no longer match, treat the directory as corrupted so
        # the pinned snapshot is downloaded again instead of silently blessing
        # changed bytes.
        record_can_migrate = (
            isinstance(record, dict)
            and record.get("revision") == revision
            and _recorded_files_unchanged(target, record)
        )
        if ready and (not isinstance(record, dict) or record_can_migrate):
            _, files, total_bytes = _model_file_records(target, model)
            state_models[key] = {
                "component": name,
                "repository": repository,
                "revision": revision,
                "destination": str(target),
                "status": "ready",
                "source": "existing",
                "total_bytes": total_bytes,
                "files": files,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            atomic_write_json(model_state_path(root), state)
            downloaded.append(
                {
                    "repository": repository,
                    "revision": revision,
                    "destination": str(target),
                    "bytes": total_bytes,
                    "status": "reused",
                }
            )
            continue
        _ensure_model_disk_space(root, model, existing_bytes)
        state_models[key] = {
            "component": name,
            "repository": repository,
            "revision": revision,
            "destination": str(target),
            "status": "downloading",
            "missing": missing,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write_json(model_state_path(root), state)
        allow_patterns = model.get("allow_patterns")
        if allow_patterns is not None and (
            not isinstance(allow_patterns, list) or not all(isinstance(value, str) and value for value in allow_patterns)
        ):
            raise SkillError(f"model allow_patterns is invalid for {repository}")
        script = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download(repo_id={repository!r}, revision={revision!r}, "
            f"local_dir='/models', allow_patterns={allow_patterns!r})"
        )
        try:
            _docker("run", "--rm", "-v", f"{target}:/models", image, python, "-c", script)
        except Exception as error:
            state_models[key] = {
                **state_models[key],
                "status": "failed",
                "error": str(error)[:1000],
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            atomic_write_json(model_state_path(root), state)
            raise
        missing, files, total_bytes = _model_file_records(target, model)
        if missing:
            state_models[key] = {
                **state_models[key],
                "status": "failed",
                "missing": missing,
                "error": "download completed without all required files",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            atomic_write_json(model_state_path(root), state)
            raise SkillError(f"model download completed without required files for {repository}: {', '.join(missing)}")
        state_models[key] = {
            "component": name,
            "repository": repository,
            "revision": revision,
            "destination": str(target),
            "status": "ready",
            "source": "download",
            "total_bytes": total_bytes,
            "files": files,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write_json(model_state_path(root), state)
        downloaded.append({"repository": repository, "revision": revision, "destination": str(target), "bytes": total_bytes, "status": "downloaded"})
    return downloaded


def setup_component_runtimes(profile: str, *, accept_downloads: bool, include_models: bool) -> dict[str, Any]:
    if not accept_downloads:
        raise SkillError("runtime and model downloads require --accept-downloads after the user approves disk and network use")
    if not shutil.which("docker"):
        raise SkillError("Docker Desktop with NVIDIA GPU support is required for managed local services")
    settings = load_component_settings()
    if settings.get("profile") != profile:
        settings["profile"] = profile
        atomic_write_json(component_settings_path(), settings)
    plan = component_plan(profile, settings)
    source_root = Path(str(settings["source_root"]))
    models_root = Path(str(settings["models_root"]))
    models_root.mkdir(parents=True, exist_ok=True)
    results = []
    for item in plan["components"]:
        name = str(item["id"])
        source = source_root / name
        if not source.is_dir():
            raise SkillError(f"component source is missing; run components-install first: {source}")
        dockerfile = SKILL_ROOT / "assets" / "docker" / f"{name}.Dockerfile"
        image = f"gvs-{name}:{str(item['commit'])[:12]}"
        _docker("build", "-f", str(dockerfile), "-t", image, str(source))
        model_result: str | list[dict[str, Any]] = "skipped"
        if include_models:
            model_result = _download_component_models(name, item, image=image, models_root=models_root)
        results.append({"id": name, "image": image, "models": model_result})
    return {"profile": profile, "runtimes": results, "models_downloaded": include_models, "next": "Run components-start, then components-doctor."}


def _container_name(component: str) -> str:
    return f"gvs-{component}-service"


def _prepare_container(name: str, image: str) -> bool:
    inspected = _docker("inspect", name, check=False)
    if inspected.returncode != 0:
        return False
    running = _docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip().lower() == "true"
    if running:
        actual_image = _docker("inspect", "-f", "{{.Config.Image}}", name).stdout.strip()
        if actual_image != image:
            raise SkillError(f"managed container {name} is running an unexpected image: {actual_image}")
        return True
    _docker("rm", name)
    return False


def _select_component_items(plan: dict[str, Any], component: str | None, *, require_explicit_multi: bool) -> list[dict[str, Any]]:
    items = list(plan["components"])
    if component == "all":
        return items
    if component:
        selected = [item for item in items if item["id"] == component]
        if not selected:
            raise SkillError(f"component {component} is not included in profile {plan['profile']}")
        return selected
    if require_explicit_multi and len(items) > 1:
        choices = ", ".join(str(item["id"]) for item in items)
        raise SkillError(
            f"profile {plan['profile']} contains multiple GPU-heavy services ({choices}); "
            "pass --component with one service for staged 8 GB operation, or --component all after confirming sufficient VRAM"
        )
    return items


def start_components(profile: str, component: str | None = None) -> dict[str, Any]:
    settings = load_component_settings()
    plan = component_plan(profile, settings)
    selected_items = _select_component_items(plan, component, require_explicit_multi=True)
    models_root = Path(str(settings["models_root"])).resolve()
    services_root = SKILL_ROOT / "scripts" / "services"
    started = []
    started_now: list[str] = []
    stopped_for_vram = []
    try:
        if len(selected_items) == 1 and len(plan["components"]) > 1:
            selected_id = str(selected_items[0]["id"])
            for sibling in plan["components"]:
                sibling_id = str(sibling["id"])
                if sibling_id == selected_id:
                    continue
                container = _container_name(sibling_id)
                if _docker("inspect", container, check=False).returncode == 0:
                    running = _docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip().lower() == "true"
                    if running:
                        _docker("stop", "--time", "20", container)
                    _docker("rm", container)
                    stopped_for_vram.append(container)
        for item in selected_items:
            name = str(item["id"])
            image = f"gvs-{name}:{str(item['commit'])[:12]}"
            container = _container_name(name)
            if _prepare_container(container, image):
                started.append({"id": name, "container": container, "container_id": "", "service_url": item["service_url"], "already_running": True})
                continue
            port = urllib.parse.urlsplit(str(item["service_url"])).port
            if not port:
                raise SkillError(f"component service URL has no port: {item['service_url']}")
            for model_spec in item.get("models", []):
                model_target = (models_root / str(model_spec["destination"])).resolve()
                ready, missing, _ = _model_files_ready(model_target, model_spec)
                if not ready:
                    raise SkillError(
                        f"{name} model is incomplete at {model_target}; missing or empty files: {', '.join(missing)}; "
                        "run components-setup --include-models"
                    )
            common = ["run", "-d", "--name", container, "--gpus", "all", "-p", f"127.0.0.1:{port}:{port}"]
            if name == "cosyvoice":
                model = models_root / "cosyvoice" / "Fun-CosyVoice3-0.5B-2512"
                if not model.is_dir():
                    raise SkillError(f"CosyVoice model is missing: {model}; run components-setup --include-models")
                wrapper = services_root / "cosyvoice_server.py"
                command = [
                    *common,
                    "-v",
                    f"{models_root / 'cosyvoice'}:/models:ro",
                    "-v",
                    f"{wrapper}:/opt/gvs/cosyvoice_server.py:ro",
                    image,
                    "/opt/conda/envs/cosyvoice/bin/python",
                    "/opt/gvs/cosyvoice_server.py",
                    "--source-root",
                    "/workspace/CosyVoice",
                    "--model-dir",
                    "/models/Fun-CosyVoice3-0.5B-2512",
                    "--port",
                    str(port),
                    "--host",
                    "0.0.0.0",
                ]
            else:
                model = models_root / "musetalk"
                if not model.is_dir():
                    raise SkillError(f"MuseTalk models are missing: {model}; run components-setup --include-models")
                wrapper = services_root / "musetalk_server.py"
                command = [
                    *common,
                    "-v",
                    f"{model}:/models:ro",
                    "-v",
                    f"{model}:/workspace/MuseTalk/models:ro",
                    "-v",
                    f"{wrapper}:/opt/gvs/musetalk_server.py:ro",
                    image,
                    "python3.10",
                    "/opt/gvs/musetalk_server.py",
                    "--source-root",
                    "/workspace/MuseTalk",
                    "--models-root",
                    "/models",
                    "--port",
                    str(port),
                    "--host",
                    "0.0.0.0",
                ]
            container_id = _docker(*command).stdout.strip()
            started_now.append(container)
            started.append({"id": name, "container": container, "container_id": container_id, "service_url": item["service_url"], "already_running": False})
    except Exception:
        for container in reversed(started_now):
            _docker("stop", "--time", "10", container, check=False)
            _docker("rm", container, check=False)
        raise
    return {"profile": profile, "component": component or "auto", "stopped_for_vram": stopped_for_vram, "started": started}


def stop_components(profile: str, component: str | None = None) -> dict[str, Any]:
    plan = component_plan(profile)
    selected_items = _select_component_items(plan, component, require_explicit_multi=False)
    stopped = []
    for item in selected_items:
        name = _container_name(str(item["id"]))
        inspected = _docker("inspect", name, check=False)
        if inspected.returncode != 0:
            continue
        _docker("stop", "--time", "20", name)
        _docker("rm", name)
        stopped.append(name)
    return {"profile": profile, "component": component or "all", "stopped": stopped}


def _service_health(base_url: str) -> dict[str, Any]:
    url = normalize_base_url(base_url) + "/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = response.read(65536)
            text = payload.decode("utf-8", "replace")
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = {}
            reported_ok = value.get("ok", True) if isinstance(value, dict) else True
            return {"ok": response.status == 200 and reported_ok is not False, "status": response.status, "response": value or text[:500]}
    except (OSError, urllib.error.URLError) as error:
        return {"ok": False, "error": str(error)[:500]}


def component_status(profile: str | None = None, component: str | None = None) -> dict[str, Any]:
    settings = load_component_settings()
    selected = profile or str(settings.get("profile", "core"))
    plan = component_plan(selected, settings)
    selected_items = _select_component_items(plan, component, require_explicit_multi=False)
    models_root = Path(str(settings["models_root"])).resolve()
    model_state = load_model_state(models_root)
    components = []
    for item in selected_items:
        source_target = Path(item["source"])
        actual = ""
        if (source_target / ".git").is_dir():
            try:
                actual = _git_output("rev-parse", "HEAD", cwd=source_target)
            except SkillError:
                actual = "unreadable"
        models = []
        for model in item.get("models", []):
            target = (models_root / str(model["destination"])).resolve()
            try:
                target.relative_to(models_root)
            except ValueError:
                models.append({"repository": str(model.get("repository", "")), "ready": False, "error": "destination escapes models root"})
                continue
            record = model_state.get("models", {}).get(_model_key(str(item["id"]), model))
            ready, missing, total_bytes = _model_files_ready(target, model)
            recorded = isinstance(record, dict) and _record_sizes_match_files(target, model, record)
            models.append(
                {
                    "repository": str(model.get("repository", "")),
                    "revision": str(model.get("revision", "")),
                    "destination": str(target),
                    "ready": ready and (recorded or not isinstance(record, dict)),
                    "missing": missing,
                    "bytes": total_bytes,
                    "state": record.get("status", "untracked") if isinstance(record, dict) else "untracked",
                }
            )
        components.append(
            {
                "id": item["id"],
                "source": str(source_target),
                "source_ready": actual == item["commit"],
                "expected_commit": item["commit"],
                "actual_commit": actual,
                "service_url": item["service_url"],
                "service": _service_health(item["service_url"]),
                "models": models,
            }
        )
    return {
        "ok": all(
            item["source_ready"]
            and item["service"]["ok"]
            and all(model["ready"] for model in item["models"])
            for item in components
        ),
        "profile": selected,
        "component": component or "all",
        "config": str(component_settings_path()),
        "components": components,
        "ffmpeg": shutil.which("ffmpeg") or "not_found",
        "docker": shutil.which("docker") or "not_found",
        "wsl": shutil.which("wsl") or "not_found",
    }
