from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

from gvs_common import SkillError  # noqa: E402
from content_reviews import (  # noqa: E402
    LAYERS, apply_review_receipt, content_review_summary, create_review_template,
    review_asset_contract, validate_review_receipt,
)


class ContentReviewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "clips").mkdir()
        (self.root / "deliverables").mkdir()
        (self.root / "clips" / "shot-001.mp4").write_bytes(b"original clip bytes")
        (self.root / "deliverables" / "final.mp4").write_bytes(b"original final bytes")
        self.project = {"shots": [{"id": "shot-001", "seconds": 5, "edit_in": 0.1, "edit_out": 2.1}]}
        (self.root / "project.json").write_text(json.dumps(self.project), encoding="utf-8")
        self.state = {
            "shots": {"shot-001": {"video": {"status": "completed", "path": "clips/shot-001.mp4"}}},
            "deliverables": {"final": {"path": "deliverables/final.mp4"}},
        }
        self.receipt = json.loads(create_review_template(self.root, self.project, self.state).read_text(encoding="utf-8-sig"))
        self.receipt["reviewer_id"] = "fixture-human-reviewer"
        methods = {"audio": "human-listening", "clean_frame": "codex-vision"}
        for asset in self.receipt["assets"].values():
            for layer in LAYERS:
                asset["layers"][layer] = {
                    "status": "approved", "method": methods.get(layer, "human-video"),
                    "evidence": [f"Observed {layer} in retained 0.1–2.1 second clip; no corresponding defect detected."],
                }
        self.receipt["assets"]["final"]["understanding"] = {
            "goal": "Catch the duck", "obstacle": "It escaped", "choice": "Close the door", "consequence": "The exit is blocked",
        }

    def apply(self, receipt: dict | None = None) -> None:
        path = self.root / "deliverables" / "observed-review.json"
        path.write_text(json.dumps(receipt or self.receipt), encoding="utf-8")
        apply_review_receipt(self.root, self.project, self.state, path)

    def test_hash_bound_review_can_close_all_independent_layers(self) -> None:
        self.apply()
        summary = content_review_summary(self.root, self.project, self.state)
        self.assertEqual(summary["status"], "reviewed")
        self.assertTrue(all(v is True for v in summary["layers"].values()))

    def test_source_bytes_and_final_bytes_changes_invalidate_prior_approval(self) -> None:
        for relative in ("clips/shot-001.mp4", "deliverables/final.mp4"):
            with self.subTest(relative=relative):
                original = (self.root / relative).read_bytes()
                self.state.pop("content_reviews", None)
                self.apply()
                (self.root / relative).write_bytes(b"regenerated unreviewed bytes")
                summary = content_review_summary(self.root, self.project, self.state)
                self.assertEqual(summary["status"], "stale")
                self.assertTrue(all(v is None for v in summary["layers"].values()))
                (self.root / relative).write_bytes(original)

    def test_window_change_and_project_contract_change_invalidate_review(self) -> None:
        self.apply()
        self.project["shots"][0]["edit_out"] = 2.0
        self.assertEqual(content_review_summary(self.root, self.project, self.state)["status"], "stale")
        self.project["shots"][0]["edit_out"] = 2.1
        (self.root / "project.json").write_text('{"shots":[],"story":"a changed story"}', encoding="utf-8")
        summary = content_review_summary(self.root, self.project, self.state)
        self.assertEqual(summary["status"], "stale")
        self.assertTrue(any("project hash" in e for e in summary["errors"]))

    def test_frame_only_method_cannot_certify_audible_speaker_or_words(self) -> None:
        self.receipt["assets"]["shot-001"]["layers"]["audio"]["method"] = "codex-vision"
        with self.assertRaisesRegex(SkillError, "actual review method"):
            self.apply()
        self.assertNotIn("content_reviews", self.state)

    def test_partial_review_keeps_missing_assets_and_layers_unknown(self) -> None:
        receipt = copy.deepcopy(self.receipt)
        receipt["assets"] = {"shot-001": receipt["assets"]["shot-001"]}
        receipt["assets"]["shot-001"]["layers"] = {"visual": receipt["assets"]["shot-001"]["layers"]["visual"]}
        self.apply(receipt)
        summary = content_review_summary(self.root, self.project, self.state)
        self.assertTrue(all(v is None for v in summary["layers"].values()))
        self.assertEqual(summary["status"], "incomplete-or-rejected")

    def test_observed_wrong_speaker_stays_failed_when_other_layers_pass(self) -> None:
        rejected = self.receipt["assets"]["shot-001"]["layers"]["audio"]
        rejected.update(status="rejected", evidence=["At 0.4–1.1s the listener says the lead's line while the lead is silent."])
        self.apply()
        summary = content_review_summary(self.root, self.project, self.state)
        self.assertIs(summary["layers"]["audio"], False)
        self.assertIs(summary["layers"]["visual"], True)
        self.assertEqual(summary["status"], "incomplete-or-rejected")

    def test_invalid_approval_receipt_does_not_overwrite_a_recorded_failure(self) -> None:
        self.receipt["assets"]["shot-001"]["layers"]["audio"]["status"] = "rejected"
        self.apply()
        before = copy.deepcopy(self.state)
        false_approval = copy.deepcopy(self.receipt)
        false_approval["assets"]["shot-001"]["layers"]["audio"].update(status="approved", method="codex-vision", evidence=["Only a still image was inspected."])
        with self.assertRaises(SkillError):
            self.apply(false_approval)
        self.assertEqual(self.state, before)
        self.assertIs(content_review_summary(self.root, self.project, self.state)["layers"]["audio"], False)

    def test_final_narrative_cannot_pass_without_viewer_understanding_answers(self) -> None:
        self.receipt["assets"]["final"]["understanding"]["obstacle"] = ""
        errors = validate_review_receipt(self.root, self.project, self.state, self.receipt)
        self.assertTrue(any("viewer-understanding" in e for e in errors))

    def test_default_null_edit_out_can_generate_a_review_template(self) -> None:
        self.project["shots"][0]["edit_out"] = None
        contract = review_asset_contract(self.root, self.project, self.state)
        self.assertEqual(contract["shot-001"]["edit_out"], 5)

    def test_missing_evidence_or_unknown_asset_cannot_be_approved(self) -> None:
        self.receipt["assets"]["shot-001"]["layers"]["visual"]["evidence"] = []
        self.receipt["assets"]["phantom"] = copy.deepcopy(self.receipt["assets"]["shot-001"])
        errors = validate_review_receipt(self.root, self.project, self.state, self.receipt)
        self.assertTrue(any("observed evidence" in e for e in errors))
        self.assertTrue(any("unknown/malformed" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
