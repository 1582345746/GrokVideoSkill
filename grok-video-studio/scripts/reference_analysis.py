"""Local reference-video measurements and hash-bound evidence, without semantic grading."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
from pathlib import Path
from typing import Any

from gvs_common import SkillError, atomic_write_json


ANALYSIS_VERSION = 1
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _run(command: list[str], action: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SkillError(f"reference analysis could not complete {action}: {type(error).__name__}") from error
    if result.returncode:
        raise SkillError(f"reference analysis failed during {action}: {(result.stderr or result.stdout).strip()[-800:]}")
    return result


def _probe(source: Path, ffprobe: str) -> dict[str, Any]:
    result = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)], "media probe", timeout=60)
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (ValueError, TypeError, KeyError, StopIteration) as error:
        raise SkillError("reference source must contain a readable video stream") from error
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise SkillError("reference source must have positive finite duration and dimensions")
    return {
        "duration_seconds": duration, "width": width, "height": height,
        "video_codec": video.get("codec_name"), "average_frame_rate": video.get("avg_frame_rate"),
        "nominal_frame_rate": video.get("r_frame_rate"),
        "has_audio": audio is not None,
        "audio": {k: audio.get(k) for k in ("codec_name", "sample_rate", "channels")} if audio else None,
        "subtitle_stream_count": sum(s.get("codec_type") == "subtitle" for s in streams),
    }


def _filter_candidates(candidates: list[float], duration: float, min_interval: float) -> tuple[list[float], list[float]]:
    accepted: list[float] = []
    discarded: list[float] = []
    for value in sorted(candidates):
        previous = accepted[-1] if accepted else 0.0
        if (not math.isfinite(value) or value <= 0 or value >= duration
                or value - previous < min_interval or duration - value < min_interval):
            discarded.append(value)
        else:
            accepted.append(value)
    return accepted, discarded


def _statistics(intervals: list[float]) -> dict[str, float]:
    return {
        "mean_seconds": statistics.mean(intervals), "median_seconds": statistics.median(intervals),
        "minimum_seconds": min(intervals), "maximum_seconds": max(intervals),
    }


def _scene_measurements(source: Path, ffmpeg: str, duration: float, threshold: float, min_interval: float) -> dict[str, Any]:
    result = _run([ffmpeg, "-hide_banner", "-nostats", "-i", str(source), "-an", "-vf",
                   f"select='gt(scene,{threshold:.8g})',showinfo", "-f", "null", "-"], "scene candidate detection")
    candidates = [float(t) for t in re.findall(r"pts_time:(" + _NUMBER + r")", result.stderr)]
    filtered, discarded = _filter_candidates(candidates, duration, min_interval)
    points = [0.0, *filtered, duration]
    intervals = [b-a for a, b in zip(points, points[1:])]
    return {
        "method": "ffmpeg_scene_score", "threshold": threshold, "minimum_interval_seconds": min_interval,
        "raw_candidate_count": len(candidates), "raw_candidates_seconds": candidates,
        "filtered_candidate_count": len(filtered), "filtered_candidates_seconds": filtered,
        "discarded_candidate_count": len(discarded), "discarded_candidates_seconds": discarded,
        "candidate_intervals_seconds": intervals, "candidate_interval_statistics": _statistics(intervals),
        "verified_shot_count": None,
        "status": "candidate-measurements-only",
        "limit": "Candidates are not verified edits. Motion, flashes and recording overlays can create false positives; similar views can hide real cuts.",
    }


def _parse_silence(log: str, duration: float) -> list[dict[str, float]]:
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value_text in re.findall(r"silence_(start|end): (" + _NUMBER + r")", log):
        value = max(0.0, min(duration, float(value_text)))
        if kind == "start":
            start = value if start is None else min(start, value)
        elif start is not None:
            if value > start:
                intervals.append((start, value))
            start = None
    if start is not None and start < duration:
        intervals.append((start, duration))
    merged: list[list[float]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return [{"start_seconds": left, "end_seconds": right, "duration_seconds": right-left} for left, right in merged]


def _audio_measurements(source: Path, ffmpeg: str, duration: float, has_audio: bool) -> dict[str, Any]:
    limit = "Amplitude silence is not speech activity. It cannot identify wording, speaker, lip sync, intelligibility, sighs, or intentional pauses."
    if not has_audio:
        return {"status": "no-audio-stream", "silence_ratio": None, "silence_intervals": [], "limit": limit}
    result = _run([ffmpeg, "-hide_banner", "-nostats", "-i", str(source), "-vn", "-af",
                   "silencedetect=noise=-35dB:d=0.3,volumedetect", "-f", "null", "-"], "audio amplitude measurement")
    intervals = _parse_silence(result.stderr, duration)
    def volume(name: str) -> float | None:
        match = re.search(name + r": (" + _NUMBER + r"|-inf) dB", result.stderr)
        if not match or match.group(1) == "-inf":
            return None
        return float(match.group(1))
    return {
        "status": "amplitude-measurements-only", "threshold_db": -35, "minimum_silence_seconds": 0.3,
        "silence_ratio": min(1.0, sum(i["duration_seconds"] for i in intervals)/duration),
        "silence_intervals": intervals, "mean_volume_db": volume("mean_volume"),
        "max_volume_db": volume("max_volume"), "limit": limit,
    }


def _sample_times(boundaries: list[float], duration: float, maximum: int) -> list[float]:
    if not boundaries:
        count = min(maximum, max(1, math.ceil(duration/2)))
        return [duration*(i+0.5)/count for i in range(count)]
    points = [0.0, *boundaries, duration]
    centers = [(left+right)/2 for left, right in zip(points, points[1:])]
    if len(centers) <= maximum:
        return centers
    indices = [round(i*(len(centers)-1)/(maximum-1)) for i in range(maximum)] if maximum > 1 else [len(centers)//2]
    return [centers[i] for i in indices]


def _frames(source: Path, ffmpeg: str, destination: Path, times: list[float]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    frame_root = destination / "frames"
    frame_root.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for index, at in enumerate(times, 1):
        path = frame_root / f"frame-{index:03d}.jpg"
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.6f}", "-i", str(source),
              "-frames:v", "1", "-vf", "scale=240:426:force_original_aspect_ratio=decrease,pad=240:426:(ow-iw)/2:(oh-ih)/2",
              "-q:v", "3", str(path)], "evidence frame extraction", timeout=60)
        if not path.is_file() or path.stat().st_size == 0:
            raise SkillError(f"reference evidence extraction produced no frame at {at:.3f}s")
        frames.append({"id": f"frame-{index:03d}", "at_seconds": at, "path": path.relative_to(destination).as_posix(), "sha256": _digest(path)})
    contact = destination / "contact-sheet.jpg"
    columns = min(4, len(frames))
    rows = math.ceil(len(frames)/columns)
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-i", str(frame_root/"frame-%03d.jpg"),
          "-vf", f"tile=layout={columns}x{rows}:nb_frames={len(frames)}:padding=4:margin=4",
          "-frames:v", "1", "-q:v", "3", str(contact)], "contact sheet rendering", timeout=60)
    if not contact.is_file() or not contact.stat().st_size:
        raise SkillError("reference contact sheet is missing or empty")
    return frames, {"path": contact.name, "sha256": _digest(contact)}


def analyze_reference(
    path: Path | str,
    output: Path | str,
    *,
    scene_threshold: float = 0.25,
    min_interval: float = 0.5,
    max_frames: int = 12,
) -> dict[str, Any]:
    """Measure a local video; evidence always remains unreviewed until Codex/human review."""
    source = Path(path).expanduser().resolve()
    output_root = Path(output).expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise SkillError("reference video is missing or empty")
    try:
        threshold, spacing = float(scene_threshold), float(min_interval)
    except (ValueError, TypeError) as error:
        raise SkillError("reference scene settings must be finite numbers") from error
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise SkillError("scene_threshold must be greater than 0 and at most 1")
    if not math.isfinite(spacing) or spacing <= 0:
        raise SkillError("min_interval must be positive and finite")
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= 48:
        raise SkillError("max_frames must be an integer from 1 to 48")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SkillError("ffmpeg and ffprobe are required for reference video analysis")
    source_sha = _digest(source)
    settings = {"scene_threshold": threshold, "min_interval": spacing, "max_frames": max_frames}
    analysis_id = f"{source_sha[:16]}-{_json_digest(settings)[:8]}"
    destination = output_root / analysis_id
    destination.mkdir(parents=True, exist_ok=True)
    media = _probe(source, ffprobe)
    duration = media["duration_seconds"]
    scenes = _scene_measurements(source, ffmpeg, duration, threshold, spacing)
    audio = _audio_measurements(source, ffmpeg, duration, media["has_audio"])
    times = _sample_times(scenes["filtered_candidates_seconds"], duration, max_frames)
    frames, contact = _frames(source, ffmpeg, destination, times)
    if _digest(source) != source_sha:
        raise SkillError("reference source changed during analysis; rerun on a stable file")
    report: dict[str, Any] = {
        "version": ANALYSIS_VERSION, "analysis_id": analysis_id,
        "source": {"filename": source.name, "sha256": source_sha, "bytes": source.stat().st_size},
        "settings": settings, "media": media, "scene_measurements": scenes, "audio_measurements": audio,
        "sampling": {"method": "candidate-interval-centers-or-uniform", "actual_frames": len(frames),
                     "limit": "A small sample is evidence for review, not exhaustive video QA."},
        "frames": frames, "contact_sheet": contact,
        "semantic_review": {"status": "unverified", "narrative_pass": None, "speaker_pass": None,
                            "visual_continuity_pass": None, "clean_frame_pass": None, "audio_content_pass": None},
        "manual_review_required": [
            "Inspect every sampled frame; identify story, shot function, speakers/listeners and continuity without inferring from file names.",
            "Listen to the source; record actual lines, speaker identity, timing, pauses and dialogue handoffs.",
            "Review recording overlays and embedded subtitles separately; do not copy player UI as creative style.",
            "Separate episode boundaries before interpreting a multi-episode recording as one event.",
            "Review continuous action/contact frames for anatomy, collisions, sighs and cuttable exits; a contact sheet cannot prove these are correct.",
        ],
    }
    report["report_sha256"] = _json_digest(report)
    report_path = destination / "reference-analysis.json"
    atomic_write_json(report_path, report)
    return {"report_path": str(report_path), "contact_sheet_path": str(destination/contact["path"]),
            "frame_paths": [str(destination/frame["path"]) for frame in frames], "report": report}


def validate_reference_analysis(report_path: Path | str, source_path: Path | str | None = None) -> list[str]:
    """Check measurements/evidence haven't changed; this never approves semantic quality."""
    report_path = Path(report_path).resolve()
    errors: list[str] = []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["reference analysis report is missing or unreadable"]
    if not isinstance(report, dict):
        return ["reference analysis report must be an object"]
    if report.get("version") != ANALYSIS_VERSION:
        errors.append("reference analysis version is unsupported")
    material = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("report_sha256") != _json_digest(material):
        errors.append("reference analysis report digest changed")
    frames = report.get("frames") if isinstance(report.get("frames"), list) else []
    if not frames:
        errors.append("reference analysis has no evidence frames")
    contact = report.get("contact_sheet")
    for asset in [*frames, contact]:
        if not isinstance(asset, dict):
            errors.append("reference analysis evidence entry is invalid")
            continue
        try:
            asset_path = (report_path.parent / str(asset.get("path", ""))).resolve()
            asset_path.relative_to(report_path.parent)
        except ValueError:
            errors.append("reference analysis evidence path escapes its directory")
            continue
        if not asset_path.is_file() or _digest(asset_path) != asset.get("sha256"):
            errors.append("reference analysis evidence is missing or changed")
    if source_path is not None:
        source = Path(source_path).expanduser().resolve()
        source_record = report.get("source") if isinstance(report.get("source"), dict) else {}
        if not source.is_file() or _digest(source) != source_record.get("sha256"):
            errors.append("reference source is missing or changed")
    return errors
