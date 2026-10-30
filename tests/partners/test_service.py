import copy
import unittest
from unittest.mock import patch

from automations.partners.service import reconcile_partner
from common.clickup import ClickUpError
from tests.partners.factories import parent_task, ready_config
from tests.partners.fakes import FakeClickUp, FakeStore


class PartnerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ",
            {"CLICKUP_PARTNER_ALLOWED_PARENT_IDS": "partner-parent"},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ready_config()
        self.parent = parent_task(1)
        self.client = FakeClickUp(self.parent)
        self.store = FakeStore()

    def reconcile(self):
        return reconcile_partner(
            self.client, self.store, self.parent["id"], self.config
        )

    def seed_stages(self, done=False):
        for stage in self.config["stages"]:
            if (
                stage["applies_to"] == "new_clients"
                and self.parent["custom_fields"][0]["value"] == 0
            ):
                continue
            task = {
                "id": f"seed-{stage['key']}",
                "name": f"Nome editado manualmente {stage['key']}",
                "markdown_content": "Descrição editada pelo time",
                "list": {"id": stage["list_id"]},
                "status": {
                    "status": "concluído" if done else "não iniciado",
                    "type": "done" if done else "open",
                },
                "custom_fields": [
                    {
                        "id": self.config["fields"]["parent_task_id"],
                        "value": self.parent["id"],
                    },
                    {
                        "id": self.config["fields"]["stage_order"],
                        "value": stage["order"],
                    },
                ],
                "checklists": [
                    {
                        "id": f"manual-{stage['key']}",
                        "name": "Checklist manual",
                        "items": [
                            {
                                "id": "manual-item",
                                "name": "Conferido pelo time",
                                "resolved": True,
                            }
                        ],
                    }
                ],
                "dependencies": [],
            }
            self.client.tasks[task["id"]] = task

    def test_new_client_creates_nine_cards_and_native_unchecked_checklists(self):
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(len(self.client.created_tasks), 9)
        self.assertEqual(len(self.client.created_checklists), 9)
        self.assertEqual(len(self.client.created_items), 54)
        self.assertEqual(len(self.client.dependencies_added), 9)
        for task in self.client.tasks.values():
            for checklist in task.get("checklists", []):
                self.assertTrue(
                    all(item["resolved"] is False for item in checklist["items"])
                )

    def test_existing_client_creates_eight_cards_without_products_or_replication(self):
        self.client.tasks[self.parent["id"]]["custom_fields"][0]["value"] = 0
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(len(self.client.created_tasks), 8)
        self.assertNotIn(
            self.config["stages"][4]["list_id"],
            [list_id for list_id, _ in self.client.created_tasks],
        )
        self.assertFalse(
            any(
                "replica" in payload["name"].lower()
                for _, payload in self.client.created_tasks
            )
        )

    def test_repeated_event_does_not_duplicate_cards_checklists_or_dependencies(self):
        self.reconcile()
        self.reconcile()
        self.assertEqual(len(self.client.created_tasks), 9)
        self.assertEqual(len(self.client.created_checklists), 9)
        self.assertEqual(len(self.client.created_items), 54)
        self.assertEqual(len(self.client.dependencies_added), 9)

    def test_missing_answer_blocks_without_any_external_write(self):
        self.client.tasks[self.parent["id"]]["custom_fields"] = []
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.client.created_tasks, [])
        self.assertEqual(self.client.status_changes, [])

    def test_archived_parent_is_ignored(self):
        self.client.tasks[self.parent["id"]]["status"] = {
            "status": "arquivado",
            "type": "closed",
        }
        result = self.reconcile()
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.created_tasks, [])
        self.assertEqual(self.client.dependencies_added, [])

    def test_parent_in_another_list_cannot_be_changed(self):
        self.client.tasks[self.parent["id"]]["list"]["id"] = "211186088"
        result = self.reconcile()
        self.assertIn(result["action"], ("blocked", "ignored"))
        self.assertEqual(self.client.created_tasks, [])
        self.assertEqual(self.client.status_changes, [])

    def test_all_required_stages_done_finishes_parent(self):
        self.seed_stages(done=True)
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertIn((self.parent["id"], "implantado"), self.client.status_changes)

    def test_archived_stage_is_not_treated_as_finished(self):
        self.seed_stages(done=True)
        self.client.tasks["seed-qualidade"]["status"] = {
            "status": "arquivado",
            "type": "closed",
        }
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertNotIn((self.parent["id"], "implantado"), self.client.status_changes)

    def test_finance_precedes_post_implantation_and_reopening_rewinds(self):
        self.seed_stages(done=True)
        self.client.tasks["seed-configuracao_financeira"]["status"] = {
            "status": "em progresso",
            "type": "custom",
        }
        self.client.tasks["seed-pos_implantacao"]["status"] = {
            "status": "não iniciado",
            "type": "open",
        }
        self.reconcile()
        self.assertEqual(
            self.client.status_changes[-1],
            (self.parent["id"], "configuração financeira"),
        )
        self.client.tasks["seed-configuracao_financeira"]["status"] = {
            "status": "concluído",
            "type": "done",
        }
        self.reconcile()
        self.assertEqual(
            self.client.status_changes[-1], (self.parent["id"], "pós-implantação")
        )
        self.client.tasks["seed-configuracao_financeira"]["status"] = {
            "status": "revisão",
            "type": "custom",
        }
        self.reconcile()
        self.assertEqual(
            self.client.status_changes[-1],
            (self.parent["id"], "configuração financeira"),
        )

    def test_existing_client_advances_from_marketplace_to_quality(self):
        self.parent["custom_fields"][0]["value"] = 0
        self.client.tasks[self.parent["id"]]["custom_fields"][0]["value"] = 0
        self.seed_stages(done=True)
        self.client.tasks["seed-qualidade"]["status"] = {
            "status": "não iniciado",
            "type": "open",
        }
        self.reconcile()
        self.assertEqual(
            self.client.status_changes[-1], (self.parent["id"], "qualidade")
        )

    def test_new_client_cannot_skip_pending_product_registration(self):
        self.seed_stages(done=True)
        self.client.tasks["seed-cadastro_produtos"]["status"] = {
            "status": "não iniciado",
            "type": "open",
        }
        self.reconcile()
        self.assertEqual(
            self.client.status_changes[-1], (self.parent["id"], "cadastro produtos")
        )

    def test_existing_manual_content_is_preserved(self):
        self.seed_stages()
        before = copy.deepcopy(self.client.tasks["seed-onboarding"])
        self.reconcile()
        self.assertEqual(self.client.tasks["seed-onboarding"], before)
        self.assertEqual(self.client.created_checklists, [])

    def test_duplicate_stage_blocks_instead_of_creating_more(self):
        self.seed_stages()
        duplicate = copy.deepcopy(self.client.tasks["seed-onboarding"])
        duplicate["id"] = "duplicate-onboarding"
        self.client.tasks[duplicate["id"]] = duplicate
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.client.created_tasks, [])

    def test_customer_branch_change_requires_review(self):
        self.reconcile()
        self.client.tasks[self.parent["id"]]["custom_fields"][0]["value"] = 0
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(len(self.client.created_tasks), 9)

    def test_creation_without_response_is_never_blindly_retried(self):
        self.client.fail_create = ClickUpError("Connection lost after request")
        first = self.reconcile()
        second = self.reconcile()
        self.assertEqual(first["action"], "blocked")
        self.assertEqual(second["action"], "blocked")
        self.assertEqual(self.client.created_tasks, [])
        self.assertEqual(self.client.create_attempts, 1)
        self.assertGreater(len(self.store.operations), 0)

    def test_creation_committed_before_lost_response_is_recovered(self):
        self.client.fail_create = ClickUpError("Connection lost after request")
        self.client.commit_before_failure = True
        self.reconcile()
        self.client.fail_create = None
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(len(self.client.created_tasks), 9)
        self.assertEqual(len(self.client.dependencies_added), 9)

    def test_lost_checklist_response_recovers_without_second_checklist(self):
        self.client.fail_checklist = True
        first = self.reconcile()
        self.assertEqual(first["action"], "blocked")
        self.client.fail_checklist = False
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(self.client.checklist_attempts, 9)
        self.assertEqual(len(self.client.created_checklists), 9)

    def test_lost_checklist_item_response_recovers_without_duplicate_item(self):
        self.client.fail_item = True
        first = self.reconcile()
        self.assertEqual(first["action"], "blocked")
        self.client.fail_item = False
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(self.client.item_attempts, 54)
        self.assertEqual(len(self.client.created_items), 54)

    def test_snapshot_and_completed_checklist_items_preserve_manual_edits(self):
        self.reconcile()
        original = self.client.tasks["created-1"]
        original["name"] = "Título personalizado pelo time"
        original["markdown_content"] = "Instruções adicionais da loja"
        original["checklists"][0]["items"][0].update(
            {"name": "Conferência ajustada pelo time", "resolved": True}
        )
        self.config["stages"][0]["checklists"][0]["items"][0] = (
            "Modelo atualizado para outra loja"
        )
        self.reconcile()
        self.assertEqual(original["name"], "Título personalizado pelo time")
        self.assertEqual(original["markdown_content"], "Instruções adicionais da loja")
        self.assertEqual(
            original["checklists"][0]["items"][0]["name"],
            "Conferência ajustada pelo time",
        )
        self.assertIs(original["checklists"][0]["items"][0]["resolved"], True)
        self.assertEqual(len(self.client.created_items), 54)

    def test_missing_previously_created_card_requires_review_instead_of_recreation(
        self,
    ):
        self.reconcile()
        self.client.tasks.pop("created-1")
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(len(self.client.created_tasks), 9)

    def test_manually_removed_dependency_is_restored_on_next_reconciliation(self):
        self.reconcile()
        self.client.tasks[self.parent["id"]]["dependencies"] = [
            dependency
            for dependency in self.client.tasks[self.parent["id"]]["dependencies"]
            if dependency["depends_on"] != "created-1"
        ]
        self.reconcile()
        self.assertEqual(
            self.client.dependencies_added.count((self.parent["id"], "created-1")), 2
        )
        self.assertEqual(len(self.client.created_tasks), 9)

    def test_marker_recovery_cannot_adopt_task_with_a_changed_parent_link(self):
        self.seed_stages()
        task = self.client.tasks["seed-onboarding"]
        task["custom_fields"][0]["value"] = "another-parent"
        task["description"] = (
            f"Automação parceiros: parent={self.parent['id']}; etapa=onboarding"
        )
        self.store.reserve_operation(self.parent["id"], "task:onboarding")
        result = self.reconcile()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["reason"], "stage_link_changed:onboarding")
        self.assertEqual(self.client.created_tasks, [])
        self.assertEqual(self.client.created_checklists, [])
        self.assertEqual(self.client.created_items, [])
        self.assertEqual(self.client.dependencies_added, [])

    def test_parent_moved_to_another_list_during_reconciliation_gets_no_dependencies_or_status(
        self,
    ):
        self.seed_stages(done=True)
        original_get = self.client.get_task_by_internal_id
        parent_reads = 0

        def move_parent_on_refresh(task_id):
            nonlocal parent_reads
            task = original_get(task_id)
            if task_id == self.parent["id"]:
                parent_reads += 1
                if parent_reads > 1:
                    task["list"]["id"] = "211186088"
            return task

        with patch.object(
            self.client, "get_task_by_internal_id", side_effect=move_parent_on_refresh
        ):
            result = self.reconcile()
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.dependencies_added, [])
        self.assertEqual(self.client.status_changes, [])

    def test_adopted_checklist_and_item_require_reservations_before_completion(self):
        self.seed_stages()
        task = self.client.tasks["seed-onboarding"]
        checklist = self.config["stages"][0]["checklists"][0]
        task["checklists"] = [
            {
                "id": "adopted-checklist",
                "name": checklist["name"],
                "items": [
                    {
                        "id": "adopted-item",
                        "name": checklist["items"][0],
                        "resolved": True,
                    }
                ],
            }
        ]
        self.store.reserve_operation(self.parent["id"], "task:onboarding")
        self.store.complete_operation(self.parent["id"], "task:onboarding", task["id"])
        result = self.reconcile()
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(self.client.created_checklists, [])
        self.assertEqual(len(self.client.created_items), 5)
        self.assertEqual(
            self.store.operation(self.parent["id"], "checklist:onboarding:0")["state"],
            "done",
        )
        self.assertEqual(
            self.store.operation(self.parent["id"], "checklist:onboarding:0:item:0")[
                "state"
            ],
            "done",
        )
        self.assertTrue(task["checklists"][0]["items"][0]["resolved"])


if __name__ == "__main__":
    unittest.main()
