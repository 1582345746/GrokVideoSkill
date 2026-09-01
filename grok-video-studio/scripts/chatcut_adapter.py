#!/usr/bin/env python3
"""ChatCut integration contracts that are safe to use from the standalone CLI.

The ChatCut MCP tools are task-scoped and are therefore invoked by the host
agent, not by this Python process.  This module owns the portable boundary:
installation discovery, semantic edit-plan mapping, and hash-bound render
receipt validation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from gvs_common import SkillError, assert_mp4, atomic_write_json
from media_tools import probe_media, quality_report


CHATCUT_ADAPTER_VERSION = 1
CHATCUT_PLUGIN_NAME = "chatcut"
CHATCUT_MCP_SERVER = "chatcut"
CHATCUT_TOOL_PREFIX = "mcp__chatcut__"
CHATCUT_PLUGIN_REPOSITORY = "https://github.com/ChatCut-Inc/agent-plugin.git"

# These are semantic capabilities documented by the ChatCut plugin.  The
# adapter deliberately does not hard-code undocumented argument shapes.
CHATCUT_CORE_TOOLS = (
    "list_projects",
    "create_project",
    "target_project",
    "read_project",
    "browse_assets",
    "import_media",
    "manage_timelines",
    "edit_item",
    "preview_timeline",
)
CHATCUT_EXPORT_TOOLS = ("submit_export", "track_export")
CHATCUT_OPTIONAL_TOOLS = (
    "inspect_item",
    "inspect_asset",
    "manage_media_pool",
    "browse_library",
    "render_cloud_screenshot",
    "edit_captions",
)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _codex_root() -> Path:
    configured = str(os.environ.get("CODEX_HOME", "")).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _plugin_manifests(codex_root: Path | None = None) -> list[Path]:
    root = (codex_root or _codex_root()).expanduser()
    candidates: list[Path] = []
    cache_root = root / "plugins" / "cache" / "chatcut-inc" / "chatcut"
    if cache_root.is_dir():
        candidates.extend(sorted(cache_root.glob("*/.codex-plugin/plugin.json"), reverse=True))
    marketplace_root = root / ".tmp" / "marketplaces"
    if marketplace_root.is_dir():
        candidates.extend(sorted(marketplace_root.glob("*/codex/.codex-plugin/plugin.json"), reverse=True))
    # Some Codex installations use a materialized plugin directory instead of
    # the cache layout.  Keep this bounded to known plugin roots.
    for parent in (root / "plugins", root / "plugins" / "installed"):
        if parent.is_dir():
            candidates.extend(sorted(parent.glob("**/chatcut/.codex-plugin/plugin.json"), reverse=True))
            candidates.extend(sorted(parent.glob("**/chatcut-*/.codex-plugin/plugin.json"), reverse=True))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()).casefold()
        if key not in seen and path.is_file():
            seen.add(key)
            unique.append(path)
    return unique


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _normalize_tool_names(tool_names: Iterable[str] | None) -> tuple[set[str] | None, str]:
    if tool_names is None:
        return None, "standalone-cli-unknown"
    normalized: set[str] = set()
    for value in tool_names:
        name = str(value).strip()
        if name.startswith(CHATCUT_TOOL_PREFIX):
            name = name[len(CHATCUT_TOOL_PREFIX) :]
        if name:
            normalized.add(name)
    return normalized, ("available" if normalized else "missing")


def detect_chatcut_installation(
    *, codex_root: Path | None = None, tool_names: Iterable[str] | None = None
) -> dict[str, Any]:
    """Report installation and task-tool state without reading OAuth secrets."""
    manifests = _plugin_manifests(codex_root)
    plugins: list[dict[str, Any]] = []
    for manifest_path in manifests:
        manifest = _read_object(manifest_path) or {}
        if str(manifest.get("name", "")).strip().lower() != CHATCUT_PLUGIN_NAME:
            continue
        mcp_path = manifest_path.parent.parent / ".mcp.json"
        mcp = _read_object(mcp_path) or {}
        servers = mcp.get("mcpServers") if isinstance(mcp.get("mcpServers"), dict) else {}
        configured = isinstance(servers.get(CHATCUT_MCP_SERVER), dict)
        plugins.append(
            {
                "name": CHATCUT_PLUGIN_NAME,
                "version": str(manifest.get("version", "unknown")),
                "manifest": str(manifest_path),
                "mcp_configured": configured,
                "mcp_config": str(mcp_path) if mcp_path.is_file() else None,
            }
        )
    tools, runtime_status = _normalize_tool_names(tool_names)
    if not plugins and tools is None:
        runtime_status = "plugin-not-installed"
    visible = sorted(tools) if tools is not None else []
    required = set(CHATCUT_CORE_TOOLS)
    missing = sorted(required - tools) if tools is not None else list(CHATCUT_CORE_TOOLS)
    return {
        "schema_version": 1,
        "plugin": {
            "installed": bool(plugins),
            "name": CHATCUT_PLUGIN_NAME,
            "repository": CHATCUT_PLUGIN_REPOSITORY,
            "versions": sorted({item["version"] for item in plugins}),
            "instances": plugins,
        },
        "mcp": {
            "server": CHATCUT_MCP_SERVER,
            "configured": any(item["mcp_configured"] for item in plugins),
            "auth": "task-scoped OAuth; never inferred from plugin files",
            "login_command": "codex mcp login chatcut",
        },
        "runtime": {
            "status": runtime_status,
            "task_tools_visible": tools is not None and bool(tools),
            "required_tools": list(CHATCUT_CORE_TOOLS),
            "missing_required_tools": missing,
            "visible_tools": visible,
            "ready_for_edit": bool(plugins) and tools is not None and required.issubset(tools),
        },
    }


def chatcut_capability_report(*, tool_names: Iterable[str] | None = None, codex_root: Path | None = None) -> dict[str, Any]:
    report = detect_chatcut_installation(codex_root=codex_root, tool_names=tool_names)
    report["adapter"] = {"name": "grok-video-studio-chatcut", "version": CHATCUT_ADAPTER_VERSION}
    report["features"] = {
        "editable_timeline": {"required_tools": ["manage_timelines", "edit_item"]},
        "source_asset_import": {"required_tools": ["browse_assets", "import_media"]},
        "effects_and_transitions": {"required_tools": ["browse_library", "edit_item"]},
        "structural_preview": {"required_tools": ["preview_timeline"]},
        "cloud_export": {"required_tools": list(CHATCUT_EXPORT_TOOLS)},
        "visual_proof": {"required_tools": ["preview_timeline", "render_cloud_screenshot"]},
    }
    return report


def _feature_mapping(plan: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = plan.get("timeline") if isinstance(plan.get("timeline"), dict) else {}
    inputs = timeline.get("inputs") if isinstance(timeline.get("inputs"), list) else []
    transitions = timeline.get("transitions") if isinstance(timeline.get("transitions"), list) else []
    filters = plan.get("filters") if isinstance(plan.get("filters"), list) else []
    audio = plan.get("audio_mix") if isinstance(plan.get("audio_mix"), dict) else {}
    mappings = [
        {
            "feature": "project_and_timeline",
            "source": "edit_plan.timeline",
            "operation": "create_or_target_project -> manage_timelines",
            "verification": "read_project + preview_timeline",
            "required_tools": ["list_projects", "create_project", "target_project", "manage_timelines"],
        },
        {
            "feature": "source_media",
            "source": f"{len(inputs)} timeline input(s)",
            "operation": "browse_assets -> import_media session -> upload-media helper",
            "verification": "browse_assets / inspect_asset",
            "required_tools": ["browse_assets", "import_media"],
        },
        {
            "feature": "clip_windows_and_speed",
            "source": "timeline.inputs[].edit_in/edit_out/speed",
            "operation": "edit_item timeline placement and playback speed",
            "verification": "preview_timeline + inspect_item",
            "required_tools": ["edit_item", "preview_timeline"],
        },
        {
            "feature": "transitions",
            "source": f"{len(transitions)} boundary transition(s)",
            "operation": "browse_library(category=transitions) -> edit_item",
            "verification": "preview_timeline + render_cloud_screenshot",
            "required_tools": ["browse_library", "edit_item", "preview_timeline"],
        },
        {
            "feature": "filters",
            "source": f"{len(filters)} global filter(s) plus per-shot filters",
            "operation": "browse_library(category=effects) -> edit_item",
            "verification": "preview_timeline + render_cloud_screenshot",
            "required_tools": ["browse_library", "edit_item", "preview_timeline"],
        },
        {
            "feature": "audio_mix_and_normalization",
            "source": "edit_plan.audio_mix",
            "operation": "map only fields exposed by the live edit_item schema",
            "verification": "preview_timeline + inspect_item + exported audio QA",
            "required_tools": ["edit_item", "preview_timeline", "submit_export", "track_export"],
        },
        {
            "feature": "clean_master_separation",
            "source": "deliveries.clean_master/edited_master",
            "operation": "ChatCut editable timeline remains separate from native clean master",
            "verification": "receipt.source_plan_sha256 + output SHA-256",
            "required_tools": ["preview_timeline", "submit_export", "track_export"],
        },
    ]
    return mappings


def build_chatcut_contract(plan: dict[str, Any], media_manifest: list[dict[str, Any]], *, installation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create the host-facing request contract; no remote mutation occurs here."""
    contract = {
        "schema_version": 1,
        "adapter_version": CHATCUT_ADAPTER_VERSION,
        "backend": CHATCUT_PLUGIN_NAME,
        "status": "ready_for_task_tool_execution",
        "source_plan_sha256": _canonical_digest(plan),
        "tool_prefix": CHATCUT_TOOL_PREFIX,
        "tool_requirements": {
            "core": list(CHATCUT_CORE_TOOLS),
            "export": list(CHATCUT_EXPORT_TOOLS),
            "optional": list(CHATCUT_OPTIONAL_TOOLS),
        },
        "operation_sequence": [
            "discover_or_create_project",
            "browse_or_import_source_assets",
            "create_or_target_timeline",
            "apply_edit_plan_features",
            "re_read_and_preview_timeline",
            "submit_and_track_export_only_when_requested",
        ],
        "feature_mapping": _feature_mapping(plan),
        "media_manifest": copy.deepcopy(media_manifest),
        "preservation": {
            "clean_master": str((plan.get("deliveries") or {}).get("clean_master", "deliverables/final.mp4")),
            "edited_master": str((plan.get("deliveries") or {}).get("edited_master", "deliverables/final-edited.mp4")),
            "local_flattening_for_chatcut": False,
        },
        "required_receipt": {
            "schema_version": 1,
            "status": "completed",
            "remote_project_id": "string",
            "remote_timeline_id": "string",
            "rendered_asset": {"path": "project-relative.mp4", "sha256": "64-hex"},
            "output_sha256": "64-hex",
            "source_plan_sha256": _canonical_digest(plan),
            "unmapped_features": [],
            "verification": {"structural": True, "visual": True},
            "tool_trace": [{"tool": "mcp__chatcut__preview_timeline", "phase": "verify", "status": "completed"}],
            "confirmed": True,
        },
        "capability_gate": {
            "installed_plugin_is_not_task_authorization": True,
            "task_tools_must_be_visible": True,
            "native_fallback": "Use edit-plan --backend native and edit when the gate is not met.",
        },
    }
    if installation is not None:
        contract["installation"] = copy.deepcopy(installation)
    return contract


def _project_relative(root: Path, value: str) -> Path:
    raw = Path(str(value))
    if raw.is_absolute():
        raise SkillError("ChatCut receipt rendered_asset.path must be project-relative")
    base = root.resolve()
    path = (base / raw).resolve()
    try:
        common = Path(os.path.commonpath([str(base), str(path)]))
    except ValueError as error:
        raise SkillError("ChatCut receipt output leaves the project") from error
    if common != base:
        raise SkillError("ChatCut receipt output leaves the project")
    return path


def _receipt_error(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def validate_chatcut_receipt(root: Path, plan: dict[str, Any], receipt: dict[str, Any], *, require_confirmation: bool = False) -> dict[str, Any]:
    """Validate a downloaded ChatCut render before it can be accepted."""
    errors: list[str] = []
    if not isinstance(receipt, dict):
        raise SkillError("ChatCut receipt must be a JSON object")
    if receipt.get("schema_version") != 1:
        _receipt_error(errors, "ChatCut receipt schema_version must be 1")
    if receipt.get("status") != "completed":
        _receipt_error(errors, "ChatCut receipt status must be completed")
    for field in ("remote_project_id", "remote_timeline_id", "output_sha256"):
        if not str(receipt.get(field, "")).strip():
            _receipt_error(errors, f"ChatCut receipt.{field} is required")
    expected_plan = _canonical_digest(plan)
    if receipt.get("source_plan_sha256") != expected_plan:
        _receipt_error(errors, "ChatCut receipt source_plan_sha256 does not match the current edit plan")
    unmapped = receipt.get("unmapped_features")
    if not isinstance(unmapped, list):
        _receipt_error(errors, "ChatCut receipt.unmapped_features must be an array")
    elif unmapped:
        _receipt_error(errors, "ChatCut receipt contains unmapped features")
    verification = receipt.get("verification") if isinstance(receipt.get("verification"), dict) else {}
    if verification.get("structural") is not True:
        _receipt_error(errors, "ChatCut receipt structural verification is incomplete")
    if verification.get("visual") is not True:
        _receipt_error(errors, "ChatCut receipt visual verification is incomplete")
    trace = receipt.get("tool_trace")
    if not isinstance(trace, list) or not trace:
        _receipt_error(errors, "ChatCut receipt tool_trace must be a non-empty array")
    elif any(not isinstance(item, dict) or not str(item.get("tool", "")).strip() for item in trace):
        _receipt_error(errors, "ChatCut receipt tool_trace entries require a tool name")
    if require_confirmation and receipt.get("confirmed") is not True:
        _receipt_error(errors, "ChatCut receipt requires confirmed=true before apply")
    asset = receipt.get("rendered_asset") if isinstance(receipt.get("rendered_asset"), dict) else {}
    asset_path = str(asset.get("path", "")).strip()
    if not asset_path:
        _receipt_error(errors, "ChatCut receipt.rendered_asset.path is required")
        output = None
    else:
        try:
            output = _project_relative(root, asset_path)
        except SkillError as error:
            _receipt_error(errors, str(error))
            output = None
    if output is not None:
        media: dict[str, Any] = {}
        try:
            assert_mp4(output)
            output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
            if output_sha256 != str(receipt.get("output_sha256", "")):
                _receipt_error(errors, "ChatCut receipt output_sha256 does not match the downloaded render")
            if asset.get("sha256") and asset.get("sha256") != output_sha256:
                _receipt_error(errors, "ChatCut receipt rendered_asset.sha256 does not match the downloaded render")
            target_size = str((plan.get("timeline") or {}).get("target_size") or "auto")
            qa = quality_report(output, expected_size=target_size)
            if not qa.get("ok"):
                _receipt_error(errors, "ChatCut render failed technical QA: " + "; ".join(qa.get("errors") or []))
        except (OSError, SkillError) as error:
            _receipt_error(errors, str(error))
            output_sha256 = ""
            qa = {"ok": False, "errors": [str(error)]}
        if output.is_file() and not media:
            try:
                media = probe_media(output)
            except SkillError as error:
                _receipt_error(errors, f"ChatCut render media probe failed: {error}")
                media = {}
    else:
        output_sha256 = ""
        qa = {"ok": False, "errors": list(errors)}
        media = {}
    if errors:
        return {"ok": False, "errors": errors, "qa": qa, "media": media}
    return {
        "ok": True,
        "receipt": copy.deepcopy(receipt),
        "output": {"path": asset_path, "sha256": output_sha256, "media": media},
        "qa": qa,
    }


def apply_chatcut_receipt(root: Path, plan: dict[str, Any], receipt_path: Path, *, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise SkillError("Applying a ChatCut receipt requires --confirm")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SkillError(f"unable to read ChatCut receipt: {receipt_path}") from error
    result = validate_chatcut_receipt(root, plan, receipt, require_confirmation=True)
    if not result.get("ok"):
        raise SkillError("invalid ChatCut receipt: " + "; ".join(result.get("errors") or []))
    root = root.resolve()
    delivery = root / "deliverables"
    delivery.mkdir(parents=True, exist_ok=True)
    receipt_digest = _canonical_digest(receipt)
    immutable = delivery / "chatcut-receipts" / f"receipt-{receipt_digest[:16]}.json"
    atomic_write_json(immutable, receipt)
    applied = {
        "schema_version": 1,
        "backend": CHATCUT_PLUGIN_NAME,
        "status": "accepted",
        "receipt_sha256": receipt_digest,
        "receipt_path": immutable.relative_to(root).as_posix(),
        "source_plan_sha256": _canonical_digest(plan),
        "output": result["output"],
        "qa": result["qa"],
    }
    atomic_write_json(delivery / "chatcut-receipt.json", applied)
    return {"ok": True, "status": "accepted", "path": str(delivery / "chatcut-receipt.json"), "receipt": applied}
