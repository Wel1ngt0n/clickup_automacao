"""Entrada HTTP compartilhada: valida assinatura e encaminha para cada automação."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from flask import Flask, jsonify, request

from automations.integration_repair.service import reconcile_event_task
from automations.partners.events import queue_partner_event
from automations.partners.state import PartnerStateError
from common.clickup import DEFAULT_WORKSPACE_ID, ClickUpClient, ClickUpError
from common.env import required_env

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
app = Flask(__name__)


def valid_signature(raw_body: bytes) -> bool:
    secret = required_env("CLICKUP_WEBHOOK_SECRET").encode("utf-8")
    received = request.headers.get("X-Signature", "")
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return bool(received) and hmac.compare_digest(received, expected)


def reconcile_task(task_id: str) -> dict:
    client = ClickUpClient(
        required_env("CLICKUP_API_TOKEN"),
        os.environ.get("CLICKUP_WORKSPACE_ID", DEFAULT_WORKSPACE_ID).strip(),
    )
    event_task = client.get_task_by_internal_id(task_id)
    partner_result = queue_partner_event(client, event_task)
    if partner_result is not None:
        return partner_result
    return reconcile_event_task(client, event_task)


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
        action = result.get("action")
        if action == "queued":
            logger.info(
                "Evento=%s | fluxo=parceiros | loja=%r | pai=%s | enfileirado",
                event,
                result.get("store_name"),
                result.get("parent_id"),
            )
        elif action == "reconciled":
            logger.info(
                "Evento=%s | loja=%r | custom_id=%s | dep_integracao=%s | status=%s | trigger=%r (%s)",
                event,
                result.get("store_name"),
                result.get("parent_custom_id"),
                result.get("dependency"),
                result.get("status"),
                result.get("trigger_task_name"),
                result.get("trigger_task_id"),
            )
        elif action == "debounced":
            logger.info(
                "Evento=%s | loja=%r | custom_id=%s | debounced (reconciliado ha %.1fs)",
                event,
                result.get("store_name"),
                result.get("parent_custom_id"),
                result.get("reconciled_seconds_ago", 0),
            )
        else:
            logger.info(
                "Evento=%s | tarefa=%r (%s) | ignorado: %s",
                event,
                result.get("task_name"),
                task_id,
                result.get("reason"),
            )
        return jsonify(result), 200
    except ClickUpError as exc:
        logger.exception("Falha ao processar tarefa %s", task_id)
        is_rate_limit = getattr(exc, "status_code", None) == 429
        status_code = 429 if is_rate_limit else 502
        response = jsonify({"error": "clickup_api_error", "detail": str(exc)})
        if is_rate_limit:
            response.headers["Retry-After"] = "15"
        return response, status_code
    except (PartnerStateError, ValueError) as exc:
        logger.error(
            "Configuração/armazenamento de parceiros indisponível: %s",
            type(exc).__name__,
        )
        return jsonify({"error": "partner_not_ready"}), 503
    except RuntimeError as exc:
        logger.error(str(exc))
        return jsonify({"error": "server_not_configured"}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))  # nosec B104 # bind exigido pelo Render
