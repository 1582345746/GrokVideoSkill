#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, read_json
from visual_profiles import (
    VISUAL_LEVELS,
    VISUAL_MEDIA,
    VISUAL_REALISM,
    VISUAL_SUBJECTS,
    VISUAL_SUBJECT_NATURES,
    apply_visual_profile,
)


EVIDENCE_VERSION = 1
REVIEW_RECEIPT_VERSION = 1
BENCHMARK_VERSION = 1
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
IDENTITY_ORIGINS = {"unknown", "captured-real-person", "generated-or-unknown", "fictional-design", "not-applicable"}
MIN_REVIEW_CONFIDENCE = 0.70


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _json_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run(command: list[str], action: str, *, timeout: int = 180) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise SkillError(f"ffmpeg failed while {action}: {detail}")


def _probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("ffprobe is required for visual evidence extraction")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise SkillError(f"ffprobe could not read visual source {path.name}: {result.stderr.strip()[-500:]}")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next(item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video")
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (json.JSONDecodeError, StopIteration, TypeError, ValueError) as error:
        raise SkillError(f"visual source metadata is invalid: {path}") from error
    if width <= 0 or height <= 0:
        raise SkillError(f"visual source dimensions are invalid: {path}")
    return {"duration": max(0.0, duration), "width": width, "height": height, "codec": str(video.get("codec_name") or "")}


def _source_locator(path: Path, *, project_root: Path | None, source_label: str) -> dict[str, str]:
    if source_label.strip():
        return {"scope": "declared", "value": source_label.strip().replace("\\", "/")}
    if project_root is not None:
        try:
            relative = path.resolve().relative_to(project_root.resolve())
            return {"scope": "project", "value": relative.as_posix()}
        except ValueError:
            pass
    return {"scope": "external", "value": path.name}


def validate_evidence_manifest(manifest: dict[str, Any], manifest_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if manifest.get("version") != EVIDENCE_VERSION:
        errors.append(f"visual evidence version must be {EVIDENCE_VERSION}")
    expected_digest = str(manifest.get("manifest_sha256", ""))
    material = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if not expected_digest or _json_digest(material) != expected_digest:
        errors.append("visual evidence manifest digest is invalid")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if source.get("kind") not in {"image", "video"} or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
        errors.append("visual evidence source metadata is invalid")
    frames = manifest.get("frames") if isinstance(manifest.get("frames"), list) else []
    if not frames:
        errors.append("visual evidence must contain at least one frame")
    seen: set[str] = set()
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            errors.append(f"visual evidence frames[{index}] is invalid")
            continue
        frame_id = str(frame.get("id", ""))
        frame_name = str(frame.get("path", ""))
        if not frame_id or frame_id in seen or Path(frame_name).name != frame_name:
            errors.append(f"visual evidence frames[{index}] identity or path is invalid")
        seen.add(frame_id)
        if not re.fullmatch(r"[0-9a-f]{64}", str(frame.get("sha256", ""))):
            errors.append(f"visual evidence frames[{index}] digest is invalid")
        if manifest_path is not None:
            frame_path = manifest_path.resolve().parent / frame_name
            if not frame_path.is_file() or _digest(frame_path) != frame.get("sha256"):
                errors.append(f"visual evidence frames[{index}] file is missing or changed")
    return errors


def collect_visual_evidence(
    source: Path,
    output_root: Path,
    *,
    frame_count: int = 5,
    project_root: Path | None = None,
    source_label: str = "",
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise SkillError(f"visual source is missing or empty: {source}")
    suffix = source.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        raise SkillError(f"visual evidence source type is unsupported: {source.suffix}")
    if frame_count < 1 or frame_count > 9:
        raise SkillError("visual evidence frame_count must be from 1 to 9")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required for visual evidence extraction")

    source_sha = _digest(source)
    locator = _source_locator(source, project_root=project_root, source_label=source_label)
    locator_material = f"{source_sha}\0{locator['scope']}\0{locator['value']}".encode("utf-8")
    asset_id = hashlib.sha256(locator_material).hexdigest()[:16]
    evidence_dir = output_root.expanduser().resolve() / asset_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / "evidence-manifest.json"
    if manifest_path.is_file():
        previous = read_json(manifest_path)
        sampling = previous.get("sampling") if isinstance(previous.get("sampling"), dict) else {}
        previous_source = previous.get("source") if isinstance(previous.get("source"), dict) else {}
        if (
            previous_source.get("sha256") == source_sha
            and previous_source.get("locator") == locator
            and int(sampling.get("requested_frames", 0)) == frame_count
            and not validate_evidence_manifest(previous, manifest_path)
        ):
            return {
                "manifest": previous,
                "manifest_path": manifest_path,
                "review_template_path": evidence_dir / "review-template.json",
                "resumed": True,
            }
    media = _probe(source)
    kind = "image" if suffix in IMAGE_EXTENSIONS else "video"
    if kind == "image":
        positions = [0.0]
    else:
        duration = media["duration"]
        if duration <= 0:
            raise SkillError(f"video duration is invalid: {source}")
        positions = [duration * (index + 1) / (frame_count + 1) for index in range(frame_count)]

    frames: list[dict[str, Any]] = []
    for index, at in enumerate(positions, 1):
        output = evidence_dir / f"frame-{index:02d}.jpg"
        command = [ffmpeg, "-y"]
        if kind == "video":
            command.extend(["-ss", f"{at:.3f}"])
        command.extend(
            [
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-update",
                "1",
                "-vf",
                "scale=1280:1280:force_original_aspect_ratio=decrease",
                "-q:v",
                "2",
                str(output),
            ]
        )
        _run(command, f"extracting visual evidence frame {index}")
        if not output.is_file() or output.stat().st_size == 0:
            raise SkillError(f"visual evidence frame is missing: {output.name}")
        frames.append(
            {
                "id": f"frame-{index:02d}",
                "path": output.name,
                "at_seconds": round(at, 3),
                "bytes": output.stat().st_size,
                "sha256": _digest(output),
            }
        )

    manifest = {
        "version": EVIDENCE_VERSION,
        "asset_id": asset_id,
        "created_at": int(time.time()),
        "source": {
            "locator": locator,
            "name": source.name,
            "kind": kind,
            "bytes": source.stat().st_size,
            "sha256": source_sha,
            "media": media,
        },
        "sampling": {
            "strategy": "single-normalized-frame" if kind == "image" else "evenly-spaced-interior-frames",
            "requested_frames": frame_count,
            "actual_frames": len(frames),
        },
        "frames": frames,
        "review_contract": {
            "pixel_review_required": True,
            "folder_labels_are_weak_evidence": True,
            "real_person_requires_confirmed_provenance": True,
            "minimum_confidence": MIN_REVIEW_CONFIDENCE,
        },
    }
    manifest["manifest_sha256"] = _json_digest(manifest)
    atomic_write_json(manifest_path, manifest)
    template = {
        "version": REVIEW_RECEIPT_VERSION,
        "evidence_manifest": manifest_path.name,
        "evidence_manifest_sha256": manifest["manifest_sha256"],
        "reviewer": {"kind": "multimodal-agent", "id": ""},
        "classification": {
            "medium": "",
            "subject": "",
            "subject_nature": "",
            "realism": "",
            "identity_strictness": "auto",
            "performance_complexity": "auto",
        },
        "confidence": {"overall": 0.0, "medium": 0.0, "subject": 0.0, "subject_nature": 0.0},
        "provenance": {"identity_origin": "unknown", "confirmed": False, "evidence": ""},
        "frame_evidence": [],
        "limitations": [],
        "decision": "pending",
    }
    atomic_write_json(evidence_dir / "review-template.json", template)
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "review_template_path": evidence_dir / "review-template.json",
        "resumed": False,
    }


def validate_visual_review(
    receipt: dict[str, Any], manifest: dict[str, Any], manifest_path: Path | None = None
) -> list[str]:
    errors: list[str] = validate_evidence_manifest(manifest, manifest_path)
    if receipt.get("version") != REVIEW_RECEIPT_VERSION:
        errors.append(f"visual review version must be {REVIEW_RECEIPT_VERSION}")
    if receipt.get("evidence_manifest_sha256") != manifest.get("manifest_sha256"):
        errors.append("visual review is not bound to this evidence manifest")
    reviewer = receipt.get("reviewer") if isinstance(receipt.get("reviewer"), dict) else {}
    if reviewer.get("kind") not in {"multimodal-agent", "human"} or not str(reviewer.get("id", "")).strip():
        errors.append("visual review reviewer.kind and reviewer.id are required")
    classification = receipt.get("classification") if isinstance(receipt.get("classification"), dict) else {}
    allowed = {
        "medium": VISUAL_MEDIA - {"auto"},
        "subject": VISUAL_SUBJECTS - {"auto"},
        "subject_nature": VISUAL_SUBJECT_NATURES - {"auto"},
        "realism": VISUAL_REALISM - {"auto"},
        "identity_strictness": VISUAL_LEVELS,
        "performance_complexity": VISUAL_LEVELS,
    }
    for field, choices in allowed.items():
        if classification.get(field) not in choices:
            errors.append(f"visual review classification.{field} is unsupported")
    confidence = receipt.get("confidence") if isinstance(receipt.get("confidence"), dict) else {}
    for field in ("overall", "medium", "subject", "subject_nature"):
        try:
            value = float(confidence.get(field))
            if value < 0 or value > 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"visual review confidence.{field} must be from 0 to 1")
    provenance = receipt.get("provenance") if isinstance(receipt.get("provenance"), dict) else {}
    if provenance.get("identity_origin") not in IDENTITY_ORIGINS or not isinstance(provenance.get("confirmed"), bool):
        errors.append("visual review provenance is invalid")
    if classification.get("subject_nature") == "real-person" and not (
        provenance.get("identity_origin") == "captured-real-person"
        and provenance.get("confirmed") is True
        and str(provenance.get("evidence", "")).strip()
    ):
        errors.append("real-person requires confirmed captured-real-person provenance and evidence; pixels alone are insufficient")
    frames = {str(item.get("id")) for item in manifest.get("frames", []) if isinstance(item, dict)}
    frame_evidence = receipt.get("frame_evidence") if isinstance(receipt.get("frame_evidence"), list) else []
    if not frame_evidence:
        errors.append("visual review must cite at least one evidence frame")
    for index, item in enumerate(frame_evidence):
        if not isinstance(item, dict) or item.get("frame_id") not in frames or not str(item.get("observation", "")).strip():
            errors.append(f"visual review frame_evidence[{index}] is invalid")
    if receipt.get("decision") not in {"accepted", "needs-review", "rejected"}:
        errors.append("visual review decision is unsupported")
    return errors


def create_visual_review_receipt(
    manifest_path: Path,
    *,
    reviewer_id: str,
    reviewer_kind: str,
    medium: str,
    subject: str,
    subject_nature: str,
    realism: str,
    confidence: float,
    identity_origin: str,
    provenance_confirmed: bool,
    provenance_evidence: str,
    frame_evidence: list[dict[str, str]],
    limitations: list[str] | None = None,
    identity_strictness: str = "auto",
    performance_complexity: str = "auto",
    confirm: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = read_json(manifest_path)
    score = round(float(confidence), 3)
    provenance_ok = subject_nature != "real-person" or (
        identity_origin == "captured-real-person" and provenance_confirmed and provenance_evidence.strip()
    )
    decision = "accepted" if confirm and score >= MIN_REVIEW_CONFIDENCE and provenance_ok else "needs-review"
    receipt = {
        "version": REVIEW_RECEIPT_VERSION,
        "evidence_manifest": manifest_path.name,
        "evidence_manifest_sha256": manifest.get("manifest_sha256"),
        "reviewed_at": int(time.time()),
        "reviewer": {"kind": reviewer_kind, "id": reviewer_id.strip()},
        "classification": {
            "medium": medium,
            "subject": subject,
            "subject_nature": subject_nature,
            "realism": realism,
            "identity_strictness": identity_strictness,
            "performance_complexity": performance_complexity,
        },
        "confidence": {"overall": score, "medium": score, "subject": score, "subject_nature": score},
        "provenance": {
            "identity_origin": identity_origin,
            "confirmed": bool(provenance_confirmed),
            "evidence": provenance_evidence.strip(),
        },
        "frame_evidence": frame_evidence,
        "limitations": [str(item) for item in (limitations or []) if str(item).strip()],
        "decision": decision,
    }
    errors = validate_visual_review(receipt, manifest, manifest_path)
    if errors:
        raise SkillError("invalid visual review: " + "; ".join(errors))
    output = manifest_path.parent / "review-receipt.json"
    if output.is_file():
        history = manifest_path.parent / "review-history"
        history.mkdir(parents=True, exist_ok=True)
        archived = history / f"review-receipt-{time.time_ns()}-{_digest(output)[:8]}.json"
        shutil.copy2(output, archived)
    atomic_write_json(output, receipt)
    return {"receipt": receipt, "receipt_path": output, "manifest": manifest}


def apply_visual_review_receipt(
    project: dict[str, Any],
    manifest_path: Path,
    receipt_path: Path,
    *,
    confirm: bool,
    project_root: Path | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    receipt_path = receipt_path.expanduser().resolve()
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    errors = validate_visual_review(receipt, manifest, manifest_path)
    if errors:
        raise SkillError("invalid visual review: " + "; ".join(errors))
    if not confirm or receipt.get("decision") != "accepted":
        raise SkillError("visual review must be accepted and --confirm must be supplied before applying it")
    applied_receipt_path = receipt_path
    if project_root is not None:
        project_root = project_root.expanduser().resolve()
        for path, label in ((manifest_path, "manifest"), (receipt_path, "receipt")):
            try:
                path.relative_to(project_root)
            except ValueError as error:
                raise SkillError(f"visual review {label} must be inside the project before it can be applied") from error
        receipt_sha = _digest(receipt_path)
        applied_root = manifest_path.parent / "applied-receipts"
        applied_root.mkdir(parents=True, exist_ok=True)
        applied_receipt_path = applied_root / f"review-{receipt_sha[:16]}.json"
        if applied_receipt_path.is_file() and _digest(applied_receipt_path) != receipt_sha:
            raise SkillError("immutable applied visual review receipt has changed")
        if not applied_receipt_path.is_file():
            shutil.copy2(receipt_path, applied_receipt_path)
    classification = receipt["classification"]
    resolved = apply_visual_profile(
        project,
        mode="manual",
        medium=str(classification["medium"]),
        subject=str(classification["subject"]),
        subject_nature=str(classification["subject_nature"]),
        realism=str(classification["realism"]),
        identity_strictness=str(classification.get("identity_strictness", "auto")),
        performance_complexity=str(classification.get("performance_complexity", "auto")),
        confirmed=True,
    )
    project["visual_profile"]["pixel_review"] = {
        "version": REVIEW_RECEIPT_VERSION,
        "asset_id": manifest.get("asset_id"),
        "source_sha256": (manifest.get("source") or {}).get("sha256"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "receipt_sha256": _digest(applied_receipt_path),
        "reviewer": receipt.get("reviewer"),
        "reviewed_at": receipt.get("reviewed_at"),
        "decision": receipt.get("decision"),
        "confidence": receipt.get("confidence"),
        "provenance": receipt.get("provenance"),
        "manifest_locator": _source_locator(manifest_path, project_root=project_root, source_label=""),
        "receipt_locator": _source_locator(applied_receipt_path, project_root=project_root, source_label=""),
    }
    resolved["pixel_review"] = project["visual_profile"]["pixel_review"]
    return resolved


def validate_project_pixel_review(root: Path, project: dict[str, Any]) -> list[str]:
    profile = project.get("visual_profile") if isinstance(project.get("visual_profile"), dict) else {}
    review = profile.get("pixel_review")
    if review is None:
        return []
    if not isinstance(review, dict):
        return ["project.visual_profile.pixel_review must be an object"]
    errors: list[str] = []
    paths: dict[str, Path] = {}
    for name in ("manifest", "receipt"):
        locator = review.get(f"{name}_locator") if isinstance(review.get(f"{name}_locator"), dict) else {}
        if locator.get("scope") != "project" or not str(locator.get("value", "")).strip():
            errors.append(f"project.visual_profile.pixel_review.{name}_locator must be project-relative")
            continue
        path = (root.resolve() / str(locator["value"])).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            errors.append(f"project.visual_profile.pixel_review.{name}_locator leaves the project")
            continue
        if not path.is_file():
            errors.append(f"project.visual_profile.pixel_review {name} is missing")
            continue
        paths[name] = path
    manifest = None
    receipt = None
    if "manifest" in paths:
        try:
            manifest = read_json(paths["manifest"])
            errors.extend(validate_evidence_manifest(manifest, paths["manifest"]))
        except (SkillError, OSError, json.JSONDecodeError) as error:
            errors.append(f"project.visual_profile.pixel_review manifest is invalid: {error}")
    if "receipt" in paths:
        if _digest(paths["receipt"]) != review.get("receipt_sha256"):
            errors.append("project.visual_profile.pixel_review receipt digest does not match")
        try:
            receipt = read_json(paths["receipt"])
        except (SkillError, OSError, json.JSONDecodeError) as error:
            errors.append(f"project.visual_profile.pixel_review receipt is invalid: {error}")
    if manifest is not None:
        if manifest.get("manifest_sha256") != review.get("manifest_sha256"):
            errors.append("project.visual_profile.pixel_review manifest digest does not match")
        if (manifest.get("source") or {}).get("sha256") != review.get("source_sha256"):
            errors.append("project.visual_profile.pixel_review source digest does not match")
        if manifest.get("asset_id") != review.get("asset_id"):
            errors.append("project.visual_profile.pixel_review asset id does not match")
        source_locator = (manifest.get("source") or {}).get("locator")
        if isinstance(source_locator, dict) and source_locator.get("scope") == "project":
            source_path = (root.resolve() / str(source_locator.get("value", ""))).resolve()
            try:
                source_path.relative_to(root.resolve())
                if not source_path.is_file() or _digest(source_path) != (manifest.get("source") or {}).get("sha256"):
                    errors.append("project visual source is missing or changed after pixel review")
            except ValueError:
                errors.append("project visual source locator leaves the project")
    if manifest is not None and receipt is not None:
        errors.extend(validate_visual_review(receipt, manifest, paths["manifest"]))
        for field in ("reviewer", "reviewed_at", "decision", "confidence", "provenance"):
            if review.get(field) != receipt.get(field):
                errors.append(f"project.visual_profile.pixel_review {field} does not match its receipt")
        classification = receipt.get("classification") if isinstance(receipt.get("classification"), dict) else {}
        for field in ("medium", "subject", "subject_nature", "realism", "identity_strictness", "performance_complexity"):
            if profile.get(field) != classification.get(field):
                errors.append(f"project.visual_profile.{field} does not match its applied pixel review")
    return list(dict.fromkeys(errors))


def _weak_expectation(category: str) -> dict[str, Any]:
    if category == "动漫":
        return {
            "medium_any": ["2d-anime", "3d-anime", "stylized-3d", "photoreal", "hybrid"],
            "subject_nature_any": ["human-like-fictional", "non-human-fictional", "mixed"],
            "note": "Near-real anime remains fictional; a photoreal appearance does not make it a real person.",
        }
    if category == "动画":
        return {
            "medium_any": ["2d-anime", "3d-anime", "stylized-3d", "motion-graphics", "hybrid"],
            "subject_nature_excludes": ["real-person"],
            "note": "Animation is a broad weak label and may contain characters, mascots, objects, or graphics.",
        }
    if category == "真人":
        return {
            "medium_any": ["photoreal", "hybrid"],
            "subject_nature_any": ["real-person", "synthetic-human", "unknown", "mixed"],
            "note": "The folder asserts live-action appearance only; actual-person provenance must be reviewed separately.",
        }
    return {"note": "Unknown folder label; score only receipt completeness."}


def _case_passes(expectation: dict[str, Any], classification: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    medium = classification.get("medium")
    nature = classification.get("subject_nature")
    if expectation.get("medium_any") and medium not in expectation["medium_any"]:
        reasons.append(f"medium {medium} is outside weak expectation")
    if expectation.get("subject_nature_any") and nature not in expectation["subject_nature_any"]:
        reasons.append(f"subject_nature {nature} is outside weak expectation")
    if nature in expectation.get("subject_nature_excludes", []):
        reasons.append(f"subject_nature {nature} is excluded by weak expectation")
    return not reasons, reasons


def build_visual_benchmark(
    dataset_root: Path,
    output_root: Path,
    *,
    per_group: int = 3,
    frame_count: int = 3,
) -> dict[str, Any]:
    dataset_root = dataset_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not dataset_root.is_dir():
        raise SkillError(f"visual benchmark dataset does not exist: {dataset_root}")
    try:
        output_root.relative_to(dataset_root)
        raise SkillError("visual benchmark output must be outside the source dataset")
    except ValueError:
        pass
    if per_group < 1 or per_group > 20:
        raise SkillError("visual benchmark per_group must be from 1 to 20")
    cases: list[dict[str, Any]] = []
    for kind_dir in sorted((item for item in dataset_root.iterdir() if item.is_dir()), key=lambda item: item.name):
        for category_dir in sorted((item for item in kind_dir.iterdir() if item.is_dir()), key=lambda item: item.name):
            media = [
                item
                for item in category_dir.rglob("*")
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
            ]
            ranked = sorted(media, key=lambda item: hashlib.sha256(item.relative_to(dataset_root).as_posix().encode("utf-8")).hexdigest())
            for source in ranked[:per_group]:
                relative = source.relative_to(dataset_root).as_posix()
                case_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
                result = collect_visual_evidence(
                    source,
                    output_root / "evidence",
                    frame_count=frame_count,
                    source_label=relative,
                )
                receipt_path = result["manifest_path"].parent / "review-receipt.json"
                cases.append(
                    {
                        "id": case_id,
                        "group": f"{kind_dir.name}/{category_dir.name}",
                        "kind": result["manifest"]["source"]["kind"],
                        "source_relative": relative,
                        "source_sha256": result["manifest"]["source"]["sha256"],
                        "evidence_manifest": os.path.relpath(result["manifest_path"], output_root).replace("\\", "/"),
                        "review_receipt": os.path.relpath(receipt_path, output_root).replace("\\", "/"),
                        "weak_expectation": _weak_expectation(category_dir.name),
                    }
                )
    plan = {
        "version": BENCHMARK_VERSION,
        "created_at": int(time.time()),
        "dataset": {"root": str(dataset_root), "ingested_into_skill": False, "labels": "weak-folder-labels"},
        "sampling": {"strategy": "stable-relative-path-hash", "per_group": per_group, "frame_count": frame_count},
        "cases": cases,
    }
    atomic_write_json(output_root / "benchmark.json", plan)
    return refresh_visual_benchmark(output_root / "benchmark.json")


def refresh_visual_benchmark(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    plan = read_json(plan_path)
    if plan.get("version") != BENCHMARK_VERSION or not isinstance(plan.get("cases"), list):
        raise SkillError("visual benchmark contract is invalid")
    completed = passed = failed = 0
    results: list[dict[str, Any]] = []

    def benchmark_path(value: Any) -> Path:
        path = (plan_path.parent / str(value or "")).resolve()
        try:
            path.relative_to(plan_path.parent)
        except ValueError as error:
            raise SkillError("visual benchmark case path leaves the benchmark directory") from error
        return path

    for case in plan["cases"]:
        receipt_path = benchmark_path(case.get("review_receipt"))
        manifest_path = benchmark_path(case.get("evidence_manifest"))
        if not receipt_path.is_file():
            results.append({"id": case.get("id"), "status": "pending"})
            continue
        manifest = read_json(manifest_path)
        receipt = read_json(receipt_path)
        errors = validate_visual_review(receipt, manifest, manifest_path)
        if errors or receipt.get("decision") != "accepted":
            completed += 1
            failed += 1
            results.append({"id": case.get("id"), "status": "invalid", "errors": errors or ["receipt is not accepted"]})
            continue
        ok, reasons = _case_passes(case.get("weak_expectation") or {}, receipt.get("classification") or {})
        completed += 1
        passed += int(ok)
        failed += int(not ok)
        results.append(
            {
                "id": case.get("id"),
                "status": "passed" if ok else "failed",
                "classification": receipt.get("classification"),
                "confidence": receipt.get("confidence"),
                "reasons": reasons,
            }
        )
    total = len(plan["cases"])
    report = {
        "version": BENCHMARK_VERSION,
        "generated_at": int(time.time()),
        "benchmark": str(plan_path),
        "summary": {
            "total": total,
            "completed": completed,
            "pending": total - completed,
            "passed": passed,
            "failed": failed,
            "weak_label_accuracy": round(passed / completed, 4) if completed else None,
        },
        "guardrails": {
            "external_media_copied_into_repository": False,
            "folder_labels_are_ground_truth": False,
            "real_person_inferred_from_pixels": False,
        },
        "results": results,
    }
    atomic_write_json(plan_path.parent / "benchmark-report.json", report)
    return {"plan": plan, "report": report, "plan_path": plan_path, "report_path": plan_path.parent / "benchmark-report.json"}
