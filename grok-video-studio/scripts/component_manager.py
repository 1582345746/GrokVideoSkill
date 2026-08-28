#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, config_dir, normalize_base_url


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_ROOT / "assets" / "components.json"
COMPONENT_SETTINGS_VERSION = 1


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
    for name in manifest["profiles"][profile]:
        item = dict(manifest["components"][name])
        item.update({"id": name, "source": str(source_root / name), "service_url": settings["services"].get(name, item["service_url"])})
        required.append(item)
    return {
        "profile": profile,
        "requires_local_services": bool(required),
        "components": required,
        "source_root": str(source_root),
        "models_root": str(settings["models_root"]),
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
        _git_output("fetch", "--depth", "1", "origin", commit, cwd=target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        _git_output("clone", "--filter=blob:none", "--no-checkout", repository, str(target))
        _git_output("fetch", "--depth", "1", "origin", commit, cwd=target)
    _git_output("checkout", "--detach", commit, cwd=target)
    if component.get("submodules"):
        _git_output("submodule", "update", "--init", "--recursive", "--depth", "1", cwd=target)
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
) -> list[dict[str, str]]:
    python = "/opt/conda/envs/cosyvoice/bin/python" if name == "cosyvoice" else "python3.10"
    root = models_root.resolve()
    downloaded = []
    for model in component.get("models", []):
        repository = str(model["repository"])
        revision = str(model["revision"])
        target = (root / str(model["destination"])).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SkillError(f"model destination escapes the configured models root: {target}") from error
        target.mkdir(parents=True, exist_ok=True)
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
        _docker("run", "--rm", "-v", f"{target}:/models", image, python, "-c", script)
        if allow_patterns:
            missing = [pattern for pattern in allow_patterns if not (target / pattern).is_file()]
            if missing:
                raise SkillError(f"model download completed without required files for {repository}: {', '.join(missing)}")
            empty = [pattern for pattern in allow_patterns if (target / pattern).stat().st_size <= 0]
            if empty:
                raise SkillError(f"model download produced empty required files for {repository}: {', '.join(empty)}")
        total_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
        downloaded.append({"repository": repository, "revision": revision, "destination": str(target), "bytes": total_bytes})
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
        model_result: str | list[dict[str, str]] = "skipped"
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
    components = []
    for item in selected_items:
        target = Path(item["source"])
        actual = ""
        if (target / ".git").is_dir():
            try:
                actual = _git_output("rev-parse", "HEAD", cwd=target)
            except SkillError:
                actual = "unreadable"
        components.append(
            {
                "id": item["id"],
                "source": str(target),
                "source_ready": actual == item["commit"],
                "expected_commit": item["commit"],
                "actual_commit": actual,
                "service_url": item["service_url"],
                "service": _service_health(item["service_url"]),
            }
        )
    return {
        "ok": all(item["source_ready"] and item["service"]["ok"] for item in components),
        "profile": selected,
        "component": component or "all",
        "config": str(component_settings_path()),
        "components": components,
        "ffmpeg": shutil.which("ffmpeg") or "not_found",
        "docker": shutil.which("docker") or "not_found",
        "wsl": shutil.which("wsl") or "not_found",
    }
