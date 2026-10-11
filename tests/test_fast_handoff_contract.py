from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

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
