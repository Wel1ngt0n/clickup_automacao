"""Webhook ClickUp para manter dependencias de implantacao sincronizadas."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from flask import Flask, jsonify, request

from clickup_dependency_audit import (
    ClickUpClient,
    ClickUpError,
    DEFAULT_FATHER_FIELD_ID,
    DEFAULT_FLOW_STEP_FIELD_ID,
    DEFAULT_MAIN_LIST_ID,
    DEFAULT_WORKSPACE_ID,
    dependency_pairs,
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

DEFAULT_STEP_STATUS_MAP = {
    2: "cadastro omie",
    3: "onboarding",
    4: "criação de loja",
    5: "integração erp",
    6: "cadastro de produtos",
    7: "subir apps",
    8: "controle de qualidade",
    9: "treinamento",
    10: "loja entregue",
}
INITIAL_FLOW_STEPS = {2, 3, 4}


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variavel obrigatoria ausente: {name}")
    return value


def valid_signature(raw_body: bytes) -> bool:
    secret = required_env("CLICKUP_WEBHOOK_SECRET").encode("utf-8")
    received = request.headers.get("X-Signature", "")
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return bool(received) and hmac.compare_digest(received, expected)


def custom_field_value(task: dict, field_id: str):
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            value = field.get("value")
            return value.strip() if isinstance(value, str) else value
    return None


def status_name(task: dict) -> str:
    status = task.get("status") or {}
    return str(status.get("status", "") if isinstance(status, dict) else status).strip().lower()


def is_pending(task: dict) -> bool:
    status = task.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("type", "")).lower() not in {"done", "closed"}
    return status_name(task) not in {"finalizado", "concluído", "complete", "closed"}


def step_number(task: dict, field_id: str):
    value = custom_field_value(task, field_id)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def reconcile_parent_status(
    client: ClickUpClient, parent: dict, steps: list[dict], *, allow_later_steps: bool = True
) -> dict:
    flow_field_id = os.environ.get("CLICKUP_FLOW_STEP_FIELD_ID", DEFAULT_FLOW_STEP_FIELD_ID)
    candidates = []
    for step in steps:
        number = step_number(step, flow_field_id)
        if number not in INITIAL_FLOW_STEPS and not allow_later_steps:
            continue
        if number is not None and is_pending(step):
            candidates.append((number, step))

    if not candidates:
        return {"action": "unchanged", "reason": "no_pending_mapped_step"}

    number, current_step = min(candidates, key=lambda item: item[0])
    desired_status = DEFAULT_STEP_STATUS_MAP.get(number)
    if not desired_status:
        return {"action": "unchanged", "reason": "flow_step_without_status", "flow_step": number}

    current_status = status_name(parent)
    if current_status == desired_status:
        return {
            "action": "already_correct",
            "status": desired_status,
            "flow_step": number,
            "step_id": str(current_step["id"]),
        }

    client.update_task_status(str(parent["id"]), desired_status)
    return {
        "action": "status_updated",
        "before": current_status,
        "status": desired_status,
        "flow_step": number,
        "step_id": str(current_step["id"]),
    }


def initial_stage_gate(steps: list[dict], flow_field_id: str) -> dict:
    by_number = {
        number: step
        for step in steps
        if (number := step_number(step, flow_field_id)) in INITIAL_FLOW_STEPS
    }
    missing = sorted(INITIAL_FLOW_STEPS - set(by_number))
    pending = sorted(number for number, step in by_number.items() if is_pending(step))
    return {
        "open": not missing and not pending,
        "missing": missing,
        "pending": pending,
    }


def reconcile_task(task_id: str) -> dict:
    token = required_env("CLICKUP_API_TOKEN")
    workspace_id = os.environ.get("CLICKUP_WORKSPACE_ID", DEFAULT_WORKSPACE_ID)
    father_field_id = os.environ.get("CLICKUP_FATHER_FIELD_ID", DEFAULT_FATHER_FIELD_ID)
    main_list_id = os.environ.get("CLICKUP_MAIN_LIST_ID", DEFAULT_MAIN_LIST_ID)
    client = ClickUpClient(token, workspace_id)

    event_task = client.get_task_by_internal_id(task_id)
    father_custom_id = custom_field_value(event_task, father_field_id)
    event_list_id = str((event_task.get("list") or {}).get("id", ""))
    is_parent = event_list_id == main_list_id and bool(event_task.get("custom_id"))
    if father_custom_id:
        parent = client.get_task_by_custom_id(str(father_custom_id))
        step = event_task
    elif is_parent:
        parent = event_task
        father_custom_id = str(parent["custom_id"])
        step = None
    else:
        return {"action": "ignored", "reason": "task_outside_workflow", "task_id": task_id}

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
            "parent_id": str(parent.get("id", "")),
        }

    parent_id = str(parent["id"])
    steps = list(client.find_steps(father_field_id, str(father_custom_id)))
    # Inclui o evento mesmo se a busca indexada do ClickUp ainda nao refletir uma criacao recente.
    if step is not None and all(str(item["id"]) != str(step["id"]) for item in steps):
        steps.append(step)

    flow_field_id = os.environ.get("CLICKUP_FLOW_STEP_FIELD_ID", DEFAULT_FLOW_STEP_FIELD_ID)
    gate = initial_stage_gate(steps, flow_field_id)
    existing_dependencies = dependency_pairs(parent)
    dependencies_added = []
    dependencies_ok = []
    dependencies_deferred = []
    for related_step in steps:
        related_step_id = str(related_step["id"])
        related_step_number = step_number(related_step, flow_field_id)
        if related_step_number not in INITIAL_FLOW_STEPS and not gate["open"]:
            dependencies_deferred.append(related_step_id)
            continue
        if (parent_id, related_step_id) in existing_dependencies:
            dependencies_ok.append(related_step_id)
            continue
        client.add_waiting_on_dependency(parent_id, related_step_id)
        dependencies_added.append(related_step_id)
        existing_dependencies.add((parent_id, related_step_id))

    status_result = reconcile_parent_status(
        client, parent, steps, allow_later_steps=gate["open"]
    )
    return {
        "action": "reconciled",
        "parent_custom_id": father_custom_id,
        "parent_id": parent_id,
        "dependencies_added": dependencies_added,
        "dependencies_ok": dependencies_ok,
        "dependencies_deferred": dependencies_deferred,
        "initial_stage_gate": gate,
        "status": status_result,
        "steps_found": len(steps),
    }


@app.get("/")
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/webhook/clickup")
def clickup_webhook():
    raw_body = request.get_data(cache=True)
    try:
        if not valid_signature(raw_body):
            logger.warning("Webhook rejeitado: assinatura invalida")
            return jsonify({"error": "invalid_signature"}), 401
    except RuntimeError as exc:
        logger.error(str(exc))
        return jsonify({"error": "server_not_configured"}), 503

    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "")
    task_id = payload.get("task_id")
    if not event.startswith("task") or not task_id or event == "taskDeleted":
        return jsonify({"action": "ignored", "event": event}), 200

    try:
        result = reconcile_task(str(task_id))
        logger.info("Evento %s: %s", event, result)
        return jsonify(result), 200
    except ClickUpError as exc:
        # Retorna erro para o ClickUp tentar entregar novamente em falhas transitorias.
        logger.exception("Falha ao processar tarefa %s", task_id)
        return jsonify({"error": "clickup_api_error", "detail": str(exc)}), 502
    except RuntimeError as exc:
        logger.error(str(exc))
        return jsonify({"error": "server_not_configured"}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
