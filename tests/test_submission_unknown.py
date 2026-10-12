from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "grok-video-studio" / "scripts"))

from media_client import NewApiJsonVideoClient, NewApiVideoClient, Sub2ApiVideoClient  # noqa: E402
import grok_video_studio as gvs  # noqa: E402
from provider_contracts import (  # noqa: E402
    SubmissionUnknownError,
    allows_automatic_failover,
    classify_provider_error,
)


class SubmissionUnknownTests(unittest.TestCase):
    def test_successful_response_without_task_id_is_unknown_for_both_adapters(self) -> None:
        for client_type in (Sub2ApiVideoClient, NewApiVideoClient, NewApiJsonVideoClient):
            for response in ({}, {"status": "queued"}, {"data": {"status": "processing"}}):
                with self.subTest(adapter=client_type.__name__, response=response):
                    client = client_type("http://127.0.0.1:1", "fixture-key", "fixture-model")
                    with mock.patch("media_client.request_json", return_value=response) as request:
                        with self.assertRaises(SubmissionUnknownError) as raised:
                            client.create(
                                "One uninterrupted physical camera view.", seconds=4, size="auto",
                                resolution="720p", aspect_ratio="9:16", generate_audio=True,
                                references=[],
                            )
                    request.assert_called_once()
                    self.assertEqual(
                        classify_provider_error(raised.exception, phase="create", task_known=False),
                        "submission_unknown",
                    )
                    self.assertFalse(allows_automatic_failover(
                        raised.exception, phase="create", task_known=False,
                    ))

    def test_valid_wrapped_task_id_remains_accepted_for_both_adapters(self) -> None:
        for client_type in (Sub2ApiVideoClient, NewApiVideoClient, NewApiJsonVideoClient):
            with self.subTest(adapter=client_type.__name__):
                client = client_type("http://127.0.0.1:1", "fixture-key", "fixture-model")
                with mock.patch("media_client.request_json", return_value={"data": {"id": "task-known"}}):
                    task_id = client.create(
                        "One uninterrupted physical camera view.", seconds=4, size="auto",
                        resolution="720p", aspect_ratio="9:16", references=[],
                    )
                self.assertEqual(task_id, "task-known")

    def test_newapi_json_uses_documented_json_video_contract(self) -> None:
        client = NewApiJsonVideoClient("https://suanliai.example/v1", "fixture-key", "remote-seedance")
        with mock.patch("media_client.request_json", return_value={"id": "task-json"}) as request:
            task_id = client.create(
                "One uninterrupted physical camera view.", seconds=4, size="auto",
                resolution="720p", aspect_ratio="9:16", generate_audio=True,
                references=[],
            )
        self.assertEqual(task_id, "task-json")
        method, url = request.call_args.args[:2]
        payload = request.call_args.kwargs["value"]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://suanliai.example/v1/videos")
        self.assertEqual(payload["model"], "remote-seedance")
        self.assertEqual(payload["seconds"], 4)
        self.assertEqual(payload["generate_audio"], True)
        self.assertNotIn("duration", payload)

    def test_newapi_json_can_omit_unsupported_generate_audio_field(self) -> None:
        client = NewApiJsonVideoClient(
            "https://suanliai.example/v1",
            "fixture-key",
            "remote-seedance",
            include_generate_audio_field=False,
        )
        with mock.patch("media_client.request_json", return_value={"id": "task-json"}) as request:
            client.create(
                "One uninterrupted physical camera view.", seconds=4, size="auto",
                resolution="720p", aspect_ratio="9:16", generate_audio=False,
                references=[],
            )
        payload = request.call_args.kwargs["value"]
        self.assertNotIn("generate_audio", payload)
        self.assertEqual(payload["model"], "remote-seedance")
        self.assertEqual(payload["seconds"], 4)

    def test_newapi_json_capability_controls_audio_field(self) -> None:
        settings = {
            "video_upstreams": {
                "suanliai-seedance": {
                    "key": "fixture-key",
                    "base_url": "https://suanliai.example/v1",
                    "adapter": "newapi-json",
                    "capabilities": {"generate_audio_field": False},
                },
            },
        }
        client = gvs.select_video_client(settings, "suanliai-seedance", "remote-seedance")
        self.assertIsInstance(client, NewApiJsonVideoClient)
        self.assertFalse(client.include_generate_audio_field)


if __name__ == "__main__":
    unittest.main()
