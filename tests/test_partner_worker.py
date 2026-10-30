import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from test_partner_config import parent_task, ready_config

import partner_worker as worker
from clickup_dependency_audit import ClickUpError


class PartnerEventQueueTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ",
            {
                "CLICKUP_PARTNER_ENABLED": "true",
                "CLICKUP_PARTNER_ALLOWED_PARENT_IDS": "partner-parent",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = Mock()

    def test_non_partner_event_does_not_load_catalog_or_connect_to_database(self):
        event = {"id": "main-event", "list": {"id": "211186088"}}
        with (
            patch.object(
                worker, "load_config", side_effect=ValueError("Invalid catalog")
            ) as load,
            patch.object(worker, "configured_store") as store,
        ):
            self.assertIsNone(worker.queue_partner_event(self.client, event))
        load.assert_not_called()
        store.assert_not_called()

    def test_disabled_partner_does_not_load_catalog_or_connect_to_database(self):
        with (
            patch.dict("os.environ", {"CLICKUP_PARTNER_ENABLED": "false"}),
            patch.object(
                worker, "load_config", side_effect=ValueError("Invalid catalog")
            ) as load,
            patch.object(worker, "configured_store") as store,
        ):
            result = worker.queue_partner_event(self.client, parent_task())
        self.assertEqual(result["reason"], "partner_disabled")
        load.assert_not_called()
        store.assert_not_called()

    def stage_event(self):
        config = ready_config()
        return config, {
            "id": "partner-stage",
            "name": "Etapa de parceiro",
            "list": {"id": config["stages"][0]["list_id"]},
            "custom_fields": [
                {"id": config["fields"]["parent_task_id"], "value": "partner-parent"}
            ],
        }

    def test_partner_stage_pointing_to_parent_in_other_list_is_not_enqueued(self):
        config, event = self.stage_event()
        parent = parent_task()
        parent["list"]["id"] = "211186088"
        self.client.get_task_by_internal_id.return_value = parent
        with (
            patch.object(worker, "load_config", return_value=config),
            patch.object(worker, "configured_store") as store,
        ):
            result = worker.queue_partner_event(self.client, event)
        self.assertEqual(result["action"], "ignored")
        store.assert_not_called()

    def test_partner_parent_without_custom_id_is_enqueued_durably(self):
        parent = parent_task()
        parent["custom_id"] = None
        store = Mock()
        store.enqueue.return_value = 3
        with (
            patch.object(worker, "load_config", return_value=ready_config()),
            patch.object(worker, "configured_store", return_value=store),
        ):
            result = worker.queue_partner_event(self.client, parent)
        self.assertEqual(result["action"], "queued")
        self.assertEqual(result["version"], 3)
        store.enqueue.assert_called_once_with(parent["id"])

    def test_missing_stage_parent_reference_does_not_fetch_or_enqueue_any_parent(self):
        config, event = self.stage_event()
        event["custom_fields"] = []
        with (
            patch.object(worker, "load_config", return_value=config),
            patch.object(worker, "configured_store") as store,
        ):
            result = worker.queue_partner_event(self.client, event)
        self.assertEqual(result["reason"], "missing_partner_parent_id")
        self.client.get_task_by_internal_id.assert_not_called()
        store.assert_not_called()


class PartnerPendingJobTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = Mock()
        self.store.pending_jobs.return_value = [
            {"parent_id": "parent-test", "version": 4}
        ]
        self.store.store_lock.return_value = nullcontext(True)

    def test_busy_parent_is_skipped_without_reconciling_or_finishing_job(self):
        self.store.store_lock.return_value = nullcontext(False)
        with patch.object(worker, "reconcile_partner") as reconcile:
            processed = worker.run_pending(self.client, self.store, {})
        self.assertEqual(processed, 0)
        reconcile.assert_not_called()
        self.store.finish.assert_not_called()
        self.store.retry.assert_not_called()

    def test_completion_marks_only_the_version_that_was_selected(self):
        with patch.object(
            worker, "reconcile_partner", return_value={"action": "reconciled"}
        ):
            processed = worker.run_pending(self.client, self.store, {})
        self.assertEqual(processed, 1)
        self.store.finish.assert_called_once_with("parent-test", 4)

    def test_business_block_is_rescheduled_and_not_marked_done(self):
        with patch.object(
            worker,
            "reconcile_partner",
            return_value={
                "action": "blocked",
                "reason": "uncertain_creation:task:onboarding",
            },
        ):
            processed = worker.run_pending(self.client, self.store, {})
        self.assertEqual(processed, 1)
        self.store.finish.assert_not_called()
        self.store.retry.assert_called_once_with(
            "parent-test", 60, "uncertain_creation:task:onboarding"
        )

    def test_api_failure_is_rescheduled_with_a_safe_error_code(self):
        with patch.object(
            worker,
            "reconcile_partner",
            side_effect=ClickUpError("Remote response details", status_code=429),
        ):
            worker.run_pending(self.client, self.store, {})
        self.store.finish.assert_not_called()
        self.store.retry.assert_called_once_with("parent-test", 60, "clickup_http_429")


class PartnerEventCycleTests(unittest.TestCase):
    def test_queued_jobs_run_before_next_recovery_poll(self):
        with (
            patch.object(worker, "poll_parents") as poll,
            patch.object(worker, "run_pending") as pending,
            patch.object(worker.time, "monotonic", return_value=10),
        ):
            next_poll = worker.run_cycle(Mock(), Mock(), {}, 60, 60)
        poll.assert_not_called()
        pending.assert_called_once()
        self.assertEqual(next_poll, 60)

    def test_disabled_recovery_poll_still_consumes_webhook_jobs(self):
        with (
            patch.object(worker, "poll_parents") as poll,
            patch.object(worker, "run_pending") as pending,
        ):
            worker.run_cycle(Mock(), Mock(), {}, 0, 0)
        poll.assert_not_called()
        pending.assert_called_once()

    def test_due_recovery_poll_enqueues_then_consumes_jobs(self):
        sequence = Mock()
        with (
            patch.object(worker, "poll_parents", sequence.poll),
            patch.object(worker, "run_pending", sequence.consume),
            patch.object(worker.time, "monotonic", return_value=100),
        ):
            next_poll = worker.run_cycle(Mock(), Mock(), {}, 0, 60)
        self.assertEqual([call[0] for call in sequence.mock_calls], ["poll", "consume"])
        self.assertEqual(next_poll, 160)


class PartnerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = ready_config()
        self.client = Mock()
        self.main_statuses = [
            {"status": stage["parent_status"]} for stage in self.config["stages"]
        ] + [{"status": "implantado"}]
        self.wrong_folder = False
        self.missing_father = False

        def response(method, path):
            if method != "GET":
                raise AssertionError("Preflight must only read ClickUp")
            if path == f"/list/{self.config['main_list_id']}":
                return {"statuses": self.main_statuses}
            if path.endswith("/field"):
                fields = [self.config["fields"]["stage_order"]]
                if not self.missing_father:
                    fields.append(self.config["fields"]["parent_task_id"])
                return {"fields": [{"id": field_id} for field_id in fields]}
            return {
                "folder": {
                    "id": "wrong-folder"
                    if self.wrong_folder
                    else self.config["stage_folder_id"]
                },
                "statuses": [
                    {
                        "status": self.config["defaults"]["initial_status"],
                        "type": "open",
                    }
                ],
            }

        self.client.request.side_effect = response

    def assert_no_creation(self):
        self.client.create_task.assert_not_called()
        self.client.create_checklist.assert_not_called()
        self.client.update_task_status.assert_not_called()
        self.assertTrue(
            all(call.args[0] == "GET" for call in self.client.request.call_args_list)
        )

    def test_missing_parent_status_blocks_before_any_card_creation(self):
        self.main_statuses = [
            status
            for status in self.main_statuses
            if status["status"] != "configuração financeira"
        ]
        with self.assertRaises(ValueError):
            worker.preflight(self.client, self.config)
        self.assert_no_creation()

    def test_destination_in_wrong_folder_blocks_before_any_card_creation(self):
        self.wrong_folder = True
        with self.assertRaises(ValueError):
            worker.preflight(self.client, self.config)
        self.assert_no_creation()

    def test_missing_parent_link_field_blocks_before_any_card_creation(self):
        self.missing_father = True
        with self.assertRaises(ValueError):
            worker.preflight(self.client, self.config)
        self.assert_no_creation()


if __name__ == "__main__":
    unittest.main()
