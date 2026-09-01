from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
CLI = SKILL_ROOT / "scripts" / "grok_video_studio.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from visual_profiles import (  # noqa: E402
    apply_visual_profile,
    classify_visual_text,
    resolve_visual_profile,
    validate_visual_profile,
    visual_prompt_direction,
)
import grok_video_studio as gvs  # noqa: E402


class VisualProfileTests(unittest.TestCase):
    def test_medium_classifier_covers_distinct_generation_policies(self) -> None:
        cases = {
            "photoreal": "真人演员，真实摄影和自然皮肤纹理",
            "2d-anime": "2D anime heroine with cel shading and stable line art",
            "3d-anime": "三渲二 3D anime character with a toon render",
            "stylized-3d": "stylized 3D cartoon character in a CGI animation",
            "motion-graphics": "动态图形 motion graphics infographic with kinetic typography",
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                result = classify_visual_text(text)
                self.assertEqual(result["medium"], expected)
                self.assertGreaterEqual(result["confidence"], 0.7)

    def test_ambiguous_or_unknown_input_requires_review(self) -> None:
        mixed = classify_visual_text("真人演员走进 2D anime hand-drawn world")
        self.assertEqual(mixed["medium"], "hybrid")
        self.assertTrue(mixed["review_required"])

        unknown = classify_visual_text("A quiet story about time.")
        self.assertEqual(unknown["confidence"], 0.25)
        self.assertTrue(unknown["review_required"])

    def test_reference_filename_is_declared_as_non_pixel_evidence(self) -> None:
        result = classify_visual_text("portrait", reference_paths=["refs/真人-女演员-01.png"])
        self.assertEqual(result["medium"], "photoreal")
        self.assertEqual(result["source"], "text-and-reference-name-heuristic")
        self.assertIn("真人-女演员-01.png", result["evidence"]["reference_names"])
        self.assertIn("No image or video pixels were inspected.", result["limitations"])

    def test_near_real_anime_is_fictional_not_a_real_person(self) -> None:
        result = classify_visual_text("3D anime heroine based on a real actor reference, realistic human-like character render")
        self.assertEqual(result["medium"], "3d-anime")
        self.assertEqual(result["subject"], "fictional-character")
        self.assertEqual(result["subject_nature"], "human-like-fictional")
        self.assertNotEqual(result["subject_nature"], "real-person")

    def test_heroine_is_an_explicit_fictional_subject_term(self) -> None:
        result = classify_visual_text("near-real 3D anime heroine")
        self.assertEqual(result["medium"], "3d-anime")
        self.assertEqual(result["subject"], "fictional-character")
        self.assertEqual(result["subject_nature"], "human-like-fictional")
        self.assertGreaterEqual(result["subject_confidence"], 0.7)

    def test_low_subject_confidence_requires_review_even_when_medium_is_clear(self) -> None:
        result = classify_visual_text("volumetric 3D anime render with no declared subject")
        self.assertEqual(result["medium"], "3d-anime")
        self.assertLess(result["subject_confidence"], 0.7)
        self.assertTrue(result["review_required"])

    def test_synthetic_photoreal_human_is_not_a_real_person(self) -> None:
        result = classify_visual_text("photoreal original adult actor, an AI-generated person in live-action visual grammar")
        self.assertEqual(result["medium"], "photoreal")
        self.assertEqual(result["subject_nature"], "synthetic-human")
        self.assertNotEqual(result["subject_nature"], "real-person")

    def test_real_person_requires_actual_identity_evidence(self) -> None:
        generic = classify_visual_text("真人演员，真实摄影和自然皮肤纹理")
        confirmed_source = classify_visual_text("live-action portrait using a supplied real-person photo")
        self.assertEqual(generic["subject_nature"], "unknown")
        self.assertTrue(generic["review_required"])
        self.assertEqual(confirmed_source["subject_nature"], "real-person")

    def test_synthetic_human_policy_guards_contact_physics_and_identity_claims(self) -> None:
        project = {
            "title": "Generated actor",
            "story": "A photoreal AI-generated person waters a plant.",
            "visual_profile": {
                "version": 1,
                "mode": "manual",
                "medium": "photoreal",
                "subject": "human",
                "subject_nature": "synthetic-human",
                "realism": "high",
                "identity_strictness": "high",
                "performance_complexity": "medium",
                "confirmed": True,
            },
            "shots": [],
        }
        resolved = resolve_visual_profile(project)
        policy = resolved["generation_policy"]
        self.assertIn("AI-generated human", policy["prompt_direction"])
        self.assertIn("real-person identity claims", policy["avoid"])
        self.assertIn("prop contact, liquids, cloth, and physical interaction", policy["qa_priorities"])

    def test_photoreal_appearance_without_reality_evidence_requires_review(self) -> None:
        result = classify_visual_text("photoreal human-like portrait with detailed skin")
        self.assertEqual(result["medium"], "photoreal")
        self.assertEqual(result["subject_nature"], "unknown")
        self.assertTrue(result["review_required"])

    def test_manual_profile_wins_over_auto_analysis(self) -> None:
        project = {"title": "Photo", "topic": "真人", "story": "真人演员", "shots": []}
        resolved = apply_visual_profile(
            project,
            mode="manual",
            medium="2d-anime",
            subject="fictional-character",
            confirmed=True,
        )
        self.assertEqual(resolved["medium"], "2d-anime")
        self.assertEqual(resolved["subject_nature"], "human-like-fictional")
        self.assertEqual(resolved["realism"], "low")
        self.assertEqual(resolved["source"], "manual-project-contract")
        self.assertFalse(resolved["review_required"])
        self.assertEqual(resolve_visual_profile(project)["medium"], "2d-anime")

    def test_confirmed_manual_medium_does_not_confirm_unknown_auto_nature(self) -> None:
        project = {"title": "Portrait", "topic": "人物", "story": "A portrait", "shots": []}
        resolved = apply_visual_profile(
            project,
            mode="manual",
            medium="photoreal",
            subject="human",
            confirmed=True,
        )
        self.assertEqual(resolved["medium"], "photoreal")
        self.assertEqual(resolved["subject_nature"], "unknown")
        self.assertTrue(resolved["review_required"])

    def test_confirmed_explicit_nature_resolves_manual_profile(self) -> None:
        project = {"title": "Generated portrait", "story": "An AI-generated person", "shots": []}
        resolved = apply_visual_profile(
            project,
            mode="manual",
            medium="photoreal",
            subject="human",
            subject_nature="synthetic-human",
            confirmed=True,
        )
        self.assertFalse(resolved["review_required"])

    def test_old_project_without_visual_profile_is_compatible(self) -> None:
        project = {"title": "Legacy", "topic": "Unknown", "story": "A scene", "shots": []}
        self.assertEqual(validate_visual_profile(project), [])
        resolved = resolve_visual_profile(project)
        self.assertEqual(resolved["mode"], "auto")
        self.assertTrue(resolved["review_required"])

    def test_profile_policy_is_added_to_prompt_direction(self) -> None:
        project = {
            "visual_profile": {
                "version": 1,
                "mode": "manual",
                "medium": "motion-graphics",
                "subject": "graphic",
                "realism": "not-applicable",
                "identity_strictness": "low",
                "performance_complexity": "low",
                "confirmed": True,
            }
        }
        direction = visual_prompt_direction(project)
        self.assertIn("motion-graphics", direction)
        self.assertIn("legible hierarchy", direction)
        self.assertIn("generated text", direction)

    def test_animation_clean_frame_and_prompt_never_claim_photography(self) -> None:
        project = {
            "visual_profile": {
                "version": 1,
                "mode": "manual",
                "medium": "3d-anime",
                "subject": "fictional-character",
                "subject_nature": "human-like-fictional",
                "realism": "medium",
                "identity_strictness": "high",
                "performance_complexity": "medium",
                "confirmed": True,
            },
            "audio": {"mode": "native-dialogue", "subtitle_source": "none"},
            "allow_ui_elements": False,
            "frame_layout": "single-full-frame",
            "allow_multi_panel": False,
            "director": {"mode": "single-shot", "project_type": "single-clip", "genre_packs": []},
            "characters": [],
            "character_bible": "",
            "style_bible": "",
        }
        shot = {
            "id": "shot-001",
            "summary": "A fictional character turns.",
            "shot_role": "medium",
            "video_prompt": "The fictional character turns toward a train.",
            "image_prompt": "A clearly 3D anime character render.",
            "seconds": 6,
            "exit_behavior": "continue-action",
            "dialogue": [],
        }
        full = gvs.composed_video_prompt(project, shot)
        minimal = gvs.prompt_variants(project, shot, kind="video")["minimal"]
        self.assertIn("Animation camera original", full)
        self.assertIn("three-dimensional CG-rendered anime", full)
        self.assertNotIn("photographed physical scene", full + minimal)
        self.assertNotIn("feature-film photography", full + minimal)
        self.assertIn("feature-film animation in the declared 3D anime medium", full + minimal)

    def test_display_progress_is_monotonic_and_caps_queued_false_completion(self) -> None:
        self.assertEqual(gvs.normalized_task_progress("queued", 100.0, None), 20.0)
        self.assertEqual(gvs.normalized_task_progress("queued", 25.0, 20.0), 20.0)
        self.assertEqual(gvs.normalized_task_progress("processing", 25.0, 20.0), 25.0)
        self.assertEqual(gvs.normalized_task_progress("processing", 15.0, 25.0), 25.0)
        self.assertEqual(gvs.normalized_task_progress("done", 12.0, 25.0), 100.0)

    def test_cli_init_and_manual_apply_persist_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            init = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "init",
                    str(root),
                    "--title",
                    "Demo",
                    "--topic",
                    "Anime",
                    "--shots",
                    "1",
                    "--seconds",
                    "6",
                    "--visual-medium",
                    "2d-anime",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            value = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(value["visual_profile"]["medium"], "2d-anime")
            self.assertEqual(value["frame_layout"], "single-full-frame")

            applied = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "visual-profile-apply",
                    str(root),
                    "--mode",
                    "manual",
                    "--medium",
                    "stylized-3d",
                    "--subject",
                    "mascot",
                    "--confirm",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            payload = json.loads(applied.stdout)
            self.assertEqual(payload["visual_profile"]["medium"], "stylized-3d")
            self.assertFalse(payload["visual_profile"]["review_required"])

    def test_migrate_adds_optional_fields_once_and_preserves_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            init = subprocess.run(
                [sys.executable, str(CLI), "init", str(root), "--title", "Legacy", "--topic", "Test", "--shots", "1", "--seconds", "6"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            value = json.loads((root / "project.json").read_text(encoding="utf-8"))
            value["story"] = "A legacy project with one valid shot."
            value["shots"][0]["image_prompt"] = "One clean keyframe."
            value["shots"][0]["video_prompt"] = "One gentle camera move."
            value.pop("visual_profile")
            value["limits"]["max_prompt_chars"] = value["limits"].pop("max_prompt_bytes")
            (root / "project.json").write_text(json.dumps(value), encoding="utf-8")

            first = subprocess.run(
                [sys.executable, str(CLI), "migrate", str(root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            migrated = json.loads(first.stdout)
            self.assertTrue(migrated["changed"])
            self.assertTrue((root / "project.pre-v2.2.json").is_file())
            current = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertIn("visual_profile", current)
            self.assertIn("max_prompt_bytes", current["limits"])

            second = subprocess.run(
                [sys.executable, str(CLI), "migrate", str(root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse(json.loads(second.stdout)["changed"])


if __name__ == "__main__":
    unittest.main()
