import copy

from clickup_dependency_audit import ClickUpError
from partner_state import PartnerStateError


class FakeStore:
    def __init__(self):
        self.contexts = {}
        self.operations = {}

    def job_context(self, parent_id, snapshot):
        self.contexts.setdefault(parent_id, copy.deepcopy(snapshot))
        return copy.deepcopy(self.contexts[parent_id])

    def operation(self, parent_id, key):
        return copy.deepcopy(self.operations.get((parent_id, key)))

    def reserve_operation(self, parent_id, key):
        identifier = (parent_id, key)
        if identifier in self.operations:
            return False
        self.operations[identifier] = {"state": "pending", "result_id": None}
        return True

    def complete_operation(self, parent_id, key, result_id):
        operation = self.operations.get((parent_id, key))
        if operation is None or operation.get("result_id") not in (None, result_id):
            raise PartnerStateError(
                "Reserva inexistente ou resultado conflitante da operacao."
            )
        self.operations[(parent_id, key)] = {"state": "done", "result_id": result_id}


class FakeClickUp:
    def __init__(self, parent):
        self.tasks = {parent["id"]: copy.deepcopy(parent)}
        self.created_tasks = []
        self.created_checklists = []
        self.created_items = []
        self.dependencies_added = []
        self.status_changes = []
        self.assignee_changes = []
        self.fail_assignment = False
        self.fail_create = None
        self.commit_before_failure = False
        self.create_attempts = 0
        self.fail_checklist = False
        self.fail_item = False
        self.checklist_attempts = 0
        self.item_attempts = 0

    def get_task_by_internal_id(self, task_id):
        if task_id not in self.tasks:
            raise ClickUpError("Task not found", status_code=404)
        return copy.deepcopy(self.tasks[task_id])

    def find_steps(self, father_field_id, parent_id):
        for task in self.tasks.values():
            if any(
                field.get("id") == father_field_id
                and str(field.get("value")) == str(parent_id)
                for field in task.get("custom_fields", [])
            ):
                yield copy.deepcopy(task)

    def list_main_tasks(self, list_id):
        return (
            copy.deepcopy(task)
            for task in self.tasks.values()
            if str(task.get("list", {}).get("id")) == str(list_id)
        )

    def create_task(self, list_id, payload):
        self.create_attempts += 1
        if self.fail_create and not self.commit_before_failure:
            raise self.fail_create
        task = copy.deepcopy(payload)
        task.update(
            {
                "id": f"created-{len(self.created_tasks) + 1}",
                "list": {"id": str(list_id)},
                "status": {
                    "status": payload.get("status", "não iniciado"),
                    "type": "open",
                },
                "checklists": [],
                "dependencies": [],
            }
        )
        self.tasks[task["id"]] = task
        self.created_tasks.append((str(list_id), copy.deepcopy(payload)))
        if self.fail_create:
            raise self.fail_create
        return copy.deepcopy(task)

    def create_checklist(self, task_id, name):
        self.checklist_attempts += 1
        checklist = {
            "id": f"checklist-{len(self.created_checklists) + 1}",
            "name": name,
            "items": [],
        }
        self.tasks[task_id].setdefault("checklists", []).append(checklist)
        self.created_checklists.append((task_id, name))
        if self.fail_checklist:
            raise ClickUpError("Checklist response lost")
        return {"checklist": copy.deepcopy(checklist)}

    def create_checklist_item(self, checklist_id, name):
        self.item_attempts += 1
        for task in self.tasks.values():
            for checklist in task.get("checklists", []):
                if checklist["id"] == checklist_id:
                    item = {
                        "id": f"item-{len(self.created_items) + 1}",
                        "name": name,
                        "resolved": False,
                    }
                    checklist["items"].append(item)
                    self.created_items.append((checklist_id, name))
                    if self.fail_item:
                        raise ClickUpError("Checklist item response lost")
                    return {"checklist": copy.deepcopy(checklist)}
        raise AssertionError(f"Unknown checklist {checklist_id}")

    def add_waiting_on_dependency(self, parent_id, step_id):
        self.dependencies_added.append((parent_id, step_id))
        self.tasks[parent_id].setdefault("dependencies", []).append(
            {"task_id": parent_id, "depends_on": step_id}
        )
        return {}

    def assign_task(self, task_id, assignee_ids):
        if self.fail_assignment:
            raise ClickUpError("Assignment response lost")
        self.assignee_changes.append((task_id, list(assignee_ids)))
        current = self.tasks[task_id].get("assignees", [])
        current_ids = {
            int(item["id"]) if isinstance(item, dict) else int(item) for item in current
        }
        self.tasks[task_id]["assignees"] = [
            {"id": user_id} for user_id in sorted(current_ids | set(assignee_ids))
        ]
        return copy.deepcopy(self.tasks[task_id])

    def update_task_status(self, task_id, status):
        self.status_changes.append((task_id, status))
        self.tasks[task_id]["status"] = {
            "status": status,
            "type": "done" if status == "implantado" else "custom",
        }
        return copy.deepcopy(self.tasks[task_id])
