import os
import unittest
import uuid
from unittest.mock import patch

from fakes import FakeClickUp
from test_partner_config import parent_task, ready_config

from partner_state import PartnerStore
from partner_workflow import reconcile_partner


@unittest.skipUnless(
    os.environ.get("PARTNER_TEST_DATABASE_URL"),
    "Defina PARTNER_TEST_DATABASE_URL para testar um PostgreSQL dedicado a testes.",
)
class PartnerWorkflowPostgreSQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.parent_id = f"partner-integration-{uuid.uuid4().hex}"
        self.store = PartnerStore(os.environ["PARTNER_TEST_DATABASE_URL"])
        self.store.initialize()
        self.addCleanup(self.cleanup_test_parent)
        self.env = patch.dict(
            "os.environ",
            {
                "CLICKUP_PARTNER_ALLOWED_PARENT_IDS": self.parent_id,
                "CLICKUP_PARTNER_ALLOWED_ASSIGNEE_IDS": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ready_config()
        self.parent = parent_task(1)
        self.parent.update(
            {"id": self.parent_id, "url": f"https://app.clickup.com/t/{self.parent_id}"}
        )
        self.client = FakeClickUp(self.parent)

    def cleanup_test_parent(self):
        with self.store._connection() as connection:
            connection.execute(
                "DELETE FROM clickup_partner_operations WHERE parent_id = %s",
                (self.parent_id,),
            )
            connection.execute(
                "DELETE FROM clickup_partner_jobs WHERE parent_id = %s",
                (self.parent_id,),
            )

    def reconcile_under_lock(self, store=None):
        store = store or self.store
        with store.store_lock(self.parent_id) as acquired:
            self.assertTrue(acquired)
            return reconcile_partner(self.client, store, self.parent_id, self.config)

    def test_new_client_persists_complete_creation_and_repeated_event_has_no_duplicates(
        self,
    ):
        version = self.store.enqueue(self.parent_id)
        first = self.reconcile_under_lock()
        self.assertEqual(first["action"], "reconciled")
        self.store.finish(self.parent_id, version)
        self.assertEqual(len(self.client.created_tasks), 9)
        self.assertEqual(len(self.client.created_checklists), 9)
        self.assertEqual(len(self.client.created_items), 54)
        self.assertEqual(len(self.client.dependencies_added), 9)

        fresh_store = PartnerStore(self.store.database_url)
        fresh_store.enqueue(self.parent_id)
        second = self.reconcile_under_lock(fresh_store)
        self.assertEqual(second["action"], "reconciled")
        self.assertEqual(second["created"], [])
        self.assertEqual(second["linked"], [])
        self.assertEqual(len(self.client.created_tasks), 9)
        self.assertEqual(len(self.client.created_checklists), 9)
        self.assertEqual(len(self.client.created_items), 54)
        with fresh_store._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM clickup_partner_operations WHERE parent_id = %s AND state = 'done'",
                (self.parent_id,),
            ).fetchone()
        self.assertEqual(row["count"], 76)

    def test_existing_client_reuses_manual_card_then_blocks_if_that_card_disappears(
        self,
    ):
        self.client.tasks[self.parent_id]["custom_fields"][0]["value"] = 0
        stage = self.config["stages"][0]
        manual_id = f"manual-{uuid.uuid4().hex}"
        self.client.tasks[manual_id] = {
            "id": manual_id,
            "name": "Onboarding acompanhado pelo time",
            "markdown_content": "Instruções já editadas pelo time",
            "list": {"id": stage["list_id"]},
            "status": {"status": "não iniciado", "type": "open"},
            "custom_fields": [
                {
                    "id": self.config["fields"]["parent_task_id"],
                    "value": self.parent_id,
                },
                {"id": self.config["fields"]["stage_order"], "value": 1},
            ],
            "checklists": [
                {
                    "id": "manual-checklist",
                    "name": "Anotações do time",
                    "items": [
                        {"id": "manual-item", "name": "Revisado", "resolved": True}
                    ],
                }
            ],
        }
        first = self.reconcile_under_lock()
        self.assertEqual(first["action"], "reconciled")
        self.assertEqual(len(self.client.created_tasks), 7)
        self.assertEqual(len(self.client.tasks) - 1, 8)
        self.assertEqual(len(self.client.created_checklists), 7)
        self.assertEqual(len(self.client.created_items), 42)
        self.assertEqual(len(self.client.dependencies_added), 8)
        self.assertNotIn(
            self.config["stages"][4]["list_id"],
            [list_id for list_id, _ in self.client.created_tasks],
        )
        self.assertEqual(
            self.client.tasks[manual_id]["markdown_content"],
            "Instruções já editadas pelo time",
        )
        self.assertEqual(
            self.store.operation(self.parent_id, "adopted_task:onboarding")[
                "result_id"
            ],
            manual_id,
        )

        self.client.tasks.pop(manual_id)
        second = self.reconcile_under_lock(PartnerStore(self.store.database_url))
        self.assertEqual(second["action"], "blocked")
        self.assertEqual(second["reason"], "stage_missing_review_required:onboarding")
        self.assertEqual(len(self.client.created_tasks), 7)
        self.assertEqual(len(self.client.created_items), 42)


if __name__ == "__main__":
    unittest.main()
