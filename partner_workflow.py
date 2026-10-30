"""Reconciliação de parceiros; nenhuma operação em sistemas fora do ClickUp."""

from __future__ import annotations

import json
import logging
import os

from clickup_dependency_audit import ClickUpError, dependency_pairs
from partner_config import (
    assignment_allowed,
    build_snapshot,
    existing_client,
    field_value,
    stage_assignees,
    task_assignee_ids,
)

logger = logging.getLogger(__name__)


class WorkflowBlocked(RuntimeError):
    """Uma divergência exige revisão ou confirmação antes de repetir uma criação."""


def status_label(task: dict) -> str:
    status = task.get("status") or {}
    return (
        str(status.get("status", "") if isinstance(status, dict) else status)
        .strip()
        .lower()
    )


def is_archived(task: dict) -> bool:
    status = task.get("status") or {}
    return (
        bool(task.get("archived"))
        or status_label(task) in {"arquivado", "archived"}
        or (isinstance(status, dict) and status.get("type") == "closed")
    )


def is_done(task: dict) -> bool:
    if is_archived(task):
        return False
    status = task.get("status") or {}
    return (
        status.get("type") == "done"
        if isinstance(status, dict)
        else status_label(task) in {"concluído", "concluido", "implantado"}
    )


def is_enrolled(parent: dict) -> bool:
    allowed = {
        i.strip()
        for i in os.environ.get("CLICKUP_PARTNER_ALLOWED_PARENT_IDS", "").split(",")
        if i.strip()
    }
    if allowed:
        return str(parent["id"]) in allowed
    try:
        cutoff = int(os.environ.get("CLICKUP_PARTNER_START_AFTER", ""))
        created = int(parent.get("date_created", ""))
        return cutoff > 0 and created >= cutoff
    except (ValueError, TypeError):
        return False


def reconcile_partner(client, store, parent_id: str, config: dict) -> dict:
    parent = client.get_task_by_internal_id(parent_id)
    base = {
        "workflow": "parceiros",
        "parent_id": parent_id,
        "store_name": parent.get("name"),
    }
    if str((parent.get("list") or {}).get("id")) != config["main_list_id"]:
        return dict(base, action="ignored", reason="parent_outside_partner_list")
    if is_archived(parent):
        return dict(base, action="ignored", reason="parent_archived")
    if not is_enrolled(parent):
        return dict(base, action="ignored", reason="parent_not_enrolled")
    client_exists = existing_client(parent, config["fields"]["existing_client"])
    if client_exists is None:
        return dict(base, action="blocked", reason="missing_or_invalid_existing_client")
    try:
        snapshot = store.job_context(
            parent_id, build_snapshot(config, parent, client_exists)
        )
        base["store_name"] = snapshot["store_name"]
        if snapshot["existing_client"] != client_exists:
            raise WorkflowBlocked("existing_client_changed_review_required")
        return _reconcile_stages(client, store, parent, config, snapshot, base)
    except WorkflowBlocked as exc:
        return dict(base, action="blocked", reason=str(exc))


def _reconcile_stages(
    client, store, parent: dict, config: dict, snapshot: dict, base: dict
) -> dict:
    parent_id = str(parent["id"])
    father_field = config["fields"]["parent_task_id"]
    steps = [
        s
        for s in client.find_steps(father_field, parent_id)
        if str(field_value(s, father_field)).strip() == parent_id
    ]
    created, linked, resolved = [], [], []
    # Audita todos os candidatos antes de criar ou alterar qualquer etapa.
    for stage in snapshot["stages"]:
        _stage_matches(steps, stage, config)
    for stage in snapshot["stages"]:
        task, was_created = _ensure_task(client, store, parent_id, stage, steps, config)
        if is_archived(task):
            raise WorkflowBlocked(f"stage_archived:{stage['key']}")
        if was_created:
            created.append(str(task["id"]))
        # Cards anteriores ao serviço são reaproveitados sem reescrever seu conteúdo.
        task_operation = store.operation(parent_id, f"task:{stage['key']}")
        if task_operation is not None:
            _ensure_checklists(client, store, parent_id, stage, task)
        resolved.append((stage, task))
    # Atualiza o principal após a criação completa, sem decidir a partir de uma busca vazia.
    parent = client.get_task_by_internal_id(parent_id)
    if str((parent.get("list") or {}).get("id")) != config[
        "main_list_id"
    ] or is_archived(parent):
        return dict(base, action="ignored", reason="parent_archived")
    if (
        existing_client(parent, config["fields"]["existing_client"])
        != snapshot["existing_client"]
    ):
        raise WorkflowBlocked("existing_client_changed_review_required")
    pairs = dependency_pairs(parent)
    assignments_added, assignments_pending = [], []
    current_models = {stage["key"]: stage for stage in config["stages"]}
    for _, task in resolved:
        task_id = str(task["id"])
        if (parent_id, task_id) not in pairs:
            client.add_waiting_on_dependency(parent_id, task_id)
            linked.append(task_id)
    for stage, task in resolved:
        assignment = ensure_initial_assignment(
            client, store, parent, config, current_models[stage["key"]], task
        )
        if assignment == "assigned":
            assignments_added.append(stage["key"])
        elif assignment in {
            "awaiting_parent_assignee",
            "needs_review",
            "restricted_for_private_test",
        }:
            assignments_pending.append(stage["key"])
            logger.warning(
                "Fluxo=parceiros | loja=%r | etapa=%s | responsavel=%s",
                base["store_name"],
                stage["key"],
                assignment,
            )
    current = next((s for s, t in resolved if not is_done(t)), None)
    desired_status = current["parent_status"] if current else "implantado"
    before = status_label(parent)
    if before != desired_status:
        client.update_task_status(parent_id, desired_status)
    result = dict(
        base,
        action="reconciled",
        existing_client=snapshot["existing_client"],
        created=created,
        linked=linked,
        status=desired_status,
        status_changed=before != desired_status,
        current_stage=current["key"] if current else None,
        assignments_added=assignments_added,
        assignments_pending=assignments_pending,
    )
    logger.info(
        "Fluxo=parceiros | loja=%r | pai=%s | cliente_existente=%s | criados=%d | vinculados=%d | status=%s",
        base["store_name"],
        parent_id,
        snapshot["existing_client"],
        len(created),
        len(linked),
        desired_status,
    )
    return result


def ensure_initial_assignment(
    client, store, parent: dict, config: dict, stage: dict, task: dict
) -> str:
    key = f"initial_assignment:{stage['key']}"
    parent_id = str(parent["id"])
    operation = store.operation(parent_id, key)
    if operation and operation["state"] == "done":
        return "preserved"
    if is_done(task) or is_archived(task):
        return "not_applicable"
    actual = task_assignee_ids(task)
    if actual:
        store.reserve_operation(parent_id, key)
        store.complete_operation(parent_id, key, json.dumps(actual))
        return "preserved"
    desired = stage_assignees(config, stage, parent)
    if not desired:
        return (
            "awaiting_parent_assignee"
            if stage.get("assignee_source") == "parent"
            else "not_configured"
        )
    if not assignment_allowed(desired):
        return "restricted_for_private_test"
    # Confere alterações manuais feitas desde a primeira leitura da etapa.
    task = client.get_task_by_internal_id(str(task["id"]))
    _validate_task_link(task, parent_id, stage, config)
    if is_done(task) or is_archived(task):
        return "not_applicable"
    actual = task_assignee_ids(task)
    if not actual and (operation or not store.reserve_operation(parent_id, key)):
        return "needs_review"
    if not actual:
        try:
            client.assign_task(str(task["id"]), desired)
        except ClickUpError:
            return "needs_review"
        actual = task_assignee_ids(client.get_task_by_internal_id(str(task["id"])))
        if not actual:
            return "needs_review"
    else:
        store.reserve_operation(parent_id, key)
    store.complete_operation(parent_id, key, json.dumps(actual))
    return "assigned" if set(desired).issubset(actual) else "preserved"


def _stage_matches(steps: list[dict], stage: dict, config: dict) -> list[dict]:
    matches = [
        s for s in steps if str((s.get("list") or {}).get("id")) == stage["list_id"]
    ]
    if len(matches) > 1:
        raise WorkflowBlocked(f"duplicate_stage:{stage['key']}")
    if matches:
        order = field_value(matches[0], config["fields"]["stage_order"])
        if order is not None and str(order) not in {
            str(stage["order"]),
            f"{stage['order']}.0",
        }:
            raise WorkflowBlocked(f"stage_order_mismatch:{stage['key']}")
        if is_archived(matches[0]):
            raise WorkflowBlocked(f"stage_archived:{stage['key']}")
    return matches


def _ensure_task(
    client, store, parent_id: str, stage: dict, steps: list[dict], config: dict
) -> tuple[dict, bool]:
    key = f"task:{stage['key']}"
    operation = store.operation(parent_id, key)
    adopted_key = f"adopted_task:{stage['key']}"
    operation_key = key
    if operation is None:
        operation = store.operation(parent_id, adopted_key)
        operation_key = adopted_key
    if operation and operation.get("state") == "done":
        try:
            task = client.get_task_by_internal_id(operation["result_id"])
        except ClickUpError as exc:
            if exc.status_code == 404:
                raise WorkflowBlocked(
                    f"stage_missing_review_required:{stage['key']}"
                ) from exc
            raise
        if (
            str((task.get("list") or {}).get("id")) != stage["list_id"]
            or str(field_value(task, config["fields"]["parent_task_id"])).strip()
            != parent_id
        ):
            raise WorkflowBlocked(f"stage_link_changed:{stage['key']}")
        return task, False
    candidates = _stage_matches(steps, stage, config)
    if operation and not candidates:
        # Busca direta na lista: a busca por custom field pode ter indexação atrasada.
        marker = f"Automação parceiros: parent={parent_id}; etapa={stage['key']}"
        candidates = [
            s
            for s in client.list_main_tasks(stage["list_id"])
            if str(field_value(s, config["fields"]["parent_task_id"])).strip()
            == parent_id
            or marker
            in str(s.get("markdown_description") or s.get("description") or "")
        ]
        candidates = _stage_matches(candidates, stage, config)
    if candidates:
        task = client.get_task_by_internal_id(str(candidates[0]["id"]))
        _validate_task_link(task, parent_id, stage, config)
        if operation:
            store.complete_operation(parent_id, operation_key, str(candidates[0]["id"]))
        else:
            store.reserve_operation(parent_id, adopted_key)
            store.complete_operation(parent_id, adopted_key, str(task["id"]))
        return task, False
    if not store.reserve_operation(parent_id, key):
        raise WorkflowBlocked(f"uncertain_creation:{key}")
    try:
        payload = dict(stage["payload"])
        if not assignment_allowed(payload.get("assignees", [])):
            payload["assignees"] = []
        task = client.create_task(stage["list_id"], payload)
    except ClickUpError as exc:
        raise WorkflowBlocked(f"uncertain_creation:{key}") from exc
    if not task.get("id"):
        raise WorkflowBlocked(f"uncertain_creation:{key}")
    store.complete_operation(parent_id, key, str(task["id"]))
    task = client.get_task_by_internal_id(str(task["id"]))
    _validate_task_link(task, parent_id, stage, config)
    return task, True


def _validate_task_link(task: dict, parent_id: str, stage: dict, config: dict) -> None:
    if (
        str((task.get("list") or {}).get("id")) != stage["list_id"]
        or str(field_value(task, config["fields"]["parent_task_id"])).strip()
        != parent_id
    ):
        raise WorkflowBlocked(f"stage_link_changed:{stage['key']}")


def _ensure_checklists(client, store, parent_id: str, stage: dict, task: dict) -> None:
    for number, checklist in enumerate(stage["checklists"]):
        key = f"checklist:{stage['key']}:{number}"
        matches = [
            c for c in task.get("checklists", []) if c.get("name") == checklist["name"]
        ]
        if len(matches) > 1:
            raise WorkflowBlocked(f"duplicate_checklist:{stage['key']}")
        operation = store.operation(parent_id, key)
        if operation and operation.get("state") == "done":
            actual = next(
                (
                    c
                    for c in task.get("checklists", [])
                    if str(c.get("id")) == operation["result_id"]
                ),
                None,
            )
            if actual is None:
                raise WorkflowBlocked(
                    f"checklist_missing_review_required:{stage['key']}"
                )
        else:
            actual = _ensure_checklist(
                client, store, parent_id, key, task, checklist, matches
            )
        for index, name in enumerate(checklist["items"]):
            _ensure_item(client, store, parent_id, f"{key}:item:{index}", actual, name)


def _ensure_checklist(
    client, store, parent_id: str, key: str, task: dict, checklist: dict, matches: list
) -> dict:
    if matches:
        actual = matches[0]
        store.reserve_operation(parent_id, key)
    elif store.reserve_operation(parent_id, key):
        try:
            response = client.create_checklist(str(task["id"]), checklist["name"])
            actual = response.get("checklist", response)
        except ClickUpError as exc:
            raise WorkflowBlocked(f"uncertain_creation:{key}") from exc
        if not actual.get("id"):
            raise WorkflowBlocked(f"uncertain_creation:{key}")
    else:
        raise WorkflowBlocked(f"uncertain_creation:{key}")
    store.complete_operation(parent_id, key, str(actual["id"]))
    return actual


def _ensure_item(
    client, store, parent_id: str, key: str, actual: dict, name: str
) -> None:
    operation = store.operation(parent_id, key)
    # Não reescreve itens já criados, mesmo se o time editar, concluir ou remover.
    if operation and operation.get("state") == "done":
        return
    matches = [i for i in actual.get("items", []) if i.get("name") == name]
    if len(matches) > 1:
        raise WorkflowBlocked(f"duplicate_checklist_item:{key}")
    if matches:
        store.reserve_operation(parent_id, key)
    else:
        if not store.reserve_operation(parent_id, key):
            raise WorkflowBlocked(f"uncertain_creation:{key}")
        try:
            client.create_checklist_item(str(actual["id"]), name)
        except ClickUpError as exc:
            raise WorkflowBlocked(f"uncertain_creation:{key}") from exc
    store.complete_operation(parent_id, key, name)
