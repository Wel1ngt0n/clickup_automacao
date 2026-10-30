"""Regras da correção de Integração ERP do quadro principal."""

from __future__ import annotations

import logging
import os
import threading
import time

from automations.integration_repair.config import (
    DEFAULT_FATHER_FIELD_ID,
    DEFAULT_FLOW_STEP_FIELD_ID,
    DEFAULT_MAIN_LIST_ID,
)
from common.clickup import (
    ClickUpClient,
    dependency_pairs,
)

logger = logging.getLogger(__name__)


INITIAL_FLOW_STEPS = {2, 3, 4}
INTEGRATION_FLOW_STEP = 5
STATUS_INTEGRACAO = "integração erp"


def custom_field_value(task: dict, field_id: str):
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            value = field.get("value")
            return value.strip() if isinstance(value, str) else value
    return None


def status_name(task: dict) -> str:
    status = task.get("status") or {}
    return (
        str(status.get("status", "") if isinstance(status, dict) else status)
        .strip()
        .lower()
    )


def is_pending(task: dict) -> bool:
    status = task.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("type", "")).lower() not in {"done", "closed"}
    return status_name(task) not in {
        "finalizado",
        "concluído",
        "concluido",
        "complete",
        "closed",
    }


def step_number(task: dict, field_id: str):
    value = custom_field_value(task, field_id)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_integration_step(task: dict, flow_field_id: str) -> bool:
    number = step_number(task, flow_field_id)
    if number == INTEGRATION_FLOW_STEP:
        return True
    name = (task.get("name") or "").lower()
    return "integração" in name or "integracao" in name


_locks_guard = threading.Lock()
_store_locks: dict[str, threading.Lock] = {}
_recent_reconciliations: dict[str, dict] = {}
DEBOUNCE_WINDOW_SECONDS = 4.0


def get_store_lock(store_key: str) -> threading.Lock:
    with _locks_guard:
        if store_key not in _store_locks:
            _store_locks[store_key] = threading.Lock()
        return _store_locks[store_key]


def prune_recent_cache(now: float) -> None:
    expired = [
        k for k, v in _recent_reconciliations.items() if now - v.get("time", 0) > 60.0
    ]
    for k in expired:
        _recent_reconciliations.pop(k, None)


def reconcile_event_task(client: ClickUpClient, event_task: dict) -> dict:
    task_id = str(event_task["id"])
    father_field_id = os.environ.get(
        "CLICKUP_FATHER_FIELD_ID", DEFAULT_FATHER_FIELD_ID
    ).strip()
    flow_field_id = os.environ.get(
        "CLICKUP_FLOW_STEP_FIELD_ID", DEFAULT_FLOW_STEP_FIELD_ID
    ).strip()
    main_list_id = os.environ.get("CLICKUP_MAIN_LIST_ID", DEFAULT_MAIN_LIST_ID).strip()
    father_custom_id = custom_field_value(event_task, father_field_id)
    event_list_id = str((event_task.get("list") or {}).get("id", ""))
    is_parent = event_list_id == main_list_id and bool(event_task.get("custom_id"))

    if father_custom_id:
        # É uma tarefa de etapa.
        number = step_number(event_task, flow_field_id)
        is_int = is_integration_step(event_task, flow_field_id)
        # Processamos apenas se for o card de integração OU uma das etapas iniciais (2, 3, 4) que podem liberar a integração
        if not is_int and number not in INITIAL_FLOW_STEPS:
            return {
                "action": "ignored",
                "reason": "unrelated_flow_step",
                "task_id": task_id,
                "task_name": event_task.get("name"),
            }
        parent = client.get_task_by_custom_id(str(father_custom_id))
        step = event_task
    elif is_parent:
        parent = event_task
        father_custom_id = str(parent["custom_id"])
        step = None
    else:
        return {
            "action": "ignored",
            "reason": "task_outside_workflow",
            "task_id": task_id,
            "task_name": event_task.get("name"),
        }

    parent_list_id = str((parent.get("list") or {}).get("id", ""))
    if parent_list_id != main_list_id:
        logger.warning(
            "Tarefa %s ignorada: pai %s pertence a lista %s, nao a %s",
            task_id,
            parent.get("id"),
            parent_list_id,
            main_list_id,
        )
        return {
            "action": "ignored",
            "reason": "parent_outside_main_list",
            "task_id": task_id,
            "task_name": event_task.get("name"),
            "parent_id": str(parent.get("id", "")),
            "store_name": parent.get("name"),
        }

    parent_id = str(parent["id"])
    store_key = str(father_custom_id)
    store_lock = get_store_lock(store_key)

    with store_lock:
        now = time.time()
        with _locks_guard:
            prune_recent_cache(now)
            recent = _recent_reconciliations.get(store_key)

        if recent:
            elapsed = now - recent.get("time", 0)
            covered_ids = recent.get("covered_ids", set())
            if elapsed < DEBOUNCE_WINDOW_SECONDS and str(task_id) in covered_ids:
                logger.info(
                    "Reconciliacao para loja %r (%s) ignorada por debounce: "
                    "reconciliada recentemente (ha %.1fs) cobrindo a tarefa %s",
                    parent.get("name"),
                    store_key,
                    elapsed,
                    task_id,
                )
                return {
                    "action": "debounced",
                    "reason": "already_reconciled_recently",
                    "store_name": parent.get("name"),
                    "parent_custom_id": father_custom_id,
                    "parent_id": parent_id,
                    "trigger_task_name": event_task.get("name"),
                    "trigger_task_id": task_id,
                    "reconciled_seconds_ago": round(elapsed, 2),
                }

        steps = list(client.find_steps(father_field_id, str(father_custom_id)))
        if step is not None and all(
            str(item["id"]) != str(step["id"]) for item in steps
        ):
            steps.append(step)

        integration_step = next(
            (s for s in steps if is_integration_step(s, flow_field_id)), None
        )

        dependency_action = "none"
        if integration_step is not None:
            integration_step_id = str(integration_step["id"])
            existing_dependencies = dependency_pairs(parent)
            if (parent_id, integration_step_id) not in existing_dependencies:
                client.add_waiting_on_dependency(parent_id, integration_step_id)
                dependency_action = "added"
                time.sleep(0.15)
            else:
                dependency_action = "already_linked"

        status_action = "none"
        if integration_step is not None:
            initial_steps = [
                s for s in steps if step_number(s, flow_field_id) in INITIAL_FLOW_STEPS
            ]
            initial_numbers = {step_number(s, flow_field_id) for s in initial_steps}
            initial_all_exist = INITIAL_FLOW_STEPS.issubset(initial_numbers)
            initial_all_done = initial_all_exist and not any(
                is_pending(s) for s in initial_steps
            )

            # Deveria estar em integração se as etapas iniciais (2, 3, 4) estão concluídas e o card de integração está pendente
            should_be_in_integration = initial_all_done and is_pending(integration_step)

            if should_be_in_integration:
                current_status = status_name(parent)
                if current_status != STATUS_INTEGRACAO:
                    client.update_task_status(parent_id, STATUS_INTEGRACAO)
                    status_action = "updated_to_integration"
                    logger.info(
                        "Loja %r (%s): status divergente %r corrigido para %r",
                        parent.get("name"),
                        store_key,
                        current_status,
                        STATUS_INTEGRACAO,
                    )
                else:
                    status_action = "already_integration"

        covered = {parent_id, str(task_id)}
        if integration_step and integration_step.get("id"):
            covered.add(str(integration_step["id"]))
        for s in steps:
            if s.get("id"):
                covered.add(str(s["id"]))

        with _locks_guard:
            _recent_reconciliations[store_key] = {
                "time": time.time(),
                "covered_ids": covered,
            }

        return {
            "action": "reconciled",
            "store_name": parent.get("name"),
            "parent_custom_id": father_custom_id,
            "parent_id": parent_id,
            "integration_step_found": integration_step is not None,
            "dependency": dependency_action,
            "status": status_action,
            "trigger_task_name": event_task.get("name"),
            "trigger_task_id": task_id,
        }
