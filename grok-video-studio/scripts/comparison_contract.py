#!/usr/bin/env python3
"""Contracts for fast then-versus-now comparison shorts.

The comparison workflow is intentionally a small semantic layer on top of the
normal project/shot contract.  It does not generate media or alter provider
selection.  A valid comparison is a sequence of adjacent ``before`` and
``after`` shots, each pair answering the same question and carrying its own
era lock.
"""

from __future__ import annotations

from typing import Any


COMPARISON_WORKFLOW = "comparison-short"
COMPARISON_AXES = {"then_vs_now", "old_vs_new", "generation_gap", "seasonal_before_after"}
COMPARISON_ORDERS = {"before_then_after", "after_then_before"}
LABEL_DELIVERIES = {"none", "post_overlay"}
SIDES = {"before", "after"}
ERA_LOCK_FIELDS = ("era", "wardrobe", "props", "location", "lighting")


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _side(shot: dict[str, Any]) -> str:
    for key in ("comparison_side", "era", "comparison_era"):
        value = _text(shot.get(key)).lower()
        if value:
            return value
    return ""


def _pair_id(shot: dict[str, Any]) -> str:
    for key in ("comparison_pair_id", "pair_id", "comparison_id"):
        value = _text(shot.get(key))
        if value:
            return value
    return ""


def comparison_config(project: dict[str, Any]) -> dict[str, Any]:
    raw = project.get("comparison_contract")
    value = raw if isinstance(raw, dict) else {}
    return {
        # The built-in workflow is intentionally strict; a draft cannot turn
        # off its pair contract by writing required=false.  Standalone legacy
        # projects may still opt in/out with the explicit field.
        "required": bool(value.get("required", False)) or project.get("workflow") == COMPARISON_WORKFLOW,
        "axis": _text(value.get("axis")) or "then_vs_now",
        "shared_question": _text(value.get("shared_question")),
        "comparison_order": _text(value.get("comparison_order")) or "before_then_after",
        "hook": _text(value.get("hook")),
        "payoff": _text(value.get("payoff")),
        "label_delivery": _text(value.get("label_delivery")) or "post_overlay",
        "max_pair_gap_seconds": _number(value.get("max_pair_gap_seconds"), 0.8),
        "hook_deadline_seconds": _number(value.get("hook_deadline_seconds"), 3.0),
        "max_no_information_seconds": _number(value.get("max_no_information_seconds"), 3.0),
        "pairs": value.get("pairs") if isinstance(value.get("pairs"), list) else [],
    }


def _era_lock_errors(lock: Any, prefix: str) -> list[str]:
    if not isinstance(lock, dict):
        return [f"{prefix} must be an object with era, wardrobe, props, location, and lighting"]
    missing = [field for field in ERA_LOCK_FIELDS if not _text(lock.get(field))]
    if missing:
        return [f"{prefix} is incomplete: fill " + ", ".join(missing)]
    return []


def _effective_duration(shot: dict[str, Any]) -> float | None:
    try:
        if shot.get("timeline_duration") not in (None, ""):
            return float(shot["timeline_duration"])
        seconds = float(shot.get("seconds", 0))
        edit_in = float(shot.get("edit_in", 0) or 0)
        edit_out = shot.get("edit_out")
        if edit_out in (None, ""):
            edit_out = seconds
        return float(edit_out) - edit_in
    except (TypeError, ValueError):
        return None


def _caption_prompt_error(shot: dict[str, Any], index: int) -> str | None:
    """Catch positive caption instructions while allowing explicit exclusions."""
    for field in ("image_prompt", "video_prompt"):
        prompt = _text(shot.get(field)).lower()
        if not prompt:
            continue
        negative_markers = (
            "无字幕",
            "无画面字幕",
            "无可读文字",
            "不要字幕",
            "不加字幕",
            "不生成字幕",
            "不要生成字幕",
            "不出现字幕",
            "禁止字幕",
            "no subtitle",
            "no captions",
            "without captions",
        )
        if any(marker in prompt for marker in negative_markers):
            continue
        positive_markers = (
            "添加字幕",
            "显示字幕",
            "生成字幕",
            "add subtitles",
            "show subtitles",
            "caption overlay",
            "text overlay",
            "captions at the",
            "subtitles at the",
        )
        if any(marker in prompt for marker in positive_markers):
            return f"shots[{index}].{field} requests captions/text overlay; use label_delivery=post_overlay and keep provider pixels clean"
    return None


def validate_comparison_contract(project: dict[str, Any]) -> list[str]:
    """Return blocking errors for a required comparison project.

    Projects without the comparison workflow and without a contract retain
    legacy behavior.  A present contract is still type-checked so accidental
    malformed drafts are visible before generation.
    """
    raw = project.get("comparison_contract")
    errors: list[str] = []
    if raw is not None and not isinstance(raw, dict):
        return ["project.comparison_contract must be an object"]
    if isinstance(raw, dict):
        if "required" in raw and not isinstance(raw.get("required"), bool):
            errors.append("comparison_contract.required must be a boolean")
        if "pairs" in raw and not isinstance(raw.get("pairs"), list):
            errors.append("comparison_contract.pairs must be an array")
        for field in ("axis", "shared_question", "comparison_order", "hook", "payoff", "label_delivery"):
            if field in raw and raw.get(field) is not None and not isinstance(raw.get(field), str):
                errors.append(f"comparison_contract.{field} must be a string")
        for field in ("max_pair_gap_seconds", "hook_deadline_seconds", "max_no_information_seconds"):
            if field in raw and raw.get(field) is not None and _number(raw.get(field)) is None:
                errors.append(f"comparison_contract.{field} must be a number")
    config = comparison_config(project)
    required = config["required"]
    if not required and raw is None:
        return []

    if config["axis"] not in COMPARISON_AXES:
        errors.append("comparison_contract.axis is unsupported")
    if config["comparison_order"] not in COMPARISON_ORDERS:
        errors.append("comparison_contract.comparison_order must be before_then_after or after_then_before")
    if config["label_delivery"] not in LABEL_DELIVERIES:
        errors.append("comparison_contract.label_delivery must be none or post_overlay")
    for field in ("max_pair_gap_seconds", "hook_deadline_seconds", "max_no_information_seconds"):
        value = config[field]
        if value is None or value <= 0:
            errors.append(f"comparison_contract.{field} must be greater than zero")
    if not required:
        return errors

    pairs = config["pairs"]
    if len(pairs) < 3:
        errors.append("comparison_contract.pairs requires at least 3 comparison groups")
    if not config["shared_question"]:
        errors.append("comparison_contract.shared_question is required")
    if not config["hook"]:
        errors.append("comparison_contract.hook is required")
    if not config["payoff"]:
        errors.append("comparison_contract.payoff is required")
    pair_defs: dict[str, dict[str, Any]] = {}
    raw_locks = raw.get("era_locks") if isinstance(raw, dict) else None
    for index, pair in enumerate(pairs):
        prefix = f"comparison_contract.pairs[{index}]"
        if not isinstance(pair, dict):
            errors.append(f"{prefix} must be an object")
            continue
        pair_id = _text(pair.get("id")) or _text(pair.get("pair_id"))
        if not pair_id:
            errors.append(f"{prefix}.id is required")
            continue
        if pair_id in pair_defs:
            errors.append(f"duplicate comparison pair id: {pair_id}")
        pair_defs[pair_id] = pair
        for field in ("shared_question", "before_claim", "after_claim", "visible_difference", "bridge_in", "bridge_out", "payoff"):
            if not _text(pair.get(field)):
                errors.append(f"{prefix}.{field} is required")
        for side in ("before", "after"):
            shot_key = f"{side}_shot_id"
            if shot_key in pair and not _text(pair.get(shot_key)):
                errors.append(f"{prefix}.{shot_key} must be a non-empty shot id")
        pair_question = _text(pair.get("shared_question"))
        if pair_question != config["shared_question"]:
            errors.append(f"{prefix}.shared_question must match comparison_contract.shared_question")
        lock = pair.get("era_lock")
        if lock is None and isinstance(raw_locks, dict):
            lock = raw_locks
        if not isinstance(lock, dict):
            errors.append(f"{prefix}.era_lock must contain before and after locks")
        else:
            for side in ("before", "after"):
                side_lock = lock.get(side)
                if isinstance(side_lock, dict) and "location" not in side_lock and "setting" in side_lock:
                    side_lock = {**side_lock, "location": side_lock.get("setting")}
                errors.extend(_era_lock_errors(side_lock, f"{prefix}.era_lock.{side}"))

    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    if len(shots) != len(pair_defs) * 2:
        errors.append(
            f"comparison project must contain exactly two shots per pair ({len(pair_defs) * 2} expected; got {len(shots)})"
        )
    observed: dict[str, dict[str, list[int]]] = {}
    for index, shot in enumerate(shots):
        prefix = f"shots[{index}]"
        pair_id = _pair_id(shot)
        side = _side(shot)
        if not pair_id:
            errors.append(f"{prefix}.comparison_pair_id is required")
        elif pair_id not in pair_defs:
            errors.append(f"{prefix}.comparison_pair_id references unknown pair {pair_id}")
        if side not in SIDES:
            errors.append(f"{prefix}.comparison_side must be before or after")
        if pair_id and side in SIDES:
            observed.setdefault(pair_id, {"before": [], "after": []})[side].append(index)
        pair = pair_defs.get(pair_id, {})
        difference = _text(shot.get("visible_difference"))
        if not difference and isinstance(pair, dict):
            claim_key = "before_claim" if side == "before" else "after_claim"
            difference = _text(pair.get(claim_key))
        if not difference:
            errors.append(f"{prefix}.visible_difference is required")
        if not _text(shot.get("summary")):
            errors.append(f"{prefix}.summary is required for viewer comprehension")
        duration = _effective_duration(shot)
        if duration is not None and duration > 5.0:
            errors.append(f"{prefix} comparison shot is {duration:.2f}s; keep comparison beats at or below 5s")
        caption_error = _caption_prompt_error(shot, index)
        if caption_error:
            errors.append(caption_error)

    ordered_pair_ids = list(pair_defs)
    for pair_index, (pair_id, pair) in enumerate(pair_defs.items()):
        sides = observed.get(pair_id, {"before": [], "after": []})
        if len(sides["before"]) != 1 or len(sides["after"]) != 1:
            errors.append(
                f"comparison pair {pair_id} must have exactly one before and one after shot"
            )
            continue
        before_index = sides["before"][0]
        after_index = sides["after"][0]
        expected = (before_index + 1, after_index) if config["comparison_order"] == "before_then_after" else (after_index + 1, before_index)
        if expected[0] != expected[1]:
            errors.append(f"comparison pair {pair_id} before/after shots must be adjacent in {config['comparison_order']} order")
        expected_positions = {pair_index * 2, pair_index * 2 + 1}
        if {before_index, after_index} != expected_positions:
            errors.append(
                f"comparison pair {pair_id} must follow the declared pair order ({', '.join(ordered_pair_ids)})"
            )
        shot_by_index = {before_index: shots[before_index], after_index: shots[after_index]}
        for side, shot_index in (("before", before_index), ("after", after_index)):
            declared = _text(pair.get(f"{side}_shot_id"))
            if declared and declared != _text(shot_by_index[shot_index].get("id")):
                errors.append(
                    f"comparison pair {pair_id}.{side}_shot_id must reference the mapped {side} shot"
                )

    audio = project.get("audio") if isinstance(project.get("audio"), dict) else {}
    if audio:
        subtitle_source = _text(audio.get("subtitle_source")).lower()
        if subtitle_source in {"upstream", "project"}:
            errors.append("comparison projects require audio.subtitle_source=none; add labels/subtitles only as a reviewed post overlay")
        if bool(audio.get("allow_upstream_captions", False)):
            errors.append("comparison projects must set audio.allow_upstream_captions=false")
    return errors


def comparison_shot_direction(project: dict[str, Any], shot: dict[str, Any]) -> str:
    """Return the shared question, claim, and era lock for this shot's side."""
    config = comparison_config(project)
    pair_id = _pair_id(shot)
    side = _side(shot)
    if not pair_id or side not in SIDES:
        return ""
    pair = next(
        (
            item
            for item in config["pairs"]
            if isinstance(item, dict)
            and (_text(item.get("id")) or _text(item.get("pair_id"))) == pair_id
        ),
        None,
    )
    if pair is None:
        return ""
    lock = pair.get("era_lock")
    if lock is None:
        raw = project.get("comparison_contract")
        raw_locks = raw.get("era_locks") if isinstance(raw, dict) else None
        lock = raw_locks if isinstance(raw_locks, dict) else {}
    side_lock = lock.get(side) if isinstance(lock, dict) else None
    if isinstance(side_lock, dict) and "location" not in side_lock and "setting" in side_lock:
        side_lock = {**side_lock, "location": side_lock.get("setting")}
    side_lock = side_lock if isinstance(side_lock, dict) else {}
    claim_key = "before_claim" if side == "before" else "after_claim"
    claim = _text(pair.get(claim_key))
    values = [
        f"Pair: {pair_id}; show only the {side} side.",
        f"Shared comparison question: {config['shared_question']}",
        f"This side's claim: {claim}",
        "Lock this shot to the declared era state:",
    ]
    values.extend(f"{field}: {_text(side_lock.get(field))}" for field in ERA_LOCK_FIELDS)
    values.append("Do not mix in wardrobe, props, setting, or lighting from the opposite era.")
    return "\n".join(values)


def comparison_contract_report(project: dict[str, Any]) -> dict[str, Any]:
    """Return machine-readable metrics for preflight and audit output."""
    config = comparison_config(project)
    errors = validate_comparison_contract(project)
    pairs = config["pairs"]
    shots = [shot for shot in project.get("shots", []) if isinstance(shot, dict)]
    durations = [duration for shot in shots if (duration := _effective_duration(shot)) is not None]
    pair_ids = [_pair_id(shot) for shot in shots]
    return {
        "enabled": bool(project.get("comparison_contract") is not None or project.get("workflow") == COMPARISON_WORKFLOW),
        "required": config["required"],
        "ok": not errors,
        "errors": errors,
        "axis": config["axis"],
        "comparison_order": config["comparison_order"],
        "label_delivery": config["label_delivery"],
        "max_pair_gap_seconds": config["max_pair_gap_seconds"],
        "hook_deadline_seconds": config["hook_deadline_seconds"],
        "max_no_information_seconds": config["max_no_information_seconds"],
        "pair_count": len(pairs),
        "shot_count": len(shots),
        "paired_shot_count": sum(1 for pair_id in pair_ids if pair_id),
        "max_shot_seconds": max(durations, default=0.0),
        "shared_question": config["shared_question"],
        "hook_present": bool(config["hook"]),
        "payoff_present": bool(config["payoff"]),
    }

