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


def reconcile_parent_status(client: ClickUpClient, parent: dict, steps: list[dict]) -> dict:
    flow_field_id = os.environ.get("CLICKUP_FLOW_STEP_FIELD_ID", DEFAULT_FLOW_STEP_FIELD_ID)
    candidates = []
    for step in steps:
        number = step_number(step, flow_field_id)
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

    parent_id = str(parent["id"])
    dependency_action = "not_applicable"
    if step is not None:
        step_id = str(step["id"])
        if (parent_id, step_id) in dependency_pairs(parent):
            dependency_action = "already_exists"
        else:
            client.add_waiting_on_dependency(parent_id, step_id)
            dependency_action = "dependency_added"

    steps = list(client.find_steps(father_field_id, str(father_custom_id)))
    # Inclui o evento mesmo se a busca indexada do ClickUp ainda nao refletir uma criacao recente.
    if step is not None and all(str(item["id"]) != str(step["id"]) for item in steps):
        steps.append(step)
    status_result = reconcile_parent_status(client, parent, steps)
    return {
        "action": "reconciled",
        "parent_custom_id": father_custom_id,
        "parent_id": parent_id,
        "dependency": dependency_action,
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
