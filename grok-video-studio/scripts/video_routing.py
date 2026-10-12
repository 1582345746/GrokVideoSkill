from __future__ import annotations

from typing import Any


DEFAULT_VIDEO_MODEL = "seedance-2.0-mini-720p"
LEGACY_VIDEO_MODEL = "grok-imagine-video-1.5"
XINGAPI_SEEDANCE_REMOTE_MODEL = "seedance-2.0-mini-720P"

DEFAULT_MODEL_ROUTES: dict[str, dict[str, Any]] = {
    DEFAULT_VIDEO_MODEL: {
        "family": "seedance",
        "display_name": "Seedance-2.0-Mini-720P",
        "default_upstream": "sub2api",
        "upstreams": ["sub2api", "newapi"],
    },
    LEGACY_VIDEO_MODEL: {
        "family": "grok",
        "display_name": "Grok Imagine Video 1.5",
        "default_upstream": "sub2api",
        "upstreams": ["sub2api", "newapi"],
    },
}


def effective_video_model(
    project: dict[str, Any],
    shot: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> str:
    """Resolve a pinned model while preserving projects written before model selection existed."""
    runtime_status = str(runtime.get("status", "")).strip().lower() if isinstance(runtime, dict) else ""
    task_is_pinned = isinstance(runtime, dict) and (
        bool(str(runtime.get("task_id", "")).strip())
        or runtime_status in {"queued", "running", "processing", "submission_unknown", "poll_timeout"}
    )
    sources = (runtime, shot, project) if task_is_pinned else (shot, project, runtime)
    for source in sources:
        if isinstance(source, dict):
            value = str(source.get("video_model", source.get("model", ""))).strip()
            if value:
                return value
    return LEGACY_VIDEO_MODEL


def upstream_registry(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    configured = settings.get("video_upstreams")
    if isinstance(configured, dict) and configured:
        return {
            str(upstream_id): dict(value)
            for upstream_id, value in configured.items()
            if isinstance(value, dict)
        }
    result: dict[str, dict[str, Any]] = {}
    if settings.get("sub2api_video_base_url") or settings.get("sub2api_video_key"):
        result["sub2api"] = {
            "id": "sub2api",
            "adapter": "sub2api-json",
            "provider_kind": "sub2api",
            "base_url": settings.get("sub2api_video_base_url", ""),
            "key": settings.get("sub2api_video_key", ""),
            "model_map": {},
        }
    if settings.get("newapi_video_base_url") or settings.get("newapi_video_key"):
        result["newapi"] = {
            "id": "newapi",
            "adapter": "newapi-multipart",
            "provider_kind": "newapi",
            "base_url": settings.get("newapi_video_base_url", ""),
            "key": settings.get("newapi_video_key", ""),
            "model_map": {},
        }
    return result


def _route_spec(settings: dict[str, Any], model: str) -> dict[str, Any]:
    routes = settings.get("video_model_routes")
    value = routes.get(model) if isinstance(routes, dict) else None
    if isinstance(value, dict):
        return dict(value)
    return dict(DEFAULT_MODEL_ROUTES.get(model, {
        "family": "custom",
        "display_name": model,
        "default_upstream": "sub2api",
        "upstreams": ["sub2api", "newapi"],
    }))


def route_candidates(
    project: dict[str, Any],
    settings: dict[str, Any],
    model: str,
    *,
    mode: str,
) -> list[str]:
    """Resolve configured upstream IDs for a model without permitting cross-model fallback."""
    capability = "text_to_video" if mode == "text-to-video" else "image_to_video"
    registry = upstream_registry(settings)
    route = _route_spec(settings, model)
    configured_ids = route.get("upstreams", [])
    if not isinstance(configured_ids, list):
        configured_ids = []
    configured_ids = [str(item).strip() for item in configured_ids if str(item).strip()]

    project_route = project.get("video_route") if isinstance(project.get("video_route"), dict) else {}
    requested_id = str(project_route.get("preferred_upstream") or project.get("video_upstream", "")).strip()
    if requested_id and requested_id not in configured_ids:
        raise ValueError(f"video upstream {requested_id!r} is not routed for model {model!r}")
    route_policy = str(project_route.get("policy", "")).strip().lower()
    legacy_policy = str(project.get("video_provider_policy", "")).strip().lower()
    legacy_provider = str(project.get("video_provider", "")).strip().lower()
    explicit_preference = bool(requested_id or legacy_provider)
    if requested_id:
        preferred = requested_id
    elif legacy_provider:
        matches = [
            upstream_id
            for upstream_id in configured_ids
            if str(registry.get(upstream_id, {}).get("provider_kind", upstream_id)).lower() == legacy_provider
        ]
        preferred = matches[0] if matches else legacy_provider
    else:
        preferred = str(route.get("default_upstream", "sub2api")).strip()

    if requested_id:
        policy = route_policy or legacy_policy or "fixed"
    elif legacy_provider:
        policy = legacy_policy or route_policy or ("fixed" if legacy_provider == "newapi" else "automatic")
    else:
        policy = route_policy or legacy_policy or "automatic"
    if policy not in {"fixed", "automatic"}:
        raise ValueError("video route policy must be automatic or fixed")

    candidates = [preferred]
    candidates.extend(item for item in configured_ids if item != preferred)
    available: list[str] = []
    for upstream_id in candidates:
        upstream = registry.get(upstream_id)
        if (
            not isinstance(upstream, dict)
            or not upstream.get("enabled", True)
            or not upstream.get("key")
            or not upstream.get("base_url")
        ):
            continue
        capabilities = upstream.get("capabilities")
        if isinstance(capabilities, dict) and capabilities.get(capability) is False:
            continue
        model_map = upstream.get("model_map")
        supported_models = upstream.get("models")
        if isinstance(model_map, dict) and model_map and model not in model_map:
            continue
        if isinstance(supported_models, list) and supported_models and model not in supported_models:
            continue
        available.append(upstream_id)
    if not available:
        raise ValueError(f"no configured upstream supports video model {model!r}")
    if policy == "fixed":
        if preferred not in available:
            reason = "not configured or unavailable" if preferred not in registry else "does not support this model or mode"
            raise ValueError(f"video upstream {preferred!r} {reason}")
        return [preferred]
    return available
