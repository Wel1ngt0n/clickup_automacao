import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import app as webhook


class WebhookSignatureTests(unittest.TestCase):
    def setUp(self):
        self.client = webhook.app.test_client()
        self.env = patch.dict(
            "os.environ",
            {"CLICKUP_WEBHOOK_SECRET": "test-signature-secret"},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_invalid_signature_rejects_before_reading_any_task(self):
        with patch.object(webhook, "reconcile_task") as reconcile:
            response = self.client.post(
                "/webhook/clickup", json={"event": "taskCreated", "task_id": "test"}
            )
        self.assertEqual(response.status_code, 401)
        reconcile.assert_not_called()

    def test_valid_signature_preserves_event_delivery(self):
        body = json.dumps({"event": "taskUpdated", "task_id": "test"}).encode()
        signature = hmac.new(b"test-signature-secret", body, hashlib.sha256).hexdigest()
        with patch.object(
            webhook,
            "reconcile_task",
            return_value={"action": "ignored", "reason": "test"},
        ) as reconcile:
            response = self.client.post(
                "/webhook/clickup",
                data=body,
                content_type="application/json",
                headers={"X-Signature": signature},
            )
        self.assertEqual(response.status_code, 200)
        reconcile.assert_called_once_with("test")


if __name__ == "__main__":
    unittest.main()
