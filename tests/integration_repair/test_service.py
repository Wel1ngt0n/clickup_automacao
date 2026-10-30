import copy
import unittest
from unittest.mock import Mock, patch

import app as webhook
from automations.integration_repair import service as repair
from clickup_dependency_audit import (
    DEFAULT_FATHER_FIELD_ID,
    DEFAULT_FLOW_STEP_FIELD_ID,
    DEFAULT_MAIN_LIST_ID,
)


def main_task(task_id, number=None, done=False, parent=False):
    fields = []
    if number is not None:
        fields = [
            {"id": DEFAULT_FATHER_FIELD_ID, "value": "F0H-TEST"},
            {"id": DEFAULT_FLOW_STEP_FIELD_ID, "value": number},
        ]
    return {
        "id": task_id,
        "custom_id": "F0H-TEST" if parent else None,
        "name": "Loja de teste" if parent else f"Etapa {number}",
        "list": {"id": DEFAULT_MAIN_LIST_ID if parent else "list-etapa"},
        "status": {
            "status": "concluído" if done else "cadastro de produtos",
            "type": "done" if done else "custom",
        },
        "custom_fields": fields,
        "dependencies": [],
    }


class MainWorkflowRegressionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ",
            {
                "CLICKUP_API_TOKEN": "test-token",
                "CLICKUP_WEBHOOK_SECRET": "test-signature-secret",
                "CLICKUP_PARTNER_ENABLED": "false",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        repair._recent_reconciliations.clear()
        self.parent = main_task("parent-main", parent=True)
        self.steps = [
            main_task(f"step-{number}", number, done=number < 5)
            for number in (2, 3, 4, 5)
        ]
        self.client = Mock()
        self.client.get_task_by_internal_id.return_value = copy.deepcopy(self.parent)
        self.client.get_task_by_custom_id.return_value = copy.deepcopy(self.parent)
        self.client.find_steps.return_value = copy.deepcopy(self.steps)
        self.client_patch = patch.object(
            webhook, "ClickUpClient", return_value=self.client
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.sleep_patch = patch.object(repair.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_missing_integration_dependency_is_added_and_status_corrected(self):
        result = webhook.reconcile_task(self.parent["id"])
        self.client.add_waiting_on_dependency.assert_called_once_with(
            "parent-main", "step-5"
        )
        self.client.update_task_status.assert_called_once_with(
            "parent-main", "integração erp"
        )
        self.assertEqual(result["action"], "reconciled")

    def test_existing_dependency_is_not_created_again(self):
        self.parent["dependencies"] = [
            {"task_id": "parent-main", "depends_on": "step-5"}
        ]
        self.client.get_task_by_internal_id.return_value = self.parent
        webhook.reconcile_task("parent-main")
        self.client.add_waiting_on_dependency.assert_not_called()

    def test_pending_initial_stage_prevents_status_correction(self):
        self.steps[0]["status"] = {"status": "em progresso", "type": "custom"}
        self.client.find_steps.return_value = self.steps
        webhook.reconcile_task("parent-main")
        self.client.update_task_status.assert_not_called()

    def test_completed_integration_does_not_rewind_parent(self):
        self.steps[-1]["status"] = {"status": "concluído", "type": "done"}
        self.client.find_steps.return_value = self.steps
        webhook.reconcile_task("parent-main")
        self.client.update_task_status.assert_not_called()

    def test_later_main_flow_stage_is_ignored(self):
        event = main_task("step-6", 6)
        self.client.get_task_by_internal_id.return_value = event
        result = webhook.reconcile_task(event["id"])
        self.assertEqual(result["reason"], "unrelated_flow_step")
        self.client.add_waiting_on_dependency.assert_not_called()
        self.client.update_task_status.assert_not_called()

    def test_unrelated_workspace_task_is_ignored(self):
        self.client.get_task_by_internal_id.return_value = {
            "id": "support-ticket",
            "name": "Solicitação",
            "list": {"id": "support-list"},
            "custom_fields": [],
        }
        result = webhook.reconcile_task("support-ticket")
        self.assertEqual(result["reason"], "task_outside_workflow")
        self.client.add_waiting_on_dependency.assert_not_called()
        self.client.update_task_status.assert_not_called()

    def test_stage_linked_to_parent_outside_main_list_cannot_mutate_it(self):
        event = main_task("step-5", 5)
        self.client.get_task_by_internal_id.return_value = event
        self.parent["list"]["id"] = "other-main-list"
        self.client.get_task_by_custom_id.return_value = self.parent
        result = webhook.reconcile_task(event["id"])
        self.assertEqual(result["reason"], "parent_outside_main_list")
        self.client.add_waiting_on_dependency.assert_not_called()
        self.client.update_task_status.assert_not_called()

    def test_partner_parent_without_custom_id_routes_before_old_parent_filter(self):
        event = {
            "id": "partner-parent",
            "name": "Loja parceira",
            "custom_id": None,
            "list": {"id": "901327379509"},
            "custom_fields": [],
        }
        self.client.get_task_by_internal_id.return_value = event
        with patch.object(
            webhook,
            "queue_partner_event",
            create=True,
            return_value={"action": "queued", "workflow": "parceiros"},
        ) as queue:
            result = webhook.reconcile_task(event["id"])
        self.assertEqual(result["action"], "queued")
        queue.assert_called_once()
        self.client.get_task_by_custom_id.assert_not_called()
        self.client.add_waiting_on_dependency.assert_not_called()

    def test_partner_stage_routes_before_old_field_and_integration_name_rules(self):
        event = main_task("partner-marketplace", 9)
        event["name"] = "Integração Marketplace de parceiro"
        event["list"] = {"id": "901328325681"}
        event["custom_fields"].append(
            {"id": "17f49983-94ed-47a2-9b94-8c83a0d99d79", "value": "partner-parent"}
        )
        self.client.get_task_by_internal_id.return_value = event
        with patch.object(
            webhook,
            "queue_partner_event",
            create=True,
            return_value={"action": "queued", "workflow": "parceiros"},
        ) as queue:
            result = webhook.reconcile_task(event["id"])
        self.assertEqual(result["action"], "queued")
        queue.assert_called_once()
        self.client.get_task_by_custom_id.assert_not_called()
        self.client.update_task_status.assert_not_called()
