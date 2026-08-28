from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
CLI = SKILL_ROOT / "scripts" / "grok_video_studio.py"
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
FAKE_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"fake-video-payload"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from gvs_common import load_settings  # noqa: E402
from grok_video_studio import composed_video_prompt  # noqa: E402
from provider_contracts import is_completed, result_urls, task_error, task_id, task_progress, task_status  # noqa: E402


class ProviderContractTests(unittest.TestCase):
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
        if self.path == "/v1/models":
            self.send_json({"data": [{"id": "gpt-image-2"}, {"id": "grok-imagine-video-1.5"}]})
            return
        if self.path == "/v1/videos/task-1":
            payload: dict[str, object] = {"id": "task-1", "status": type(self).video_status}
            if type(self).video_status == "failed":
                payload["error"] = {"message": "provider capacity is full"}
            self.send_json(payload)
            return
        if self.path == "/v1/videos/task-1/content":
            self.send_bytes(type(self).video_payload, "video/mp4")
            return
        if self.path == "/v1/videos/generations/task-1":
            payload: dict[str, object] = {"id": "task-1", "status": type(self).video_status}
            if type(self).video_status == "failed":
                payload["error"] = {"message": "provider capacity is full"}
            self.send_json(payload)
            return
        if self.path == "/v1/videos/generations/task-1/content":
            self.send_bytes(type(self).video_payload, "video/mp4")
            return
        self.send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
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
            self.send_json({"data": {"id": "task-1", "status": "queued"}})
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
                    "color=c=black:s=320x240:r=30",
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
                    "color=c=black:s=320x240:r=30",
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
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
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
        self.assertEqual(FakeProviderHandler.video_creates, 1)
        self.assertIn(b'720x1280', FakeProviderHandler.last_video_body)
        self.assertEqual(FakeProviderHandler.last_video_body.count(b'name="input_reference"'), 1)

        second = self.run_cli("run", str(project), "--poll-timeout", "5")
        self.assertTrue(second["ok"])
        self.assertEqual(FakeProviderHandler.image_creates, 1)
        self.assertEqual(FakeProviderHandler.video_creates, 1)

        state_path = project / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["shots"]["shot-001"]["video"]["status"] = "queued"
        state["shots"]["shot-001"]["video"]["path"] = ""
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        (project / "clips" / "shot-001.mp4").unlink()
        resumed = self.run_cli("resume", str(project), "--poll-timeout", "5")
        self.assertTrue(resumed["ok"])
        self.assertEqual(FakeProviderHandler.video_creates, 1)
        self.assertTrue((project / "clips" / "shot-001.mp4").is_file())

    def test_multiple_video_references_use_repeated_field(self) -> None:
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
        self.assertEqual(FakeProviderHandler.last_video_body.count(b'name="input_reference"'), 2)

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
        self.assertIn("No app UI", composed_video_prompt(value, shot))

        value["allow_ui_elements"] = True
        self.assertNotIn("No app UI", composed_video_prompt(value, shot))

        shot["allow_ui_elements"] = False
        self.assertIn("No app UI", composed_video_prompt(value, shot))

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

    def test_capabilities_and_dynamic_duration_planning(self) -> None:
        capabilities = self.run_cli("capabilities")
        ids = {item["id"] for item in capabilities["workflows"]}
        routes = {item["id"] for item in capabilities["product_routes"]}
        self.assertEqual(routes, {"text-to-video", "image-to-video", "episodic-series", "news-video"})
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
        value["character_bible"] = "x" * 4090
        (project / "project.json").write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("validate", str(project), expected=1)
        self.assertTrue(any("1 to 15" in error for error in result["errors"]))
        self.assertTrue(any("maximum is 4096" in error for error in result["errors"]))

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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_qa_rejects_legacy_final_without_audio(self) -> None:
        project = self.create_project("legacy-final", generate_image=False)
        self.run_cli("generate-videos", str(project), "--poll-timeout", "5")
        final = project / "deliverables" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / "clips" / "shot-001.mp4", final)
        state_path = project / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["deliverables"] = {"final": {"path": "deliverables/final.mp4"}}
        state_path.write_text(json.dumps(state), encoding="utf-8")

        qa = self.run_cli("qa", str(project))
        final_report = next(report for report in qa["reports"] if report["kind"] == "deliverable")
        self.assertFalse(final_report["ok"])
        self.assertTrue(any("no audio track" in error for error in final_report["errors"]))

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
        self.assertEqual(FakeProviderHandler.last_video_body.count(b'name="input_reference"'), 1)
        self.assertNotIn(b"character-master", FakeProviderHandler.last_video_body)

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
        self.assertEqual(FakeProviderHandler.video_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["shots"]["shot-001"]["video"]["status"], "submission_unknown")

        second = self.run_cli("resume", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(second["ok"])
        self.assertEqual(FakeProviderHandler.video_creates, 1)

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
        self.assertEqual(FakeProviderHandler.video_creates, 2)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        history = state["shots"]["shot-001"]["video"]["history"]
        self.assertEqual(history[-1]["reason"], "provider dashboard confirmed no usable task")

    def test_terminal_failed_task_requires_explicit_retry_authorization(self) -> None:
        project = self.create_project("terminal-failure", generate_image=False)
        FakeProviderHandler.video_status = "failed"

        first = self.run_cli("generate-videos", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(first["ok"])
        self.assertEqual(FakeProviderHandler.video_creates, 1)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "failed")
        self.assertEqual(video["task_id"], "task-1")

        blocked = self.run_cli("resume", str(project), "--poll-timeout", "5", expected=1)
        self.assertFalse(blocked["ok"])
        self.assertIn("--retry-failed", blocked["error"])
        self.assertEqual(FakeProviderHandler.video_creates, 1)

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
        self.assertEqual(FakeProviderHandler.video_creates, 2)
        state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        video = state["shots"]["shot-001"]["video"]
        self.assertEqual(video["status"], "completed")
        self.assertEqual(video["previous_task_id"], "task-1")
        self.assertEqual(video["history"][-1]["reason"], "terminal provider failure confirmed")

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

        result = self.run_cli("subtitles", str(project), "--burn")
        self.assertEqual(result["subtitles"]["cue_count"], 2)
        srt = project / "deliverables" / "subtitles.srt"
        subtitled = project / "deliverables" / "final-subtitled.mp4"
        self.assertIn("00:00:00,050 --> 00:00:00,450", srt.read_text(encoding="utf-8"))
        self.assertTrue(subtitled.is_file())
        self.assertEqual(clean.read_bytes(), clean_digest)
        self.assertTrue(result["burned_video"]["has_audio"])

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
        self.assertNotIn("input_reference", FakeProviderHandler.last_json_video)
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
        self.assertNotIn(b'name="input_reference"', FakeProviderHandler.last_video_body)

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
        series["characters"] = [
            {
                "id": "lead",
                "name": "Chen",
                "identity": "A 24-year-old man with short black hair and a narrow face.",
                "wardrobe": "Faded denim jacket and canvas backpack.",
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
        episode_two = series_root / "episodes" / "ep-002"
        project_two = json.loads((episode_two / "project.json").read_text(encoding="utf-8"))
        project_two["story"] = "Chen continues from the reviewed restaurant ending."
        project_two["shots"][0].update(
            {
                "image_prompt": "Chen waits beside the same restaurant counter.",
                "video_prompt": "Chen puts the backpack down beside the counter.",
                "character_ids": ["lead"],
            }
        )
        (episode_two / "project.json").write_text(json.dumps(project_two), encoding="utf-8")
        self.run_cli("series-sync", str(series_root))
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
