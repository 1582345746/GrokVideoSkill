#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gvs_common import SkillError, assert_mp4


SIZE_RE = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")


def _run(command: list[str], action: str, *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise SkillError(f"ffmpeg failed while {action}: {detail}")
    return result


def probe_media(path: Path) -> dict[str, Any]:
    assert_mp4(path)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("ffprobe is required for media validation")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SkillError(f"ffprobe could not read {path.name}: {result.stderr.strip()[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SkillError(f"ffprobe returned invalid JSON for {path.name}") from error
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    if not video:
        raise SkillError(f"media has no video stream: {path}")
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as error:
        raise SkillError(f"invalid media metadata: {path}") from error
    if duration <= 0 or width <= 0 or height <= 0:
        raise SkillError(f"media duration or dimensions are invalid: {path}")
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "has_audio": any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams),
    }


def quality_report(path: Path, *, expected_size: str = "auto", expected_duration: float | None = None) -> dict[str, Any]:
    media = probe_media(path)
    errors: list[str] = []
    warnings: list[str] = []
    if expected_size != "auto":
        if not SIZE_RE.fullmatch(expected_size):
            raise SkillError("expected size must be WIDTHxHEIGHT or auto")
        width, height = (int(value) for value in expected_size.split("x", 1))
        if (media["width"], media["height"]) != (width, height):
            errors.append(f"orientation or dimensions mismatch: expected {width}x{height}, got {media['width']}x{media['height']}")
    if expected_duration is not None and abs(media["duration"] - expected_duration) > max(1.5, expected_duration * 0.25):
        warnings.append(f"duration differs from request: expected about {expected_duration}s, got {media['duration']}s")
    if media["codec"] != "h264" or media["pixel_format"] != "yuv420p":
        warnings.append("delivery compatibility is best with H.264 yuv420p")

    ffmpeg = shutil.which("ffmpeg")
    black_events: list[str] = []
    freeze_events: list[str] = []
    if ffmpeg:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-vf",
                "blackdetect=d=0.6:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        log = result.stderr or ""
        black_events = re.findall(r"black_start:[^\r\n]+", log)
        freeze_events = re.findall(r"freeze_(?:start|end|duration):[^\r\n]+", log)
        if black_events:
            warnings.append(f"detected {len(black_events)} black segment event(s)")
        if freeze_events:
            warnings.append(f"detected {len(freeze_events)} freeze event(s)")
    else:
        warnings.append("ffmpeg not found; black/freeze scan was skipped")

    return {
        "ok": not errors,
        "path": str(path.resolve()),
        "media": media,
        "errors": errors,
        "warnings": warnings,
        "signals": {"black_events": black_events[:20], "freeze_events": freeze_events[:20]},
        "manual_review_required": [
            "character identity and wardrobe continuity",
            "hands, fingers, limbs, and facial anatomy",
            "motion naturalness and camera continuity",
            "clean frame: reject unintended app UI, buttons, counters, comments, captions, logos, and watermarks",
        ],
    }


def export_review_frames(input_path: Path, output_dir: Path, *, stem: str, count: int = 3) -> list[dict[str, Any]]:
    if count < 1 or count > 9:
        raise SkillError("review frame count must be from 1 to 9")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to export review frames")
    media = probe_media(input_path)
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-") or "video"
    fractions = [0.5] if count == 1 else [0.05 + index * 0.95 / (count - 1) for index in range(count)]
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for index, fraction in enumerate(fractions, 1):
        at = max(0.0, min(media["duration"] * fraction, max(0.0, media["duration"] - 0.05)))
        output = output_dir / f"{safe_stem}-review-{index:02d}.jpg"
        _run(
            [ffmpeg, "-y", "-ss", f"{at:.3f}", "-i", str(input_path), "-frames:v", "1", "-update", "1", "-q:v", "2", str(output)],
            f"exporting review frame {index}",
            timeout=120,
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise SkillError(f"review frame export produced no image: {output.name}")
        frames.append({"path": str(output.resolve()), "at_seconds": round(at, 3), "bytes": output.stat().st_size})
    return frames


def _subtitle_filter(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", "\\'").replace(":", "\\:")
    return f"subtitles=filename='{value}'"


def postprocess_video(
    input_path: Path,
    output_path: Path,
    *,
    music: Path | None = None,
    voice: Path | None = None,
    subtitles: Path | None = None,
    fade_seconds: float = 0.0,
) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required for post-processing")
    media = probe_media(input_path)
    if output_path.suffix.lower() != ".mp4":
        raise SkillError("post-processed output filename must end with .mp4")
    for optional in (music, voice, subtitles):
        if optional is not None and not optional.is_file():
            raise SkillError(f"post-production input does not exist: {optional}")
    if fade_seconds < 0 or fade_seconds > media["duration"] / 2:
        raise SkillError("fade seconds must be non-negative and no more than half the video duration")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-i", str(input_path)]
    audio_inputs: list[tuple[str, int]] = []
    next_index = 1
    if music:
        command.extend(["-stream_loop", "-1", "-i", str(music)])
        audio_inputs.append(("music", next_index))
        next_index += 1
    if voice:
        command.extend(["-i", str(voice)])
        audio_inputs.append(("voice", next_index))

    video_filters: list[str] = []
    if subtitles:
        video_filters.append(_subtitle_filter(subtitles))
    if fade_seconds:
        video_filters.extend(
            [
                f"fade=t=in:st=0:d={fade_seconds:.3f}",
                f"fade=t=out:st={max(0.0, media['duration'] - fade_seconds):.3f}:d={fade_seconds:.3f}",
            ]
        )
    if video_filters:
        command.extend(["-vf", ",".join(video_filters)])
    command.extend(["-map", "0:v:0"])

    if audio_inputs:
        filters: list[str] = []
        labels: list[str] = []
        for name, index in audio_inputs:
            label = f"a{index}"
            volume = "0.20" if name == "music" else "1.0"
            filters.append(f"[{index}:a]volume={volume},apad,atrim=0:{media['duration']:.3f}[{label}]")
            labels.append(f"[{label}]")
        if len(labels) == 1:
            filters.append(f"{labels[0]}anull[aout]")
        else:
            filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=2[aout]")
        command.extend(["-filter_complex", ";".join(filters), "-map", "[aout]"])
    elif media["has_audio"]:
        command.extend(["-map", "0:a:0?"])

    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    _run(command, "post-processing video")
    result = probe_media(output_path)
    return {"path": str(output_path.resolve()), "bytes": output_path.stat().st_size, **result}


def extract_cover(input_path: Path, output_path: Path, *, at_seconds: float = 0.5) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to export a cover")
    media = probe_media(input_path)
    if output_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise SkillError("cover output must end with .jpg, .jpeg, or .png")
    at = max(0.0, min(float(at_seconds), max(0.0, media["duration"] - 0.05)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-y", "-ss", f"{at:.3f}", "-i", str(input_path), "-frames:v", "1", "-q:v", "2", str(output_path)], "exporting cover", timeout=120)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise SkillError("cover export produced no image")
    return {"path": str(output_path.resolve()), "bytes": output_path.stat().st_size, "at_seconds": round(at, 3)}
