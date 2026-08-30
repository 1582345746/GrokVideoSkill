#!/usr/bin/env python3
from __future__ import annotations

import re
import time
import urllib.parse
from copy import deepcopy
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json, read_json


NEWS_VERSION = 1
RECORD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EDITORIAL_STATUSES = {"researching", "verified", "blocked"}
SOURCE_TYPES = {"primary", "secondary"}
VISUAL_RIGHTS = {"facts-only", "licensed", "public-domain", "user-provided"}


def news_file(root: Path) -> Path:
    return root / "news.json"


def create_news_contract(root: Path, *, topic: str, region: str, language: str, window_hours: int) -> dict[str, Any]:
    if window_hours < 1 or window_hours > 720:
        raise SkillError("news window must be from 1 to 720 hours")
    path = news_file(root)
    if path.exists():
        raise SkillError(f"news contract already exists: {path}")
    value = {
        "version": NEWS_VERSION,
        "topic": topic.strip(),
        "region": region.strip(),
        "language": language.strip(),
        "as_of": "",
        "created_at": int(time.time()),
        "selection": {
            "mode": "hot-topic-research",
            "window_hours": window_hours,
            "rationale": "",
            "search_queries": [],
        },
        "sources": [],
        "claims": [],
        "script_segments": [],
        "editorial": {
            "status": "researching",
            "fact_checked_at": "",
            "unresolved_conflicts": [],
            "corrections": [],
        },
    }
    atomic_write_json(path, value)
    return value


def load_news_contract(root: Path) -> dict[str, Any]:
    value = read_json(news_file(root))
    if value.get("version") != NEWS_VERSION:
        raise SkillError("news.json has an unsupported version")
    return value


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _valid_https_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    sensitive_query = {
        name.lower().replace("-", "_")
        for name, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    } & {"key", "api_key", "token", "access_token", "signature", "secret"}
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not sensitive_query
    )


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).lower().replace("-", "_")
            if name in {
                "api_key",
                "quickai_key",
                "quickainew_key",
                "quickai_image_key",
                "quickai_video_key",
                "quickainew_video_key",
                "authorization",
                "secret",
            }:
                return True
            if _contains_secret_field(child):
                return True
    return isinstance(value, list) and any(_contains_secret_field(item) for item in value)


def _looks_like_iso_time(value: str) -> bool:
    text = value.strip()
    return bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?", text))


def validate_news_contract(root: Path, project: dict[str, Any], *, allow_missing: bool = False) -> list[str]:
    errors: list[str] = []
    path = news_file(root)
    if not path.is_file():
        return [] if allow_missing else ["news-video project requires news.json"]
    try:
        news = load_news_contract(root)
    except SkillError as error:
        return [str(error)]
    if _contains_secret_field(news):
        errors.append("news contract contains a credential-like field")
    for name in ("topic", "region", "language", "as_of"):
        if not str(news.get(name, "")).strip():
            errors.append(f"news.{name} is required")
    if str(news.get("as_of", "")).strip() and not _looks_like_iso_time(str(news["as_of"])):
        errors.append("news.as_of must be an ISO 8601 date or timestamp")
    selection = news.get("selection") if isinstance(news.get("selection"), dict) else {}
    if not str(selection.get("rationale", "")).strip():
        errors.append("news.selection.rationale is required")
    queries = selection.get("search_queries", [])
    if not isinstance(queries, list) or not all(isinstance(item, str) and item.strip() for item in queries):
        errors.append("news.selection.search_queries must be an array of non-empty strings")

    raw_sources = news.get("sources", [])
    if not isinstance(raw_sources, list):
        errors.append("news.sources must be an array")
    sources = _records(raw_sources)
    if len(sources) < 2:
        errors.append("news.sources requires at least two sources")
    source_ids: set[str] = set()
    publishers: set[str] = set()
    primary_ids: set[str] = set()
    for index, source in enumerate(sources):
        prefix = f"news.sources[{index}]"
        source_id = str(source.get("id", "")).strip()
        if not RECORD_ID_RE.fullmatch(source_id):
            errors.append(f"{prefix}.id must use lowercase letters, digits, and hyphens")
        elif source_id in source_ids:
            errors.append(f"duplicate news source id: {source_id}")
        source_ids.add(source_id)
        for name in ("title", "publisher", "published_at", "accessed_at"):
            if not str(source.get(name, "")).strip():
                errors.append(f"{prefix}.{name} is required")
        publisher = str(source.get("publisher", "")).strip().lower()
        if publisher:
            publishers.add(publisher)
        for name in ("published_at", "accessed_at"):
            value = str(source.get(name, "")).strip()
            if value and not _looks_like_iso_time(value):
                errors.append(f"{prefix}.{name} must be an ISO 8601 date or timestamp")
        url = str(source.get("url", "")).strip()
        if not _valid_https_url(url):
            errors.append(f"{prefix}.url must be HTTPS without embedded credentials")
        source_type = str(source.get("source_type", "")).strip()
        if source_type not in SOURCE_TYPES:
            errors.append(f"{prefix}.source_type must be primary or secondary")
        elif source_type == "primary":
            primary_ids.add(source_id)
        rights = str(source.get("visual_rights", "facts-only")).strip()
        if rights not in VISUAL_RIGHTS:
            errors.append(f"{prefix}.visual_rights must be one of {', '.join(sorted(VISUAL_RIGHTS))}")
    if len(publishers) < 2:
        errors.append("news.sources must include at least two distinct publishers")

    raw_claims = news.get("claims", [])
    if not isinstance(raw_claims, list):
        errors.append("news.claims must be an array")
    claims = _records(raw_claims)
    if not claims:
        errors.append("news.claims requires at least one sourced factual claim")
    claim_ids: set[str] = set()
    for index, claim in enumerate(claims):
        prefix = f"news.claims[{index}]"
        claim_id = str(claim.get("id", "")).strip()
        if not RECORD_ID_RE.fullmatch(claim_id):
            errors.append(f"{prefix}.id must use lowercase letters, digits, and hyphens")
        elif claim_id in claim_ids:
            errors.append(f"duplicate news claim id: {claim_id}")
        claim_ids.add(claim_id)
        if not str(claim.get("text", "")).strip():
            errors.append(f"{prefix}.text is required")
        supported_by = claim.get("source_ids", [])
        if not isinstance(supported_by, list) or not supported_by or not all(isinstance(item, str) for item in supported_by):
            errors.append(f"{prefix}.source_ids must be a non-empty array")
            continue
        unknown = [source_id for source_id in supported_by if source_id not in source_ids]
        if unknown:
            errors.append(f"{prefix}.source_ids has unknown ids: {', '.join(unknown)}")
        if len(set(supported_by)) < 2 and not any(source_id in primary_ids for source_id in supported_by):
            errors.append(f"{prefix} needs two independent sources or one primary source")

    raw_segments = news.get("script_segments", [])
    if not isinstance(raw_segments, list):
        errors.append("news.script_segments must be an array")
    segments = _records(raw_segments)
    shot_ids = {
        str(shot.get("id", "")) for shot in project.get("shots", []) if isinstance(shot, dict) and str(shot.get("id", ""))
    }
    segment_shots: set[str] = set()
    for index, segment in enumerate(segments):
        prefix = f"news.script_segments[{index}]"
        shot_id = str(segment.get("shot_id", "")).strip()
        if shot_id not in shot_ids:
            errors.append(f"{prefix}.shot_id must match a project shot")
        elif shot_id in segment_shots:
            errors.append(f"duplicate news script segment for shot: {shot_id}")
        segment_shots.add(shot_id)
        if not str(segment.get("narration", "")).strip():
            errors.append(f"{prefix}.narration is required")
        used_claims = segment.get("claim_ids", [])
        if not isinstance(used_claims, list) or not used_claims or not all(isinstance(item, str) for item in used_claims):
            errors.append(f"{prefix}.claim_ids must be a non-empty array")
        else:
            unknown = [claim_id for claim_id in used_claims if claim_id not in claim_ids]
            if unknown:
                errors.append(f"{prefix}.claim_ids has unknown ids: {', '.join(unknown)}")
    missing_segments = sorted(shot_ids - segment_shots)
    if missing_segments:
        errors.append("news.script_segments is missing project shots: " + ", ".join(missing_segments))

    editorial = news.get("editorial") if isinstance(news.get("editorial"), dict) else {}
    status = str(editorial.get("status", "")).strip()
    if status not in EDITORIAL_STATUSES:
        errors.append("news.editorial.status must be researching, verified, or blocked")
    elif status != "verified":
        errors.append("news.editorial.status must be verified before generation")
    checked = str(editorial.get("fact_checked_at", "")).strip()
    if not checked or not _looks_like_iso_time(checked):
        errors.append("news.editorial.fact_checked_at must be an ISO 8601 date or timestamp")
    conflicts = editorial.get("unresolved_conflicts", [])
    if not isinstance(conflicts, list):
        errors.append("news.editorial.unresolved_conflicts must be an array")
    elif conflicts:
        errors.append("news.editorial.unresolved_conflicts must be empty before generation")
    return errors


def news_context(root: Path) -> dict[str, Any]:
    news = load_news_contract(root)
    project = apply_news_script(read_json(root / "project.json"), news)
    return {
        "project": project,
        "news": news,
        "project_path": str((root / "project.json").resolve()),
        "news_path": str(news_file(root).resolve()),
    }


def apply_news_script(project: dict[str, Any], news: dict[str, Any]) -> dict[str, Any]:
    """Overlay narration and visible beats without mutating project.json."""
    value = deepcopy(project)
    segments = {
        str(item.get("shot_id", "")): item
        for item in news.get("script_segments", [])
        if isinstance(item, dict) and str(item.get("shot_id", "")).strip()
    }
    generated_beats: list[dict[str, Any]] = []
    for shot in value.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("id", ""))
        segment = segments.get(shot_id)
        if not segment:
            continue
        narration = str(segment.get("narration", "")).strip()
        if narration and not str(shot.get("narration", "")).strip():
            shot["narration"] = narration
        if not str(shot.get("beat_id", "")).strip():
            shot["beat_id"] = f"beat-{shot_id}"
        visible = str(shot.get("summary", "")).strip() or str(shot.get("video_prompt", "")).strip()
        generated_beats.append(
            {
                "id": str(shot["beat_id"]),
                "role": "verified-claim",
                "visible_event": visible,
                "claim_ids": list(segment.get("claim_ids", [])) if isinstance(segment.get("claim_ids"), list) else [],
            }
        )
    if not value.get("story_beats") and generated_beats:
        value["story_beats"] = generated_beats
    return value
