"""Contratos de transporte do cliente ClickUp compartilhado."""

import io
import unittest
import urllib.error
from unittest.mock import patch

from clickup_dependency_audit import (
    ClickUpClient,
    ClickUpError,
)


class ClickUpCreationRetryTests(unittest.TestCase):
    def test_transport_failure_never_blindly_repeats_creation_posts(self):
        client = ClickUpClient("test-token", "36953879", max_retries=3)
        calls = (
            lambda: client.create_task("test-list", {"name": "Loja teste"}),
            lambda: client.create_checklist("test-task", "Conferência"),
            lambda: client.create_checklist_item("test-checklist", "Conferir dados"),
            lambda: client.assign_task("test-task", [12345]),
        )
        for operation in calls:
            with (
                self.subTest(operation=operation),
                patch(
                    "urllib.request.urlopen",
                    side_effect=urllib.error.URLError("Connection reset"),
                ) as urlopen,
                patch("common.clickup.time.sleep") as sleep,
            ):
                with self.assertRaises(ClickUpError):
                    operation()
                self.assertEqual(urlopen.call_count, 1)
                sleep.assert_not_called()

    def test_server_error_does_not_blindly_repeat_task_creation(self):
        client = ClickUpClient("test-token", "36953879", max_retries=3)
        failure = urllib.error.HTTPError(
            "https://example.invalid", 503, "Unavailable", {}, io.BytesIO(b"{}")
        )
        self.addCleanup(failure.close)
        with (
            patch("urllib.request.urlopen", side_effect=failure) as urlopen,
            patch("common.clickup.time.sleep") as sleep,
        ):
            with self.assertRaises(ClickUpError):
                client.create_task("test-list", {"name": "Loja teste"})
            self.assertEqual(urlopen.call_count, 1)
            sleep.assert_not_called()

    def test_read_requests_keep_existing_retry_behavior(self):
        client = ClickUpClient("test-token", "36953879", max_retries=2)
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("Temporary connection failure"),
            ) as urlopen,
            patch("common.clickup.time.sleep") as sleep,
        ):
            with self.assertRaises(ClickUpError):
                client.get_task_by_internal_id("test-task")
            self.assertEqual(urlopen.call_count, 3)
            self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
