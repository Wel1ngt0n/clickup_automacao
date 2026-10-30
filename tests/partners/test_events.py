import unittest
from unittest.mock import Mock, patch

from automations.partners import events
from tests.partners.factories import parent_task, ready_config


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
                events, "load_config", side_effect=ValueError("Invalid catalog")
            ) as load,
            patch.object(events, "configured_store") as store,
        ):
            self.assertIsNone(events.queue_partner_event(self.client, event))
        load.assert_not_called()
        store.assert_not_called()

    def test_disabled_partner_does_not_load_catalog_or_connect_to_database(self):
        with (
            patch.dict("os.environ", {"CLICKUP_PARTNER_ENABLED": "false"}),
            patch.object(
                events, "load_config", side_effect=ValueError("Invalid catalog")
            ) as load,
            patch.object(events, "configured_store") as store,
        ):
            result = events.queue_partner_event(self.client, parent_task())
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
            patch.object(events, "load_config", return_value=config),
            patch.object(events, "configured_store") as store,
        ):
            result = events.queue_partner_event(self.client, event)
        self.assertEqual(result["action"], "ignored")
        store.assert_not_called()

    def test_partner_parent_without_custom_id_is_enqueued_durably(self):
        parent = parent_task()
        parent["custom_id"] = None
        store = Mock()
        store.enqueue.return_value = 3
        with (
            patch.object(events, "load_config", return_value=ready_config()),
            patch.object(events, "configured_store", return_value=store),
        ):
            result = events.queue_partner_event(self.client, parent)
        self.assertEqual(result["action"], "queued")
        self.assertEqual(result["version"], 3)
        store.enqueue.assert_called_once_with(parent["id"])

    def test_missing_stage_parent_reference_does_not_fetch_or_enqueue_any_parent(self):
        config, event = self.stage_event()
        event["custom_fields"] = []
        with (
            patch.object(events, "load_config", return_value=config),
            patch.object(events, "configured_store") as store,
        ):
            result = events.queue_partner_event(self.client, event)
        self.assertEqual(result["reason"], "missing_partner_parent_id")
        self.client.get_task_by_internal_id.assert_not_called()
        store.assert_not_called()
