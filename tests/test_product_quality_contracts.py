from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "grok-video-studio" / "scripts"))

from director_contracts import (  # noqa: E402
    continuity_state_diff,
    narrative_contract_coverage,
    story_clarity_score,
    validate_narrative_contract,
)
from dialogue_workflow import validate_dialogue  # noqa: E402
import media_tools  # noqa: E402
import grok_video_studio as gvs  # noqa: E402


class ProductQualityContractTests(unittest.TestCase):
    def test_required_narrative_contract_blocks_incomplete_causal_shot(self) -> None:
        project = {
            "narrative_contract_v2": {
                "required": True,
                "protagonist_goal": "Leave before the flood.",
                "audience_knows": "The bridge is failing.",
                "character_knows": "The warning is real.",
                "choice": "Take the old road.",
                "visible_consequence": "The truck reaches the washed-out turn.",
                "next_question": "Can they cross in time?",
            },
            "shots": [{"id": "shot-001", "summary": "A truck waits."}],
        }
        errors = validate_narrative_contract(project)
        self.assertTrue(any("narrative contract is incomplete" in item for item in errors))
        self.assertEqual(narrative_contract_coverage(project)["complete_shots"], 0)

    def test_complete_narrative_contract_contributes_to_clarity_score(self) -> None:
        project = {
            "narrative_contract_v2": {
                "required": True,
                "protagonist_goal": "Leave before the flood.",
                "audience_knows": "The bridge is failing.",
                "character_knows": "The warning is real.",
                "choice": "Take the old road.",
                "visible_consequence": "The truck reaches the washed-out turn.",
                "next_question": "Can they cross in time?",
            },
            "shots": [{
                "id": "shot-001",
                "summary": "The driver turns toward the old road.",
                "motivation": "The bridge alarm sounds.",
                "result": "The truck leaves the main road.",
                "next_reason": "The old road leads toward the hill.",
            }],
        }
        self.assertEqual(validate_narrative_contract(project), [])
        self.assertGreaterEqual(story_clarity_score(project)["score"], 70)

    def test_continuity_diff_exposes_unannounced_asset_change(self) -> None:
        diff = continuity_state_diff(
            {"scene_state": {"weather": "clear"}, "asset_state": {"crate_count": 2}},
            {"scene_state": {"weather": "rain"}, "asset_state": {"crate_count": 1}},
        )
        self.assertTrue(diff["has_diff"])
        self.assertEqual({item["field"] for item in diff["changed"]}, {"scene_state.weather", "asset_state.crate_count"})

    def test_narration_cue_outside_shot_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = {
                "audio": {"mode": "narration", "generate_audio": True, "generate_speech": True},
                "shots": [{
                    "id": "shot-001",
                    "seconds": 2,
                    "narration": "The bridge fails.",
                    "narration_cues": [{"start": 0.5, "end": 2.5, "text": "The bridge fails."}],
                }],
                "characters": [],
            }
            errors = validate_dialogue(Path(temp_dir), project)
        self.assertTrue(any("narration_cues" in item and "shot seconds" in item for item in errors))

    def test_duration_policy_can_block_short_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clip.mp4"
            path.write_bytes(b"placeholder")
            media = {
                "path": str(path),
                "duration": 28.0,
                "has_audio": True,
                "width": 720,
                "height": 1280,
                "codec": "h264",
                "pixel_format": "yuv420p",
            }
            with mock.patch.object(media_tools, "probe_media", return_value=media), mock.patch.object(media_tools.shutil, "which", return_value=None):
                report = media_tools.quality_report(
                    path,
                    expected_duration=30,
                    duration_policy="block",
                    duration_tolerance_seconds=0.5,
                    duration_tolerance_ratio=0.05,
                )
        self.assertFalse(report["ok"])
        self.assertFalse(report["signals"]["duration"]["within_tolerance"])

    def test_caption_block_fails_when_ocr_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clip.mp4"
            path.write_bytes(b"placeholder")
            media = {
                "path": str(path),
                "duration": 1.0,
                "has_audio": False,
                "width": 320,
                "height": 240,
                "codec": "h264",
                "pixel_format": "yuv420p",
            }
            with mock.patch.object(media_tools, "probe_media", return_value=media), mock.patch.object(media_tools.shutil, "which", return_value=None):
                report = media_tools.quality_report(path, caption_detection="block")
        self.assertFalse(report["ok"])
        self.assertTrue(any("OCR/text detection is unavailable" in item for item in report["errors"]))

    def test_pipeline_status_exposes_staged_delivery_state(self) -> None:
        project = {
            "story": "A staged project",
            "shots": [{"id": "shot-001"}],
            "character_master": {"enabled": False},
        }
        state = {
            "character_master": {"status": "pending"},
            "shots": {"shot-001": {"image": {"status": "completed", "path": "assets/keyframes/shot-001.png", "sha256": "abc"}, "video": {"status": "pending"}}},
            "deliverables": {},
        }
        status = gvs.pipeline_status(Path("."), project, state)
        self.assertEqual(status["phases"]["keyframes"], "completed")
        self.assertEqual(status["phases"]["clips"], "pending")
        self.assertEqual(status["current_phase"], "preflight")
        registry = gvs.asset_registry_from_state(state)
        self.assertEqual(registry["shot-001:image"]["sha256"], "abc")


if __name__ == "__main__":
    unittest.main()
