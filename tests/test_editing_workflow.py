from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from editing_workflow import (  # noqa: E402
    create_edit_plan,
    edit_state_path,
    export_edit_handoff,
    render_native_edit,
    validate_edit_plan,
)
from media_tools import probe_media  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class EditingWorkflowTests(unittest.TestCase):
    def _make_clip(self, root: Path, name: str, color: str) -> Path:
        output = root / "clips" / name
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x240:r=30:d=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return output

    def _project(self, root: Path) -> tuple[dict[str, object], dict[str, object]]:
        first = self._make_clip(root, "shot-001.mp4", "red")
        second = self._make_clip(root, "shot-002.mp4", "blue")
        project: dict[str, object] = {
            "defaults": {"video_size": "320x240"},
            "postproduction": {},
            "shots": [
                {"id": "shot-001", "seconds": 1},
                {"id": "shot-002", "seconds": 1},
            ],
        }
        state: dict[str, object] = {
            "shots": {
                "shot-001": {"video": {"status": "completed", "path": "clips/shot-001.mp4"}},
                "shot-002": {"video": {"status": "completed", "path": "clips/shot-002.mp4"}},
            }
        }
        self.assertTrue(first.is_file() and second.is_file())
        return project, state

    def test_native_hard_cut_is_resumable_and_preserves_clean_master_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, state = self._project(root)
            plan = create_edit_plan(root, project, state, transition="cut", filter_preset="warm")
            self.assertEqual(validate_edit_plan(root, plan, require_inputs=True), [])
            first = render_native_edit(root, plan)
            self.assertFalse(first["resumed"])
            output = root / "deliverables" / "final-edited.mp4"
            self.assertTrue(output.is_file())
            self.assertGreater(first["media"]["duration"], 2.0)
            self.assertFalse((root / "deliverables" / "final.mp4").exists())

            second = render_native_edit(root, plan)
            self.assertTrue(second["resumed"])
            edit_state = json.loads(edit_state_path(root).read_text(encoding="utf-8"))
            self.assertEqual(edit_state["status"], "completed")
            self.assertEqual(edit_state["output"]["sha256"], first["sha256"])

    def test_dissolve_shortens_timeline_and_has_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, state = self._project(root)
            plan = create_edit_plan(root, project, state, transition="dissolve", transition_seconds=0.2)
            result = render_native_edit(root, plan)
            media = probe_media(root / "deliverables" / "final-edited.mp4")
            self.assertLess(media["duration"], 2.35)
            self.assertGreater(media["duration"], 1.7)
            self.assertTrue(media["has_audio"])
            self.assertTrue(result["qa"]["ok"])

    def test_backend_selection_is_explicit_and_does_not_claim_chatcut_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, state = self._project(root)
            plan = create_edit_plan(root, project, state, backend="chatcut")
            self.assertEqual(plan["backend"]["selected"], "chatcut")
            self.assertTrue(any("Explicit user selection" in plan["backend"]["selection_reason"] for _ in [0]))
            with self.assertRaisesRegex(Exception, "selected chatcut"):
                render_native_edit(root, plan)

    def test_chatcut_handoff_requires_task_scoped_capability_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, state = self._project(root)
            plan = create_edit_plan(root, project, state)
            result = export_edit_handoff(root, plan, backend="chatcut")
            packet = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertFalse(packet["capability_gate"]["verified_by_cli"])
            self.assertIn("mcp__chatcut__", packet["capability_gate"]["required"])
            self.assertIn("output_sha256", packet["required_receipt"])
            self.assertEqual(len(packet["media_manifest"]), 2)
            self.assertEqual(packet["edit_plan"]["backend"]["selected"], "chatcut")

    def test_jianying_export_is_a_media_bundle_not_a_fake_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, state = self._project(root)
            plan = create_edit_plan(root, project, state)
            result = export_edit_handoff(root, plan, backend="jianying-draft")
            bundle = Path(result["path"])
            compatibility = json.loads((bundle / "compatibility.json").read_text(encoding="utf-8"))
            manifest = json.loads((bundle / "media-manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result["actual_draft"])
            self.assertFalse(compatibility["actual_draft"])
            bundled_plan = json.loads((bundle / "edit-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_plan["backend"]["selected"], "jianying-draft")
            self.assertEqual(len(manifest["media"]), 2)
            self.assertTrue(all((bundle / item["bundle_path"]).is_file() for item in manifest["media"]))
            self.assertFalse((bundle / "draft_content.json").exists())


if __name__ == "__main__":
    unittest.main()
