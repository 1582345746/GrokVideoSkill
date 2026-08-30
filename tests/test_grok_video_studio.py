from __future__ import annotations

import base64
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import struct
import wave
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
CLI = SKILL_ROOT / "scripts" / "grok_video_studio.py"
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
FAKE_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"fake-video-payload"
FAKE_PCM = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * index / 22050))) for index in range(11025))


def pcm_wav(payload: bytes, sample_rate: int = 22050) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)
    return output.getvalue()


FAKE_WAV = pcm_wav(FAKE_PCM)

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from gvs_common import APIError, load_settings  # noqa: E402
from component_manager import (  # noqa: E402
    _docker,
    _download_component_models,
    _ensure_model_disk_space,
    _checkout_component,
    component_status,
    load_model_state,
)
import grok_video_studio as gvs  # noqa: E402
import media_tools  # noqa: E402
import voice_workflow  # noqa: E402
from grok_video_studio import composed_video_prompt, prompt_bytes, prompt_variants, review_shot_asset  # noqa: E402
from media_client import image_reference_report  # noqa: E402
from media_tools import probe_media as probe_media_tool  # noqa: E402
from provider_contracts import (  # noqa: E402
    PROVIDER_CAPABILITIES,
    allows_automatic_failover,
    classify_provider_error,
    is_completed,
    result_urls,
    task_error,
    task_id,
    task_progress,
    task_status,
)


class ProviderContractTests(unittest.TestCase):
    def test_dialogue_and_subtitle_outputs_force_standard_audio_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "source.mp4"
            dialogue = root / "dialogue.wav"
            subtitles = root / "dialogue.srt"
            video.write_bytes(FAKE_MP4)
            dialogue.write_bytes(FAKE_WAV)
            subtitles.write_text("1\n00:00:00,000 --> 00:00:00,500\nTest\n", encoding="utf-8")
            commands: list[list[str]] = []

            def fake_run(command: list[str], _: str) -> None:
                commands.append(command)
                Path(command[-1]).write_bytes(FAKE_MP4)

            video_probe = {
                "path": str(video),
                "duration": 1.0,
                "has_audio": True,
                "width": 1280,
                "height": 720,
                "codec": "h264",
                "pixel_format": "yuv420p",
                "frame_rate": 30.0,
                "has_subtitles": False,
                "subtitle_streams": [],
            }
            with (
                mock.patch("media_tools.shutil.which", return_value="ffmpeg"),
                mock.patch("media_tools._run", side_effect=fake_run),
                mock.patch("media_tools.probe_media", return_value=video_probe),
                mock.patch("media_tools.probe_audio", return_value={"duration": 1.0}),
                mock.patch("media_tools.analyze_audio", return_value={}),
            ):
                media_tools.mix_dialogue_track(video, dialogue, root / "mixed.mp4")
                media_tools.replace_audio_track(video, video, root / "replaced.mp4")
                media_tools.postprocess_video(video, root / "subtitled.mp4", subtitles=subtitles)

        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertIn("-ar", command)
            self.assertEqual(command[command.index("-ar") + 1], "48000")
            self.assertIn("-ac", command)
            self.assertEqual(command[command.index("-ac") + 1], "2")

    def test_media_probe_decodes_ffprobe_output_as_utf8(self) -> None:
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "24/1",
                }
            ],
            "format": {"duration": "10.0", "tags": {"comment": "洪水救援"}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            clip = Path(temp_dir) / "clip.mp4"
            clip.write_bytes(FAKE_MP4)
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload, ensure_ascii=False), stderr="")
            with mock.patch("media_tools.subprocess.run", return_value=completed) as run:
                media = probe_media_tool(clip)

        self.assertEqual(media["width"], 720)
        self.assertEqual(media["height"], 1280)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_offline_response_fixtures(self) -> None:
        fixtures = json.loads((REPO_ROOT / "tests" / "fixtures" / "provider-contracts.json").read_text(encoding="utf-8"))
        for fixture in fixtures:
            with self.subTest(fixture["name"]):
                payload = fixture["payload"]
                self.assertEqual(task_id(payload), fixture["task_id"])
                self.assertEqual(task_status(payload), fixture["status"])
                self.assertEqual(task_progress(payload), fixture["progress"])
                urls = result_urls(payload)
                self.assertEqual(urls[0] if urls else "", fixture["url"])
                if fixture.get("error"):
                    self.assertEqual(task_error(payload), fixture["error"])
                if fixture["url"]:
                    self.assertTrue(is_completed(payload))

    def test_provider_capabilities_and_failover_classification(self) -> None:
        self.assertTrue(PROVIDER_CAPABILITIES["quickai"].text_to_image)
        self.assertTrue(PROVIDER_CAPABILITIES["quickai"].text_to_video)
        self.assertTrue(PROVIDER_CAPABILITIES["quickai"].image_to_video)
        self.assertFalse(PROVIDER_CAPABILITIES["quickainew"].text_to_image)
        self.assertTrue(PROVIDER_CAPABILITIES["quickainew"].text_to_video)
        self.assertTrue(PROVIDER_CAPABILITIES["quickainew"].image_to_video)
        self.assertFalse(PROVIDER_CAPABILITIES["quickai"].video_reference)
        self.assertFalse(PROVIDER_CAPABILITIES["quickainew"].video_reference)
        self.assertEqual(PROVIDER_CAPABILITIES["quickai"].audio_generation, "model_default")
        self.assertEqual(PROVIDER_CAPABILITIES["quickainew"].audio_generation, "explicit_generate_audio")
        self.assertLess(PROVIDER_CAPABILITIES["quickai"].priority, PROVIDER_CAPABILITIES["quickainew"].priority)

        unsupported = APIError(404, "unsupported endpoint")
        ambiguous = APIError(502, "gateway timed out")
        invalid = APIError(422, "invalid prompt")
        known_lookup = APIError(404, "Video request not found")
        self.assertEqual(classify_provider_error(unsupported, phase="create", task_known=False), "capability_unsupported")
        self.assertTrue(allows_automatic_failover(unsupported, phase="create", task_known=False))
        self.assertEqual(classify_provider_error(ambiguous, phase="create", task_known=False), "submission_unknown")
        self.assertFalse(allows_automatic_failover(ambiguous, phase="create", task_known=False))
        self.assertEqual(classify_provider_error(invalid, phase="create", task_known=False), "invalid_input")
        self.assertFalse(allows_automatic_failover(invalid, phase="create", task_known=False))
        self.assertEqual(classify_provider_error(known_lookup, phase="task", task_known=True), "task_lookup_transient")
        self.assertFalse(allows_automatic_failover(known_lookup, phase="task", task_known=True))

    def test_input_error_categories_are_specific(self) -> None:
        self.assertEqual(classify_provider_error(APIError(400, "prompt too long"), phase="create", task_known=False), "prompt_too_long")
        self.assertEqual(classify_provider_error(APIError(400, "size and aspect ratio conflict"), phase="create", task_known=False), "size_conflict")
        self.assertEqual(classify_provider_error(APIError(422, "unsupported reference image"), phase="create", task_known=False), "reference_error")

    def test_failed_payload_with_result_url_is_not_completed(self) -> None:
        payload = {
            "status": "failed",
            "progress": 100,
            "result": {"url": "http://provider.invalid/failed-task"},
            "error": {"message": "upstream rejected the source image"},
        }
        self.assertFalse(is_completed(payload))
        self.assertEqual(task_error(payload), "upstream rejected the source image")


class FakeProviderHandler(BaseHTTPRequestHandler):
    image_creates = 0
    video_creates = 0
    last_video_body = b""
    json_video_creates = 0
    last_json_video: dict[str, object] = {}
    image_requests: list[tuple[str, bytes]] = []
    image_authorization = ""
    json_video_authorization = ""
    multipart_video_authorization = ""
    video_payload = FAKE_MP4
    audio_payload = FAKE_MP4
    fail_video_create = False
    video_status = "completed"
    json_video_status = ""
    multipart_video_status = ""
    json_video_error = "provider capacity is full"
    multipart_video_error = "provider capacity is full"
    json_video_create_status = 0
    json_video_create_error = "provider rejected video create"
    task_lookup_404_remaining = 0
    task_content_404_remaining = 0
    tts_creates = 0
    voicebox_generations = 0
    voicebox_profiles: list[dict[str, object]] = []
    voicebox_model_downloaded = True
    voicebox_cache_dir = ""
    model_ids = ["gpt-image-2", "grok-imagine-video-1.5"]

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, value: dict[str, object], status: int = 200) -> None:
        self.send_bytes(json.dumps(value).encode("utf-8"), "application/json", status)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json({"ok": True, "service": "fake-local-media"})
            return
        if self.path == "/v1/models":
            self.send_json({"data": [{"id": model_id} for model_id in type(self).model_ids]})
            return
        if self.path == "/models/status":
            self.send_json(
                {
                    "models": [
                        {
                            "model_name": "qwen-custom-voice-0.6B",
                            "engine": "qwen_custom_voice",
                            "model_size": "0.6B",
                            "hf_repo_id": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                            "downloaded": type(self).voicebox_model_downloaded,
                            "downloading": False,
                        }
                    ]
                }
            )
            return
        if self.path == "/models/cache-dir":
            self.send_json({"path": type(self).voicebox_cache_dir})
            return
        if self.path == "/profiles":
            self.send_bytes(json.dumps(type(self).voicebox_profiles).encode("utf-8"), "application/json")
            return
        if self.path == "/profiles/presets/qwen_custom_voice":
            self.send_json(
                {
                    "engine": "qwen_custom_voice",
                    "voices": [
                        {"voice_id": "Dylan", "name": "Dylan", "gender": "male", "language": "zh"},
                        {"voice_id": "Vivian", "name": "Vivian", "gender": "female", "language": "zh"},
                    ],
                }
            )
            return
        if self.path == "/generate/voicebox-gen-1/status":
            self.send_bytes(
                b'data: {"id":"voicebox-gen-1","status":"generating"}\n\n'
                b'data: {"id":"voicebox-gen-1","status":"completed","duration":0.5}\n\n',
                "text/event-stream",
            )
            return
        if self.path == "/audio/voicebox-gen-1":
            self.send_bytes(FAKE_WAV, "audio/wav")
            return
        if self.path == "/v1/videos/task-1":
            if type(self).task_lookup_404_remaining > 0:
                type(self).task_lookup_404_remaining -= 1
                self.send_json({"error": {"message": "Video request not found"}}, 404)
                return
            authorization = self.headers.get("Authorization", "")
            is_json_video = bool(type(self).json_video_authorization) and authorization == type(self).json_video_authorization
            status = (
                type(self).json_video_status if is_json_video else type(self).multipart_video_status
            ) or type(self).video_status
            payload: dict[str, object] = {"id": "task-1", "status": status}
            if status == "failed":
                payload["error"] = {
                    "message": type(self).json_video_error if is_json_video else type(self).multipart_video_error
                }
            self.send_json(payload)
            return
        if self.path == "/v1/videos/task-1/content":
            if type(self).task_content_404_remaining > 0:
                type(self).task_content_404_remaining -= 1
                self.send_json({"error": {"message": "Video request not found"}}, 404)
                return
            self.send_bytes(type(self).audio_payload, "video/mp4")
            return
        self.send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path in {"/inference_sft", "/inference_zero_shot", "/inference_instruct2"}:
            type(self).tts_creates += 1
            self.send_bytes(FAKE_PCM, "application/octet-stream")
            return
        if self.path == "/profiles":
            try:
                value = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                value = {}
            profile = {
                **value,
                "id": "voicebox-profile-1",
                "created_at": "2026-08-29T00:00:00Z",
                "updated_at": "2026-08-29T00:00:00Z",
            }
            type(self).voicebox_profiles.append(profile)
            self.send_json(profile)
            return
        if self.path == "/generate":
            type(self).voicebox_generations += 1
            self.send_json(
                {
                    "id": "voicebox-gen-1",
                    "profile_id": "voicebox-profile-1",
                    "text": "audition",
                    "language": "zh",
                    "status": "generating",
                    "created_at": "2026-08-29T00:00:00Z",
                }
            )
            return
        if self.path == "/v1/images/generations" or self.path == "/v1/images/edits":
            type(self).image_creates += 1
            type(self).image_requests.append((self.path, body))
            type(self).image_authorization = self.headers.get("Authorization", "")
            self.send_json({"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode("ascii")}]})
            return
        if self.path == "/v1/videos":
            type(self).video_creates += 1
            type(self).last_video_body = body
            type(self).multipart_video_authorization = self.headers.get("Authorization", "")
            if type(self).fail_video_create:
                self.send_json({"error": {"message": "temporary upstream failure"}}, 502)
                return
            self.send_json({"data": {"id": "task-1", "status": "queued"}})
            return
        if self.path == "/v1/videos/generations":
            type(self).json_video_creates += 1
            type(self).json_video_authorization = self.headers.get("Authorization", "")
            try:
                type(self).last_json_video = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                type(self).last_json_video = {}
            if type(self).fail_video_create:
                self.send_json({"error": {"message": "temporary upstream failure"}}, 502)
                return
            if type(self).json_video_create_status:
                self.send_json({"error": {"message": type(self).json_video_create_error}}, type(self).json_video_create_status)
                return
            self.send_json({"request_id": "task-1"})
            return
        self.send_json({"error": {"message": "not found"}}, 404)


class SkillIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.media_temp = tempfile.TemporaryDirectory()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            media_path = Path(cls.media_temp.name) / "valid.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x240:r=30",
                    "-t",
                    "1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(media_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            FakeProviderHandler.video_payload = media_path.read_bytes()
            audio_path = Path(cls.media_temp.name) / "valid-audio.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x240:r=30",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-t",
                    "1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            FakeProviderHandler.audio_payload = audio_path.read_bytes()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.media_temp.cleanup()

    def setUp(self) -> None:
        FakeProviderHandler.image_creates = 0
        FakeProviderHandler.video_creates = 0
        FakeProviderHandler.last_video_body = b""
        FakeProviderHandler.json_video_creates = 0
        FakeProviderHandler.last_json_video = {}
        FakeProviderHandler.image_requests = []
        FakeProviderHandler.image_authorization = ""
        FakeProviderHandler.json_video_authorization = ""
        FakeProviderHandler.multipart_video_authorization = ""
        FakeProviderHandler.fail_video_create = False
        FakeProviderHandler.video_status = "completed"
        FakeProviderHandler.json_video_status = ""
        FakeProviderHandler.multipart_video_status = ""
        FakeProviderHandler.json_video_error = "provider capacity is full"
        FakeProviderHandler.multipart_video_error = "provider capacity is full"
        FakeProviderHandler.json_video_create_status = 0
        FakeProviderHandler.json_video_create_error = "provider rejected video create"
        FakeProviderHandler.task_lookup_404_remaining = 0
        FakeProviderHandler.task_content_404_remaining = 0
        FakeProviderHandler.tts_creates = 0
        FakeProviderHandler.voicebox_generations = 0
        FakeProviderHandler.voicebox_profiles = []
        FakeProviderHandler.voicebox_model_downloaded = True
        FakeProviderHandler.model_ids = ["gpt-image-2", "grok-imagine-video-1.5"]
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        voicebox_ref = (
            self.root
            / "voicebox-cache"
            / "models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice"
            / "refs"
            / "main"
        )
        voicebox_ref.parent.mkdir(parents=True)
        voicebox_ref.write_text("85e237c12c027371202489a0ec509ded67b5e4b5", encoding="utf-8")
        FakeProviderHandler.voicebox_cache_dir = str(self.root / "voicebox-cache")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "quickai_base_url": self.base_url,
                    "quickainew_base_url": self.base_url,
                    "image_model": "gpt-image-2",
                    "video_model": "grok-imagine-video-1.5",
                    "secret_provider": "environment",
                }
            ),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update({"GVS_CONFIG_DIR": str(self.config_dir), "GVS_QUICKAI_KEY": "test-image-key", "GVS_QUICKAINEW_KEY": "test-video-key", "PYTHONUTF8": "1"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str, expected: int = 0) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        output = result.stdout if result.stdout.strip() else result.stderr
        return json.loads(output)

    def create_project(self, name: str = "project", *, generate_image: bool = True, references: list[str] | None = None) -> Path:
        project = self.root / name
        self.run_cli("init", str(project), "--title", "Test", "--topic", "Motion", "--shots", "1", "--video-size", "720x1280", "--seconds", "6")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "A short portrait shot."
        shot = value["shots"][0]
        shot.update(
            {
                "summary": "The subject smiles.",
                "generate_image": generate_image,
                "image_prompt": "A consistent portrait frame." if generate_image else "",
                "video_prompt": "The subject smiles and blinks once; keep identity and clothing unchanged.",
                "video_references": references or [],
            }
        )
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        return project

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_doctor_and_complete_run_are_resumable(self) -> None:
        doctor = self.run_cli("doctor")
        self.assertTrue(doctor["ok"])
        project = self.create_project()
        self.assertTrue(self.run_cli("validate", str(project))["ok"])
        result = self.run_cli("run", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        self.assertTrue(result["media"]["has_audio"])
        self.assertEqual(result["media"]["audio_policy"], "preserve")
        final = project / "deliverables" / "final.mp4"
        self.assertGreater(final.stat().st_size, 1000)
        self.assertEqual(FakeProviderHandler.image_creates, 1)
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertEqual(FakeProviderHandler.last_json_video["duration"], 6)
        self.assertNotIn("seconds", FakeProviderHandler.last_json_video)
        self.assertNotIn("size", FakeProviderHandler.last_json_video)
        self.assertNotIn("generate_audio", FakeProviderHandler.last_json_video)
        self.assertTrue(str(FakeProviderHandler.last_json_video["image"]["url"]).startswith("data:image/png;base64,"))

        second = self.run_cli("run", str(project), "--poll-timeout", "5")
        self.assertTrue(second["ok"])
        self.assertEqual(FakeProviderHandler.image_creates, 1)
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        state_path = project / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["shots"]["shot-001"]["video"]["status"] = "queued"
        state["shots"]["shot-001"]["video"]["path"] = ""
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        (project / "clips" / "shot-001.mp4").unlink()
        resumed = self.run_cli("resume", str(project), "--poll-timeout", "5")
        self.assertTrue(resumed["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertTrue((project / "clips" / "shot-001.mp4").is_file())

    def test_doctor_accepts_known_billing_suffix_without_fuzzy_model_matching(self) -> None:
        FakeProviderHandler.model_ids = ["gpt-image-2", "grok-imagine-video-1.5（按次）"]
        doctor = self.run_cli("doctor")
        self.assertTrue(doctor["ok"])
        self.assertEqual(doctor["providers"]["quickainew_video"]["matched_model"], "grok-imagine-video-1.5（按次）")

        FakeProviderHandler.model_ids = ["gpt-image-2", "prefix-grok-imagine-video-1.5-other"]
        blocked = self.run_cli("doctor", expected=1)
        self.assertFalse(blocked["providers"]["quickainew_video"]["model_present"])

    def test_multiple_video_references_use_quickai_reference_array(self) -> None:
        project = self.root / "multi"
        refs = []
        for index in (1, 2):
            relative = f"assets/references/ref-{index}.png"
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(FAKE_PNG)
            refs.append(relative)
        project = self.create_project("multi", generate_image=False, references=refs)
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        references = FakeProviderHandler.last_json_video["reference_images"]
        self.assertEqual(len(references), 2)
        self.assertTrue(all(str(item["url"]).startswith("data:image/png;base64,") for item in references))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for QuickAI I2V image normalization")
    def test_quickai_valid_png_reference_is_normalized_to_jpeg(self) -> None:
        project = self.root / "valid-reference"
        project = self.create_project("valid-reference", generate_image=False, references=["assets/references/reference.png"])
        reference = project / "assets" / "references" / "reference.png"
        reference.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["video_references"] = ["assets/references/reference.png"]
        value["shots"][0]["video_prompt"] = "Animate the supplied image with one gentle movement."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")

        report = image_reference_report(reference, "16:9")
        self.assertEqual(report["format"], "png")
        self.assertEqual((report["width"], report["height"]), (1, 1))
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        self.assertTrue(str(FakeProviderHandler.last_json_video["image"]["url"]).startswith("data:image/jpeg;base64,"))

    def test_selected_shot_generation_leaves_other_shots_pending(self) -> None:
        project = self.root / "selected"
        self.run_cli("init", str(project), "--title", "Selected", "--topic", "Shots", "--shots", "2", "--seconds", "6")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "Two independent shots."
        for index, shot in enumerate(value["shots"], 1):
            shot["image_prompt"] = f"Keyframe {index}."
            shot["video_prompt"] = f"Motion {index}."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("generate-images", str(project), "--shot", "shot-002")
        self.assertEqual(result["images"]["completed"], ["shot-002"])
        self.assertEqual(FakeProviderHandler.image_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("shot-001", state["shots"])
        self.assertEqual(state["shots"]["shot-002"]["image"]["status"], "completed")

    def test_parallel_reviews_preserve_both_state_updates(self) -> None:
        project = self.root / "parallel-review"
        self.run_cli("init", str(project), "--title", "Review", "--topic", "Two shots", "--shots", "2", "--seconds", "6")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "Two keyframes are reviewed independently."
        for index, shot in enumerate(value["shots"], 1):
            shot["image_prompt"] = f"Keyframe {index}."
            shot["video_prompt"] = f"Motion {index}."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("generate-images", str(project))

        original_save_state = gvs.save_state
        start = threading.Barrier(3)
        errors: list[Exception] = []

        def slow_save(root: Path, state: dict[str, object]) -> None:
            time.sleep(0.1)
            original_save_state(root, state)

        def approve(shot_id: str) -> None:
            try:
                start.wait()
                review_shot_asset(project, shot_id, kind="image", decision="approve", notes="parallel review")
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch("grok_video_studio.save_state", side_effect=slow_save):
            threads = [threading.Thread(target=approve, args=(shot_id,)) for shot_id in ("shot-001", "shot-002")]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["image"]["review_status"], "approved")
        self.assertEqual(state["shots"]["shot-002"]["image"]["review_status"], "approved")

    def test_validation_rejects_credentials_and_missing_prompts(self) -> None:
        project = self.create_project("invalid")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["api_key"] = "must-not-be-here"
        value["shots"][0]["video_prompt"] = ""
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("validate", str(project), expected=1)
        self.assertFalse(result["ok"])
        self.assertTrue(any("credential" in error for error in result["errors"]))

    def test_clean_frame_policy_requires_explicit_ui_override(self) -> None:
        project = self.create_project("clean-frame", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        shot = value["shots"][0]
        self.assertIn("[CLEAN FRAME POLICY]", composed_video_prompt(value, shot))
        self.assertIn("One camera view fills the entire frame", composed_video_prompt(value, shot))

        value["allow_ui_elements"] = True
        self.assertNotIn("[CLEAN FRAME POLICY]", composed_video_prompt(value, shot))
        self.assertIn("One camera view fills the entire frame", composed_video_prompt(value, shot))

        shot["allow_ui_elements"] = False
        self.assertIn("[CLEAN FRAME POLICY]", composed_video_prompt(value, shot))

    def test_multi_panel_layout_requires_explicit_shot_authorization(self) -> None:
        project = self.create_project("frame-layout", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        shot = value["shots"][0]
        self.assertEqual(value["frame_layout"], "single-full-frame")
        self.assertFalse(value["allow_multi_panel"])

        shot["frame_layout"] = "triptych"
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        blocked = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("allow_multi_panel" in error for error in blocked["errors"]))

        shot["allow_multi_panel"] = True
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        self.assertTrue(self.run_cli("validate", str(project))["ok"])
        prompt = composed_video_prompt(value, shot)
        self.assertIn("explicitly requested three-panel triptych", prompt)
        self.assertNotIn("One camera view fills the entire frame", prompt)

    def test_genre_coverage_planning_is_not_sent_to_one_video_request(self) -> None:
        project = self.create_project("single-shot-genre", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["director"]["genre_packs"] = ["comedy"]
        prompt = composed_video_prompt(value, value["shots"][0])
        self.assertNotIn("cut to inserts and reactions", prompt)
        self.assertIn("One camera view fills the entire frame", prompt)

    def test_vertical_multi_character_single_frame_uses_compact_prompt(self) -> None:
        project = self.create_project("vertical-two-people", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["characters"] = [
            {"id": "left", "name": "Left", "identity": "Original adult in a navy coat."},
            {"id": "right", "name": "Right", "identity": "Original adult in a gray coat."},
        ]
        value["video_mode"] = "text-to-video"
        value["shots"][0]["character_ids"] = ["left", "right"]
        value["shots"][0]["video_aspect_ratio"] = "9:16"
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        blocked = self.run_cli("preflight", str(project), expected=1)
        self.assertTrue(any("high-risk vertical multi-character T2V" in error for error in blocked["errors"]))
        self.assertEqual(value["layout_risk_policy"], "block")
        self.assertEqual(blocked["preflight"]["layout_risks"][0]["policy"], "block")

        value["shots"][0]["layout_risk_policy"] = "allow"
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        preflight = self.run_cli("preflight", str(project))["preflight"]
        video = next(item for item in preflight["prompts"] if item["kind"] == "video")
        self.assertEqual(video["selected_version"], "compact")
        self.assertEqual(preflight["layout_risks"][0]["recommended_route"], "image-to-video with one approved single-full-frame keyframe")
        self.assertEqual(preflight["layout_risks"][0]["policy"], "allow")

        value["shots"][0]["prompt_version"] = "minimal"
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        preflight = self.run_cli("preflight", str(project))["preflight"]
        video = next(item for item in preflight["prompts"] if item["kind"] == "video")
        self.assertEqual(video["selected_version"], "minimal")

    def test_prompt_hard_limit_is_enforced_before_safe_limit(self) -> None:
        variants = {"full": "x" * 1200, "compact": "x" * 900, "minimal": "x" * 500}
        version, prompt = gvs.select_prompt_variant(variants, hard_limit=1000)
        self.assertEqual(version, "compact")
        self.assertEqual(len(prompt), 900)

    def test_multishot_text_to_video_identity_lock_is_audited(self) -> None:
        project = self.root / "t2v-identity"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Identity",
            "--topic",
            "Prompt-only continuity",
            "--workflow",
            "text-to-video",
            "--shots",
            "2",
            "--seconds",
            "1",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "The same lead appears in two adjacent shots."
        value["character_bible"] = "One adult man with short black hair, a gray shirt, and a brown canvas bag."
        for shot in value["shots"]:
            shot["video_prompt"] = "He takes one calm step forward."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")

        self.assertIn("Same character every shot", composed_video_prompt(value, value["shots"][0]))
        preflight = self.run_cli("preflight", str(project))
        self.assertTrue(any("identity continuity is prompt-only" in warning for warning in preflight["preflight"]["warnings"]))

    def test_structured_video_prompt_omits_offscreen_character_bible(self) -> None:
        project = self.create_project("structured-identity", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["character_bible"] = "Lead identity. Offscreen identity that must not be sent."
        value["characters"] = [
            {"id": "lead", "name": "Lead", "identity": "Lead identity.", "wardrobe": "Yellow raincoat."},
            {"id": "offscreen", "name": "Offscreen", "identity": "Offscreen identity.", "wardrobe": "Blue coat."},
        ]
        value["shots"][0]["character_ids"] = ["lead"]

        prompt = composed_video_prompt(value, value["shots"][0])

        self.assertIn("Lead identity.", prompt)
        self.assertNotIn("Offscreen identity that must not be sent.", prompt)
        self.assertNotIn("Blue coat.", prompt)

    def test_capabilities_and_dynamic_duration_planning(self) -> None:
        capabilities = self.run_cli("capabilities")
        ids = {item["id"] for item in capabilities["workflows"]}
        routes = {item["id"] for item in capabilities["product_routes"]}
        self.assertEqual(
            routes,
            {
                "text-to-video",
                "image-to-video",
                "cinematic-short",
                "dialogue-scene",
                "silent-cinema",
                "action-scene",
                "episodic-series",
                "news-video",
            },
        )
        self.assertIn("short-drama", ids)
        self.assertIn("single-image-animation", ids)
        project = self.root / "dynamic"
        created = self.run_cli(
            "init",
            str(project),
            "--title",
            "Dynamic",
            "--topic",
            "Flexible shots",
            "--workflow",
            "general-video",
            "--target-seconds",
            "37",
        )
        self.assertEqual(sum(created["shot_seconds"]), 37)
        self.assertNotEqual(len(created["shot_seconds"]), 8)

    def test_custom_workflow_can_extend_a_builtin_without_overriding_it(self) -> None:
        candidate = self.root / "my-history-workflow.json"
        candidate.write_text(
            json.dumps(
                {
                    "id": "my-history-short",
                    "extends": "cinematic-short",
                    "title": "My historical short",
                    "genre_packs": ["historical"],
                    "guidance": {"story": "A custom visible-event planning rule."},
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli("workflow-validate", str(candidate))
        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved"]["director_mode"], "cinematic-short")
        self.assertEqual(result["resolved"]["genre_packs"], ["historical"])

    def test_native_audio_prompt_covers_ambience_and_explicit_upstream_captions(self) -> None:
        project = self.create_project("native-audio-contract", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        shot = value["shots"][0]
        prompt = composed_video_prompt(value, shot)
        self.assertIn("background music", prompt)
        self.assertIn("no spoken words", prompt)
        self.assertIn("speech remains audible only", prompt)

        shot["audio_intent"] = "narration"
        shot["narration"] = "这是只在声音里出现的旁白。"
        prompt = composed_video_prompt(value, shot)
        self.assertIn("Audio-channel-only voice script", prompt)
        self.assertIn("every written word exclusively in the soundtrack", prompt)
        self.assertIn("这是只在声音里出现的旁白", prompt)

        value["characters"] = [{"id": "lead", "name": "Lead", "identity": "Original character."}]
        shot["character_ids"] = ["lead"]
        shot["dialogue"] = [{"id": "line-001", "speaker": "lead", "text": "为什么要这样？", "start": 0.2, "end": 2.0}]
        shot["audio_intent"] = "dialogue"
        value["audio"]["subtitle_source"] = "upstream"
        prompt = composed_video_prompt(value, shot)
        self.assertIn("Render synchronized, readable upstream captions", prompt)
        self.assertNotIn("No app UI, controls, overlays, text", prompt)
        self.assertNotIn("says exactly", prompt)

    def test_workflow_route_contract_is_enforced_by_init_and_validate(self) -> None:
        failed = self.run_cli(
            "init",
            str(self.root / "bad-performance-route"),
            "--title",
            "Bad route",
            "--topic",
            "Wrong route",
            "--workflow",
            "dance-performance",
            "--mode",
            "text-to-video",
            expected=1,
        )
        self.assertFalse(failed["ok"])
        self.assertIn("does not support text-to-video", str(failed["error"]))

        project = self.create_project("edited-bad-route", generate_image=False)
        path = project / "project.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["workflow"] = "dance-performance"
        value["video_mode"] = "text-to-video"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        report = self.run_cli("validate", str(project), expected=1)
        self.assertFalse(report["ok"])
        self.assertTrue(any("does not support text-to-video" in item for item in report["errors"]))

    def test_strict_director_gate_blocks_unplanned_dialogue_only_generation(self) -> None:
        project = self.root / "director-gate"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Director gate",
            "--topic",
            "A rigid conversation",
            "--workflow",
            "dialogue-scene",
            "--mode",
            "text-to-video",
            "--shots",
            "3",
            "--seconds",
            "2",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "Two people argue without visual coverage."
        value["characters"] = [{"id": "lead", "name": "Lead", "identity": "Original character."}]
        for index, shot in enumerate(value["shots"], 1):
            shot["video_prompt"] = "The speaker faces camera and talks."
            shot["character_ids"] = ["lead"]
            shot["audio_intent"] = "dialogue"
            shot["dialogue"] = [
                {"id": f"line-{index:03d}", "speaker": "lead", "text": "A short line.", "start": 0.1, "end": 1.5}
            ]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("story_beats" in error for error in result["errors"]))
        self.assertTrue(any("100% dialogue" in error for error in result["errors"]))

    def test_i2v_init_aligns_keyframe_orientation_and_warns_for_legacy_square_size(self) -> None:
        landscape = self.root / "landscape-i2v"
        self.run_cli(
            "init",
            str(landscape),
            "--title",
            "Landscape",
            "--topic",
            "Aspect alignment",
            "--shots",
            "1",
            "--seconds",
            "1",
            "--mode",
            "image-to-video",
            "--aspect-ratio",
            "16:9",
        )
        value = json.loads((landscape / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(value["defaults"]["image_size"], "1536x1024")
        self.assertEqual(value["character_master"]["image_size"], "1024x1024")
        value["story"] = "A landscape image-to-video test."
        value["shots"][0]["image_prompt"] = "One landscape keyframe."
        value["shots"][0]["video_prompt"] = "One continuous movement."
        value["defaults"]["image_size"] = "1024x1024"
        (landscape / "project.json").write_text(json.dumps(value), encoding="utf-8")
        report = self.run_cli("preflight", str(landscape))
        self.assertTrue(any("orientation does not match" in warning for warning in report["preflight"]["warnings"]))

        value["defaults"]["video_size"] = "1280x720"
        value["shots"][0]["video_aspect_ratio"] = "9:16"
        (landscape / "project.json").write_text(json.dumps(value), encoding="utf-8")
        mismatch = self.run_cli("preflight", str(landscape))
        self.assertTrue(any("video_size 1280x720 orientation" in warning for warning in mismatch["preflight"]["warnings"]))

        portrait = self.root / "portrait-i2v"
        self.run_cli(
            "init",
            str(portrait),
            "--title",
            "Portrait",
            "--topic",
            "Aspect alignment",
            "--shots",
            "1",
            "--seconds",
            "1",
            "--mode",
            "image-to-video",
            "--aspect-ratio",
            "9:16",
        )
        portrait_value = json.loads((portrait / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(portrait_value["defaults"]["image_size"], "1024x1536")

    def test_cli_forces_utf8_json_even_when_parent_requests_legacy_encoding(self) -> None:
        env = self.env.copy()
        env.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp936"})
        result = subprocess.run(
            [sys.executable, str(CLI), "capabilities"],
            env=env,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        decoded = result.stdout.decode("utf-8")
        self.assertIn("文生视频", decoded)
        self.assertTrue(json.loads(decoded)["ok"])

    def test_news_video_requires_verified_sourced_claims(self) -> None:
        project = self.root / "news"
        created = self.run_cli(
            "news-init",
            str(project),
            "--title",
            "Daily Brief",
            "--topic",
            "A verified product announcement",
            "--target-seconds",
            "2",
            "--shots",
            "2",
            "--clip-seconds",
            "1",
        )
        self.assertEqual(created["research_status"], "researching")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "A concise sourced explanation of the announcement."
        for index, shot in enumerate(value["shots"], 1):
            shot["video_prompt"] = f"A neutral illustrative news visual for verified point {index}."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        news = json.loads((project / "news.json").read_text(encoding="utf-8"))
        news.update(
            {
                "as_of": "2026-08-28T12:00:00+08:00",
                "sources": [
                    {
                        "id": "official",
                        "title": "Official announcement",
                        "publisher": "Example Organization",
                        "url": "https://example.org/announcement",
                        "published_at": "2026-08-28T08:00:00+08:00",
                        "accessed_at": "2026-08-28T12:00:00+08:00",
                        "source_type": "primary",
                        "visual_rights": "facts-only",
                    },
                    {
                        "id": "report",
                        "title": "Independent report",
                        "publisher": "Example Newsroom",
                        "url": "https://news.example.com/report",
                        "published_at": "2026-08-28T09:00:00+08:00",
                        "accessed_at": "2026-08-28T12:00:00+08:00",
                        "source_type": "secondary",
                        "visual_rights": "facts-only",
                    },
                ],
                "claims": [
                    {"id": "announcement-fact", "text": "The organization published an announcement.", "source_ids": ["official"]}
                ],
                "script_segments": [
                    {"shot_id": "shot-001", "narration": "The organization issued a new announcement.", "claim_ids": ["announcement-fact"]},
                    {"shot_id": "shot-002", "narration": "Independent reporting supplied additional context.", "claim_ids": ["announcement-fact"]},
                ],
            }
        )
        news["selection"].update(
            {
                "rationale": "Selected because it was recent and independently reported.",
                "search_queries": ["verified product announcement"],
            }
        )
        news["editorial"].update(
            {"status": "verified", "fact_checked_at": "2026-08-28T12:05:00+08:00", "unresolved_conflicts": []}
        )
        (project / "news.json").write_text(json.dumps(news), encoding="utf-8")
        validated = self.run_cli("news-validate", str(project))
        self.assertTrue(validated["ok"])

        news["sources"] = news["sources"][:1]
        (project / "news.json").write_text(json.dumps(news), encoding="utf-8")
        rejected = self.run_cli("news-validate", str(project), expected=1)
        self.assertTrue(any("at least two sources" in error for error in rejected["errors"]))
        self.assertTrue(all(1 <= value <= 15 for value in created["shot_seconds"]))

    def test_validation_enforces_video_and_prompt_limits(self) -> None:
        project = self.create_project("limits")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["seconds"] = 16
        value["character_bible"] = "中" * 1400
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("1 to 15" in error for error in result["errors"]))
        self.assertTrue(any("UTF-8 bytes" in error for error in result["errors"]))

        value["shots"][0]["seconds"] = 15
        value["character_bible"] = "x" * 3500
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("preflight", str(project))
        self.assertTrue(result["ok"])
        self.assertTrue(any("headroom" in warning for warning in result["preflight"]["warnings"]))
        video_prompt = next(item for item in result["preflight"]["prompts"] if item["kind"] == "video")
        self.assertEqual(video_prompt["hard_limit"], 4096)
        self.assertEqual(video_prompt["safe_limit"], 3800)
        self.assertLess(video_prompt["remaining"], 4096)
        self.assertTrue(video_prompt["within_hard_limit"])

    def test_v2_prompt_budget_reports_utf8_variants_and_reference_blockers(self) -> None:
        project = self.create_project("v2-contract", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["character_bible"] = "角色身份 " + ("中" * 1200)
        shot = value["shots"][0]
        shot["character_ids"] = []
        shot["video_prompt"] = "角色向前走一步并停下。"
        image_ref = project / "assets" / "references" / "reference.mp4"
        audio_ref = project / "assets" / "references" / "reference.wav"
        image_ref.parent.mkdir(parents=True, exist_ok=True)
        image_ref.write_bytes(FAKE_MP4)
        audio_ref.write_bytes(FAKE_WAV)
        shot["video_references"] = ["assets/references/reference.mp4", "assets/references/reference.wav"]
        (project / "project.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

        report = self.run_cli("preflight", str(project), expected=1)
        prompt = next(item for item in report["preflight"]["prompts"] if item["kind"] == "video")
        self.assertGreater(prompt["versions"]["full"]["utf8_bytes"], prompt["versions"]["full"]["characters"] - 1)
        self.assertIn("compression_suggestion", prompt)
        self.assertTrue(any("video reference" in error for error in report["errors"]))
        self.assertTrue(any("audio reference" in error for error in report["errors"]))

    def test_prompt_error_stops_at_three_total_attempts(self) -> None:
        project = self.create_project("prompt-retry-limit", generate_image=False)
        FakeProviderHandler.json_video_create_status = 400
        FakeProviderHandler.json_video_create_error = "prompt too long"
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(result["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 3)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["attempts"], 3)
        self.assertEqual(video["error_category"], "prompt_too_long")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_assembly_audio_policy_preserves_or_mutes_audio(self) -> None:
        with_audio = self.root / "with-audio.mp4"
        without_audio = self.root / "without-audio.mp4"
        with_audio.write_bytes(FakeProviderHandler.audio_payload)
        without_audio.write_bytes(FakeProviderHandler.video_payload)

        preserved = self.run_cli(
            "assemble-files",
            str(self.root / "preserved.mp4"),
            str(with_audio),
            str(without_audio),
            "--target-size",
            "320x240",
            "--audio-policy",
            "preserve",
        )
        self.assertTrue(preserved["media"]["has_audio"])
        self.assertEqual(preserved["media"]["audio_policy"], "preserve")

        muted = self.run_cli(
            "assemble-files",
            str(self.root / "muted.mp4"),
            str(with_audio),
            str(without_audio),
            "--target-size",
            "320x240",
            "--audio-policy",
            "mute",
        )
        self.assertFalse(muted["media"]["has_audio"])
        self.assertEqual(muted["media"]["audio_policy"], "mute")

        trimmed_path = self.root / "trimmed.mp4"
        trimmed = gvs.assemble_clips(
            [with_audio],
            trimmed_path,
            target_size="320x240",
            audio_policy="preserve",
            edit_windows=[{"edit_in": 0.2, "edit_out": 0.7, "timeline_duration": 0.5}],
            require_audio=True,
        )
        self.assertTrue(trimmed["has_audio"])
        self.assertLess(trimmed["duration"], 0.9)
        self.assertAlmostEqual(trimmed["edit_windows"][0]["timeline_duration"], 0.5)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_qa_rejects_legacy_final_without_audio(self) -> None:
        project = self.create_project("legacy-final", generate_image=False)
        self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        final = project / "deliverables" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(FakeProviderHandler.video_payload)
        state_path = project / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["deliverables"] = {"final": {"path": "deliverables/final.mp4"}}
        state_path.write_text(json.dumps(state), encoding="utf-8")

        qa = self.run_cli("qa", str(project))
        final_report = next(report for report in qa["reports"] if report["kind"] == "deliverable")
        self.assertFalse(final_report["ok"])
        self.assertTrue(any("no audio track" in error for error in final_report["errors"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_strict_quality_report_blocks_detected_black_segments(self) -> None:
        path = self.root / "black-segment.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.8",
                "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,0.8)'",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        warning_report = media_tools.quality_report(path)
        self.assertTrue(warning_report["ok"])
        strict_report = media_tools.quality_report(path, black_is_error=True)
        self.assertFalse(strict_report["ok"])
        self.assertTrue(any("black segment" in error for error in strict_report["errors"]))
        selected_report = media_tools.quality_report(path, black_is_error=True, scan_start=0.85, scan_end=1.8)
        self.assertTrue(selected_report["ok"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_single_full_frame_qa_blocks_repeated_horizontal_panels(self) -> None:
        path = self.root / "repeated-panels.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "testsrc2=s=320x80:r=24",
                "-filter_complex", "[0:v]split=3[a][b][c];[a][b][c]vstack=inputs=3[v]",
                "-map", "[v]", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        report = media_tools.quality_report(
            path,
            expected_frame_layout="single-full-frame",
            layout_is_error=True,
        )
        self.assertFalse(report["ok"])
        layout = report["signals"]["repeated_panel_layout"]
        self.assertTrue(layout["detected"])
        self.assertEqual(layout["panel_count"], 3)
        self.assertTrue(any("repeated 3-panel" in error for error in report["errors"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_single_sheet_character_master_feeds_keyframe_not_video(self) -> None:
        project = self.root / "character"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Character",
            "--topic",
            "Identity",
            "--workflow",
            "character-consistent-story",
            "--shots",
            "1",
            "--seconds",
            "6",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "The same character gives a small wave."
        value["character_bible"] = "One adult woman with a round face, black bob haircut, green jacket, and black trousers."
        value["style_bible"] = "Natural cinematic realism and stable colors."
        value["character_master"]["prompt"] = (
            "Create one single character turnaround sheet in one image. Show front, side, and back full-body views "
            "of the same person on a plain gray background. No other people and no text."
        )
        value["shots"][0]["summary"] = "She waves once."
        value["shots"][0]["image_prompt"] = "Medium shot in a quiet office, facing the camera with one hand raised."
        value["shots"][0]["video_prompt"] = "She gives one small natural wave and blinks once. Keep identity, clothing, and background unchanged."
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")

        preflight = self.run_cli("preflight", str(project))
        self.assertEqual(preflight["preflight"]["requests"]["total_images"], 2)
        result = self.run_cli("run", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        self.assertEqual(FakeProviderHandler.image_creates, 2)
        self.assertEqual(FakeProviderHandler.image_requests[0][0], "/v1/images/generations")
        self.assertEqual(FakeProviderHandler.image_requests[1][0], "/v1/images/edits")
        self.assertEqual(FakeProviderHandler.image_requests[1][1].count(b'name="image"'), 1)
        self.assertIn("image", FakeProviderHandler.last_json_video)
        self.assertNotIn("reference_images", FakeProviderHandler.last_json_video)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_assemble_files_accepts_arbitrary_clip_count(self) -> None:
        clips = []
        for index in range(3):
            path = self.root / f"input-{index}.mp4"
            path.write_bytes(FakeProviderHandler.video_payload)
            clips.append(path)
        output = self.root / "combined.mp4"
        result = self.run_cli(
            "assemble-files",
            str(output),
            *(str(path) for path in clips),
            "--target-size",
            "640x360",
        )
        self.assertEqual(result["media"]["clip_count"], 3)
        self.assertEqual(result["media"]["width"], 640)
        self.assertEqual(result["media"]["height"], 360)
        self.assertGreater(result["media"]["duration"], 2.5)

    def test_ambiguous_create_failure_is_not_retried_implicitly(self) -> None:
        project = self.create_project("ambiguous", generate_image=False)
        FakeProviderHandler.fail_video_create = True
        first = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(first["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["video"]["status"], "submission_unknown")

        second = self.run_cli("resume", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(second["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        FakeProviderHandler.fail_video_create = False
        missing_reason = self.run_cli("resume", str(project), "--poll-timeout", "5", "--retry-failed", expected=1)
        self.assertIn("--retry-reason", missing_reason["error"])
        retried = self.run_cli(
            "resume",
            str(project),
            "--poll-timeout",
            "5",
            "--retry-failed",
            "--retry-reason",
            "provider dashboard confirmed no usable task",
        )
        self.assertTrue(retried["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 2)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        history = state["shots"]["shot-001"]["video"]["history"]
        self.assertEqual(history[-1]["reason"], "provider dashboard confirmed no usable task")

    def test_known_task_lookup_404_resumes_without_a_second_create(self) -> None:
        project = self.create_project("known-task-lookup", generate_image=False)
        FakeProviderHandler.task_lookup_404_remaining = 1
        generated = self.run_cli("generate-videos", str(project), "--poll-timeout", "6")
        self.assertTrue(generated["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        state_path = project / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        video.update(
            {
                "status": "failed",
                "error_category": "capability_unsupported",
                "error": "HTTP 404: Video request not found (request_id=trace-only)",
                "path": "",
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (project / "clips" / "shot-001.mp4").unlink()

        resumed = self.run_cli("resume", str(project), "--poll-timeout", "6")
        self.assertTrue(resumed["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["video"]["status"], "completed")

    def test_explicit_lost_task_replacement_requires_two_404_checks(self) -> None:
        project = self.create_project("lost-task-replacement", generate_image=False)
        FakeProviderHandler.task_lookup_404_remaining = 20
        first = self.run_cli("generate-videos", str(project), "--poll-timeout", "1", expected=1)
        self.assertIn("timed out", first["error"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        blocked = self.run_cli(
            "resume",
            str(project),
            "--shot",
            "shot-001",
            "--replace-lost-task",
            "--retry-reason",
            "status and content are gone",
            expected=1,
        )
        self.assertIn("requires --retry-failed", blocked["error"])

        FakeProviderHandler.task_lookup_404_remaining = 0
        still_queryable = self.run_cli(
            "resume",
            str(project),
            "--shot",
            "shot-001",
            "--replace-lost-task",
            "--retry-failed",
            "--retry-reason",
            "operator suspects the task is gone",
            expected=1,
        )
        self.assertIn("still queryable", still_queryable["error"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        FakeProviderHandler.task_lookup_404_remaining = 1
        FakeProviderHandler.task_content_404_remaining = 1
        replaced = self.run_cli(
            "resume",
            str(project),
            "--shot",
            "shot-001",
            "--poll-timeout",
            "5",
            "--replace-lost-task",
            "--retry-failed",
            "--retry-reason",
            "status and content endpoints both confirmed 404",
        )
        self.assertTrue(replaced["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 2)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "completed")
        self.assertEqual(video["previous_task_id"], "task-1")
        self.assertIn("confirmed lost upstream task", video["history"][-1]["reason"])

    def test_lost_task_replacement_recovers_existing_content_without_create(self) -> None:
        project = self.create_project("lost-task-content-recovery", generate_image=False)
        FakeProviderHandler.task_lookup_404_remaining = 20
        self.run_cli("generate-videos", str(project), "--poll-timeout", "1", expected=1)
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        FakeProviderHandler.task_lookup_404_remaining = 1
        recovered = self.run_cli(
            "resume",
            str(project),
            "--shot",
            "shot-001",
            "--replace-lost-task",
            "--retry-failed",
            "--retry-reason",
            "status endpoint is gone after bounded recovery polling",
        )
        self.assertTrue(recovered["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "completed")
        self.assertTrue((project / video["path"]).is_file())

    def test_terminal_failed_task_requires_explicit_retry_authorization(self) -> None:
        self.env.update({"GVS_QUICKAINEW_KEY": "", "GVS_QUICKAINEW_VIDEO_KEY": ""})
        project = self.create_project("terminal-failure", generate_image=False)
        FakeProviderHandler.video_status = "failed"

        first = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(first["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "failed")
        self.assertEqual(video["task_id"], "task-1")

        blocked = self.run_cli("resume", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(blocked["ok"])
        self.assertIn("--retry-failed", blocked["error"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)

        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["video_prompt"] += " Use the corrected framing."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        FakeProviderHandler.video_status = "completed"
        retried = self.run_cli(
            "resume",
            str(project),
            "--poll-timeout",
            "5",
            "--retry-failed",
            "--retry-reason",
            "terminal provider failure confirmed",
        )
        self.assertTrue(retried["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 2)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "completed")
        self.assertEqual(video["previous_task_id"], "task-1")
        self.assertEqual(video["history"][-1]["reason"], "terminal provider failure confirmed")

    def test_quickai_terminal_failure_fails_over_to_quickainew(self) -> None:
        project = self.create_project("provider-failover", generate_image=False)
        FakeProviderHandler.json_video_status = "failed"
        FakeProviderHandler.multipart_video_status = "completed"
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertEqual(result["videos"]["final_providers"], {"shot-001": "quickainew"})
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertEqual(FakeProviderHandler.video_creates, 1)

        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        attempts = video["provider_attempts"]
        self.assertEqual([item["provider"] for item in attempts], ["quickai", "quickainew"])
        self.assertEqual(attempts[0]["error_category"], "provider_task_failed")
        self.assertEqual(attempts[1]["status"], "completed")
        self.assertEqual(len({item["request_id"] for item in attempts}), 1)
        self.assertEqual(len({item["attempt_id"] for item in attempts}), 2)
        self.assertEqual(video["final_provider"], "quickainew")
        self.assertEqual(state["budget_usage"]["video_attempts"], 2)

    def test_content_rejection_does_not_fail_over(self) -> None:
        project = self.create_project("content-rejection", generate_image=False)
        FakeProviderHandler.json_video_status = "failed"
        FakeProviderHandler.json_video_error = "content policy moderation rejected this prompt"
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(result["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertEqual(FakeProviderHandler.video_creates, 0)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["video"]["error_category"], "content_rejected")

    def test_create_rate_limit_can_fail_over_but_ambiguous_5xx_cannot(self) -> None:
        project = self.create_project("rate-limit-failover", generate_image=False)
        FakeProviderHandler.json_video_create_status = 429
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertEqual(result["videos"]["final_providers"], {"shot-001": "quickainew"})
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertEqual(FakeProviderHandler.video_creates, 1)

    def test_shot_asset_review_locks_images_and_requires_reason_for_rejected_video_retry(self) -> None:
        project = self.create_project("asset-review")
        self.run_cli("generate-images", str(project))
        approved = self.run_cli(
            "review-shot", str(project), "shot-001", "--kind", "image", "--decision", "approve",
            "--notes", "identity and composition accepted",
        )
        self.assertTrue(approved["review"]["locked"])

        self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        rejected = self.run_cli(
            "review-shot", str(project), "shot-001", "--kind", "video", "--decision", "reject",
            "--notes", "motion drift",
        )
        self.assertEqual(rejected["review"]["decision"], "reject")
        rejected_path = Path(rejected["review"]["path"])
        self.assertTrue(rejected_path.is_file())
        self.assertEqual(rejected_path.parent, project / "clips" / "rejected")
        self.assertFalse((project / "clips" / "shot-001.mp4").exists())
        blocked = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertIn("--retry-failed", blocked["error"])
        retried = self.run_cli(
            "generate-videos", str(project), "--poll-timeout", "5", "--retry-failed",
            "--retry-reason", "user rejected motion drift",
        )
        self.assertTrue(retried["ok"])
        self.assertTrue(rejected_path.is_file())
        self.assertTrue((project / "clips" / "shot-001.mp4").is_file())
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["image"]["review_status"], "approved")
        self.assertEqual(state["shots"]["shot-001"]["video"]["status"], "completed")
        approved_video = self.run_cli(
            "review-shot", str(project), "shot-001", "--kind", "video", "--decision", "approve",
            "--notes", "replacement motion accepted",
        )
        self.assertFalse(approved_video["review"]["locked"])
        self.assertEqual(approved_video["review"]["next"], "approved video review is recorded")
        qa = self.run_cli("qa", str(project))
        self.assertTrue(qa["visual_ok"])
        self.assertTrue(qa["manual_review_complete"])
        self.assertEqual(qa["review_summary"], {"approved": 1, "rejected": 0, "pending": 0})

    def test_rejected_video_is_separate_from_technical_qa(self) -> None:
        project = self.create_project("visual-rejection", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["defaults"]["video_size"] = "320x240"
        value["defaults"]["video_seconds"] = 1
        value["target_duration_seconds"] = 1
        value["shots"][0]["seconds"] = 1
        value["shots"][0]["edit_out"] = 1.0
        value["shots"][0]["timeline_duration"] = 1.0
        (project / "project.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.run_cli(
            "review-shot", str(project), "shot-001", "--kind", "video", "--decision", "reject",
            "--notes", "baked captions",
        )
        qa = self.run_cli("qa", str(project))
        self.assertTrue(qa["technical_ok"])
        self.assertFalse(qa["visual_ok"])
        self.assertFalse(qa["ok"])
        self.assertTrue(qa["manual_review_complete"])
        clip = next(report for report in qa["reports"] if report["kind"] == "clip")
        self.assertEqual(clip["asset_review"]["status"], "rejected")
        self.assertEqual(clip["performance_review"]["status"], "rejected")

    def test_budget_gate_blocks_before_billable_request(self) -> None:
        project = self.create_project("budget", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["budget"] = {"currency": "CNY", "image_request": 0.1, "video_request": 1.0, "max_estimated_cost": 0.5}
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("preflight", str(project), expected=1)
        self.assertFalse(result["preflight"]["budget"]["within_budget"])
        blocked = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertIn("estimated project cost", blocked["error"])
        self.assertEqual(FakeProviderHandler.video_creates, 0)

    def test_structured_character_and_continuity_audit(self) -> None:
        project = self.create_project("continuity")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["characters"] = [
            {"id": "lead", "name": "Lead", "identity": "Adult woman with a short black bob.", "wardrobe": "Green jacket.", "references": []}
        ]
        shot = value["shots"][0]
        shot["character_ids"] = ["lead"]
        shot["scene_id"] = "office"
        shot["continuity_notes"] = "Keep the green jacket and desk position unchanged."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        audit = self.run_cli("audit", str(project))
        self.assertTrue(audit["ok"])
        self.assertFalse(audit["warnings"])
        preflight = self.run_cli("preflight", str(project))
        video_prompt = next(item for item in preflight["preflight"]["prompts"] if item["kind"] == "video")
        self.assertGreater(video_prompt["characters"], len(shot["video_prompt"]))

    def test_multiple_selected_character_references_feed_keyframe_generation(self) -> None:
        project = self.create_project("multi-character")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["characters"] = []
        for character_id in ("lead", "friend"):
            relative = f"assets/references/{character_id}.png"
            (project / relative).write_bytes(FAKE_PNG)
            value["characters"].append(
                {
                    "id": character_id,
                    "name": character_id.title(),
                    "identity": f"Stable identity for {character_id}.",
                    "wardrobe": "Simple everyday clothes.",
                    "references": [relative],
                }
            )
        value["shots"][0]["character_ids"] = ["lead", "friend"]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")

        self.run_cli("generate-images", str(project))
        self.assertEqual(FakeProviderHandler.image_requests[-1][0], "/v1/images/edits")
        self.assertEqual(FakeProviderHandler.image_requests[-1][1].count(b'name="image"'), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_progress_qa_postprocess_and_cover_are_offline(self) -> None:
        project = self.create_project("delivery", generate_image=False)
        result = subprocess.run(
            [sys.executable, str(CLI), "generate-videos", str(project), "--poll-timeout", "5", "--progress"],
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"gvs_progress":true', result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

        qa = self.run_cli("qa", str(project))
        self.assertFalse(qa["technical_ok"])
        self.assertTrue(any("dimensions mismatch" in error for report in qa["reports"] for error in report.get("errors", [])))
        self.assertTrue(qa["visual_review_required"])
        clip_report = next(report for report in qa["reports"] if report["kind"] == "clip")
        self.assertEqual(len(clip_report["review_frames"]), 3)
        self.assertTrue(all((project / frame["path"]).is_file() for frame in clip_report["review_frames"]))

        source = project / "clips" / "shot-001.mp4"
        processed = project / "deliverables" / "processed.mp4"
        cover = project / "deliverables" / "cover.jpg"
        post = self.run_cli("postprocess", str(source), str(processed), "--fade-seconds", "0.1")
        self.assertEqual(post["media"]["codec"], "h264")
        cover_result = self.run_cli("cover", str(processed), str(cover), "--at-seconds", "0.2")
        self.assertGreater(cover_result["cover"]["bytes"], 100)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_subtitles_export_sidecar_and_preserve_clean_final(self) -> None:
        project = self.create_project("subtitles", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["subtitles"] = [
            {"start": 0.05, "end": 0.45, "text": "First line"},
            {"start": 0.5, "end": 0.9, "text": "Second line"},
        ]
        value["shots"][0]["seconds"] = 1
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("run", str(project), "--poll-timeout", "5")
        clean = project / "deliverables" / "final.mp4"
        clean_digest = clean.read_bytes()

        result = self.run_cli("subtitles", str(project), "--source", "project", "--burn", "--style", "cinematic")
        self.assertEqual(result["subtitles"]["cue_count"], 2)
        srt = project / "deliverables" / "subtitles.srt"
        subtitled = project / "deliverables" / "final-subtitled.mp4"
        self.assertIn("00:00:00,050 --> 00:00:00,450", srt.read_text(encoding="utf-8"))
        self.assertTrue(subtitled.is_file())
        self.assertEqual(clean.read_bytes(), clean_digest)
        self.assertTrue(result["burned_video"]["has_audio"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_native_dialogue_requires_clean_source_confirmation_before_subtitle_burn(self) -> None:
        project = self.create_project("native-subtitle-guard", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["characters"] = [{"id": "lead", "name": "Lead", "identity": "Original AI presenter.", "references": []}]
        value["shots"][0]["character_ids"] = ["lead"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "Source review required.", "start": 0.05, "end": 0.9}
        ]
        value["shots"][0]["seconds"] = 1
        value["audio"] = {"mode": "native-dialogue", "generate_audio": True}
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        source = project / "deliverables" / "final.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(FakeProviderHandler.video_payload)

        blocked = self.run_cli("subtitles", str(project), "--burn", expected=1)
        self.assertIn("may bake captions", blocked["error"])
        self.assertFalse((project / "deliverables" / "final-subtitled.mp4").exists())
        allowed = self.run_cli("subtitles", str(project), "--source", "project", "--burn", "--confirm-source-clean")
        self.assertGreater(allowed["burned_video"]["duration"], 0)
        self.assertTrue((project / "deliverables" / "final-subtitled.mp4").is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_local_dialogue_renders_tts_timeline_audio_and_subtitles(self) -> None:
        project = self.create_project("dialogue", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["seconds"] = 1
        value["characters"] = [
            {
                "id": "lead",
                "name": "Lead",
                "identity": "An original synthetic presenter.",
                "references": [],
                "voice": {"provider": "cosyvoice", "voice_id": "fake-speaker"},
            }
        ]
        value["shots"][0]["character_ids"] = ["lead"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "你好，欢迎来到这里。", "start": 0.05, "end": 0.9, "subtitle": True}
        ]
        value["audio"] = {
            "mode": "local-voice",
            "language": "zh-CN",
            "generate_audio": False,
            "preserve_source_audio": True,
            "duck_source_audio": True,
            "subtitle_source": "project",
        }
        (project / "project.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        local_prompt = composed_video_prompt(value, value["shots"][0])
        self.assertNotIn("你好，欢迎来到这里。", local_prompt)
        self.assertIn("do not show any legible words", local_prompt)
        source = project / "deliverables" / "final.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(FakeProviderHandler.audio_payload)

        validation = self.run_cli("validate", str(project))
        self.assertTrue(validation["ok"])
        result = self.run_cli(
            "dialogue-render",
            str(project),
            "--source-video",
            str(source),
            "--cosyvoice-url",
            self.base_url,
        )
        self.assertEqual(result["dialogue"]["line_count"], 1)
        self.assertTrue(result["dialogue"]["video"]["has_audio"])
        self.assertGreater(result["dialogue"]["video"]["audio"]["max_volume_db"], -35)
        self.assertEqual(FakeProviderHandler.tts_creates, 1)
        self.assertIn("你好，欢迎来到这里。", (project / "deliverables" / "dialogue.srt").read_text(encoding="utf-8"))

        resumed = self.run_cli(
            "dialogue-render",
            str(project),
            "--source-video",
            str(source),
            "--cosyvoice-url",
            self.base_url,
        )
        self.assertTrue(resumed["dialogue"]["rendered"][0]["skipped"])
        self.assertEqual(FakeProviderHandler.tts_creates, 1)

        value["audio"]["mode"] = "local-lipsync"
        (project / "project.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        failed_lipsync = self.run_cli(
            "dialogue-render",
            str(project),
            "--source-video",
            str(source),
            "--cosyvoice-url",
            "http://127.0.0.1:1",
            "--musetalk-url",
            self.base_url,
            expected=1,
        )
        self.assertNotIn("CosyVoice service is unavailable", failed_lipsync["error"])
        self.assertEqual(FakeProviderHandler.tts_creates, 1)

    def test_cached_dialogue_can_resume_without_tts_provider(self) -> None:
        source = (SKILL_ROOT / "scripts" / "dialogue_workflow.py").read_text(encoding="utf-8")
        self.assertIn('runtime.get("signature") == current_signature and output.is_file()', source)
        self.assertIn('health_by_provider[selected_provider] = tts.health()', source)
        self.assertIn("is unavailable and dialogue line", source)

    def test_native_dialogue_adds_exact_prompt_without_nonstandard_quickai_flag(self) -> None:
        project = self.create_project("native-dialogue", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["video_mode"] = "text-to-video"
        value["video_provider"] = "quickai"
        value["characters"] = [{"id": "lead", "name": "Lead", "identity": "An original AI person.", "references": []}]
        value["shots"][0]["character_ids"] = ["lead"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "这句话必须准确说出。", "start": 0.2, "end": 2.5}
        ]
        value["audio"] = {"mode": "native-dialogue", "generate_audio": True}
        (project / "project.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertNotIn("generate_audio", FakeProviderHandler.last_json_video)
        self.assertIn("这句话必须准确说出。", str(FakeProviderHandler.last_json_video["prompt"]))
        self.assertIn("speech remains audible only", str(FakeProviderHandler.last_json_video["prompt"]))
        self.assertNotIn("says exactly", str(FakeProviderHandler.last_json_video["prompt"]))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_dpapi_storage_keeps_plaintext_out_of_config(self) -> None:
        secure_dir = self.root / "secure-config"
        image_secret = "private-image-value"
        text_video_secret = "private-text-video-value"
        image_video_secret = "private-image-video-value"
        environment = self.env.copy()
        environment.update({"GVS_CONFIG_DIR": str(secure_dir), "GVS_QUICKAI_KEY": "", "GVS_QUICKAINEW_KEY": ""})
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "configure",
                "--credentials-stdin",
                "--skip-test",
                "--quickai-base-url",
                self.base_url,
                "--quickainew-base-url",
                self.base_url,
            ],
            input=json.dumps(
                {
                    "quickai_image_key": image_secret,
                    "quickai_video_key": text_video_secret,
                    "quickainew_video_key": image_video_secret,
                }
            ) + "\n",
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(image_secret, result.stdout + result.stderr)
        self.assertNotIn(text_video_secret, result.stdout + result.stderr)
        self.assertNotIn(image_video_secret, result.stdout + result.stderr)
        with mock.patch.dict(os.environ, environment, clear=False):
            config_bytes = (secure_dir / "config.json").read_bytes()
            secret_bytes = (secure_dir / "secrets.dpapi").read_bytes()
            self.assertNotIn(image_secret.encode(), config_bytes + secret_bytes)
            self.assertNotIn(text_video_secret.encode(), config_bytes + secret_bytes)
            self.assertNotIn(image_video_secret.encode(), config_bytes + secret_bytes)
            loaded = load_settings()
            self.assertEqual(loaded["quickai_image_key"], image_secret)
            self.assertEqual(loaded["quickai_video_key"], text_video_secret)
            self.assertEqual(loaded["quickainew_video_key"], image_video_secret)

    def test_credentials_stdin_accepts_single_provider_key(self) -> None:
        secure_dir = self.root / "invalid-config"
        environment = self.env.copy()
        environment.update({"GVS_CONFIG_DIR": str(secure_dir), "GVS_QUICKAI_KEY": "", "GVS_QUICKAINEW_KEY": ""})
        result = subprocess.run(
            [sys.executable, str(CLI), "configure", "--credentials-stdin", "--skip-test"],
            input='{"quickai_key":"only-one-key"}\n',
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if os.name != "nt":
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("requires Windows DPAPI", result.stdout + result.stderr)
            return
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((secure_dir / "config.json").exists())

    def test_text_to_video_uses_quickai_json_without_references(self) -> None:
        project = self.root / "t2v"
        self.run_cli(
            "init", str(project), "--title", "T2V", "--topic", "Text", "--workflow", "text-to-video",
            "--shots", "1", "--seconds", "1", "--video-resolution", "480p", "--aspect-ratio", "9:16",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "A short text-only video."
        value["shots"][0]["video_prompt"] = "A bright paper kite rises gently into the sky."
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        self.assertEqual(FakeProviderHandler.json_video_creates, 1)
        self.assertEqual(FakeProviderHandler.last_json_video["resolution"], "480p")
        self.assertEqual(FakeProviderHandler.last_json_video["aspect_ratio"], "9:16")
        self.assertEqual(FakeProviderHandler.last_json_video["duration"], 1)
        self.assertNotIn("seconds", FakeProviderHandler.last_json_video)
        self.assertNotIn("generate_audio", FakeProviderHandler.last_json_video)
        self.assertNotIn("image", FakeProviderHandler.last_json_video)
        self.assertNotIn("reference_images", FakeProviderHandler.last_json_video)
        self.assertIn("No app UI", FakeProviderHandler.last_json_video["prompt"])
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video_state = state["shots"]["shot-001"]["video"]
        self.assertEqual(video_state["mode"], "text-to-video")
        self.assertEqual(video_state["provider"], "quickai")
        self.assertEqual(video_state["resolution"], "480p")

    def test_quickainew_text_to_video_has_no_input_reference(self) -> None:
        project = self.create_project("new-t2v", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["video_mode"] = "text-to-video"
        value["video_provider"] = "quickainew"
        value["shots"][0]["video_resolution"] = "720p"
        value["shots"][0]["video_aspect_ratio"] = "16:9"
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        self.assertTrue(result["ok"])
        self.assertIn(b'name="resolution"\r\n\r\n720p', FakeProviderHandler.last_video_body)
        self.assertIn(b'name="aspect_ratio"', FakeProviderHandler.last_video_body)
        self.assertIn(b'name="generate_audio"\r\n\r\ntrue', FakeProviderHandler.last_video_body)
        self.assertNotIn(b'name="size"', FakeProviderHandler.last_video_body)
        self.assertNotIn(b'name="input_reference"', FakeProviderHandler.last_video_body)

    def test_component_profiles_are_explicit_and_configurable_without_downloads(self) -> None:
        plan = self.run_cli("components-plan", "--profile", "full-dialogue")
        self.assertEqual([item["id"] for item in plan["components"]], ["cosyvoice", "musetalk"])
        self.assertTrue(plan["consent_required"])
        configured = self.run_cli(
            "components-configure",
            "--profile",
            "local-voice",
            "--source-root",
            str(self.root / "component-src"),
            "--models-root",
            str(self.root / "component-models"),
            "--cosyvoice-url",
            self.base_url,
        )
        self.assertEqual(configured["settings"]["profile"], "local-voice")
        self.assertEqual(len(configured["plan"]["components"]), 1)
        blocked = self.run_cli("components-install", "--profile", "local-voice", expected=1)
        self.assertIn("--accept-downloads", blocked["error"])

    def test_install_profiles_are_side_effect_free_and_alias_compatible(self) -> None:
        basic = self.run_cli("install-plan", "--profile", "basic")
        self.assertEqual(basic["profile"], "basic")
        self.assertFalse(basic["requires_component_downloads"])
        self.assertEqual(basic["subtitle_source"], "upstream")
        lip_sync = self.run_cli("install-plan", "--profile", "lip-sync")
        self.assertEqual(lip_sync["component_profile"], "full-dialogue")
        self.assertTrue(lip_sync["consent_required"])
        dependency_ids = {item["id"]: item for item in lip_sync["dependencies"]}
        self.assertEqual(dependency_ids["ffmpeg"]["package_id"], "Gyan.FFmpeg")
        self.assertEqual(dependency_ids["docker"]["package_id"], "Docker.DockerDesktop")
        self.assertFalse(dependency_ids["nvidia-gpu"]["auto_installable"])
        self.assertGreaterEqual(lip_sync["model_download_gb"], 15)
        alias = self.run_cli("install-plan", "--profile", "full-dialogue")
        self.assertEqual(alias["profile"], "lip-sync")
        self.assertEqual(alias["component_profile"], "full-dialogue")

    def test_init_persists_audio_and_subtitle_contract(self) -> None:
        project = self.root / "audio-contract"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Audio contract",
            "--topic",
            "Dialogue",
            "--workflow",
            "text-to-video",
            "--mode",
            "text-to-video",
            "--audio-mode",
            "native-dialogue",
            "--subtitle-source",
            "upstream",
            "--shots",
            "1",
            "--seconds",
            "1",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(value["audio"]["mode"], "native-dialogue")
        self.assertTrue(value["audio"]["generate_audio"])
        self.assertEqual(value["audio"]["subtitle_source"], "upstream")

    def test_video_provider_default_and_explicit_selection_are_persisted(self) -> None:
        default_project = self.root / "default-provider"
        self.run_cli(
            "init", str(default_project), "--title", "Default", "--topic", "Provider",
            "--workflow", "text-to-video", "--shots", "1", "--seconds", "1",
        )
        default_value = json.loads((default_project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(default_value["video_provider"], "quickai")
        self.assertEqual(default_value["video_provider_policy"], "automatic")

        explicit_new = self.root / "explicit-new"
        self.run_cli(
            "init", str(explicit_new), "--title", "New", "--topic", "Provider",
            "--workflow", "text-to-video", "--shots", "1", "--seconds", "1",
            "--video-provider", "quickainew",
        )
        new_value = json.loads((explicit_new / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(new_value["video_provider"], "quickainew")
        self.assertEqual(new_value["video_provider_policy"], "fixed")
        new_value["story"] = "The user explicitly selected QuickAI New."
        new_value["shots"][0]["video_prompt"] = "A white paper plane crosses a clear blue sky."
        (explicit_new / "project.json").write_text(json.dumps(new_value), encoding="utf-8")
        generated = self.run_cli("generate-videos", str(explicit_new), "--poll-timeout", "5")
        self.assertEqual(generated["videos"]["final_providers"], {"shot-001": "quickainew"})
        self.assertEqual(FakeProviderHandler.json_video_creates, 0)
        self.assertEqual(FakeProviderHandler.video_creates, 1)

        config = json.loads((self.config_dir / "config.json").read_text(encoding="utf-8"))
        config["default_video_provider"] = "quickainew"
        (self.config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        saved_default = self.root / "saved-new-default"
        self.run_cli(
            "init", str(saved_default), "--title", "Saved", "--topic", "Provider",
            "--workflow", "text-to-video", "--shots", "1", "--seconds", "1",
        )
        saved_value = json.loads((saved_default / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_value["video_provider"], "quickainew")
        self.assertEqual(saved_value["video_provider_policy"], "fixed")

        explicit_quickai = self.root / "explicit-quickai"
        self.run_cli(
            "init", str(explicit_quickai), "--title", "QuickAI", "--topic", "Provider",
            "--workflow", "text-to-video", "--shots", "1", "--seconds", "1",
            "--video-provider", "quickai",
        )
        quickai_value = json.loads((explicit_quickai / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(quickai_value["video_provider"], "quickai")
        self.assertEqual(quickai_value["video_provider_policy"], "fixed")

    def test_init_can_inherit_user_facing_install_profile(self) -> None:
        project = self.root / "profile-contract"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Profile contract",
            "--topic",
            "Precise voice",
            "--workflow",
            "text-to-video",
            "--mode",
            "text-to-video",
            "--install-profile",
            "precise-voice",
            "--shots",
            "1",
            "--seconds",
            "1",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(value["audio"]["mode"], "local-voice")
        self.assertFalse(value["audio"]["generate_audio"])
        self.assertEqual(value["audio"]["subtitle_source"], "project")

    def test_saved_local_install_profile_does_not_change_new_project_defaults(self) -> None:
        self.run_cli("install-configure", "--profile", "lip-sync")
        project = self.root / "saved-profile-contract"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Saved profile contract",
            "--topic",
            "Upstream dialogue",
            "--workflow",
            "text-to-video",
            "--mode",
            "text-to-video",
            "--shots",
            "1",
            "--seconds",
            "1",
        )
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(value["audio"]["mode"], "native-dialogue")
        self.assertTrue(value["audio"]["generate_audio"])
        self.assertEqual(value["audio"]["subtitle_source"], "none")

    def test_full_dialogue_start_requires_an_explicit_gpu_stage(self) -> None:
        configured = self.run_cli(
            "components-configure",
            "--profile",
            "full-dialogue",
            "--source-root",
            str(self.root / "component-src"),
            "--models-root",
            str(self.root / "component-models"),
        )
        self.assertEqual(configured["settings"]["profile"], "full-dialogue")
        blocked = self.run_cli("components-start", "--profile", "full-dialogue", expected=1)
        self.assertIn("--component", blocked["error"])
        self.assertIn("8 GB", blocked["error"])

    def test_stage_switch_removes_exited_sibling_without_stopping_it(self) -> None:
        source = (SKILL_ROOT / "scripts" / "component_manager.py").read_text(encoding="utf-8")
        self.assertIn('running = _docker("inspect", "-f", "{{.State.Running}}", container)', source)
        self.assertIn('if running:', source)

    def test_installer_requires_explicit_stage_for_full_dialogue_start(self) -> None:
        source = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("InstallProfile", source)
        self.assertIn("Interactive", source)
        self.assertIn("install-plan", source)
        self.assertIn("$Check", source)
        self.assertIn("$Repair", source)
        self.assertIn("$Uninstall", source)
        self.assertIn("InstallSystemDependencies", source)
        self.assertIn("AcceptSystemDependencyChanges", source)
        self.assertIn("[IO.FileShare]::None", source)
        self.assertLess(source.index("[IO.FileShare]::None"), source.index("Remove-Item -LiteralPath $destinationPath -Recurse -Force"))
        self.assertIn("Previous installation preserved", source)
        self.assertIn('[ValidateSet("cosyvoice", "musetalk", "all")]', source)
        self.assertIn("-StartComponents with full-dialogue requires -StartComponent", source)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_subtitle_source_can_preserve_upstream_or_disable_delivery(self) -> None:
        project = self.create_project("subtitle-sources", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["subtitle"] = "A deterministic subtitle"
        value["shots"][0]["seconds"] = 1
        value["audio"]["subtitle_source"] = "upstream"
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        upstream = self.run_cli("subtitles", str(project))
        self.assertEqual(upstream["subtitles"]["source"], "upstream")
        self.assertFalse((project / "deliverables" / "subtitles.srt").exists())
        value["audio"]["subtitle_source"] = "none"
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        none = self.run_cli("subtitles", str(project))
        self.assertEqual(none["subtitles"]["source"], "none")
        self.assertFalse(none["subtitles"]["preserved"])
        blocked = self.run_cli("subtitles", str(project), "--burn", expected=1)
        self.assertIn("has no local SRT", blocked["error"])

    def test_component_docker_output_is_decoded_as_utf8_with_replacement(self) -> None:
        completed = subprocess.CompletedProcess(["docker", "version"], 0, stdout="ok", stderr="")
        with mock.patch("component_manager.subprocess.run", return_value=completed) as run:
            result = _docker("version")
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_cosyvoice_runtime_pins_legacy_build_tooling(self) -> None:
        dockerfile = (SKILL_ROOT / "assets" / "docker" / "cosyvoice.Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"setuptools==69.5.1"', dockerfile)
        self.assertIn('"numpy==1.26.4"', dockerfile)
        self.assertIn("grep -vE '^(openai-whisper|pyworld)=='", dockerfile)
        self.assertIn('"openai-whisper==20231117"', dockerfile)
        self.assertIn("--no-build-isolation --no-deps", dockerfile)
        self.assertIn("-r /tmp/cosyvoice-requirements.txt", dockerfile)
        self.assertIn("FunAudioLLM/CosyVoice-ttsfrd", dockerfile)
        self.assertIn("8c0f9244a4f7622bf8017cad347ed334f0b8f735", dockerfile)

    def test_musetalk_runtime_handles_legacy_chumpy_build(self) -> None:
        dockerfile = (SKILL_ROOT / "assets" / "docker" / "musetalk.Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"pip==24.3.1"', dockerfile)
        self.assertIn('"setuptools==69.5.1"', dockerfile)
        self.assertIn('--no-build-isolation "chumpy==0.70"', dockerfile)
        self.assertLess(dockerfile.index("chumpy==0.70"), dockerfile.index('mim install "mmpose==1.1.0"'))

    def test_component_models_are_revision_pinned_and_use_python_downloads(self) -> None:
        manifest = json.loads((SKILL_ROOT / "assets" / "components.json").read_text(encoding="utf-8"))
        for component in manifest["components"].values():
            for model in component["models"]:
                self.assertRegex(model["revision"], r"^[0-9a-f]{40}$")
                self.assertFalse(Path(model["destination"]).is_absolute())
                self.assertGreater(float(model["estimated_size_gb"]), 0)
                self.assertTrue(model.get("required_patterns"))
        musetalk_models = manifest["components"]["musetalk"]["models"]
        self.assertTrue(all(model.get("allow_patterns") for model in musetalk_models))
        self.assertEqual(
            musetalk_models[0]["allow_patterns"],
            ["musetalkV15/musetalk.json", "musetalkV15/unet.pth", "syncnet/latentsync_syncnet.pt"],
        )
        repositories = {model["repository"] for model in musetalk_models}
        self.assertNotIn("ByteDance/LatentSync", repositories)
        self.assertIn("ManyOtherFunctions/face-parse-bisent", repositories)
        source = (SKILL_ROOT / "scripts" / "component_manager.py").read_text(encoding="utf-8")
        self.assertIn("snapshot_download", source)
        self.assertIn("allow_patterns=", source)
        self.assertNotIn('image, "hf", "download"', source)

    def test_musetalk_models_are_mounted_at_official_relative_path(self) -> None:
        source = (SKILL_ROOT / "scripts" / "component_manager.py").read_text(encoding="utf-8")
        self.assertIn('f"{model}:/workspace/MuseTalk/models:ro"', source)

    def test_musetalk_health_is_complete_and_stays_responsive_during_inference(self) -> None:
        source = (SKILL_ROOT / "scripts" / "services" / "musetalk_server.py").read_text(encoding="utf-8")
        self.assertIn('models_root / "dwpose" / "dw-ll_ucoco_384.pth"', source)
        self.assertIn('models_root / "face-parse-bisent" / "79999_iter.pth"', source)
        self.assertIn('models_root / "whisper" / "pytorch_model.bin"', source)
        self.assertIn("def lipsync(", source)
        self.assertNotIn("async def lipsync(", source)
        self.assertIn("X-GVS-Inference-Seconds", source)

    def test_local_service_wrappers_resolve_fastapi_annotations_eagerly(self) -> None:
        services = SKILL_ROOT / "scripts" / "services"
        for name in ("cosyvoice_server.py", "musetalk_server.py"):
            source = (services / name).read_text(encoding="utf-8")
            self.assertNotIn("from __future__ import annotations", source, name)

    def test_model_download_state_reuses_verified_files_without_docker(self) -> None:
        models_root = self.root / "models"
        target = models_root / "demo"
        target.mkdir(parents=True)
        (target / "weights.bin").write_bytes(b"stable model bytes")
        model = {
            "repository": "example/demo",
            "revision": "a" * 40,
            "destination": "demo",
            "allow_patterns": ["weights.bin"],
            "required_patterns": ["weights.bin"],
        }
        with mock.patch("component_manager._docker") as docker:
            result = _download_component_models("demo", {"models": [model]}, image="gvs-demo:test", models_root=models_root)
        self.assertEqual(result[0]["status"], "reused")
        docker.assert_not_called()
        state = load_model_state(models_root)
        self.assertEqual(state["models"]["demo:example/demo@" + "a" * 40]["status"], "ready")

    def test_model_download_records_files_after_docker_and_repairs_corruption(self) -> None:
        models_root = self.root / "models-download"
        model = {
            "repository": "example/demo",
            "revision": "b" * 40,
            "destination": "demo",
            "allow_patterns": ["weights.bin"],
            "required_patterns": ["weights.bin"],
        }

        def fake_docker(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            target = models_root / "demo"
            target.mkdir(parents=True, exist_ok=True)
            (target / "weights.bin").write_bytes(b"downloaded model bytes")
            return subprocess.CompletedProcess(["docker"], 0, stdout="", stderr="")

        with mock.patch("component_manager._docker", side_effect=fake_docker) as docker:
            first = _download_component_models("demo", {"models": [model]}, image="gvs-demo:test", models_root=models_root)
            self.assertEqual(first[0]["status"], "downloaded")
            (models_root / "demo" / "weights.bin").write_bytes(b"corrupt")
            second = _download_component_models("demo", {"models": [model]}, image="gvs-demo:test", models_root=models_root)
        self.assertEqual(second[0]["status"], "downloaded")
        self.assertEqual(docker.call_count, 2)

    def test_model_state_migration_adopts_new_required_files(self) -> None:
        models_root = self.root / "models-migration"
        target = models_root / "demo"
        target.mkdir(parents=True)
        (target / "weights.bin").write_bytes(b"stable weights")
        revision = "c" * 40
        original = {
            "repository": "example/demo",
            "revision": revision,
            "destination": "demo",
            "allow_patterns": ["weights.bin"],
            "required_patterns": ["weights.bin"],
        }
        with mock.patch("component_manager._docker"):
            _download_component_models("demo", {"models": [original]}, image="gvs-demo:test", models_root=models_root)
        (target / "tokenizer.json").write_bytes(b"stable tokenizer")
        expanded = {**original, "required_patterns": ["weights.bin", "tokenizer.json"]}
        with mock.patch("component_manager._docker") as docker:
            result = _download_component_models("demo", {"models": [expanded]}, image="gvs-demo:test", models_root=models_root)
        self.assertEqual(result[0]["status"], "reused")
        docker.assert_not_called()
        files = load_model_state(models_root)["models"]["demo:example/demo@" + revision]["files"]
        self.assertIn("tokenizer.json", files)

    def test_model_disk_preflight_blocks_insufficient_space(self) -> None:
        model = {"repository": "example/large", "estimated_size_gb": 2.0}
        usage = shutil.disk_usage(self.root)
        constrained = shutil._ntuple_diskusage(usage.total, usage.used, 1024**3)
        with mock.patch("component_manager.shutil.disk_usage", return_value=constrained):
            with self.assertRaisesRegex(RuntimeError, "insufficient disk space"):
                _ensure_model_disk_space(self.root, model, 0)

    def test_component_status_keeps_source_path_separate_from_model_paths(self) -> None:
        source_root = self.root / "sources"
        models_root = self.root / "models-status"
        settings = {
            "version": 1,
            "profile": "local-voice",
            "source_root": str(source_root),
            "models_root": str(models_root),
            "services": {"cosyvoice": "http://127.0.0.1:9880", "musetalk": "http://127.0.0.1:9881"},
        }
        with mock.patch("component_manager.load_component_settings", return_value=settings), mock.patch(
            "component_manager._service_health", return_value={"ok": False}
        ):
            result = component_status("local-voice")
        self.assertEqual(result["components"][0]["source"], str(source_root / "cosyvoice"))

    def test_new_component_checkout_failure_leaves_no_partial_target(self) -> None:
        remote = self.root / "component-remote"
        target = self.root / "component-target"
        subprocess.run(["git", "init", str(remote)], check=True, capture_output=True)
        (remote / "README.md").write_text("fixture", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(remote), "-c", "user.name=GVS Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"],
            check=True,
            capture_output=True,
        )
        component = {"repository": str(remote), "commit": "d" * 40, "submodules": False}
        with self.assertRaises(RuntimeError):
            _checkout_component("demo", component, target)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob(".gvs-demo-partial-*")))

    def test_existing_component_checkout_rolls_back_after_checkout_failure(self) -> None:
        target = self.root / "existing-component"
        (target / ".git").mkdir(parents=True)
        previous = "a" * 40
        requested = "b" * 40
        calls: list[tuple[str, ...]] = []

        def fake_git(*args: str, cwd: Path | None = None) -> str:
            calls.append(tuple(args))
            if args[:2] == ("status", "--porcelain"):
                return ""
            if args[:3] == ("remote", "get-url", "origin"):
                return "https://example.invalid/demo.git"
            if args[:2] == ("rev-parse", "HEAD"):
                return previous
            if args[:2] == ("checkout", "--detach") and args[2] == requested:
                raise RuntimeError("simulated checkout failure")
            return ""

        component = {"repository": "https://example.invalid/demo.git", "commit": requested, "submodules": False}
        with mock.patch("component_manager._git_output", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "simulated checkout failure"):
                _checkout_component("demo", component, target)
        self.assertIn(("checkout", "--detach", previous), calls)

    def test_cosyvoice_wrapper_preserves_reference_as_a_file(self) -> None:
        source = (SKILL_ROOT / "scripts" / "services" / "cosyvoice_server.py").read_text(encoding="utf-8")
        self.assertIn("MAX_PROMPT_BYTES", source)
        self.assertIn("_store_prompt(prompt_wav)", source)
        self.assertNotIn("load_wav(prompt_wav.file", source)
        self.assertIn("<|endofprompt|>", source)

    def test_voice_reference_requires_rights_and_transcript(self) -> None:
        project = self.create_project("voice-rights", generate_image=False)
        reference = project / "assets" / "voices" / "lead.wav"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"RIFFinvalid-test-wave")
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["audio"] = {"mode": "local-voice", "generate_audio": False}
        value["characters"] = [
            {
                "id": "lead",
                "name": "Lead",
                "identity": "Original AI character.",
                "references": [],
                "voice": {"reference_audio": "assets/voices/lead.wav"},
            }
        ]
        value["shots"][0]["character_ids"] = ["lead"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "Approved line.", "start": 0.2, "end": 2.0}
        ]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("consent" in error for error in result["errors"]))
        self.assertTrue(any("reference_text" in error for error in result["errors"]))

    def test_voice_approval_gate_and_duplicate_identity_detection(self) -> None:
        project = self.create_project("voice-contract-gates", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["audio"] = {"mode": "local-voice", "generate_audio": False}
        value["characters"] = [
            {
                "id": character_id,
                "name": character_id,
                "identity": "Original synthetic character.",
                "references": [],
                "voice": {
                    "provider": "cosyvoice",
                    "voice_id": "same-speaker",
                    "voice_status": "temporary-test",
                },
            }
            for character_id in ("lead", "friend")
        ]
        value["shots"][0]["character_ids"] = ["lead", "friend"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "First.", "start": 0.1, "end": 1.0},
            {"id": "line-002", "speaker": "friend", "text": "Second.", "start": 1.1, "end": 2.0},
        ]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        blocked = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("voice_status must be approved" in item for item in blocked["errors"]))
        self.assertTrue(any("share the same voice identity" in item for item in blocked["errors"]))
        value["audio"].update({"allow_temporary_voices": True, "allow_shared_voices": True})
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(self.run_cli("validate", str(project))["ok"])

    def test_parallel_voice_approvals_preserve_both_character_updates(self) -> None:
        project = self.create_project("parallel-voice-approval", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["audio"] = {"mode": "local-voice", "generate_audio": False, "tts_provider": "cosyvoice"}
        value["characters"] = [
            {"id": "lead", "name": "Lead", "identity": "Original lead.", "references": []},
            {"id": "friend", "name": "Friend", "identity": "Original friend.", "references": []},
        ]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")

        candidates = []
        for character_id in ("lead", "friend"):
            relative = f"assets/voice-auditions/{character_id}/{character_id}.wav"
            audio_path = project / relative
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(FAKE_WAV)
            voice = {
                "provider": "cosyvoice",
                "voice_type": "reference",
                "voice_status": "auditioned",
                "reference_audio": relative,
                "reference_text": f"{character_id} reference",
                "consent": "synthetic",
                "source_license": "test fixture",
            }
            candidates.append(
                {
                    "id": f"{character_id}-candidate",
                    "character_id": character_id,
                    "status": "auditioned",
                    "provider": "cosyvoice",
                    "voice": voice,
                    "audio_path": relative,
                    "audio_sha256": voice_workflow.file_digest(audio_path),
                }
            )
        (project / "voice-catalog.json").write_text(
            json.dumps({"version": 1, "updated_at": 0, "candidates": candidates}),
            encoding="utf-8",
        )

        first_write_started = threading.Event()
        original_write = voice_workflow.atomic_write_json
        delayed = False

        def delayed_project_write(path: Path, payload: object) -> None:
            nonlocal delayed
            if path.name == "project.json" and not delayed:
                delayed = True
                first_write_started.set()
                time.sleep(0.2)
            original_write(path, payload)

        errors: list[BaseException] = []

        def approve(candidate_id: str) -> None:
            try:
                voice_workflow.review_voice_candidate(project, candidate_id, approve=True)
            except BaseException as error:  # pragma: no cover - surfaced by the assertion below
                errors.append(error)

        with mock.patch("voice_workflow.atomic_write_json", side_effect=delayed_project_write):
            first = threading.Thread(target=approve, args=("lead-candidate",))
            second = threading.Thread(target=approve, args=("friend-candidate",))
            first.start()
            self.assertTrue(first_write_started.wait(timeout=2))
            second.start()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        approved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        voices = {item["id"]: item.get("voice", {}) for item in approved["characters"]}
        self.assertEqual(voices["lead"]["voice_status"], "approved")
        self.assertEqual(voices["friend"]["voice_status"], "approved")
        catalog = json.loads((project / "voice-catalog.json").read_text(encoding="utf-8"))
        self.assertEqual({item["status"] for item in catalog["candidates"]}, {"approved"})

    def test_series_voice_sync_excludes_unapproved_candidates(self) -> None:
        series_root = self.root / "voice-sync-series"
        self.run_cli(
            "series-init",
            str(series_root),
            "--title",
            "Voice Sync",
            "--premise",
            "Two characters need reviewed voices.",
            "--episodes",
            "1",
            "--episode-seconds",
            "6",
            "--clip-seconds",
            "6",
            "--mode",
            "text-to-video",
        )
        series = json.loads((series_root / "series.json").read_text(encoding="utf-8"))
        series["characters"] = [
            {
                "id": "approved",
                "name": "Approved",
                "identity": "Original character one.",
                "voice": {"provider": "cosyvoice", "voice_id": "voice-a", "voice_status": "approved"},
            },
            {
                "id": "draft",
                "name": "Draft",
                "identity": "Original character two.",
                "voice": {"provider": "voicebox", "voice_status": "auditioned", "preset_voice_id": "Dylan"},
            },
        ]
        (series_root / "series.json").write_text(json.dumps(series), encoding="utf-8")
        synced = self.run_cli("series-voice-sync", str(series_root))
        self.assertTrue(synced["approved_voices_only"])
        project = json.loads((series_root / "episodes" / "ep-001" / "project.json").read_text(encoding="utf-8"))
        characters = {item["id"]: item for item in project["characters"]}
        self.assertEqual(characters["approved"]["voice"]["voice_id"], "voice-a")
        self.assertNotIn("voice", characters["draft"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_voicebox_audition_approval_and_dialogue_render(self) -> None:
        project = self.create_project("voicebox-dialogue", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["shots"][0]["seconds"] = 1
        value["characters"] = [
            {
                "id": "lead",
                "name": "Lead",
                "identity": "An original synthetic presenter.",
                "references": [],
                "voice": {"provider": "voicebox", "voice_status": "draft"},
            }
        ]
        value["shots"][0]["character_ids"] = ["lead"]
        value["shots"][0]["dialogue"] = [
            {"id": "line-001", "speaker": "lead", "text": "今天先把声音定下来。", "start": 0.05, "end": 0.9}
        ]
        value["audio"] = {
            "mode": "local-voice",
            "language": "zh-CN",
            "tts_provider": "voicebox",
            "generate_audio": False,
        }
        (project / "project.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

        blocked = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("voice_status must be approved" in item for item in blocked["errors"]))
        voices = self.run_cli(
            "voice-list",
            "--provider",
            "voicebox",
            "--engine",
            "qwen_custom_voice",
            "--service-url",
            self.base_url,
        )
        self.assertEqual([item["voice_id"] for item in voices["voices"]], ["Dylan", "Vivian"])
        audition = self.run_cli(
            "voice-audition",
            str(project),
            "lead",
            "--provider",
            "voicebox",
            "--preset-voice-id",
            "Dylan",
            "--engine",
            "qwen_custom_voice",
            "--text",
            "这是周小满的声音试听。",
            "--candidate-id",
            "lead-dylan-a",
            "--service-url",
            self.base_url,
        )
        self.assertEqual(audition["candidate"]["status"], "auditioned")
        self.assertTrue((project / audition["candidate"]["audio_path"]).is_file())
        self.assertEqual(FakeProviderHandler.voicebox_generations, 1)
        self.run_cli("voice-approve", str(project), "lead-dylan-a")
        already_reviewed = self.run_cli("voice-approve", str(project), "lead-dylan-a", expected=1)
        self.assertIn("already reviewed", already_reviewed["error"])
        self.assertTrue(self.run_cli("validate", str(project))["ok"])
        approved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(approved["characters"][0]["voice"]["voice_status"], "approved")
        self.assertEqual(approved["characters"][0]["voice"]["provider_profile_id"], "voicebox-profile-1")

        source = project / "deliverables" / "final.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(FakeProviderHandler.video_payload)
        FakeProviderHandler.voicebox_profiles = []
        rendered = self.run_cli(
            "dialogue-render",
            str(project),
            "--source-video",
            str(source),
            "--voicebox-url",
            self.base_url,
        )
        self.assertEqual(rendered["dialogue"]["providers"], ["voicebox"])
        self.assertEqual(FakeProviderHandler.voicebox_generations, 2)
        resumed = self.run_cli(
            "dialogue-render",
            str(project),
            "--source-video",
            str(source),
            "--voicebox-url",
            "http://127.0.0.1:1",
        )
        self.assertTrue(resumed["dialogue"]["rendered"][0]["skipped"])
        self.assertEqual(FakeProviderHandler.voicebox_generations, 2)

    def test_voicebox_setup_plan_is_side_effect_free_and_pinned(self) -> None:
        source = self.root / "missing-voicebox"
        models = self.root / "planned-models"
        data = self.root / "planned-data"
        plan = self.run_cli(
            "voicebox-setup-plan",
            "--source",
            str(source),
            "--models-root",
            str(models),
            "--data-root",
            str(data),
        )
        self.assertEqual(plan["side_effects"], "none")
        self.assertEqual(plan["models"][0]["revision"], "85e237c12c027371202489a0ec509ded67b5e4b5")
        self.assertEqual(plan["models"][0]["license"], "Apache-2.0")
        self.assertFalse(source.exists())
        self.assertFalse(models.exists())
        self.assertFalse(data.exists())

    def test_voicebox_audition_never_implicitly_downloads_a_model(self) -> None:
        project = self.create_project("voicebox-download-guard", generate_image=False)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["characters"] = [
            {"id": "lead", "name": "Lead", "identity": "Original character.", "references": []}
        ]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        FakeProviderHandler.voicebox_model_downloaded = False
        blocked = self.run_cli(
            "voice-audition",
            str(project),
            "lead",
            "--provider",
            "voicebox",
            "--preset-voice-id",
            "Dylan",
            "--engine",
            "qwen_custom_voice",
            "--text",
            "This must not trigger a download.",
            "--service-url",
            self.base_url,
            expected=1,
        )
        self.assertIn("not downloaded", blocked["error"])
        self.assertEqual(FakeProviderHandler.voicebox_generations, 0)
        self.assertEqual(FakeProviderHandler.voicebox_profiles, [])

    def test_role_specific_credentials_do_not_cross_generation_routes(self) -> None:
        self.env.update(
            {
                "GVS_QUICKAI_IMAGE_KEY": "image-role-key",
                "GVS_QUICKAI_VIDEO_KEY": "text-video-role-key",
                "GVS_QUICKAINEW_VIDEO_KEY": "image-video-role-key",
            }
        )
        image_project = self.create_project("role-image")
        self.run_cli("generate-images", str(image_project))
        self.assertEqual(FakeProviderHandler.image_authorization, "Bearer image-role-key")

        text_project = self.root / "role-text-video"
        self.run_cli(
            "init",
            str(text_project),
            "--title",
            "Text",
            "--topic",
            "Route",
            "--workflow",
            "text-to-video",
            "--shots",
            "1",
            "--seconds",
            "1",
        )
        text_value = json.loads((text_project / "project.json").read_text(encoding="utf-8"))
        text_value["story"] = "One text generated clip."
        text_value["shots"][0]["video_prompt"] = "A red paper boat floats across a still pond."
        (text_project / "project.json").write_text(json.dumps(text_value), encoding="utf-8")
        self.run_cli("generate-videos", str(text_project), "--poll-timeout", "5")
        self.assertEqual(FakeProviderHandler.json_video_authorization, "Bearer text-video-role-key")

        self.run_cli("generate-videos", str(image_project), "--poll-timeout", "5")
        self.assertEqual(FakeProviderHandler.json_video_authorization, "Bearer text-video-role-key")
        self.assertEqual(FakeProviderHandler.multipart_video_authorization, "")

        new_project = self.create_project("role-explicit-new", generate_image=False)
        new_value = json.loads((new_project / "project.json").read_text(encoding="utf-8"))
        new_value["video_provider"] = "quickainew"
        (new_project / "project.json").write_text(json.dumps(new_value), encoding="utf-8")
        self.run_cli("generate-videos", str(new_project), "--poll-timeout", "5")
        self.assertEqual(FakeProviderHandler.multipart_video_authorization, "Bearer image-video-role-key")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_supplied_image_animation_does_not_require_an_image_key(self) -> None:
        self.env.update(
            {
                "GVS_QUICKAI_KEY": "",
                "GVS_QUICKAI_IMAGE_KEY": "",
                "GVS_QUICKAI_VIDEO_KEY": "",
                "GVS_QUICKAINEW_KEY": "",
                "GVS_QUICKAINEW_VIDEO_KEY": "only-image-to-video-key",
            }
        )
        project = self.root / "supplied-image"
        self.run_cli(
            "init",
            str(project),
            "--title",
            "Animate One Image",
            "--topic",
            "Subtle portrait motion",
            "--workflow",
            "single-image-animation",
            "--shots",
            "1",
            "--seconds",
            "1",
            "--video-size",
            "320x240",
            "--aspect-ratio",
            "4:3",
        )
        reference = project / "assets" / "references" / "portrait.png"
        reference.write_bytes(FAKE_PNG)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["story"] = "Animate the supplied portrait without changing its identity or framing."
        value["shots"][0]["video_prompt"] = "The subject blinks once and breathes naturally; lock all other details."
        value["shots"][0]["video_references"] = ["assets/references/portrait.png"]
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")

        result = self.run_cli("run", str(project), "--poll-timeout", "5")
        self.assertEqual(result["images"]["skipped"], ["shot-001"])
        self.assertEqual(FakeProviderHandler.image_creates, 0)
        self.assertEqual(FakeProviderHandler.multipart_video_authorization, "Bearer only-image-to-video-key")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_series_episode_lifecycle_and_shared_character_master(self) -> None:
        series_root = self.root / "series"
        created = self.run_cli(
            "series-init",
            str(series_root),
            "--title",
            "City Story",
            "--premise",
            "A newcomer builds a life in the city.",
            "--episodes",
            "2",
            "--episode-seconds",
            "1",
            "--clip-seconds",
            "1",
            "--video-size",
            "320x240",
            "--aspect-ratio",
            "4:3",
        )
        self.assertEqual(created["episode_count"], 2)
        series = json.loads((series_root / "series.json").read_text(encoding="utf-8"))
        series["season_arc"] = "The lead moves from rejection to belonging."
        series["style_bible"] = "Naturalistic daylight and restrained handheld camera."
        series["audio"] = {"mode": "local-voice", "language": "zh-CN", "generate_audio": False}
        series["characters"] = [
            {
                "id": "lead",
                "name": "Chen",
                "identity": "A 24-year-old man with short black hair and a narrow face.",
                "wardrobe": "Faded denim jacket and canvas backpack.",
                "voice": {"provider": "cosyvoice", "voice_id": "series-lead"},
                "master": {
                    "enabled": True,
                    "generate": True,
                    "path": "assets/character-masters/lead.png",
                    "prompt": "A clean single sheet with front, side, and back full-body views of the same person.",
                    "source_references": [],
                    "image_size": "1024x1024",
                    "image_quality": "auto",
                },
            }
        ]
        for episode in series["episodes"]:
            episode["title"] = f"Episode {episode['number']}"
            episode["synopsis"] = f"Story beat {episode['number']}."
        (series_root / "series.json").write_text(json.dumps(series), encoding="utf-8")

        episode_one = series_root / "episodes" / "ep-001"
        project = json.loads((episode_one / "project.json").read_text(encoding="utf-8"))
        project["story"] = "Chen reaches the city and asks for his first job."
        project["shots"][0].update(
            {
                "summary": "Chen enters a small restaurant.",
                "scene_id": "restaurant",
                "character_ids": ["lead"],
                "continuity_notes": "Keep the denim jacket and backpack.",
                "image_prompt": "Chen stands inside a modest restaurant beside the front door.",
                "video_prompt": "Chen takes one step forward and looks toward the counter.",
            }
        )
        (episode_one / "project.json").write_text(json.dumps(project), encoding="utf-8")

        preflight = self.run_cli("series-preflight", str(series_root), "--episode", "ep-001")
        self.assertEqual(preflight["request_totals"]["pending_series_character_images"], 1)
        self.assertEqual(preflight["request_totals"]["episode_images"], 1)
        self.assertEqual(preflight["request_totals"]["episode_videos"], 1)
        self.assertEqual(preflight["request_totals"]["total_pending_images"], 2)

        characters = self.run_cli("series-generate-characters", str(series_root))
        self.assertEqual(characters["characters"]["completed"], ["lead"])
        synced_project = json.loads((episode_one / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(synced_project["characters"][0]["references"], ["assets/characters/lead.png"])
        self.assertEqual(synced_project["characters"][0]["voice"]["voice_id"], "series-lead")
        self.assertEqual(synced_project["audio"]["mode"], "local-voice")

        self.run_cli("series-approve", str(series_root), "ep-001")
        approved_project = json.loads((episode_one / "project.json").read_text(encoding="utf-8"))
        approved_prompt = approved_project["shots"][0]["video_prompt"]
        approved_project["shots"][0]["video_prompt"] += " Unreviewed change."
        (episode_one / "project.json").write_text(json.dumps(approved_project), encoding="utf-8")
        blocked = self.run_cli(
            "series-run", str(series_root), "--episode", "ep-001", "--poll-timeout", "5", expected=1
        )
        self.assertIn("changed after approval", blocked["error"])
        self.assertEqual(FakeProviderHandler.image_creates, 1)
        approved_project["shots"][0]["video_prompt"] = approved_prompt
        (episode_one / "project.json").write_text(json.dumps(approved_project), encoding="utf-8")
        generated = self.run_cli("series-run", str(series_root), "--episode", "ep-001", "--poll-timeout", "5")
        self.assertEqual(generated["status"], "needs_review")
        self.assertTrue(generated["qa"]["technical_ok"])
        self.assertEqual(FakeProviderHandler.image_requests[-1][0], "/v1/images/edits")
        self.assertIn(b'name="image"', FakeProviderHandler.image_requests[-1][1])

        accepted = self.run_cli(
            "series-accept",
            str(series_root),
            "ep-001",
            "--continuity-summary",
            "Chen ends inside the restaurant, still wearing the denim jacket and carrying the backpack.",
        )
        self.assertEqual(accepted["runtime"]["status"], "completed")
        next_episode = self.run_cli("series-next", str(series_root))
        self.assertEqual(next_episode["episode"]["id"], "ep-002")
        self.assertEqual(next_episode["next_action"], "fill_prompts_then_preflight_and_approve")
        context = self.run_cli("series-context", str(series_root), "--episode", "ep-002")
        self.assertEqual(context["previous_episodes"][0]["id"], "ep-001")
        self.assertIn("denim jacket", context["previous_episodes"][0]["continuity_summary"])
        self.assertIn("denim jacket", context["current_project"]["series_context"]["previous_episode_continuity"])
        episode_two = series_root / "episodes" / "ep-002"
        project_two = json.loads((episode_two / "project.json").read_text(encoding="utf-8"))
        self.assertIn("denim jacket", project_two["series_context"]["previous_episode_continuity"])
        project_two["story"] = "Chen continues from the reviewed restaurant ending."
        project_two["shots"][0].update(
            {
                "image_prompt": "Chen waits beside the same restaurant counter.",
                "video_prompt": "Chen puts the backpack down beside the counter.",
                "character_ids": ["lead"],
            }
        )
        (episode_two / "project.json").write_text(json.dumps(project_two), encoding="utf-8")
        synced_two = json.loads((episode_two / "project.json").read_text(encoding="utf-8"))
        self.assertIn("Reviewed previous episode end state", composed_video_prompt(synced_two, synced_two["shots"][0]))

    def test_text_to_video_series_characters_do_not_require_image_masters(self) -> None:
        series_root = self.root / "text-series"
        self.run_cli(
            "series-init",
            str(series_root),
            "--title",
            "Text Series",
            "--premise",
            "A prompt-only episodic story.",
            "--episodes",
            "1",
            "--episode-seconds",
            "1",
            "--clip-seconds",
            "1",
            "--mode",
            "text-to-video",
        )
        series = json.loads((series_root / "series.json").read_text(encoding="utf-8"))
        series["characters"] = [
            {
                "id": "lead",
                "name": "Lead",
                "identity": "A stable prompt-only identity description.",
                "wardrobe": "Blue coat.",
            }
        ]
        (series_root / "series.json").write_text(json.dumps(series), encoding="utf-8")
        status = self.run_cli("series-status", str(series_root))
        self.assertTrue(status["ok"])


if __name__ == "__main__":
    unittest.main()
