from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

from event_flow import event_flow_report, speech_units  # noqa: E402


def event_project() -> dict:
    return {
        "characters": [{"id": "lead"}, {"id": "friend"}],
        "event_flow": {
            "required": True, "event_id": "catch-duck",
            "initial_state": {"duck.where": "basket", "door.closed": False},
        },
        "shots": [{
            "id": "shot-001", "seconds": 5, "edit_in": 0.1, "edit_out": 2,
            "character_ids": ["lead"],
            "event": {
                "event_id": "catch-duck", "function": "discovery", "beat_id": "escape",
                "requires": {"duck.where": "basket"}, "effects": {"duck.where": "floor"},
                "trigger": "The duck jumps from the basket.",
                "viewer_learns": "The duck is loose.", "bridge_to_next": "The friend must block the door.",
            },
            "dialogue": [{
                "id": "line-001", "speaker": "lead", "listener": "friend", "on_screen": True,
                "text": "鸭子跑了！", "start": 0.2, "end": 1.7,
                "intent": "warn", "new_information": "The duck escaped.", "effect_key": "duck.where",
            }],
        }, {
            "id": "shot-002", "seconds": 5, "edit_in": 0.1, "edit_out": 2,
            "character_ids": ["friend"],
            "event": {
                "event_id": "catch-duck", "function": "choice", "beat_id": "block-door",
                "requires": {"duck.where": "floor", "door.closed": False}, "effects": {"door.closed": True},
                "cause_shot_id": "shot-001", "viewer_learns": "The exit is now blocked.",
                "bridge_to_next": "The lead can corner the duck.",
            },
            "dialogue": [{
                "id": "line-002", "speaker": "friend", "listener": "lead", "on_screen": True,
                "text": "我把门关上！", "start": 0.2, "end": 1.7,
                "intent": "decide", "new_information": "The friend closes the door.", "effect_key": "door.closed",
            }],
        }],
    }


class EventFlowRegressionTests(unittest.TestCase):
    def test_state_reducer_exposes_real_cause_and_does_not_claim_semantic_quality(self) -> None:
        report = event_flow_report(event_project())
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["final_state"], {"duck.where": "floor", "door.closed": True})
        self.assertEqual(report["semantic_quality"], "unverified")
        self.assertEqual(report["evidence_kind"], "declared-plan")

    def test_wrong_input_state_and_disconnected_event_block_paid_plan(self) -> None:
        project = event_project()
        project["shots"][1]["event"]["requires"]["duck.where"] = "basket"
        project["shots"][1]["event"]["event_id"] = "find-bed"
        project["shots"][1]["event"]["cause_shot_id"] = "unrelated-shot"
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("input state mismatch" in e for e in errors))
        self.assertTrue(any("must advance event_id" in e for e in errors))
        self.assertTrue(any("preceding shot" in e for e in errors))

    def test_fake_progress_and_dialogue_not_causing_change_are_rejected(self) -> None:
        project = event_project()
        project["shots"][1]["event"]["effects"] = {"door.closed": False}
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("no state change" in e for e in errors))
        self.assertTrue(any("effect_key" in e for e in errors))

    def test_coverage_of_same_beat_is_legal_without_fabricating_new_state(self) -> None:
        project = event_project()
        project["shots"] = project["shots"][:1]
        for index, role in enumerate(("listener", "object"), start=2):
            previous = project["shots"][-1]["id"]
            coverage = {
                "id": f"shot-{index:03d}", "seconds": 5, "edit_in": 0.1, "edit_out": 1.6,
                "character_ids": ["friend"],
                "event": {
                    "event_id": "catch-duck", "function": "discovery", "mode": "coverage",
                    "beat_id": "escape", "coverage_role": role,
                    "requires": {"duck.where": "floor"}, "effects": {}, "cause_shot_id": previous,
                    "viewer_learns": f"The escaped duck is seen from the {role} angle.",
                    "bridge_to_next": "Close the exit before the duck reaches it.",
                },
                "dialogue": [{
                    "id": f"line-{index:03d}", "speaker": "lead", "listener": "friend",
                    "on_screen": False, "audio_source": "offscreen", "text": f"快堵门{index}！",
                    "start": 0.2, "end": 1.3, "intent": "request",
                    "new_information": "The same chase continues.", "effect_key": "duck.where",
                }],
            }
            project["shots"].append(coverage)
        report = event_flow_report(project)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["final_state"]["duck.where"], "floor")

    def test_coverage_cannot_smuggle_new_state_or_reference_inactive_beat(self) -> None:
        project = event_project()
        shot = project["shots"][1]
        shot["event"].update(mode="coverage", beat_id="unrelated", coverage_role="object")
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("active advancing beat" in e for e in errors))
        self.assertTrue(any("coverage cannot introduce" in e for e in errors))

    def test_wrong_visible_speaker_or_two_speakers_is_rejected(self) -> None:
        project = event_project()
        project["shots"][0]["character_ids"] = ["friend"]
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("on-screen speaker must be visible" in e for e in errors))
        project = event_project()
        project["shots"][0]["dialogue"].append({
            **project["shots"][0]["dialogue"][0], "id": "extra", "speaker": "friend",
            "listener": "lead", "text": "我看见了！", "start": 1.71, "end": 1.99,
        })
        self.assertTrue(any("split speaking turns" in e for e in event_flow_report(project)["errors"]))

    def test_offscreen_source_is_motivated_and_cannot_conflict_with_visible_cast(self) -> None:
        project = event_project()
        line = project["shots"][0]["dialogue"][0]
        line.update(on_screen=False, audio_source="offscreen")
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("off-screen speaker" in e.lower() or "offscreen speaker" in e.lower() for e in errors))

    def test_edit_window_cannot_truncate_spoken_words(self) -> None:
        project = event_project()
        project["shots"][0]["edit_in"] = 0.4
        project["shots"][1]["edit_out"] = 1.2
        errors = event_flow_report(project)["errors"]
        self.assertEqual(sum("fit the retained edit window" in e for e in errors), 2)

    def test_long_lead_tail_and_silent_padding_fail_independently(self) -> None:
        project = event_project()
        line = project["shots"][0]["dialogue"][0]
        line.update(start=1, end=1.3)
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("starts too late" in e for e in errors))
        self.assertTrue(any("silent gap" in e for e in errors))
        self.assertTrue(any("dead tail" in e for e in errors))
        project["shots"][0]["dialogue"] = []
        self.assertTrue(any("silent shot" in e for e in event_flow_report(project)["errors"]))

    def test_mandarin_units_and_intelligible_rate_work_without_spaces(self) -> None:
        self.assertEqual(speech_units("鸭子跑了！Stop now, please."), 7)
        project = event_project()
        project["shots"][0]["dialogue"][0].update(text="你快点别让鸭子跑出去它会飞走的", start=0.2, end=0.7)
        self.assertTrue(any("too long to remain intelligible" in e for e in event_flow_report(project)["errors"]))

    def test_empty_dialogue_text_and_nonboolean_visibility_cannot_pass(self) -> None:
        project = event_project()
        project["shots"][0]["dialogue"][0].update(text="", on_screen="false")
        errors = event_flow_report(project)["errors"]
        self.assertTrue(any("text" in e for e in errors))
        self.assertTrue(any("on_screen" in e for e in errors))

    def test_validating_does_not_mutate_authored_plan(self) -> None:
        project = event_project()
        original = copy.deepcopy(project)
        event_flow_report(project)
        self.assertEqual(project, original)

    def test_fast_dialogue_merges_legal_tail_and_lead_across_cut(self) -> None:
        project = event_project()
        project["workflow"] = "fast-continuous-event-dialogue"
        project["shots"][0]["dialogue"][0]["end"] = 1.5
        project["shots"][1]["dialogue"][0]["start"] = 0.49
        report = event_flow_report(project)
        gaps = report["dialogue_timeline"]["no_dialogue_intervals"]
        blocked = next(gap for gap in gaps if gap["blocked"])
        self.assertEqual(blocked["shot_ids"], ["shot-001", "shot-002"])
        self.assertAlmostEqual(blocked["duration"], 0.89)
        self.assertEqual(blocked["parts"][0]["source_start"], 1.5)
        self.assertEqual(blocked["parts"][1]["source_end"], 0.49)
        self.assertFalse(blocked["task_motivated"])
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("shot-001 -> shot-002" in error for error in report["errors"]))
        self.assertFalse(report["dialogue_timeline"]["actual_speech_verified"])

    def test_legacy_event_flow_does_not_enable_new_timeline_gate(self) -> None:
        project = event_project()
        project["shots"][0]["dialogue"][0]["end"] = 1.5
        project["shots"][1]["dialogue"][0]["start"] = 0.49
        report = event_flow_report(project)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["dialogue_timeline"]["status"], "not-enabled")

    def test_fast_dialogue_short_task_insert_is_allowed_but_legacy_stays_strict(self) -> None:
        project = event_project()
        project["shots"][0].update(dialogue=[], edit_out=1.1)
        project["workflow"] = "fast-continuous-event-dialogue"
        report = event_flow_report(project)
        self.assertEqual(report["errors"], [])
        gap = report["dialogue_timeline"]["no_dialogue_intervals"][0]
        self.assertAlmostEqual(gap["duration"], 1.1)
        self.assertIn("state-effect", gap["parts"][0]["task_reasons"])
        self.assertTrue(gap["task_motivated"])
        del project["workflow"]
        self.assertTrue(any("silent shot" in error for error in event_flow_report(project)["errors"]))

    def test_fast_dialogue_consecutive_task_inserts_cannot_bypass_gap_budget(self) -> None:
        project = event_project()
        project["workflow"] = "fast-continuous-event-dialogue"
        for shot in project["shots"]:
            shot.update(dialogue=[], edit_out=0.75)
        report = event_flow_report(project)
        self.assertTrue(any("task insert gap" in error for error in report["errors"]))
        self.assertFalse(any("silent shot" in error for error in report["errors"]))
        self.assertAlmostEqual(report["dialogue_timeline"]["longest_no_dialogue_seconds"], 1.3)

    def test_fast_dialogue_allows_short_silent_coverage_without_fake_effect(self) -> None:
        project = event_project()
        project["workflow"] = "fast-continuous-event-dialogue"
        project["shots"][0]["edit_out"] = 1.7
        project["shots"][1].update(dialogue=[], edit_out=1.1)
        project["shots"][1]["event"].update(mode="coverage", beat_id="escape", coverage_role="object", effects={})
        report = event_flow_report(project)
        self.assertEqual(report["errors"], [])
        gap = report["dialogue_timeline"]["no_dialogue_intervals"][-1]
        self.assertEqual(gap["parts"][0]["task_reasons"], ["coverage:object"])
        self.assertEqual(report["final_state"]["door.closed"], False)

    def test_fast_dialogue_task_insert_cannot_hide_excessive_neighbor_padding(self) -> None:
        project = event_project()
        project["workflow"] = "fast-continuous-event-dialogue"
        final = copy.deepcopy(project["shots"][1])
        final["id"] = "shot-003"
        final["event"]["cause_shot_id"] = "shot-002"
        final["dialogue"][0]["start"] = 0.5
        project["shots"][0]["dialogue"][0]["end"] = 1.5
        project["shots"][1].update(dialogue=[], edit_out=0.3)
        project["shots"][1]["event"].update(mode="coverage", beat_id="escape", coverage_role="object", effects={})
        project["shots"].append(final)
        report = event_flow_report(project)
        gap = next(gap for gap in report["dialogue_timeline"]["no_dialogue_intervals"] if gap["blocked"])
        self.assertAlmostEqual(gap["duration"], 1.1)
        self.assertAlmostEqual(gap["unmotivated_seconds"], 0.9)
        self.assertTrue(any("task insert cannot excuse padding" in error for error in report["errors"]))

    def test_fast_dialogue_cannot_disable_required_contract(self) -> None:
        for raw in (None, {}, {"required": False}):
            with self.subTest(event_flow=raw):
                report = event_flow_report({"workflow": "fast-continuous-event-dialogue", "event_flow": raw})
                self.assertEqual(report["status"], "blocked")
                self.assertTrue(any("requires event_flow.required=true" in error for error in report["errors"]))

    def test_fast_dialogue_gap_budgets_reject_invalid_values_and_accept_override(self) -> None:
        project = event_project()
        project["workflow"] = "fast-continuous-event-dialogue"
        for value in (True, 0, float("nan"), "1.2", []):
            project["event_flow"]["pace"] = {"max_task_silent_seconds": value}
            self.assertTrue(any("max_task_silent_seconds must" in error for error in event_flow_report(project)["errors"]))
        project["event_flow"]["pace"] = {"max_timeline_silent_seconds": 0.95}
        project["shots"][0]["dialogue"][0]["end"] = 1.5
        project["shots"][1]["dialogue"][0]["start"] = 0.49
        self.assertEqual(event_flow_report(project)["errors"], [])

    def test_container_values_in_enum_fields_produce_preflight_errors(self) -> None:
        for value in ([], {}):
            for field, expected in (("mode", "mode must"), ("coverage_role", "coverage_role"), ("audio_source", "audio_source")):
                with self.subTest(field=field, value=value):
                    project = event_project()
                    if field == "mode":
                        project["shots"][0]["event"][field] = value
                    elif field == "coverage_role":
                        project["shots"][1]["event"].update(mode="coverage", beat_id="escape", effects={}, coverage_role=value)
                    else:
                        project["shots"][0]["character_ids"] = []
                        project["shots"][0]["dialogue"][0].update(on_screen=False, audio_source=value)
                    report = event_flow_report(project)
                    self.assertEqual(report["status"], "blocked")
                    self.assertTrue(any(expected in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
