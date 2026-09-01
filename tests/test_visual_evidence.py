from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
CLI = SKILL_ROOT / "scripts" / "grok_video_studio.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from visual_evidence import (  # noqa: E402
    apply_visual_review_receipt,
    build_visual_benchmark,
    collect_visual_evidence,
    create_visual_review_receipt,
    validate_project_pixel_review,
    validate_visual_review,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class VisualEvidenceTests(unittest.TestCase):
    def _image(self, path: Path, color: str = "red") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x240",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return path

    def _video(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x240:r=30:d=1.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return path

    def test_image_and_video_evidence_are_hash_bound_and_path_minimized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = self._image(root / "outside" / "portrait.jpg")
            video = self._video(root / "outside" / "clip.mp4")
            output = root / "project" / "deliverables" / "visual-evidence"
            image_result = collect_visual_evidence(image, output, frame_count=5, project_root=root / "project")
            video_result = collect_visual_evidence(video, output, frame_count=3, project_root=root / "project")
            resumed = collect_visual_evidence(video, output, frame_count=3, project_root=root / "project")

            self.assertEqual(image_result["manifest"]["sampling"]["actual_frames"], 1)
            self.assertEqual(video_result["manifest"]["sampling"]["actual_frames"], 3)
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["manifest"]["manifest_sha256"], video_result["manifest"]["manifest_sha256"])
            self.assertEqual(image_result["manifest"]["source"]["locator"]["scope"], "external")
            serialized = json.dumps(image_result["manifest"], ensure_ascii=False)
            self.assertNotIn(str((root / "outside").resolve()), serialized)
            self.assertTrue(all((video_result["manifest_path"].parent / item["path"]).is_file() for item in video_result["manifest"]["frames"]))

    def test_manifest_or_evidence_frame_tampering_invalidates_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = self._image(root / "portrait.jpg")
            evidence = collect_visual_evidence(image, root / "evidence")
            reviewed = create_visual_review_receipt(
                evidence["manifest_path"],
                reviewer_id="test-reviewer",
                reviewer_kind="multimodal-agent",
                medium="photoreal",
                subject="human",
                subject_nature="synthetic-human",
                realism="high",
                confidence=0.9,
                identity_origin="generated-or-unknown",
                provenance_confirmed=False,
                provenance_evidence="AI corpus declaration.",
                frame_evidence=[{"frame_id": "frame-01", "observation": "Photoreal generated portrait."}],
                confirm=True,
            )
            frame = evidence["manifest_path"].parent / "frame-01.jpg"
            frame.write_bytes(frame.read_bytes() + b"changed")
            errors = validate_visual_review(reviewed["receipt"], evidence["manifest"], evidence["manifest_path"])
            self.assertTrue(any("missing or changed" in error for error in errors))

    def test_real_person_cannot_be_inferred_from_pixels_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = self._image(root / "portrait.jpg")
            result = collect_visual_evidence(image, root / "evidence")
            with self.assertRaisesRegex(Exception, "captured-real-person provenance"):
                create_visual_review_receipt(
                    result["manifest_path"],
                    reviewer_id="test-reviewer",
                    reviewer_kind="multimodal-agent",
                    medium="photoreal",
                    subject="human",
                    subject_nature="real-person",
                    realism="high",
                    confidence=0.9,
                    identity_origin="unknown",
                    provenance_confirmed=False,
                    provenance_evidence="",
                    frame_evidence=[{"frame_id": "frame-01", "observation": "Photographic-looking face."}],
                    confirm=True,
                )

    def test_near_real_anime_receipt_applies_fictional_nature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = self._image(root / "anime.jpg", "blue")
            evidence = collect_visual_evidence(image, root / "evidence")
            reviewed = create_visual_review_receipt(
                evidence["manifest_path"],
                reviewer_id="test-reviewer",
                reviewer_kind="multimodal-agent",
                medium="photoreal",
                subject="fictional-character",
                subject_nature="human-like-fictional",
                realism="high",
                confidence=0.91,
                identity_origin="fictional-design",
                provenance_confirmed=True,
                provenance_evidence="The supplied asset is declared as a fictional character design.",
                frame_evidence=[{"frame_id": "frame-01", "observation": "Human-like rendered character."}],
                confirm=True,
            )
            project = {"title": "Near-real anime", "story": "A fictional hero", "shots": []}
            resolved = apply_visual_review_receipt(
                project, evidence["manifest_path"], reviewed["receipt_path"], confirm=True
            )
            self.assertEqual(resolved["subject_nature"], "human-like-fictional")
            self.assertNotEqual(resolved["subject_nature"], "real-person")
            self.assertEqual(project["visual_profile"]["pixel_review"]["decision"], "accepted")

    def test_external_benchmark_is_deterministic_and_keeps_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset"
            self._image(dataset / "0图片" / "动漫" / "b.jpg", "blue")
            self._image(dataset / "0图片" / "动漫" / "a.jpg", "red")
            self._video(dataset / "1视频" / "真人" / "a.mp4")
            output = root / "acceptance"
            result = build_visual_benchmark(dataset, output, per_group=1, frame_count=2)
            self.assertEqual(result["report"]["summary"]["total"], 2)
            self.assertEqual(result["report"]["summary"]["pending"], 2)
            self.assertFalse(result["report"]["guardrails"]["external_media_copied_into_repository"])
            copied_media = [item for item in output.rglob("*") if item.suffix.lower() in {".mp4", ".png"}]
            self.assertEqual(copied_media, [])

    def test_cli_evidence_review_and_apply_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            initialized = subprocess.run(
                [sys.executable, str(CLI), "init", str(project), "--title", "Review", "--topic", "Anime", "--shots", "1"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            image = self._image(project / "assets" / "references" / "near-real-anime.jpg", "blue")
            extracted = subprocess.run(
                [sys.executable, str(CLI), "visual-evidence", str(project), str(image), "--frames", "3"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertEqual(extracted.returncode, 0, extracted.stdout + extracted.stderr)
            evidence_payload = json.loads(extracted.stdout)
            manifest = Path(evidence_payload["evidence"][0]["manifest"])
            recorded = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "visual-review-record",
                    str(manifest),
                    "--reviewer-id",
                    "cli-test",
                    "--medium",
                    "photoreal",
                    "--subject",
                    "fictional-character",
                    "--subject-nature",
                    "human-like-fictional",
                    "--realism",
                    "high",
                    "--confidence",
                    "0.9",
                    "--identity-origin",
                    "fictional-design",
                    "--frame-evidence",
                    "frame-01=Near-real rendered fictional character.",
                    "--confirm",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
            receipt = Path(json.loads(recorded.stdout)["path"])
            applied = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "visual-review-apply",
                    str(project),
                    str(manifest),
                    str(receipt),
                    "--confirm",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            profile = json.loads(applied.stdout)["visual_profile"]
            self.assertEqual(profile["subject_nature"], "human-like-fictional")
            self.assertEqual(profile["pixel_review"]["reviewer"]["id"], "cli-test")
            self.assertEqual(profile["pixel_review"]["receipt_locator"]["scope"], "project")
            saved_project = json.loads((project / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_project_pixel_review(project, saved_project), [])
            original_source = image.read_bytes()
            image.write_bytes(original_source + b"changed")
            self.assertTrue(any("visual source" in error for error in validate_project_pixel_review(project, saved_project)))
            image.write_bytes(original_source)
            immutable_receipt = project / profile["pixel_review"]["receipt_locator"]["value"]
            immutable_receipt.write_bytes(immutable_receipt.read_bytes() + b"changed")
            self.assertTrue(any("receipt digest" in error for error in validate_project_pixel_review(project, saved_project)))


if __name__ == "__main__":
    unittest.main()
