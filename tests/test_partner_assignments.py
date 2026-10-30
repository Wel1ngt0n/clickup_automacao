import copy
import unittest
from unittest.mock import patch

from fakes import FakeClickUp, FakeStore
from test_partner_config import parent_task, ready_config

from partner_config import build_snapshot, stage_assignees, task_assignee_ids
from partner_workflow import ensure_initial_assignment, reconcile_partner


class PartnerAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.config = ready_config()
        self.parent = parent_task(1)
        self.parent["assignees"] = [{"id": 12345, "username": "Implantador teste"}]
        self.client = FakeClickUp(self.parent)
        self.store = FakeStore()
        self.environment = patch.dict(
            "os.environ",
            {"CLICKUP_PARTNER_ALLOWED_PARENT_IDS": self.parent["id"]},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def stage(self, key):
        return next(stage for stage in self.config["stages"] if stage["key"] == key)

    def test_snapshot_uses_approved_initial_owners(self):
        snapshot = build_snapshot(self.config, self.parent, False)
        expected = {
            "onboarding": [12345],
            "criar_loja": [12345],
            "integracao_erp": [81923455],
            "integracao_marketplace": [61029153],
            "cadastro_produtos": [12345],
            "qualidade": [61029153],
            "treinamento": [12345],
            "configuracao_financeira": [105951759],
            "pos_implantacao": [12345],
        }
        self.assertEqual(
            {s["key"]: s["payload"]["assignees"] for s in snapshot["stages"]}, expected
        )

    def test_private_test_never_submits_other_users_or_creates_their_assignment_reservations(
        self,
    ):
        with patch.dict(
            "os.environ", {"CLICKUP_PARTNER_ALLOWED_ASSIGNEE_IDS": "61029153"}
        ):
            result = reconcile_partner(
                self.client, self.store, self.parent["id"], self.config
            )
        self.assertEqual(result["action"], "reconciled")
        submitted_ids = {
            user_id
            for _, payload in self.client.created_tasks
            for user_id in payload.get("assignees", [])
        }
        self.assertEqual(submitted_ids, {61029153})
        self.assertEqual(self.client.assignee_changes, [])
        self.assertIn("integracao_erp", result["assignments_pending"])
        self.assertIn("configuracao_financeira", result["assignments_pending"])
        self.assertIsNone(
            self.store.operation(self.parent["id"], "initial_assignment:integracao_erp")
        )

    def test_parent_assignee_set_is_normalized_without_picking_arbitrary_owner(self):
        self.parent["assignees"] = [{"id": 56789}, {"id": "12345"}, {"id": 12345}]
        self.assertEqual(
            stage_assignees(self.config, self.stage("onboarding"), self.parent),
            [12345, 56789],
        )

    def test_parent_without_owner_can_be_assigned_after_tasks_exist(self):
        self.client.tasks[self.parent["id"]]["assignees"] = []
        first = reconcile_partner(
            self.client, self.store, self.parent["id"], self.config
        )
        self.assertEqual(first["action"], "reconciled")
        self.assertIn("onboarding", first["assignments_pending"])
        self.client.tasks[self.parent["id"]]["assignees"] = [{"id": 12345}]
        second = reconcile_partner(
            self.client, self.store, self.parent["id"], self.config
        )
        self.assertEqual(
            set(second["assignments_added"]),
            {
                "onboarding",
                "criar_loja",
                "cadastro_produtos",
                "treinamento",
                "pos_implantacao",
            },
        )
        self.assertEqual(len(self.client.created_tasks), 9)

    def test_manual_handoff_is_preserved_even_if_later_removed(self):
        reconcile_partner(self.client, self.store, self.parent["id"], self.config)
        task = next(
            t
            for t in self.client.tasks.values()
            if t.get("list", {}).get("id") == self.stage("integracao_erp")["list_id"]
        )
        self.assertEqual(task_assignee_ids(task), [81923455])
        task["assignees"] = [{"id": 77777}]
        reconcile_partner(self.client, self.store, self.parent["id"], self.config)
        self.assertEqual(task_assignee_ids(task), [77777])
        task["assignees"] = []
        reconcile_partner(self.client, self.store, self.parent["id"], self.config)
        self.assertEqual(task["assignees"], [])
        self.assertEqual(self.client.assignee_changes, [])

    def test_preexisting_manual_assignee_is_not_replaced(self):
        snapshot = build_snapshot(self.config, self.parent, False)
        stage = snapshot["stages"][2]
        task = self.client.create_task(
            stage["list_id"], dict(stage["payload"], assignees=[77777])
        )
        outcome = ensure_initial_assignment(
            self.client,
            self.store,
            self.parent,
            self.config,
            self.stage("integracao_erp"),
            task,
        )
        self.assertEqual(outcome, "preserved")
        self.assertEqual(self.client.assignee_changes, [])
        self.assertIsNotNone(
            self.store.operation(self.parent["id"], "initial_assignment:integracao_erp")
        )

    def test_existing_store_snapshot_does_not_prevent_new_owner_policy(self):
        old_config = copy.deepcopy(self.config)
        for stage in old_config["stages"]:
            stage.pop("assignee_source", None)
            stage["assignee_ids"] = []
        reconcile_partner(self.client, self.store, self.parent["id"], old_config)
        result = reconcile_partner(
            self.client, self.store, self.parent["id"], self.config
        )
        self.assertEqual(len(result["assignments_added"]), 9)
        self.assertEqual(len(self.client.created_tasks), 9)

    def test_completed_stage_does_not_receive_retroactive_owner(self):
        stage = self.stage("qualidade")
        task = {
            "id": "completed",
            "status": {"status": "concluído", "type": "done"},
            "assignees": [],
        }
        outcome = ensure_initial_assignment(
            self.client, self.store, self.parent, self.config, stage, task
        )
        self.assertEqual(outcome, "not_applicable")
        self.assertEqual(self.client.assignee_changes, [])

    def test_unknown_assignment_response_does_not_retry_or_override(self):
        stage = self.stage("qualidade")
        snapshot = build_snapshot(self.config, self.parent, False)["stages"][5]
        task = self.client.create_task(
            stage["list_id"], dict(snapshot["payload"], assignees=[])
        )
        self.client.fail_assignment = True
        outcome = ensure_initial_assignment(
            self.client, self.store, self.parent, self.config, stage, task
        )
        self.assertEqual(outcome, "needs_review")
        self.client.fail_assignment = False
        outcome = ensure_initial_assignment(
            self.client, self.store, self.parent, self.config, stage, task
        )
        self.assertEqual(outcome, "needs_review")
        self.assertEqual(self.client.assignee_changes, [])


if __name__ == "__main__":
    unittest.main()
