from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grok-video-studio" / "scripts"))

from gvs_common import SkillError  # noqa: E402
from reference_analysis import (  # noqa: E402
    _filter_candidates,
    _parse_silence,
    analyze_reference,
    validate_reference_analysis,
)


class ReferenceMeasurementTests(unittest.TestCase):
    def test_candidate_spacing_rejects_motion_bursts_without_calling_them_shots(self) -> None:
        accepted, discarded = _filter_candidates([1.0, 1.05, 2.0, 2.97, -1, 0, 3], 3.0, 0.5)
        self.assertEqual(accepted, [1.0, 2.0])
        self.assertEqual(len(discarded), 5)

    def test_silence_open_tail_is_clamped_and_overlaps_are_not_double_counted(self) -> None:
        log = "\n".join([
            "silence_start: 1", "silence_end: 2",
            "silence_start: 1.9", "silence_end: 2.5",
            "silence_start: 2.8",
        ])
        intervals = _parse_silence(log, 3.0)
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0]["start_seconds"], 1)
        self.assertEqual(intervals[0]["end_seconds"], 2.5)
        self.assertAlmostEqual(sum(x["duration_seconds"] for x in intervals), 1.7)

    def test_invalid_settings_block_before_media_tools_or_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fixture.mp4"
            source.write_bytes(b"temporary fixture")
            output = root / "output"
            for kwargs in [{"scene_threshold": float("nan")}, {"scene_threshold": 0},
                           {"min_interval": float("inf")}, {"max_frames": True}, {"max_frames": 49}]:
                with self.subTest(kwargs=kwargs), patch("reference_analysis._run") as runner:
                    with self.assertRaises(SkillError):
                        analyze_reference(source, output, **kwargs)
                    runner.assert_not_called()
                    self.assertFalse(output.exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class ReferenceAnalysisIntegrationTests(unittest.TestCase):
    def _fixture(self, path: Path, audio: bool = True) -> Path:
        command = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for color in ("red", "blue", "green"):
            command += ["-f", "lavfi", "-i", f"color=c={color}:s=160x240:r=10:d=1"]
        graph = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
        if audio:
            for signal in ("sine=frequency=440:sample_rate=16000:duration=1",
                           "anullsrc=r=16000:cl=mono:d=1", "sine=frequency=440:sample_rate=16000:duration=1"):
                command += ["-f", "lavfi", "-i", signal]
            graph += ";[3:a][4:a][5:a]concat=n=3:v=0:a=1[a]"
        command += ["-filter_complex", graph, "-map", "[v]"]
        if audio:
            command += ["-map", "[a]", "-c:a", "aac"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def test_real_cut_and_silence_measurements_are_hash_bound_but_unreviewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._fixture(root / "案例 & $Clip.mp4")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = analyze_reference(source, root / "audit")
            report = result["report"]
            cuts = report["scene_measurements"]["filtered_candidates_seconds"]
            self.assertEqual(len(cuts), 2)
            self.assertAlmostEqual(cuts[0], 1.0, delta=0.11)
            self.assertAlmostEqual(cuts[1], 2.0, delta=0.11)
            self.assertIsNone(report["scene_measurements"]["verified_shot_count"])
            self.assertAlmostEqual(report["audio_measurements"]["silence_ratio"], 1/3, delta=0.08)
            self.assertEqual(report["semantic_review"]["status"], "unverified")
            self.assertIsNone(report["semantic_review"]["speaker_pass"])
            self.assertIsNone(report["semantic_review"]["clean_frame_pass"])
            self.assertEqual(report["source"]["sha256"], before)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(validate_reference_analysis(result["report_path"], source), [])
            serialized = Path(result["report_path"]).read_text(encoding="utf-8")
            self.assertNotIn(str(source.parent), serialized)
            self.assertTrue(Path(result["contact_sheet_path"]).is_file())
            self.assertEqual(len(result["frame_paths"]), 3)

            # A valid-looking replacement picture must not inherit earlier evidence integrity.
            frame = Path(result["frame_paths"][0])
            frame.write_bytes(frame.read_bytes()+b"changed")
            self.assertIn("reference analysis evidence is missing or changed", validate_reference_analysis(result["report_path"]))

    def test_no_audio_is_explicit_and_changed_settings_have_separate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._fixture(root / "silent.mp4", audio=False)
            first = analyze_reference(source, root / "audit", max_frames=2)
            second = analyze_reference(source, root / "audit", max_frames=3)
            self.assertEqual(first["report"]["audio_measurements"]["status"], "no-audio-stream")
            self.assertIsNone(first["report"]["audio_measurements"]["silence_ratio"])
            self.assertNotEqual(first["report_path"], second["report_path"])
            self.assertEqual(len(first["frame_paths"]), 2)
            self.assertEqual(len(second["frame_paths"]), 3)
            self.assertEqual(validate_reference_analysis(first["report_path"]), [])
            altered = json.loads(Path(second["report_path"]).read_text(encoding="utf-8"))
            altered["semantic_review"]["narrative_pass"] = True
            Path(second["report_path"]).write_text(json.dumps(altered), encoding="utf-8")
            self.assertIn("reference analysis report digest changed", validate_reference_analysis(second["report_path"]))


if __name__ == "__main__":
    unittest.main()
