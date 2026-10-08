from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "grok-video-studio" / "scripts"))

from media_client import NewApiVideoClient, Sub2ApiVideoClient  # noqa: E402
from provider_contracts import (  # noqa: E402
    SubmissionUnknownError,
    allows_automatic_failover,
    classify_provider_error,
)


class SubmissionUnknownTests(unittest.TestCase):
    def test_successful_response_without_task_id_is_unknown_for_both_adapters(self) -> None:
        for client_type in (Sub2ApiVideoClient, NewApiVideoClient):
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
        for client_type in (Sub2ApiVideoClient, NewApiVideoClient):
            with self.subTest(adapter=client_type.__name__):
                client = client_type("http://127.0.0.1:1", "fixture-key", "fixture-model")
                with mock.patch("media_client.request_json", return_value={"data": {"id": "task-known"}}):
                    task_id = client.create(
                        "One uninterrupted physical camera view.", seconds=4, size="auto",
                        resolution="720p", aspect_ratio="9:16", references=[],
                    )
                self.assertEqual(task_id, "task-known")


if __name__ == "__main__":
    unittest.main()
