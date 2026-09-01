#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


VISUAL_PROFILE_VERSION = 1
VISUAL_MODES = {"auto", "manual"}
VISUAL_MEDIA = {
    "auto",
    "photoreal",
    "2d-anime",
    "3d-anime",
    "stylized-3d",
    "motion-graphics",
    "hybrid",
}
VISUAL_SUBJECTS = {
    "auto",
    "human",
    "fictional-character",
    "mascot",
    "product",
    "environment",
    "graphic",
    "mixed",
}
VISUAL_SUBJECT_NATURES = {
    "auto",
    "real-person",
    "synthetic-human",
    "human-like-fictional",
    "non-human-fictional",
    "object",
    "environment",
    "graphic",
    "mixed",
    "unknown",
}
VISUAL_LEVELS = {"auto", "high", "medium", "low"}
VISUAL_REALISM = VISUAL_LEVELS | {"not-applicable"}


_MEDIUM_TERMS: dict[str, dict[str, float]] = {
    "photoreal": {
        "photoreal": 4.0,
        "photo-real": 4.0,
        "live action": 4.0,
        "live-action": 4.0,
        "real person": 4.0,
        "真人": 4.0,
        "实拍": 4.0,
        "真实摄影": 3.0,
        "写实人物": 3.0,
        "cinematic photography": 2.5,
        "skin texture": 2.0,
    },
    "2d-anime": {
        "2d anime": 4.0,
        "2d animation": 3.0,
        "anime": 2.5,
        "动漫": 2.5,
        "二次元": 3.5,
        "日漫": 3.5,
        "cel shading": 3.0,
        "cel-shaded": 3.0,
        "line art": 2.0,
        "手绘动画": 3.0,
    },
    "3d-anime": {
        "3d anime": 5.0,
        "3d animated character": 4.0,
        "三渲二": 5.0,
        "3d动漫": 5.0,
        "3d 动漫": 5.0,
        "anime render": 3.0,
        "toon render": 3.0,
    },
    "stylized-3d": {
        "stylized 3d": 5.0,
        "3d cartoon": 4.0,
        "cg animation": 3.0,
        "cgi animation": 3.0,
        "卡通动画": 3.5,
        "三维动画": 4.0,
        "皮克斯风": 3.0,
        "pixar style": 3.0,
        "动画": 1.5,
    },
    "motion-graphics": {
        "motion graphics": 5.0,
        "kinetic typography": 4.0,
        "infographic": 3.5,
        "信息图": 3.5,
        "动态图形": 5.0,
        "mg动画": 5.0,
        "logo animation": 3.0,
        "文字动画": 3.0,
    },
}

_SUBJECT_TERMS: dict[str, tuple[str, ...]] = {
    "human": ("person", "people", "man", "woman", "boy", "girl", "真人", "人物", "男人", "女人", "演员"),
    "fictional-character": ("character", "hero", "heroine", "villain", "protagonist", "角色", "主角", "动漫人物", "魔法师"),
    "mascot": ("mascot", "creature", "吉祥物", "萌宠角色"),
    "product": ("product", "packaging", "bottle", "device", "商品", "产品", "包装", "手机", "汽车"),
    "environment": ("landscape", "cityscape", "environment", "scenery", "风景", "场景", "城市", "自然"),
    "graphic": ("typography", "diagram", "chart", "logo", "文字", "图表", "标志", "信息图"),
}

_REAL_PERSON_TERMS = (
    "actual person",
    "identified real person",
    "real actor reference",
    "supplied real-person photo",
    "本人照片",
    "真实人物本人",
    "真实演员参考",
    "实拍人物素材",
    "纪录片人物",
)
_SYNTHETIC_HUMAN_TERMS = (
    "synthetic human",
    "ai-generated person",
    "ai generated person",
    "virtual human",
    "digital human",
    "ai actor",
    "ai actress",
    "generated actor",
    "original adult actor",
    "合成人",
    "虚拟真人",
    "数字人",
    "ai演员",
    "原创真人角色",
)
_FICTIONAL_NATURE_TERMS = (
    "anime",
    "动漫",
    "二次元",
    "角色",
    "character",
    "cartoon",
    "动画",
    "3d render",
    "cgi",
    "三渲二",
    "toon render",
)

_POLICIES: dict[str, dict[str, Any]] = {
    "photoreal": {
        "preferred_route": "image-to-video for recurring people or multi-shot identity; text-to-video is acceptable for disposable establishing shots",
        "prompt_direction": "Natural live-action photography. Preserve facial identity, anatomy, skin texture, wardrobe, lighting continuity, and physically plausible motion.",
        "avoid": ["plastic skin", "face drift", "extra fingers or limbs", "rubbery motion", "unmotivated lip movement"],
        "qa_priorities": [
            "face and identity",
            "hands and anatomy",
            "skin and hair detail",
            "eye and lip motion",
            "prop contact, liquids, cloth, and physical interaction",
        ],
    },
    "2d-anime": {
        "preferred_route": "image-to-video from an approved keyframe for recurring characters; text-to-video for short non-recurring inserts",
        "prompt_direction": "Preserve the exact 2D anime design, line weight, cel shading, facial proportions, palette, costume shapes, and limited-animation language.",
        "avoid": ["line boil", "style drift", "3D material creep", "face proportion changes", "detail flicker"],
        "qa_priorities": ["line stability", "character proportions", "palette continuity", "layer deformation", "background flicker"],
    },
    "3d-anime": {
        "preferred_route": "image-to-video from an approved character render for recurring characters",
        "prompt_direction": "Use a clearly three-dimensional CG-rendered anime character with volumetric form and coherent toon/PBR materials. Preserve the character model, silhouette, facial topology, costume geometry, palette, and render style.",
        "avoid": ["flat 2D cel illustration", "model topology drift", "material flicker", "2D/3D style switching", "facial rig distortion", "costume geometry changes"],
        "qa_priorities": ["model identity", "facial topology", "materials", "costume geometry", "motion arcs"],
    },
    "stylized-3d": {
        "preferred_route": "image-to-video from approved design frames for recurring characters or branded objects",
        "prompt_direction": "Preserve the stylized 3D design language, silhouette, proportions, materials, surface detail, palette, and coherent cinematic lighting.",
        "avoid": ["proportion drift", "material changes", "uncanny realism", "geometry melting", "lighting discontinuity"],
        "qa_priorities": ["silhouette", "proportions", "materials", "geometry", "lighting continuity"],
    },
    "motion-graphics": {
        "preferred_route": "deterministic local composition when text, charts, logos, or exact timing matter; generative video only for abstract backgrounds",
        "prompt_direction": "Use clean motion-graphics composition, deliberate easing, stable shapes, controlled palette, legible hierarchy, and exact graphic timing.",
        "avoid": ["generated text", "logo mutation", "wobbling geometry", "random camera motion", "uncontrolled texture"],
        "qa_priorities": ["text accuracy", "logo fidelity", "alignment", "timing and easing", "shape stability"],
    },
    "hybrid": {
        "preferred_route": "separate the project into medium-specific shots and use approved keyframes at style boundaries",
        "prompt_direction": "Keep each shot's declared visual medium internally consistent. Make style transitions explicit and preserve shared identity, palette, and composition anchors.",
        "avoid": ["accidental style switching", "identity drift at transitions", "palette discontinuity", "unmotivated mixed rendering"],
        "qa_priorities": ["intentional style boundaries", "cross-style identity", "palette continuity", "transition frames", "shot-level consistency"],
    },
}


def default_visual_profile(*, medium: str = "auto") -> dict[str, Any]:
    mode = "auto" if medium == "auto" else "manual"
    return {
        "version": VISUAL_PROFILE_VERSION,
        "mode": mode,
        "medium": medium,
        "subject": "auto",
        "subject_nature": "auto",
        "realism": "auto",
        "identity_strictness": "auto",
        "performance_complexity": "auto",
        "confirmed": mode == "manual",
    }


def visual_profile_config(project: dict[str, Any]) -> dict[str, Any]:
    raw = project.get("visual_profile") if isinstance(project.get("visual_profile"), dict) else {}
    default = default_visual_profile()
    return {key: raw.get(key, value) for key, value in default.items()}


def validate_visual_profile(project: dict[str, Any]) -> list[str]:
    raw = project.get("visual_profile")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["project.visual_profile must be an object"]
    errors: list[str] = []
    config = visual_profile_config(project)
    if config["version"] != VISUAL_PROFILE_VERSION:
        errors.append(f"visual_profile.version must be {VISUAL_PROFILE_VERSION}")
    if config["mode"] not in VISUAL_MODES:
        errors.append("visual_profile.mode must be auto or manual")
    if config["medium"] not in VISUAL_MEDIA:
        errors.append("visual_profile.medium is unsupported")
    if config["mode"] == "manual" and config["medium"] == "auto":
        errors.append("visual_profile.medium must be explicit in manual mode")
    if config["subject"] not in VISUAL_SUBJECTS:
        errors.append("visual_profile.subject is unsupported")
    if config["subject_nature"] not in VISUAL_SUBJECT_NATURES:
        errors.append("visual_profile.subject_nature is unsupported")
    if config["realism"] not in VISUAL_REALISM:
        errors.append("visual_profile.realism is unsupported")
    for field in ("identity_strictness", "performance_complexity"):
        if config[field] not in VISUAL_LEVELS:
            errors.append(f"visual_profile.{field} is unsupported")
    if not isinstance(config["confirmed"], bool):
        errors.append("visual_profile.confirmed must be a boolean")
    if raw.get("last_analysis") is not None and not isinstance(raw.get("last_analysis"), dict):
        errors.append("visual_profile.last_analysis must be an object")
    return errors


def _normalized_text(parts: Iterable[Any]) -> str:
    return " ".join(str(part) for part in parts if str(part or "").strip()).lower()


def _term_score(text: str, terms: dict[str, float]) -> tuple[float, list[str]]:
    score = 0.0
    evidence: list[str] = []
    for term, weight in terms.items():
        if _contains_term(text, term):
            score += weight
            evidence.append(term)
    return score, evidence


def _contains_term(text: str, term: str) -> bool:
    if term.isascii() and term.replace("-", "").replace(" ", "").isalnum():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text))
    return term in text


def _infer_subject(text: str) -> tuple[str, float, list[str]]:
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for subject, terms in _SUBJECT_TERMS.items():
        matches = [term for term in terms if _contains_term(text, term)]
        scores[subject] = len(matches)
        evidence[subject] = matches
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] == 0:
        return "mixed", 0.35, []
    top, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    if second_score == top_score and second_score > 0:
        combined = evidence[top] + evidence[ranked[1][0]]
        return "mixed", 0.5, combined[:8]
    confidence = min(0.95, 0.58 + 0.12 * top_score + 0.08 * max(0, top_score - second_score))
    return top, round(confidence, 3), evidence[top][:8]


def _infer_subject_nature(text: str, *, subject: str, medium: str) -> tuple[str, list[str]]:
    real_matches = [term for term in _REAL_PERSON_TERMS if _contains_term(text, term)]
    synthetic_matches = [term for term in _SYNTHETIC_HUMAN_TERMS if _contains_term(text, term)]
    fictional_matches = [term for term in _FICTIONAL_NATURE_TERMS if _contains_term(text, term)]
    if medium in {"2d-anime", "3d-anime", "stylized-3d"}:
        if subject in {"human", "fictional-character", "mascot", "mixed"}:
            return "human-like-fictional", (fictional_matches + synthetic_matches + real_matches)[:8]
        return "non-human-fictional", fictional_matches[:8]
    if medium == "hybrid" and real_matches and fictional_matches:
        return "mixed", (real_matches + fictional_matches)[:8]
    if real_matches and synthetic_matches:
        return "mixed", (real_matches + synthetic_matches)[:8]
    if real_matches:
        return "real-person", real_matches[:8]
    if synthetic_matches and subject in {"human", "fictional-character", "mixed"}:
        return "synthetic-human", synthetic_matches[:8]
    if subject in {"product"}:
        return "object", []
    if subject in {"environment"}:
        return "environment", []
    if subject in {"graphic"} or medium == "motion-graphics":
        return "graphic", []
    if fictional_matches:
        if subject in {"human", "fictional-character", "mascot", "mixed"}:
            return "human-like-fictional", fictional_matches[:8]
        return "non-human-fictional", fictional_matches[:8]
    if subject in {"human"} and medium == "photoreal":
        return "unknown", []
    return "unknown", []


def _realism_for_medium(medium: str) -> str:
    return {
        "photoreal": "high",
        "3d-anime": "medium",
        "stylized-3d": "low",
        "2d-anime": "low",
        "motion-graphics": "not-applicable",
        "hybrid": "medium",
    }[medium]


def classify_visual_text(text: str, *, reference_paths: Iterable[str | Path] = ()) -> dict[str, Any]:
    reference_names = [Path(value).name for value in reference_paths]
    evidence_text = _normalized_text([text, *reference_names])
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for medium, terms in _MEDIUM_TERMS.items():
        scores[medium], matched[medium] = _term_score(evidence_text, terms)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_medium, top_score = ranked[0]
    second_medium, second_score = ranked[1]
    if top_score <= 0:
        medium = "hybrid"
        confidence = 0.25
        medium_evidence: list[str] = []
    elif second_score >= 3.5 and second_score >= top_score * 0.55:
        medium = "hybrid"
        confidence = min(0.75, 0.45 + (top_score + second_score) / 40)
        medium_evidence = (matched[top_medium] + matched[second_medium])[:12]
    else:
        medium = top_medium
        confidence = min(0.97, 0.58 + top_score / 18 + max(0.0, top_score - second_score) / 30)
        medium_evidence = matched[top_medium][:12]

    subject, subject_confidence, subject_evidence = _infer_subject(evidence_text)
    subject_nature, nature_evidence = _infer_subject_nature(evidence_text, subject=subject, medium=medium)
    realism = _realism_for_medium(medium)
    identity = "high" if subject in {"human", "fictional-character", "mascot"} else "medium"
    performance_terms = ("dialogue", "speaks", "talks", "lip sync", "对话", "说话", "口型", "表演", "情绪")
    performance = "high" if any(_contains_term(evidence_text, term) for term in performance_terms) else "medium"
    confidence = round(confidence, 3)
    return {
        "version": VISUAL_PROFILE_VERSION,
        "medium": medium,
        "subject": subject,
        "subject_nature": subject_nature,
        "realism": realism,
        "identity_strictness": identity,
        "performance_complexity": performance,
        "confidence": confidence,
        "subject_confidence": subject_confidence,
        "review_required": (
            confidence < 0.7
            or subject_confidence < 0.7
            or medium == "hybrid"
            or subject_nature in {"unknown", "mixed"}
        ),
        "source": "text-and-reference-name-heuristic",
        "evidence": {
            "medium_terms": medium_evidence,
            "subject_terms": subject_evidence,
            "subject_nature_terms": nature_evidence,
            "reference_names": reference_names,
        },
        "limitations": [
            "No image or video pixels were inspected.",
            "Use a multimodal reviewer for pixel-based classification and store the confirmed manual profile.",
        ],
    }


def _project_text(project: dict[str, Any]) -> str:
    parts: list[Any] = [
        project.get("title", ""),
        project.get("topic", ""),
        project.get("story", ""),
        project.get("character_bible", ""),
        project.get("style_bible", ""),
    ]
    for character in project.get("characters", []):
        if isinstance(character, dict):
            parts.extend([character.get("name", ""), character.get("identity", "")])
    for shot in project.get("shots", []):
        if isinstance(shot, dict):
            parts.extend([shot.get("summary", ""), shot.get("image_prompt", ""), shot.get("video_prompt", "")])
    return _normalized_text(parts)


def _project_reference_names(project: dict[str, Any]) -> list[str]:
    values: list[str] = []
    master = project.get("character_master") if isinstance(project.get("character_master"), dict) else {}
    values.extend(str(item) for item in master.get("source_references", []) if str(item).strip())
    for character in project.get("characters", []):
        if isinstance(character, dict):
            values.extend(str(item) for item in character.get("references", []) if str(item).strip())
    for shot in project.get("shots", []):
        if isinstance(shot, dict):
            for field in ("image_references", "video_references"):
                values.extend(str(item) for item in shot.get(field, []) if str(item).strip())
    return values


def resolve_visual_profile(project: dict[str, Any]) -> dict[str, Any]:
    config = visual_profile_config(project)
    analysis = classify_visual_text(_project_text(project), reference_paths=_project_reference_names(project))
    if config["mode"] == "manual":
        medium = str(config["medium"])
        subject = analysis["subject"] if config["subject"] == "auto" else config["subject"]
        if config["subject_nature"] == "auto":
            manual_evidence_text = _normalized_text([_project_text(project), *_project_reference_names(project)])
            subject_nature, nature_evidence = _infer_subject_nature(
                manual_evidence_text,
                subject=subject,
                medium=medium,
            )
            analysis["evidence"]["subject_nature_terms"] = nature_evidence
        else:
            subject_nature = config["subject_nature"]
        realism = _realism_for_medium(medium) if config["realism"] == "auto" else config["realism"]
        identity = analysis["identity_strictness"] if config["identity_strictness"] == "auto" else config["identity_strictness"]
        performance = analysis["performance_complexity"] if config["performance_complexity"] == "auto" else config["performance_complexity"]
        unresolved_auto_fields = [
            field
            for field in ("subject", "subject_nature", "realism", "identity_strictness", "performance_complexity")
            if config[field] == "auto"
        ]
        auto_axes_need_review = (
            ("subject" in unresolved_auto_fields and float(analysis["subject_confidence"]) < 0.7)
            or ("subject_nature" in unresolved_auto_fields and subject_nature in {"unknown", "mixed"})
        )
        fully_confirmed = bool(config["confirmed"]) and not auto_axes_need_review
        result = {
            **analysis,
            "medium": medium,
            "subject": subject,
            "subject_nature": subject_nature,
            "realism": realism,
            "identity_strictness": identity,
            "performance_complexity": performance,
            "confidence": 1.0 if fully_confirmed else 0.85,
            "review_required": not fully_confirmed,
            "source": "manual-project-contract",
        }
    else:
        result = analysis
        for field in ("subject", "subject_nature", "realism", "identity_strictness", "performance_complexity"):
            if config[field] != "auto":
                result[field] = config[field]
        if config["confirmed"]:
            result["review_required"] = False
            result["source"] = "confirmed-auto-analysis"
    policy = dict(_POLICIES[result["medium"]])
    if result["medium"] == "photoreal" and result["subject_nature"] == "real-person":
        policy["prompt_direction"] = (
            "Natural live-action photography of the confirmed real person. Preserve the supplied identity, facial likeness, anatomy, wardrobe, lighting continuity, and physically plausible motion and contact."
        )
        policy["avoid"] = list(policy["avoid"]) + ["identity substitution", "unapproved age or facial changes"]
    elif result["medium"] == "photoreal" and result["subject_nature"] == "synthetic-human":
        policy["prompt_direction"] = (
            "Natural live-action visual grammar for an explicitly synthetic, AI-generated human. Do not claim or imply that this is an actual person. Preserve the generated identity, realistic anatomy, wardrobe, lighting continuity, hand-object contact, and physically plausible motion."
        )
        policy["avoid"] = list(policy["avoid"]) + ["real-person identity claims", "fused hands or props", "broken contact physics"]
    elif result["medium"] == "photoreal" and result["subject_nature"] == "human-like-fictional":
        policy["prompt_direction"] = (
            "Photorealistic fictional character render, not a real person or live-action recording. Preserve the fictional human-like design, facial identity, anatomy, wardrobe, lighting continuity, and physically plausible motion."
        )
        policy["avoid"] = list(policy["avoid"]) + ["live-action identity assumptions"]
    elif result["medium"] == "photoreal" and result["subject_nature"] != "real-person":
        policy["prompt_direction"] = (
            "Photoreal-looking generated visual. Do not infer that the subject is a real person; preserve the declared fictional or unknown subject identity and physically plausible motion."
        )
    return {
        **result,
        "mode": config["mode"],
        "confirmed": config["confirmed"],
        "generation_policy": policy,
    }


def visual_prompt_direction(project: dict[str, Any]) -> str:
    profile = resolve_visual_profile(project)
    policy = profile["generation_policy"]
    return (
        f"Medium: {profile['medium']}; subject: {profile['subject']}; subject nature: {profile['subject_nature']}; realism: {profile['realism']}.\n"
        f"{policy['prompt_direction']}\n"
        f"Avoid: {', '.join(policy['avoid'])}."
    )


def apply_visual_profile(
    project: dict[str, Any],
    *,
    mode: str,
    medium: str = "auto",
    subject: str = "auto",
    subject_nature: str = "auto",
    realism: str = "auto",
    identity_strictness: str = "auto",
    performance_complexity: str = "auto",
    confirmed: bool = False,
) -> dict[str, Any]:
    project["visual_profile"] = {
        "version": VISUAL_PROFILE_VERSION,
        "mode": mode,
        "medium": medium,
        "subject": subject,
        "subject_nature": subject_nature,
        "realism": realism,
        "identity_strictness": identity_strictness,
        "performance_complexity": performance_complexity,
        "confirmed": bool(confirmed),
    }
    errors = validate_visual_profile(project)
    if errors:
        raise ValueError("; ".join(errors))
    resolved = resolve_visual_profile(project)
    project["visual_profile"]["last_analysis"] = {
        key: resolved[key]
        for key in (
            "medium",
            "subject",
            "subject_nature",
            "realism",
            "identity_strictness",
            "performance_complexity",
            "confidence",
            "review_required",
            "source",
            "evidence",
            "limitations",
        )
    }
    return resolved
