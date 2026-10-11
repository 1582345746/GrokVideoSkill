from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "grok-video-studio" / "scripts"))

from comparison_contract import comparison_contract_report, validate_comparison_contract  # noqa: E402
from grok_video_studio import composed_image_prompt, composed_video_prompt  # noqa: E402


def _lock(label: str) -> dict[str, str]:
    return {
        "era": label,
        "wardrobe": f"{label} clothes",
        "props": f"{label} props",
        "location": f"{label} location",
        "lighting": f"{label} light",
    }


def comparison_project() -> dict:
    shared = "孩子怎么度过春节"
    pairs = []
    shots = []
    for index in range(1, 4):
        pair_id = f"pair-{index:02d}"
        pairs.append(
            {
                "id": pair_id,
                "shared_question": shared,
                "before_claim": f"以前的做法 {index}",
                "after_claim": f"现在的做法 {index}",
                "visible_difference": f"第 {index} 组道具和行动形成反差",
                "bridge_in": "上一组动作接入",
                "bridge_out": "下一组动作接入",
                "payoff": f"第 {index} 组包袱",
                "era_lock": {"before": _lock("before"), "after": _lock("after")},
            }
        )
        for side in ("before", "after"):
            shots.append(
                {
                    "id": f"shot-{len(shots) + 1:03d}",
                    "summary": f"{side} side of pair {index}",
                    "comparison_pair_id": pair_id,
                    "comparison_side": side,
                    "visible_difference": f"{side} visible difference",
                    "seconds": 3,
                    "edit_in": 0,
                    "edit_out": 3,
                    "timeline_duration": 3,
                    "image_prompt": "clean frame, no subtitles",
                    "video_prompt": "short action, no captions",
                }
            )
    return {
        "workflow": "comparison-short",
        "comparison_contract": {
            "required": True,
            "axis": "then_vs_now",
            "shared_question": shared,
            "comparison_order": "before_then_after",
            "hook": "以前的孩子一听到过年就冲向门口",
            "payoff": "最后发现两代人抢的是同一个红包",
            "label_delivery": "post_overlay",
            "pairs": pairs,
        },
        "audio": {"subtitle_source": "none", "allow_upstream_captions": False},
        "shots": shots,
    }


class ComparisonContractTests(unittest.TestCase):
    def test_three_adjacent_pairs_pass(self) -> None:
        project = comparison_project()
        self.assertEqual(validate_comparison_contract(project), [])
        report = comparison_contract_report(project)
        self.assertTrue(report["ok"])
        self.assertEqual(report["pair_count"], 3)
        self.assertEqual(report["shot_count"], 6)

    def test_missing_after_side_is_blocked(self) -> None:
        project = comparison_project()
        project["shots"] = project["shots"][:-1]
        errors = validate_comparison_contract(project)
        self.assertTrue(any("exactly two shots per pair" in item for item in errors))
        self.assertTrue(any("exactly one before and one after" in item for item in errors))

    def test_unknown_pair_id_is_blocked(self) -> None:
        project = comparison_project()
        project["shots"][0]["comparison_pair_id"] = "pair-99"
        errors = validate_comparison_contract(project)
        self.assertTrue(any("references unknown pair pair-99" in item for item in errors))

    def test_hook_and_payoff_are_required(self) -> None:
        project = comparison_project()
        project["comparison_contract"].pop("hook")
        project["comparison_contract"]["payoff"] = ""
        errors = validate_comparison_contract(project)
        self.assertIn("comparison_contract.hook is required", errors)
        self.assertIn("comparison_contract.payoff is required", errors)

    def test_builtin_workflow_cannot_disable_the_contract(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["required"] = False
        project["comparison_contract"]["pairs"] = []
        project["comparison_contract"]["hook"] = ""
        errors = validate_comparison_contract(project)
        self.assertTrue(any("requires at least 3 comparison groups" in item for item in errors))
        self.assertIn("comparison_contract.hook is required", errors)

    def test_shared_question_is_required_at_contract_level(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["shared_question"] = ""
        errors = validate_comparison_contract(project)
        self.assertIn("comparison_contract.shared_question is required", errors)

    def test_pair_question_must_match_project_question(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["pairs"][1]["shared_question"] = "另一件事"
        errors = validate_comparison_contract(project)
        self.assertIn(
            "comparison_contract.pairs[1].shared_question must match comparison_contract.shared_question",
            errors,
        )

    def test_incomplete_era_lock_is_blocked(self) -> None:
        project = comparison_project()
        del project["comparison_contract"]["pairs"][1]["era_lock"]["after"]["lighting"]
        errors = validate_comparison_contract(project)
        self.assertTrue(any("pairs[1].era_lock.after is incomplete" in item for item in errors))

    def test_upstream_captions_are_blocked(self) -> None:
        project = comparison_project()
        project["audio"]["subtitle_source"] = "upstream"
        errors = validate_comparison_contract(project)
        self.assertTrue(any("subtitle_source=none" in item for item in errors))

    def test_positive_caption_prompt_is_blocked_but_negative_prompt_is_allowed(self) -> None:
        project = comparison_project()
        project["shots"][0]["video_prompt"] = "add large Chinese subtitles at the bottom"
        errors = validate_comparison_contract(project)
        self.assertTrue(any("requests captions/text overlay" in item for item in errors))
        project = copy.deepcopy(comparison_project())
        project["shots"][0]["video_prompt"] = "clean frame, no subtitles"
        self.assertFalse(any("requests captions/text overlay" in item for item in validate_comparison_contract(project)))

    def test_clean_frame_override_does_not_replace_shot_image_semantics(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["clean_frame_overrides"] = {
            "shot-001": "No devices or readable text."
        }
        project["shots"][0]["image_prompt"] = "A child waits with empty hands beside a wall clock."
        prompt = composed_image_prompt(project, project["shots"][0])
        self.assertIn("A child waits with empty hands beside a wall clock.", prompt)
        self.assertIn("No devices or readable text.", prompt)
        self.assertNotIn("holding a plain red envelope", prompt)

    def test_forbidden_device_override_does_not_add_device_positive_guidance(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["clean_frame_overrides"] = {
            "shot-001": "本镜完全不出现手机、电视或任何电子设备。"
        }
        prompt = composed_image_prompt(project, project["shots"][0])
        self.assertIn("Do not depict any phone, television, tablet, watch", prompt)
        self.assertNotIn("If a phone or television is present", prompt)

    def test_prompts_receive_the_matching_pair_claim_and_era_lock(self) -> None:
        project = comparison_project()
        project["comparison_contract"]["pairs"][0]["before_claim"] = "Keep the envelope for later."
        project["shots"][0]["image_prompt"] = "A child holds a paper envelope."
        project["shots"][0]["video_prompt"] = "The child places the envelope in a school bag."

        image_prompt = composed_image_prompt(project, project["shots"][0])
        video_prompt = composed_video_prompt(project, project["shots"][0])

        for prompt in (image_prompt, video_prompt):
            self.assertIn("Shared comparison question: 孩子怎么度过春节", prompt)
            self.assertIn("This side's claim: Keep the envelope for later.", prompt)
            self.assertIn("era: before", prompt)
            self.assertIn("wardrobe: before clothes", prompt)
            self.assertIn("props: before props", prompt)
            self.assertNotIn("after props", prompt)


if __name__ == "__main__":
    unittest.main()

