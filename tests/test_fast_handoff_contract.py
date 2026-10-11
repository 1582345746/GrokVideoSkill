from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

import grok_video_studio as gvs  # noqa: E402
from grok_video_studio import SkillError, previous_shot_keyframe, prompt_variants, validate_project  # noqa: E402
from scoped_prompts import compile_scoped_prompt  # noqa: E402


class FastHandoffContractTests(unittest.TestCase):
    def _project(self) -> dict:
        return {
            "workflow": "fast-continuous-event-dialogue",
            "prompt_compiler": "scoped-v1",
            "quality": {"previous_keyframe_reference": True},
            "continuity": {"enabled": True, "handoff_policy": "reviewed-clip-exit"},
            "characters": [{"id": "lead", "name": "Lead", "identity": "A stable adult", "wardrobe": "blue coat"}],
            "shots": [
                {"id": "shot-001", "scene_id": "station", "handoff_source": "", "video_prompt": "The lead runs."},
                {
                    "id": "shot-002",
                    "scene_id": "station",
                    "handoff_source": "reviewed-clip-exit",
                    "character_ids": ["lead"],
                    "video_prompt": "The lead reaches the gate.",
                    "action_description": "The lead reaches the gate.",
                },
            ],
        }

    def test_reviewed_clip_exit_does_not_fallback_to_opening_keyframe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self._project()
            state = {"shots": {"shot-001": {"video": {}}}}
            self.assertIsNone(previous_shot_keyframe(root, project, state, "shot-002"))
            with self.assertRaisesRegex(SkillError, "approved, unchanged previous video"):
                previous_shot_keyframe(root, project, state, "shot-002", require_reviewed_clip=True)

    def test_scoped_video_prompt_declares_real_exit_handoff_and_no_settling(self) -> None:
        project = self._project()
        prompt = compile_scoped_prompt(project, project["shots"][1], kind="video")
        self.assertIn("reviewed-clip-exit", prompt)
        self.assertIn("do not reset into a new pose", prompt)
        self.assertIn("No sigh, farewell, nod to conclude", prompt)

    def test_fast_project_blocks_non_final_missing_cuttable_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self._project()
            project["continuity"]["handoff_policy"] = "reviewed-clip-exit"
            errors = validate_project(root, project)
            self.assertTrue(any("exit_action is required" in error for error in errors))

    def test_fast_workflow_cannot_disable_reviewed_handoff_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for section, key in (("continuity", "enabled"), ("quality", "previous_keyframe_reference")):
                project = self._project()
                project.setdefault(section, {})[key] = False
                errors = validate_project(root, project)
                with self.subTest(section=section):
                    self.assertTrue(any("requires" in error and key in error for error in errors))

            project = self._project()
            project["continuity"]["handoff_policy"] = "keyframe"
            errors = validate_project(root, project)
            self.assertTrue(any("handoff_policy=reviewed-clip-exit" in error for error in errors))

    def test_required_clip_exit_cannot_be_silently_disabled(self) -> None:
        project = self._project()
        project["continuity"]["enabled"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SkillError, "cannot be produced while continuity/reference controls are disabled"):
                previous_shot_keyframe(
                    Path(temp_dir), project, {"shots": {}}, "shot-002", require_reviewed_clip=True
                )

    def test_generate_image_false_resolves_only_the_reviewed_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            handoff = root / "assets" / "handoffs" / "shot-001-exit-000.png"
            handoff.parent.mkdir(parents=True)
            handoff.write_bytes(b"reviewed exit")
            stale = root / "assets" / "keyframes" / "shot-002.png"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale current frame")
            project = self._project()
            project["video_mode"] = "image-to-video"
            project["shots"][1]["generate_image"] = False
            project["shots"][1]["video_references"] = []
            state = {"shots": {"shot-002": {"image": {"status": "completed", "path": str(stale.relative_to(root))}}}}
            with mock.patch.object(gvs, "previous_shot_keyframe", return_value=handoff):
                references, handoff_only = gvs.resolved_video_references(root, project, state, project["shots"][1])
            self.assertTrue(handoff_only)
            self.assertEqual(references, [handoff])

    def test_shot_mode_override_allows_t2v_opening_with_reviewed_i2v_followup(self) -> None:
        project = self._project()
        project["video_mode"] = "image-to-video"
        project["shots"][0].update(video_mode="text-to-video", generate_image=False)
        project["shots"][1].update(generate_image=False, video_references=[])

        self.assertEqual(gvs.shot_video_mode(project, project["shots"][0]), "text-to-video")
        self.assertEqual(gvs.shot_video_mode(project, project["shots"][1]), "image-to-video")
        self.assertEqual(gvs.shots_for_video_mode(project, "text-to-video"), [project["shots"][0]])
        self.assertEqual(gvs.shots_for_video_mode(project, "image-to-video"), [project["shots"][1]])
        self.assertFalse(gvs.reviewed_clip_exit_only(project, project["shots"][0]))
        self.assertTrue(gvs.reviewed_clip_exit_only(project, project["shots"][1]))
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = validate_project(Path(temp_dir), project)
        self.assertFalse(any("opening keyframe" in error for error in errors))

    def test_fast_workflow_rejects_t2v_after_the_opening_shot(self) -> None:
        project = self._project()
        project["shots"][1]["video_mode"] = "text-to-video"
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = validate_project(Path(temp_dir), project)
        self.assertTrue(any("must be image-to-video after the opening shot" in error for error in errors))

    def test_provider_candidates_check_the_effective_shot_mode(self) -> None:
        project = self._project()
        settings = {"newapi_video_key": "configured"}
        self.assertEqual(gvs.video_provider_candidates(project, settings, mode="text-to-video"), ["newapi"])

    def test_generate_image_false_rejects_explicit_reference_in_fast_i2v(self) -> None:
        project = self._project()
        project["video_mode"] = "image-to-video"
        project["shots"][1]["generate_image"] = False
        project["shots"][1]["video_references"] = ["assets/references/old.png"]
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = validate_project(Path(temp_dir), project)
        self.assertTrue(any("video_references must be empty when generate_image=false" in error for error in errors))

    def test_handoff_only_scoped_gate_rejects_non_exit_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "assets" / "keyframes" / "shot-002.png"
            current.parent.mkdir(parents=True)
            current.write_bytes(b"current")
            with self.assertRaisesRegex(SkillError, "handoff-only I2V"):
                gvs.require_approved_video_references(root, {"shots": {}}, [current], "shot-002", handoff_only=True)

    def test_multi_sheet_prompt_matches_cast_size_in_both_compilers(self) -> None:
        project = self._project()
        project["character_master"] = {"mode": "multi-sheet", "prompt": "Neutral cast sheet."}
        project["characters"] = [
            {"id": "lead", "name": "Lead", "identity": "Adult in a dark coat."},
            {"id": "friend", "name": "Friend", "identity": "Adult in a green coat."},
            {"id": "guard", "name": "Guard", "identity": "Older adult in uniform."},
        ]

        for compiler in ("scoped-v1", "legacy"):
            project["prompt_compiler"] = compiler
            variants = prompt_variants(project, kind="character_master")
            for prompt in variants.values():
                with self.subTest(compiler=compiler):
                    self.assertIn("3", prompt)
                    self.assertIn("multi-character", prompt.lower())
                    self.assertNotIn("one person only", prompt.lower())


if __name__ == "__main__":
    unittest.main()
