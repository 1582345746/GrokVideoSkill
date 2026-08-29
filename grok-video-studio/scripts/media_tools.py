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
SUBTITLE_STYLES = {
    "clean": "FontName=Microsoft YaHei,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=28",
    "cinematic": "FontName=Microsoft YaHei,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=1,Alignment=2,MarginV=34",
    "news": "FontName=Microsoft YaHei,FontSize=20,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,BorderStyle=3,Outline=0,Shadow=0,Alignment=2,MarginV=24",
}


def _frame_rate(value: Any) -> float:
    text = str(value or "").strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return round(float(numerator) / float(denominator), 3) if float(denominator) else 0.0
        return round(float(text), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _run(command: list[str], action: str, *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
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
        encoding="utf-8",
        errors="replace",
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
    audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), None)
    subtitles = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "subtitle"]
    audio_metadata: dict[str, Any] = {}
    if audio:
        tags = audio.get("tags") if isinstance(audio.get("tags"), dict) else {}
        try:
            sample_rate = int(audio.get("sample_rate") or 0)
            channels = int(audio.get("channels") or 0)
            audio_duration = float(audio.get("duration") or duration)
        except (TypeError, ValueError):
            sample_rate, channels, audio_duration = 0, 0, duration
        audio_metadata = {
            "codec": str(audio.get("codec_name") or ""),
            "sample_rate": sample_rate,
            "channels": channels,
            "duration": round(audio_duration, 3),
            "language": str(tags.get("language") or ""),
        }
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "frame_rate": _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "has_audio": bool(audio),
        "audio": audio_metadata,
        "has_subtitles": bool(subtitles),
        "subtitle_streams": [
            {
                "codec": str(item.get("codec_name") or ""),
                "language": str((item.get("tags") or {}).get("language") or "") if isinstance(item.get("tags"), dict) else "",
                "title": str((item.get("tags") or {}).get("title") or "") if isinstance(item.get("tags"), dict) else "",
            }
            for item in subtitles
        ],
    }


def probe_audio(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise SkillError(f"audio file is missing or empty: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SkillError("ffprobe is required for audio validation")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise SkillError(f"ffprobe could not read {path.name}: {result.stderr.strip()[-500:]}")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        audio = next(item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio")
        duration = float((payload.get("format") or {}).get("duration") or audio.get("duration") or 0)
    except (json.JSONDecodeError, StopIteration, TypeError, ValueError) as error:
        raise SkillError(f"audio metadata is invalid: {path}") from error
    if duration <= 0:
        raise SkillError(f"audio duration is invalid: {path}")
    return {
        "duration": round(duration, 3),
        "codec": str(audio.get("codec_name") or ""),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
    }


def analyze_audio(path: Path) -> dict[str, Any]:
    metadata = probe_audio(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required for audio analysis")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect,silencedetect=noise=-45dB:d=0.35",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    log = result.stderr or ""
    mean_match = re.search(r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", log, re.IGNORECASE)
    max_match = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", log, re.IGNORECASE)
    silence_durations = [float(value) for value in re.findall(r"silence_duration:\s*(\d+(?:\.\d+)?)", log)]

    def level(match: re.Match[str] | None) -> float | None:
        if not match or match.group(1).lower() in {"inf", "-inf"}:
            return None
        return round(float(match.group(1)), 2)

    silence_seconds = min(metadata["duration"], sum(silence_durations))
    return {
        **metadata,
        "mean_volume_db": level(mean_match),
        "max_volume_db": level(max_match),
        "silence_seconds": round(silence_seconds, 3),
        "silence_ratio": round(silence_seconds / metadata["duration"], 4),
    }


def _atempo_chain(speed: float) -> str:
    if speed <= 0:
        raise SkillError("audio speed must be positive")
    factors: list[float] = []
    while speed > 2.0:
        factors.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def render_dialogue_track(cues: list[dict[str, Any]], output: Path, *, duration: float) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to render dialogue audio")
    if duration <= 0 or not cues:
        raise SkillError("dialogue track requires cues and a positive duration")
    command = [ffmpeg, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for index, cue in enumerate(cues):
        path = Path(str(cue["path"])).resolve()
        audio = probe_audio(path)
        start = float(cue["start"])
        end = float(cue["end"])
        window = end - start
        if start < 0 or window <= 0 or end > duration + 0.05:
            raise SkillError("dialogue cue is outside the target timeline")
        command.extend(["-i", str(path)])
        speed = audio["duration"] / window
        delay = max(0, int(round(start * 1000)))
        label = f"d{index}"
        filters.append(
            f"[{index}:a]{_atempo_chain(speed)},aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=0:{window:.6f},adelay={delay}:all=1[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
        f"apad,atrim=0:{duration:.6f},loudnorm=I=-16:LRA=11:TP=-1.5[aout]"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    command.extend(["-filter_complex", ";".join(filters), "-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(output)])
    _run(command, "rendering the dialogue track")
    return {"path": str(output.resolve()), "bytes": output.stat().st_size, **analyze_audio(output)}


def mix_dialogue_track(
    input_video: Path,
    dialogue_track: Path,
    output: Path,
    *,
    preserve_source_audio: bool = True,
    duck_source_audio: bool = True,
) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to mix dialogue audio")
    media = probe_media(input_video)
    probe_audio(dialogue_track)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-i", str(input_video), "-i", str(dialogue_track)]
    filters = [f"[1:a]aresample=48000,apad,atrim=0:{media['duration']:.6f}[dialogue]"]
    if preserve_source_audio and media["has_audio"]:
        filters.append(f"[0:a]aresample=48000,apad,atrim=0:{media['duration']:.6f}[source]")
        if duck_source_audio:
            filters.append("[dialogue]asplit=2[dialogue_sidechain][dialogue_mix]")
            filters.append("[source][dialogue_sidechain]sidechaincompress=threshold=0.025:ratio=10:attack=15:release=350[ducked]")
            base = "[ducked]"
            dialogue_input = "[dialogue_mix]"
        else:
            base = "[source]"
            dialogue_input = "[dialogue]"
        filters.append(f"{base}{dialogue_input}amix=inputs=2:duration=longest:normalize=0,loudnorm=I=-16:LRA=11:TP=-1.5[aout]")
    else:
        filters.append("[dialogue]loudnorm=I=-16:LRA=11:TP=-1.5[aout]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{media['duration']:.6f}",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run(command, "mixing dialogue into the video")
    result = probe_media(output)
    return {"path": str(output.resolve()), "bytes": output.stat().st_size, **result, "audio": analyze_audio(output)}


def replace_audio_track(video_source: Path, audio_source: Path, output: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SkillError("ffmpeg is required to replace a video audio track")
    video = probe_media(video_source)
    audio = probe_media(audio_source)
    if not audio["has_audio"]:
        raise SkillError("replacement audio source has no audio stream")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_source),
            "-i",
            str(audio_source),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{video['duration']:.6f}",
            "-movflags",
            "+faststart",
            str(output),
        ],
        "restoring the final mixed audio after lip sync",
    )
    result = probe_media(output)
    return {"path": str(output.resolve()), "bytes": output.stat().st_size, **result, "audio": analyze_audio(output)}


def quality_report(path: Path, *, expected_size: str = "auto", expected_duration: float | None = None) -> dict[str, Any]:
    media = probe_media(path)
    errors: list[str] = []
    warnings: list[str] = []
    if expected_size != "auto":
        if not SIZE_RE.fullmatch(expected_size):
            raise SkillError("expected size must be WIDTHxHEIGHT or auto")
        width, height = (int(value) for value in expected_size.split("x", 1))
        if (media["width"], media["height"]) != (width, height):
            expected_ratio = width / height
            actual_ratio = media["width"] / media["height"]
            if abs(expected_ratio - actual_ratio) / expected_ratio > 0.08:
                errors.append(f"orientation or dimensions mismatch: expected {width}x{height}, got {media['width']}x{media['height']}")
            else:
                warnings.append(f"provider scaled the requested frame: expected {width}x{height}, got {media['width']}x{media['height']}")
    if expected_duration is not None and abs(media["duration"] - expected_duration) > max(1.5, expected_duration * 0.25):
        warnings.append(f"duration differs from request: expected about {expected_duration}s, got {media['duration']}s")
    if media["codec"] != "h264" or media["pixel_format"] != "yuv420p":
        warnings.append("delivery compatibility is best with H.264 yuv420p")

    ffmpeg = shutil.which("ffmpeg")
    black_events: list[str] = []
    freeze_events: list[str] = []
    audio_signals: dict[str, Any] = {}
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
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        log = result.stderr or ""
        black_events = re.findall(r"black_start:[^\r\n]+", log)
        freeze_events = re.findall(r"freeze_(?:start|end|duration):[^\r\n]+", log)
        if black_events:
            warnings.append(f"detected {len(black_events)} black segment event(s)")
        if freeze_events:
            warnings.append(f"detected {len(freeze_events)} freeze event(s)")
        if media["has_audio"]:
            try:
                audio_signals = analyze_audio(path)
                maximum = audio_signals.get("max_volume_db")
                mean = audio_signals.get("mean_volume_db")
                if maximum is None or maximum < -35:
                    warnings.append("audio track is effectively silent")
                elif mean is not None and mean < -42:
                    warnings.append("audio track is unusually quiet")
                if float(audio_signals.get("silence_ratio", 0)) > 0.95:
                    warnings.append("audio track is more than 95% silent")
            except SkillError as error:
                warnings.append(f"audio analysis failed: {error}")
    else:
        warnings.append("ffmpeg not found; black/freeze scan was skipped")

    return {
        "ok": not errors,
        "path": str(path.resolve()),
        "media": media,
        "errors": errors,
        "warnings": warnings,
        "signals": {"black_events": black_events[:20], "freeze_events": freeze_events[:20], "audio": audio_signals},
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
    labels = (["key"] if count == 1 else (["first", "key", "end"] if count == 3 else [f"frame-{index:02d}" for index in range(1, count + 1)]))
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
        frames.append({"label": labels[index - 1], "path": str(output.resolve()), "at_seconds": round(at, 3), "bytes": output.stat().st_size})
    return frames


def _subtitle_filter(path: Path, style: str = "clean") -> str:
    if style not in SUBTITLE_STYLES:
        raise SkillError("subtitle style must be clean, cinematic, or news")
    value = path.resolve().as_posix().replace("'", "\\'").replace(":", "\\:")
    return f"subtitles=filename='{value}':force_style='{SUBTITLE_STYLES[style]}'"


def postprocess_video(
    input_path: Path,
    output_path: Path,
    *,
    music: Path | None = None,
    voice: Path | None = None,
    subtitles: Path | None = None,
    subtitle_style: str = "clean",
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
        video_filters.append(_subtitle_filter(subtitles, subtitle_style))
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
        if media["has_audio"]:
            source_volume = "0.35" if voice else "0.75"
            filters.append(f"[0:a]volume={source_volume},apad,atrim=0:{media['duration']:.3f}[source]")
            labels.append("[source]")
        for name, index in audio_inputs:
            label = f"a{index}"
            volume = "0.20" if name == "music" else "1.0"
            filters.append(f"[{index}:a]volume={volume},apad,atrim=0:{media['duration']:.3f}[{label}]")
            labels.append(f"[{label}]")
        if len(labels) == 1:
            filters.append(f"{labels[0]}loudnorm=I=-16:LRA=11:TP=-1.5[aout]")
        else:
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=2:normalize=0,"
                "loudnorm=I=-16:LRA=11:TP=-1.5[aout]"
            )
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
    return {
        "path": str(output_path.resolve()),
        "bytes": output_path.stat().st_size,
        **result,
        "subtitle_style": subtitle_style if subtitles else "",
    }


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
