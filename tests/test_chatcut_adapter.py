import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "grok-video-studio"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from chatcut_adapter import (  # noqa: E402
    CHATCUT_CORE_TOOLS,
    apply_chatcut_receipt,
    build_chatcut_contract,
    chatcut_capability_report,
    detect_chatcut_installation,
    validate_chatcut_receipt,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class ChatCutAdapterTests(unittest.TestCase):
    def _make_clip(self, root: Path) -> Path:
        output = root / "deliverables" / "chatcut-render.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:r=30:d=1.2",
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
            str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return output

    def _plan(self) -> dict[str, object]:
        return {
            "version": 2,
            "backend": {"requested": "auto", "selected": "native"},
            "timeline": {
                "target_size": "320x240",
                "inputs": [{"id": "shot-001", "path": "clips/shot-001.mp4", "edit_in": 0, "edit_out": 1, "speed": 1, "filters": []}],
                "transitions": [],
            },
            "filters": [],
            "audio_mix": {"preserve_source": True, "normalize_lufs": -16},
            "deliveries": {"clean_master": "deliverables/final.mp4", "edited_master": "deliverables/final-edited.mp4"},
        }

    def test_installation_and_runtime_tool_states_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin = root / "plugins" / "cache" / "chatcut-inc" / "chatcut" / "0.2.25"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "chatcut", "version": "0.2.25"}), encoding="utf-8"
            )
            (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"chatcut": {"url": "https://example.test"}}}), encoding="utf-8")
            report = detect_chatcut_installation(codex_root=root)
            self.assertTrue(report["plugin"]["installed"])
            self.assertTrue(report["mcp"]["configured"])
            self.assertEqual(report["runtime"]["status"], "standalone-cli-unknown")
            names = ["mcp__chatcut__" + name for name in CHATCUT_CORE_TOOLS]
            runtime = detect_chatcut_installation(codex_root=root, tool_names=names)
            self.assertTrue(runtime["runtime"]["ready_for_edit"])
            self.assertEqual(runtime["runtime"]["missing_required_tools"], [])

    def test_contract_binds_to_source_plan_and_exposes_semantic_mapping(self) -> None:
        plan = self._plan()
        contract = build_chatcut_contract(plan, [{"id": "shot-001", "sha256": "a" * 64}])
        self.assertEqual(contract["source_plan_sha256"], contract["required_receipt"]["source_plan_sha256"])
        self.assertEqual(contract["status"], "ready_for_task_tool_execution")
        features = {item["feature"] for item in contract["feature_mapping"]}
        self.assertIn("clip_windows_and_speed", features)
        self.assertIn("filters", features)
        self.assertTrue(contract["capability_gate"]["installed_plugin_is_not_task_authorization"])

    def test_valid_receipt_is_hash_and_qa_bound_and_can_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = self._make_clip(root)
            plan = self._plan()
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            from chatcut_adapter import _canonical_digest  # noqa: PLC0415

            receipt = {
                "schema_version": 1,
                "status": "completed",
                "remote_project_id": "project-1",
                "remote_timeline_id": "timeline-1",
                "rendered_asset": {"path": "deliverables/chatcut-render.mp4", "sha256": digest},
                "output_sha256": digest,
                "source_plan_sha256": _canonical_digest(plan),
                "unmapped_features": [],
                "verification": {"structural": True, "visual": True},
                "tool_trace": [{"tool": "mcp__chatcut__preview_timeline", "phase": "verify", "status": "completed"}],
                "confirmed": True,
            }
            result = validate_chatcut_receipt(root, plan, receipt, require_confirmation=True)
            self.assertTrue(result["ok"], result)
            receipt_path = root / "chatcut-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            applied = apply_chatcut_receipt(root, plan, receipt_path, confirm=True)
            self.assertEqual(applied["status"], "accepted")
            self.assertTrue((root / "deliverables" / "chatcut-receipt.json").is_file())
            self.assertEqual(len(list((root / "deliverables" / "chatcut-receipts").glob("receipt-*.json"))), 1)

    def test_unmapped_or_changed_render_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = self._make_clip(root)
            plan = self._plan()
            from chatcut_adapter import _canonical_digest  # noqa: PLC0415

            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            receipt = {
                "schema_version": 1,
                "status": "completed",
                "remote_project_id": "project-1",
                "remote_timeline_id": "timeline-1",
                "rendered_asset": {"path": "deliverables/chatcut-render.mp4", "sha256": digest},
                "output_sha256": digest,
                "source_plan_sha256": _canonical_digest(plan),
                "unmapped_features": ["audio_normalization"],
                "verification": {"structural": True, "visual": True},
                "tool_trace": [{"tool": "mcp__chatcut__preview_timeline", "phase": "verify", "status": "completed"}],
                "confirmed": True,
            }
            result = validate_chatcut_receipt(root, plan, receipt)
            self.assertFalse(result["ok"])
            self.assertTrue(any("unmapped features" in error for error in result["errors"]))
            output.write_bytes(output.read_bytes() + b"tampered")
            receipt["unmapped_features"] = []
            result = validate_chatcut_receipt(root, plan, receipt)
            self.assertFalse(result["ok"])
            self.assertTrue(any("output_sha256" in error for error in result["errors"]))


class ChatCutCapabilityReportTests(unittest.TestCase):
    def test_default_report_does_not_claim_task_authorization(self) -> None:
        report = chatcut_capability_report()
        self.assertFalse(report["runtime"]["task_tools_visible"])
        self.assertFalse(report["runtime"]["ready_for_edit"])
        self.assertIn("codex mcp login chatcut", report["mcp"]["login_command"])


if __name__ == "__main__":
    unittest.main()
