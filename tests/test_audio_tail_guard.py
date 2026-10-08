from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "grok-video-studio" / "scripts"))

import editing_workflow  # noqa: E402
import grok_video_studio as gvs  # noqa: E402


class UpstreamAudioTailGuardTests(unittest.TestCase):
    def _fixture(self, root: Path, mode: str | None, *, explicit_end: bool = True) -> tuple[dict, dict]:
        project = {
            "defaults": {"video_size": "320x240", "audio_policy": "preserve"},
            "quality": {"tail_auto_trim": True, "tail_guard_seconds": 0.8},
            "shots": [
                {"id": "shot-001", "seconds": 5, "edit_in": 0.2, "exit_behavior": "cut-on-action"},
                {"id": "shot-002", "seconds": 5, "edit_in": 0.0, "exit_behavior": "cut-on-action"},
            ],
        }
        if mode is not None:
            project["audio"] = {"mode": mode}
        if explicit_end:
            project["shots"][0].update(edit_out=4.5, timeline_duration=4.3)
            project["shots"][1].update(edit_out=5.0, timeline_duration=5.0)
        state = {"shots": {}, "deliverables": {}}
        for shot in project["shots"]:
            path = root / "clips" / f"{shot['id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"clip-with-unverified-upstream-speech")
            state["shots"][shot["id"]] = {
                "video": {"status": "completed", "path": path.relative_to(root).as_posix()},
            }
        return project, state

    def _assemble(self, root: Path, project: dict, state: dict) -> tuple[list[dict], list[dict], mock.Mock]:
        captured = []

        def assemble_clips(clips: list[Path], output: Path, **kwargs: object) -> dict:
            captured.extend(copy.deepcopy(kwargs["edit_windows"]))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"assembled")
            return {"path": str(output), "bytes": 9}

        with (
            mock.patch.object(gvs, "require_valid_project", return_value=project),
            mock.patch.object(gvs, "load_state", return_value=state),
            mock.patch.object(gvs, "save_state"),
            mock.patch.object(gvs, "probe_media", return_value={"duration": 5.0}),
            mock.patch.object(gvs, "quality_report", return_value={"ok": True}),
            mock.patch.object(gvs, "assemble_clips", side_effect=assemble_clips),
            mock.patch.object(gvs, "detect_tail_motion", return_value={
                "static": True, "suggested_edit_out": 3.5, "motion_score": 0.0,
            }) as detector,
        ):
            gvs.assemble(root)
        return captured, state["deliverables"]["final"]["boundary_qa"], detector

    def test_assemble_preserves_unverified_upstream_audio_even_without_planned_lines(self) -> None:
        for mode in ("native-dialogue", "narration", None):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, mode)
                windows, boundaries, detector = self._assemble(root, project, state)
                detector.assert_not_called()
                self.assertEqual(windows[0], {"edit_in": 0.2, "edit_out": 4.5, "timeline_duration": 4.3})
                self.assertFalse(boundaries[0]["applied"])
                self.assertEqual(boundaries[0]["reason"], "tail_motion_skipped_audio_boundary_unverified")

    def test_assemble_preserves_probed_full_duration_for_upstream_audio(self) -> None:
        for mode in ("native-dialogue", "narration"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, mode, explicit_end=False)
                windows, boundaries, detector = self._assemble(root, project, state)
                detector.assert_not_called()
                self.assertEqual(windows[0]["edit_out"], 5.0)
                self.assertEqual(windows[0]["timeline_duration"], 4.8)
                self.assertFalse(boundaries[0]["applied"])

    def test_generic_or_stale_audio_approval_does_not_authorize_new_cut(self) -> None:
        for current_hash in (True, False):
            with self.subTest(current_hash=current_hash), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, "native-dialogue")
                digest = gvs.file_digest(root / "clips" / "shot-001.mp4") if current_hash else "stale"
                state["shots"]["shot-001"]["video"].update(review_status="approved", reviewed_sha256=digest)
                state["content_reviews"] = {"assets": {"shot-001": {
                    "sha256": digest,
                    "layers": {"audio": {"status": "approved", "method": "human-listening"}},
                }}}
                windows, boundaries, detector = self._assemble(root, project, state)
                detector.assert_not_called()
                self.assertEqual(windows[0]["edit_out"], 4.5)
                self.assertEqual(boundaries[0]["reason"], "tail_motion_skipped_audio_boundary_unverified")

    def test_assemble_keeps_existing_visual_trim_for_other_modes_and_skips_final(self) -> None:
        for mode in ("preserve", "mute"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, mode)
                windows, boundaries, detector = self._assemble(root, project, state)
                detector.assert_called_once()
                self.assertEqual(windows[0]["edit_out"], 3.5)
                self.assertEqual(windows[0]["timeline_duration"], 3.3)
                self.assertEqual(boundaries[0]["reason"], "tail_motion_settled")
                self.assertEqual(windows[1]["edit_out"], 5.0)
                self.assertFalse(boundaries[1]["applied"])

    def test_edit_plan_preserves_upstream_window_and_logs_skip_reason(self) -> None:
        for mode in ("native-dialogue", "narration", None):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, mode)
                with mock.patch.object(editing_workflow, "detect_tail_motion") as detector:
                    plan = editing_workflow.create_edit_plan(root, project, state)
                detector.assert_not_called()
                first = plan["timeline"]["inputs"][0]
                self.assertEqual(first["edit_in"], 0.2)
                self.assertEqual(first["edit_out"], 4.5)
                self.assertEqual(first["tail_trim"], {
                    "applied": False, "reason": "tail_motion_skipped_audio_boundary_unverified",
                })

    def test_edit_plan_keeps_existing_visual_trim_for_other_modes(self) -> None:
        for mode in ("preserve", "mute"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                project, state = self._fixture(root, mode)
                with mock.patch.object(editing_workflow, "detect_tail_motion", return_value={
                    "static": True, "suggested_edit_out": 3.5, "motion_score": 0.0,
                }) as detector:
                    plan = editing_workflow.create_edit_plan(root, project, state)
                detector.assert_called_once()
                inputs = plan["timeline"]["inputs"]
                self.assertEqual(inputs[0]["edit_out"], 3.5)
                self.assertEqual(inputs[0]["tail_trim"]["reason"], "tail_motion_settled")
                self.assertEqual(inputs[1]["edit_out"], 5.0)


if __name__ == "__main__":
    unittest.main()
