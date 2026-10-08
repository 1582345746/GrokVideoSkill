"""Hash-bound, layer-specific reviews of rendered assets and retained windows."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, read_json

LAYERS = ("narrative", "visual", "audio", "continuity", "clean_frame")
METHODS = {
    "narrative": {"codex-vision", "human-video", "model-audio-video"},
    "visual": {"codex-vision", "human-video", "model-audio-video"},
    "audio": {"human-listening", "asr-and-human", "model-audio-video"},
    "continuity": {"codex-vision", "human-video", "model-audio-video"},
    "clean_frame": {"codex-vision", "human-video", "ocr-and-vision", "model-audio-video"},
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def review_asset_contract(root: Path, project: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    assets: dict[str, Any] = {}
    for shot in project.get("shots", []):
        runtime = state.get("shots", {}).get(shot["id"], {}).get("video", {})
        if runtime.get("status") == "completed" and runtime.get("path"):
            assets[str(shot["id"])] = _asset(root, runtime, shot)
    final = state.get("deliverables", {}).get("final", {})
    if final.get("path"):
        assets["final"] = _asset(root, final, None)
    return assets


def _asset(root: Path, runtime: dict[str, Any], shot: dict[str, Any] | None) -> dict[str, Any]:
    path = (root / str(runtime["path"])).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise SkillError("review asset must be an existing file inside the project")
    result = {"path": path.relative_to(root.resolve()).as_posix(), "sha256": digest(path)}
    if shot is not None:
        result["edit_in"] = float(shot.get("edit_in", 0) or 0)
        result["edit_out"] = float(shot.get("edit_out") if shot.get("edit_out") is not None else shot.get("seconds", 6))
    return result


def create_review_template(root: Path, project: dict[str, Any], state: dict[str, Any]) -> Path:
    assets = review_asset_contract(root, project, state)
    if not assets:
        raise SkillError("generate at least one video before creating a content review")
    for asset in assets.values():
        asset["layers"] = {layer: {"status": "unverified", "method": "", "evidence": []} for layer in LAYERS}
    if "final" in assets:
        assets["final"]["understanding"] = {k: "" for k in ("goal", "obstacle", "choice", "consequence")}
    output = root / "deliverables" / "content-review.template.json"
    atomic_write_json(output, {"version": 1, "project_sha256": digest(root / "project.json"), "reviewer_id": "", "assets": assets})
    return output


def validate_review_receipt(root: Path, project: dict[str, Any], state: dict[str, Any], receipt: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["content review must be an object"]
    if receipt.get("version") != 1 or receipt.get("project_sha256") != digest(root / "project.json"):
        errors.append("content review project hash/version is stale")
    if not isinstance(receipt.get("reviewer_id"), str) or not receipt["reviewer_id"].strip():
        errors.append("content review reviewer_id is required")
    actual = review_asset_contract(root, project, state)
    supplied = receipt.get("assets")
    if not isinstance(supplied, dict) or not supplied:
        return errors + ["content review assets must be a nonempty object"]
    for asset_id, asset in supplied.items():
        if not isinstance(asset, dict) or asset_id not in actual:
            errors.append(f"unknown/malformed review asset: {asset_id}")
            continue
        expected = actual[asset_id]
        if any(asset.get(k) != v for k, v in expected.items()):
            errors.append(f"{asset_id}: asset hash/path/edit window changed")
        layers = asset.get("layers")
        if not isinstance(layers, dict):
            errors.append(f"{asset_id}: layers must be an object")
            continue
        for layer, review in layers.items():
            if layer not in LAYERS or not isinstance(review, dict):
                errors.append(f"{asset_id}: invalid layer {layer}")
                continue
            status = review.get("status")
            if status not in {"approved", "rejected", "unverified"}:
                errors.append(f"{asset_id}/{layer}: invalid status")
            if status in {"approved", "rejected"}:
                evidence = review.get("evidence")
                if review.get("method") not in METHODS[layer] or not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) and e.strip() for e in evidence):
                    errors.append(f"{asset_id}/{layer}: actual review method and observed evidence are required")
                if asset_id == "final" and layer == "narrative" and status == "approved":
                    answers = asset.get("understanding")
                    if not isinstance(answers, dict) or not all(isinstance(answers.get(k), str) and answers[k].strip() for k in ("goal", "obstacle", "choice", "consequence")):
                        errors.append("final narrative approval requires four viewer-understanding answers")
    return errors


def apply_review_receipt(root: Path, project: dict[str, Any], state: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    receipt = read_json(receipt_path)
    errors = validate_review_receipt(root, project, state, receipt)
    if errors:
        raise SkillError("; ".join(errors))
    current = state.get("content_reviews", {})
    if current.get("project_sha256") != receipt["project_sha256"]:
        current = {"version": 1, "project_sha256": receipt["project_sha256"], "assets": {}}
    current["reviewer_id"] = receipt["reviewer_id"]
    for key, asset in receipt["assets"].items():
        prior = current["assets"].get(key, {})
        if any(prior.get(k) != asset.get(k) for k in ("sha256", "path", "edit_in", "edit_out")):
            prior = {}
        current["assets"][key] = {**prior, **asset, "layers": {**prior.get("layers", {}), **asset["layers"]}}
    state["content_reviews"] = current
    return {"accepted_assets": list(receipt["assets"]), "project_sha256": receipt["project_sha256"]}


def content_review_summary(root: Path, project: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    stored = state.get("content_reviews")
    result: dict[str, Any] = {"status": "unverified", "layers": {k: None for k in LAYERS}, "errors": []}
    if not stored:
        return result
    errors = validate_review_receipt(root, project, state, stored)
    if errors:
        result.update(status="stale", errors=errors)
        return result
    required_assets = [str(s["id"]) for s in project.get("shots", [])] + ["final"]
    for layer in LAYERS:
        statuses = [stored["assets"].get(asset_id, {}).get("layers", {}).get(layer, {}).get("status", "unverified") for asset_id in required_assets]
        result["layers"][layer] = False if "rejected" in statuses else True if all(s == "approved" for s in statuses) else None
    result["status"] = "reviewed" if all(v is True for v in result["layers"].values()) else "incomplete-or-rejected"
    return result
