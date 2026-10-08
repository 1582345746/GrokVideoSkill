from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

import grok_video_studio as gvs  # noqa: E402
from content_reviews import LAYERS, apply_review_receipt, create_review_template  # noqa: E402
from director_contracts import director_gate, story_clarity_score  # noqa: E402
from gvs_common import APIError, SkillError  # noqa: E402


class ScopedPaidGateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "assets" / "keyframes").mkdir(parents=True)
        self.image = self.root / "assets" / "keyframes" / "shot-001.png"
        self.image.write_bytes(b"fixture-image-bytes")
        self.project = {
            "prompt_compiler": "scoped-v1", "video_mode": "image-to-video", "video_provider": "newapi",
            "video_provider_policy": "fixed", "story": "A crate slides onto a table.",
            "prompt_style": {"medium": "photoreal"},
            "audio": {"mode": "native-dialogue", "subtitle_source": "none"},
            "shots": [{"id": "shot-001", "generate_image": True, "character_ids": [], "seconds": 5,
                       "image_prompt": "A crate on a table.", "video_prompt": "The crate slides onto a table.",
                       "video_references": []}],
        }
        self.state = gvs.fresh_state()
        runtime = gvs.shot_state(self.state, "shot-001")["image"]
        runtime.update(status="completed", path="assets/keyframes/shot-001.png", review_status="pending")
        self.provider = mock.Mock()
        self.provider.create.side_effect = RuntimeError("fixture-paid-create-boundary")

    def run_with_fake_provider(self) -> Exception:
        with mock.patch.object(gvs, "require_valid_project", return_value=self.project), \
             mock.patch.object(gvs, "load_state", return_value=self.state), \
             mock.patch.object(gvs, "clients", return_value=(mock.Mock(), mock.Mock(), {"video_model": "fixture"})), \
             mock.patch.object(gvs, "video_provider_candidates", return_value=["newapi"]), \
             mock.patch.object(gvs, "select_video_client", return_value=self.provider), \
             mock.patch.object(gvs, "save_state"), mock.patch.object(gvs, "write_event"):
            try:
                gvs.generate_videos(self.root, retry_failed=False, poll_timeout=1)
            except Exception as error:
                return error
        self.fail("fixture provider should stop before network generation")

    def approve_current_keyframe(self) -> None:
        gvs.shot_state(self.state, "shot-001")["image"].update(
            review_status="approved", reviewed_sha256=gvs.file_digest(self.image),
        )

    def test_scoped_pending_keyframe_blocks_before_paid_create(self) -> None:
        error = self.run_with_fake_provider()
        self.assertIsInstance(error, SkillError)
        self.assertIn("approve", str(error))
        self.provider.create.assert_not_called()

    def test_scoped_approval_is_invalidated_when_pixels_change(self) -> None:
        self.approve_current_keyframe()
        self.image.write_bytes(b"changed pixels after approval")
        self.run_with_fake_provider()
        self.provider.create.assert_not_called()

    def test_scoped_approved_current_keyframe_can_reach_paid_boundary(self) -> None:
        self.approve_current_keyframe()
        error = self.run_with_fake_provider()
        self.assertIn("fixture-paid-create-boundary", str(error))
        self.provider.create.assert_called_once()

    def test_legacy_does_not_inherit_new_keyframe_approval_gate(self) -> None:
        self.project["prompt_compiler"] = "legacy"
        error = self.run_with_fake_provider()
        self.assertIn("fixture-paid-create-boundary", str(error))
        self.provider.create.assert_called_once()

    def test_explicit_reference_cannot_bypass_approval_by_disabling_generation(self) -> None:
        self.project["shots"][0].update(generate_image=False, video_references=["assets/keyframes/shot-001.png"])
        self.run_with_fake_provider()
        self.provider.create.assert_not_called()

    def test_unapproved_explicit_image_cannot_borrow_another_images_approval(self) -> None:
        self.approve_current_keyframe()
        other = self.image.with_name("unapproved.png")
        other.write_bytes(b"different unreviewed image")
        self.project["shots"][0]["video_references"] = ["assets/keyframes/unapproved.png"]
        self.run_with_fake_provider()
        self.provider.create.assert_not_called()

    def test_provider_length_rejection_does_not_resubmit_identical_scoped_prompt(self) -> None:
        self.approve_current_keyframe()
        self.provider.create.side_effect = APIError(400, "prompt too long")
        error = self.run_with_fake_provider()
        self.assertIsInstance(error, SkillError)
        self.assertIn("no shorter distinct prompt", str(error))
        self.provider.create.assert_called_once()
        video = gvs.shot_state(self.state, "shot-001")["video"]
        self.assertEqual(video["attempts"], 1)
        self.assertEqual(video["error_category"], "prompt_too_long")

    def test_size_repair_still_retries_with_changed_parameter_for_scoped_prompt(self) -> None:
        self.approve_current_keyframe()
        self.provider.create.side_effect = [APIError(400, "size and aspect ratio conflict"), RuntimeError("fixture-paid-create-boundary")]
        error = self.run_with_fake_provider()
        self.assertIn("fixture-paid-create-boundary", str(error))
        self.assertEqual(self.provider.create.call_count, 2)
        first, second = self.provider.create.call_args_list
        self.assertEqual(first.args[0], second.args[0])
        self.assertNotEqual(first.kwargs["size"], second.kwargs["size"])
        self.assertEqual(second.kwargs["size"], "auto")

    def test_length_repair_skips_duplicate_variants_to_actual_shorter_contract(self) -> None:
        variants = {"full": "long original prompt", "compact": "long original prompt", "minimal": "short"}
        self.assertEqual(gvs.next_prompt_version(variants, "full", require_shorter=True), "minimal")
        with self.assertRaisesRegex(SkillError, "no shorter distinct prompt"):
            gvs.next_prompt_version({name: "same" for name in ("full", "compact", "minimal")}, "full", require_shorter=True)


class DirectorIntegrationRegressionTests(unittest.TestCase):
    def project(self) -> dict:
        return {
            "story_contract": {"required": True, **{k: "不相关" for k in ("goal", "obstacle", "decision", "consequence", "payoff")}},
            "director": {"mode": "dialogue-scene", "strict": True, "boundary_policy": "block"},
            "story_beats": [{"id": "beat", **{k: "不相关" for k in ("visible_event", "audience_effect", "consequence", "why_next")}}],
            "shots": [{
                "id": f"shot-{i:03d}", "summary": "不相关", "shot_role": role,
                "continuity_in": "不相关", "continuity_out": "不相关", "exit_action": "in motion", "entry_action": "in motion",
                "dialogue": [{"speaker": "lead", "text": "说话"}],
            } for i, role in enumerate(("wide", "reaction", "ending_hook"), 1)],
        }

    def test_reaction_coverage_can_carry_dialogue_without_artificial_silent_shot(self) -> None:
        self.assertEqual(director_gate(self.project())["errors"], [])

    def test_irrelevant_full_fields_are_only_schema_coverage_never_semantic_proof(self) -> None:
        report = story_clarity_score(self.project())
        self.assertEqual(report["score_kind"], "schema-coverage-only")
        self.assertIn("unverified", report["semantic_quality"])


class ContentQaClosureIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "clips").mkdir()
        (self.root / "deliverables").mkdir()
        (self.root / "clips/shot-001.mp4").write_bytes(b"fixture clip")
        (self.root / "deliverables/final.mp4").write_bytes(b"fixture final")
        self.project = {
            "prompt_compiler": "scoped-v1",
            "audio": {"mode": "native-dialogue", "subtitle_source": "none", "generate_audio": True},
            "visual_profile": {"mode": "manual", "medium": "photoreal", "confirmed": True},
            "shots": [{"id": "shot-001", "seconds": 5, "edit_in": 0.1, "edit_out": 2,
                       "character_ids": ["lead"], "dialogue": [{"id": "line", "speaker": "lead", "text": "鸭子跑了！", "start": 0.2, "end": 1.7}]}],
        }
        (self.root / "project.json").write_text(json.dumps(self.project), encoding="utf-8")
        self.state = gvs.fresh_state()
        video = gvs.shot_state(self.state, "shot-001")["video"]
        video.update(status="completed", path="clips/shot-001.mp4", review_status="approved",
                     reviewed_sha256=gvs.file_digest(self.root / "clips/shot-001.mp4"))
        self.state["deliverables"]["final"] = {"path": "deliverables/final.mp4"}
        self.receipt = json.loads(create_review_template(self.root, self.project, self.state).read_text(encoding="utf-8-sig"))
        self.receipt["reviewer_id"] = "fixture-reviewer"
        for asset in self.receipt["assets"].values():
            for layer in LAYERS:
                asset["layers"][layer] = {"status": "approved", "method": "human-listening" if layer == "audio" else "human-video", "evidence": [f"Observed {layer} over retained clip."]}
        self.receipt["assets"]["final"]["understanding"] = {"goal": "Catch duck", "obstacle": "Duck loose", "choice": "Close door", "consequence": "Exit closed"}

    def qa(self, required: bool) -> dict:
        report = {
            "ok": True, "errors": [], "warnings": [],
            "media": {"duration": 5, "has_audio": True, "has_subtitles": False},
            "signals": {"audio": {"max_volume_db": -10, "silence_ratio": 0},
                        "text_overlay": {"available": False, "detected": False},
                        "duration": {"within_tolerance": True}},
        }
        with mock.patch.object(gvs, "require_valid_project", return_value=self.project), \
             mock.patch.object(gvs, "load_state", return_value=self.state), \
             mock.patch.object(gvs, "quality_report", side_effect=lambda *a, **k: copy.deepcopy(report)), \
             mock.patch.object(gvs, "export_review_frames", return_value=[]), \
             mock.patch.object(gvs, "audit_project", return_value={"ok": True, "warnings": [], "story": {"score": 100, "threshold": 70}}), \
             mock.patch.object(gvs, "event_flow_report", return_value={"required": required}), \
             mock.patch.object(gvs, "save_state"):
            return gvs.qa_project(self.root)

    def apply(self) -> None:
        path = self.root / "deliverables/review.json"
        path.write_text(json.dumps(self.receipt), encoding="utf-8")
        apply_review_receipt(self.root, self.project, self.state, path)

    def test_technical_pass_with_no_content_review_cannot_be_delivery_pass(self) -> None:
        report = self.qa(required=True)
        self.assertTrue(report["technical_pass"])
        self.assertFalse(report["delivery_pass"])
        self.assertIsNone(report["narrative_pass"])
        self.assertIsNone(report["audio_pass"])

    def test_real_layer_receipts_close_audio_and_clean_frame_when_ocr_unavailable(self) -> None:
        self.apply()
        report = self.qa(required=True)
        self.assertTrue(report["audio_pass"])
        self.assertTrue(report["clean_frame_pass"])
        self.assertTrue(report["delivery_pass"])

    def test_observed_failure_cannot_be_overridden_by_technical_or_other_layer_success(self) -> None:
        self.receipt["assets"]["shot-001"]["layers"]["audio"].update(status="rejected", evidence=["Wrong character speaks at 0.3s."])
        self.apply()
        report = self.qa(required=True)
        self.assertTrue(report["technical_pass"])
        self.assertIs(report["audio_pass"], False)
        self.assertFalse(report["delivery_pass"])

    def test_visual_content_rejection_is_consumed_even_when_event_gate_is_not_required(self) -> None:
        self.receipt["assets"]["shot-001"]["layers"]["visual"].update(status="rejected", evidence=["Hand penetrates the crate at 0.4s."])
        self.apply()
        report = self.qa(required=False)
        self.assertIs(report["visual_pass"], False)
        self.assertFalse(report["delivery_pass"])

    def test_final_state_records_closed_delivery_gate_for_stage_status(self) -> None:
        self.apply()
        report = self.qa(required=True)
        self.assertTrue(report["delivery_pass"])
        self.assertTrue(self.state["deliverables"]["final"]["qa"].get("delivery_pass"))

    def test_unexpected_speech_in_planned_silent_insert_is_still_an_audio_failure(self) -> None:
        self.project["shots"][0]["dialogue"] = []
        (self.root / "project.json").write_text(json.dumps(self.project), encoding="utf-8")
        self.receipt["project_sha256"] = gvs.file_digest(self.root / "project.json")
        self.receipt["assets"]["shot-001"]["layers"]["audio"].update(
            status="rejected", evidence=["The insert unexpectedly contains a man's unplanned line at 0.5s."],
        )
        self.apply()
        report = self.qa(required=True)
        self.assertIs(report["audio_pass"], False)
        self.assertFalse(report["delivery_pass"])

    def test_status_cannot_reuse_delivery_approval_after_source_pixels_changed(self) -> None:
        self.apply()
        self.assertTrue(self.qa(required=True)["delivery_pass"])
        (self.root / "clips/shot-001.mp4").write_bytes(b"unreviewed changed clip")
        with mock.patch.object(gvs, "validate_project", return_value=[]):
            pipeline = gvs.pipeline_status(self.root, self.project, self.state)
        self.assertNotEqual(pipeline["phases"]["delivery"], "completed")

    def test_silent_scoped_or_event_project_stays_unverified_until_audio_review(self) -> None:
        self.project["shots"][0]["dialogue"] = []
        for compiler, required in (("scoped-v1", False), ("legacy", True)):
            with self.subTest(compiler=compiler, event_required=required):
                self.project["prompt_compiler"] = compiler
                (self.root / "project.json").write_text(json.dumps(self.project), encoding="utf-8")
                self.receipt["project_sha256"] = gvs.file_digest(self.root / "project.json")
                for asset in self.receipt["assets"].values():
                    asset["layers"]["audio"] = {"status": "unverified", "method": "", "evidence": []}
                self.apply()
                report = self.qa(required=required)
                self.assertTrue(report["technical_pass"])
                self.assertIsNone(report["audio_pass"])
                self.assertFalse(report["delivery_pass"])
                self.assertFalse(self.state["deliverables"]["final"]["qa"]["delivery_pass"])

    def test_cached_legacy_delivery_requires_receipt_after_enabling_new_review_contract(self) -> None:
        for compiler, event_required, expected in (("scoped-v1", False, "blocked"), ("legacy", True, "blocked"), ("legacy", False, "completed")):
            with self.subTest(compiler=compiler, event_required=event_required):
                self.project["prompt_compiler"] = compiler
                self.project["event_flow"] = {"required": event_required}
                self.state.pop("content_reviews", None)
                self.state["deliverables"]["final"]["qa"] = {"delivery_pass": True}
                with mock.patch.object(gvs, "validate_project", return_value=[]):
                    pipeline = gvs.pipeline_status(self.root, self.project, self.state)
                self.assertEqual(pipeline["phases"]["delivery"], expected)
                self.assertEqual(self.state["deliverables"]["final"]["qa"]["delivery_pass"], expected == "completed")


if __name__ == "__main__":
    unittest.main()
