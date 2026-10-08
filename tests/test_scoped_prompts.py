from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

from gvs_common import SkillError  # noqa: E402
from scoped_prompts import compile_scoped_prompt, scoped_prompt_variants, uses_scoped_prompt_compiler  # noqa: E402


class ScopedPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = {
            "prompt_compiler": "scoped-v1",
            "characters": [
                {"id": "buyer", "name": "Buyer", "identity": "Short black hair.", "wardrobe": "Blue jacket."},
                {"id": "seller", "name": "Seller", "identity": "Grey beard.", "wardrobe": "White shirt."},
                {"id": "other", "name": "Unrelated future character", "identity": "Purple cape."},
            ],
            "character_bible": "Unrelated future character purple cape in future global scene.",
            "style_bible": "Future global scene with every cast member.",
            "story_contract": {"payoff": "Later the warehouse explodes."},
            "series_context": {"climax": "Later the warehouse explodes."},
            "prompt_style": {"medium": "photoreal", "palette": "warm natural colors"},
            "audio": {"mode": "native-dialogue", "subtitle_source": "none"},
            "character_master": {"character_id": "buyer", "prompt": "Neutral turnaround."},
        }
        self.shot = {
            "id": "shot-001", "seconds": 5, "character_ids": ["buyer", "seller"],
            "location": "Village road", "camera": "Buyer closeup, seller shoulder right",
            "image_prompt": "Buyer holds a still crate beside seller.",
            "video_prompt": "Buyer slides the crate onto the table while speaking.",
            "exit_action": "the crate slides onto the table", "exit_behavior": "cut-on-action",
            "event": {"requires": {"crate.owner": "buyer"}, "effects": {"crate.owner": "seller"}},
            "dialogue": [{"speaker": "buyer", "listener": "seller", "text": "先收这筐！", "start": 0.15, "end": 1.25}],
        }

    def test_current_request_excludes_future_plot_and_unselected_identity(self) -> None:
        prompt = compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertIn("Short black hair", prompt)
        self.assertIn("Grey beard", prompt)
        for leaked in ("Purple cape", "Unrelated future character", "warehouse explodes", "Future global scene"):
            self.assertNotIn(leaked, prompt)
        self.assertIn('"crate.owner":"buyer"', prompt)
        self.assertIn('"crate.owner":"seller"', prompt)

    def test_empty_visible_cast_does_not_inject_all_characters(self) -> None:
        shot = {**self.shot, "character_ids": [], "dialogue": [], "image_prompt": "A single unplugged cable on a table."}
        prompt = compile_scoped_prompt(self.project, shot, kind="image")
        self.assertIn("none; no people", prompt)
        for identity in ("Short black hair", "Grey beard", "Purple cape"):
            self.assertNotIn(identity, prompt)

    def test_character_master_uses_only_requested_identity(self) -> None:
        prompt = compile_scoped_prompt(self.project, kind="character_master")
        self.assertIn("Short black hair", prompt)
        self.assertNotIn("Grey beard", prompt)
        self.assertNotIn("Purple cape", prompt)
        self.assertNotIn("Village road", prompt)

    def test_standalone_master_prompt_never_falls_back_to_global_bible(self) -> None:
        self.project["character_master"] = {"prompt": "A single freckled woman turnaround."}
        prompt = compile_scoped_prompt(self.project, kind="character_master")
        self.assertIn("freckled woman", prompt)
        self.assertNotIn("Purple cape", prompt)
        self.assertNotIn("Short black hair", prompt)

    def test_still_does_not_emit_dialogue_time_or_future_state(self) -> None:
        prompt = compile_scoped_prompt(self.project, self.shot, kind="image")
        self.assertIn('"crate.owner":"buyer"', prompt)
        self.assertNotIn('"crate.owner":"seller"', prompt)
        self.assertNotIn("先收这筐", prompt)
        self.assertNotIn("0.15-1.25s", prompt)
        self.assertNotIn("slides the crate", prompt)

    def test_all_variants_keep_speech_state_and_exit_contract(self) -> None:
        variants = scoped_prompt_variants(self.project, self.shot, kind="video")
        self.assertEqual(len(set(variants.values())), 1)
        for prompt in variants.values():
            self.assertIn("0.15-1.25s", prompt)
            self.assertIn("speaker=buyer", prompt)
            self.assertIn("listener=seller", prompt)
            self.assertIn("every listener and background mouth stays closed", prompt)
            self.assertIn("Speech starts before unrelated gestures", prompt)
            self.assertIn("No sigh", prompt)
            self.assertIn('"crate.owner":"seller"', prompt)

    def test_world_locks_remain_default_when_visible_projection_is_absent(self) -> None:
        self.shot["scene_state"] = {"parked_truck": "white truck", "wall": "white plaster"}
        self.shot["asset_state"] = {"freight_crate_count": 12}
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                prompt = compile_scoped_prompt(self.project, self.shot, kind=kind)
                self.assertIn('Scene locks: {"parked_truck":"white truck","wall":"white plaster"}', prompt)
                self.assertIn('Asset locks: {"freight_crate_count":12}', prompt)

    def test_visible_projection_excludes_hidden_entities_without_losing_event_or_speech(self) -> None:
        self.shot.update(
            scene_state={"parked_truck": "white truck", "wall": "white plaster"},
            asset_state={"freight_crate_count": 12},
            prompt_state={"scene": {"wall": "white plaster"}, "assets": {}},
        )
        original = copy.deepcopy(self.shot)
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                prompt = compile_scoped_prompt(self.project, self.shot, kind=kind)
                self.assertIn('Scene locks: {"wall":"white plaster"}', prompt)
                self.assertNotIn("parked_truck", prompt)
                self.assertNotIn("white truck", prompt)
                self.assertNotIn("freight_crate_count", prompt)
                self.assertNotIn("Asset locks:", prompt)
                self.assertIn('"crate.owner":"buyer"', prompt)
                if kind == "video":
                    self.assertIn('"crate.owner":"seller"', prompt)
                    self.assertIn("0.15-1.25s", prompt)
                    self.assertIn("speaker=buyer", prompt)
                    self.assertIn("先收这筐", prompt)
                    self.assertIn("No sigh", prompt)
                else:
                    self.assertNotIn('"crate.owner":"seller"', prompt)
        self.assertEqual(self.shot, original)

    def test_empty_projection_does_not_fall_back_to_world_state(self) -> None:
        self.shot["scene_state"] = {"parked_truck": "white truck"}
        self.shot["asset_state"] = {"freight_crate_count": 12}
        for projection in ({}, {"scene": {}, "assets": {}}, {"scene": {}}, {"assets": {}}):
            for kind in ("image", "video"):
                with self.subTest(projection=projection, kind=kind):
                    self.shot["prompt_state"] = projection
                    prompt = compile_scoped_prompt(self.project, self.shot, kind=kind)
                    self.assertNotIn("Scene locks:", prompt)
                    self.assertNotIn("Asset locks:", prompt)
                    self.assertNotIn("parked_truck", prompt)
                    self.assertNotIn("freight_crate_count", prompt)
                    self.assertIn('"crate.owner":"buyer"', prompt)

    def test_malformed_projection_is_rejected_before_generation(self) -> None:
        for value in (None, [], "truck", True, 1):
            for projection in (value, {"scene": value}, {"assets": value}):
                for kind in ("image", "video"):
                    with self.subTest(projection=projection, kind=kind):
                        self.shot["prompt_state"] = projection
                        with self.assertRaisesRegex(SkillError, "prompt_state.*must be an object"):
                            compile_scoped_prompt(self.project, self.shot, kind=kind)
        self.shot["prompt_state"] = {"asset": {}}
        with self.assertRaisesRegex(SkillError, "only accepts scene and assets"):
            compile_scoped_prompt(self.project, self.shot, kind="image")

    def test_visible_speaker_requires_membership_but_explicit_offscreen_is_supported(self) -> None:
        self.shot["character_ids"] = ["seller"]
        with self.assertRaisesRegex(SkillError, "is not visible"):
            compile_scoped_prompt(self.project, self.shot, kind="video")
        self.shot["dialogue"][0]["on_screen"] = False
        prompt = compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertIn("off-screen audio only", prompt)
        self.assertIn("Every visible mouth stays closed", prompt)
        self.assertNotIn("Short black hair", prompt)

    def test_multiple_speakers_block_before_upstream(self) -> None:
        self.shot["dialogue"].append({"speaker": "seller", "listener": "buyer", "text": "不行！", "start": 1.5, "end": 2})
        with self.assertRaisesRegex(SkillError, "one speaker per shot"):
            compile_scoped_prompt(self.project, self.shot, kind="video")

    def test_out_of_range_or_overlapping_windows_block(self) -> None:
        self.shot["dialogue"][0]["end"] = 6
        with self.assertRaisesRegex(SkillError, "generation seconds"):
            compile_scoped_prompt(self.project, self.shot, kind="video")
        self.shot["dialogue"][0]["end"] = 1.25
        self.shot["dialogue"].append({"speaker": "buyer", "text": "快点！", "start": 1, "end": 2})
        with self.assertRaisesRegex(SkillError, "cannot overlap"):
            compile_scoped_prompt(self.project, self.shot, kind="video")

    def test_unknown_visible_cast_and_listener_are_rejected(self) -> None:
        self.shot["background_character_ids"] = ["unknown"]
        with self.assertRaisesRegex(SkillError, "unknown visible"):
            compile_scoped_prompt(self.project, self.shot, kind="video")
        self.shot["background_character_ids"] = []
        self.shot["dialogue"][0]["listener"] = "other"
        with self.assertRaisesRegex(SkillError, "listener"):
            compile_scoped_prompt(self.project, self.shot, kind="video")

    def test_closeup_can_address_known_offscreen_listener_without_showing_them(self) -> None:
        self.shot["character_ids"] = ["buyer"]
        self.shot["listener_position"] = "offscreen-right"
        prompt = compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertIn("listener=seller (offscreen-right)", prompt)
        self.assertNotIn("Grey beard", prompt)
        self.assertNotIn("White shirt", prompt)
        self.shot["dialogue"][0]["listener"] = "unknown"
        with self.assertRaisesRegex(SkillError, "known character"):
            compile_scoped_prompt(self.project, self.shot, kind="video")

    def test_overlong_unicode_contract_blocks_instead_of_dropping_fields(self) -> None:
        self.shot["video_prompt"] = "当前动作" * 1400
        with self.assertRaisesRegex(SkillError, "without dropping cast, state, speech timing, or edit-exit"):
            scoped_prompt_variants(self.project, self.shot, kind="video")

    def test_narration_enforces_closed_mouth_without_character_lines(self) -> None:
        self.project["audio"]["mode"] = "narration"
        self.shot["dialogue"] = []
        self.shot["narration"] = "雨马上来了。"
        prompt = compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertIn("every visible mouth stays closed", prompt)
        self.assertIn("雨马上来了", prompt)

    def test_local_dialogue_does_not_leak_script_into_picture(self) -> None:
        self.project["audio"]["mode"] = "local-voice"
        prompt = compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertIn("0.15-1.25s", prompt)
        self.assertIn("recorded voice is supplied locally", prompt)
        self.assertNotIn("先收这筐", prompt)

    def test_compiler_does_not_mutate_project_or_shot_and_is_opt_in(self) -> None:
        original = copy.deepcopy((self.project, self.shot))
        compile_scoped_prompt(self.project, self.shot, kind="video")
        self.assertEqual((self.project, self.shot), original)
        self.assertTrue(uses_scoped_prompt_compiler(self.project))
        self.assertFalse(uses_scoped_prompt_compiler({}))


if __name__ == "__main__":
    unittest.main()
