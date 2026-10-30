"""Recebimento e enfileiramento dos eventos exclusivos de parceiros."""

from __future__ import annotations

import os

from automations.partners.config import (
    MAIN_LIST_ID,
    STAGE_LIST_IDS,
    field_value,
    load_config,
)
from automations.partners.service import is_archived, is_enrolled
from automations.partners.state import PartnerStore
from common.clickup import ClickUpClient


def partner_enabled() -> bool:
    return os.environ.get("CLICKUP_PARTNER_ENABLED", "false").strip().lower() == "true"


def configured_store() -> PartnerStore:
    store = PartnerStore(os.environ.get("PARTNER_DATABASE_URL", ""))
    store.initialize()
    return store


def queue_partner_event(client: ClickUpClient, event_task: dict) -> dict | None:
    list_id = str((event_task.get("list") or {}).get("id", ""))
    if list_id not in {MAIN_LIST_ID, *STAGE_LIST_IDS}:
        return None
    base = {
        "workflow": "parceiros",
        "task_name": event_task.get("name"),
        "task_id": str(event_task["id"]),
    }
    if not partner_enabled():
        return dict(base, action="ignored", reason="partner_disabled")
    config = load_config(require_ready=True)
    if list_id == MAIN_LIST_ID:
        parent = event_task
    else:
        parent_id = field_value(event_task, config["fields"]["parent_task_id"])
        if not isinstance(parent_id, str) or not parent_id.strip():
            return dict(base, action="ignored", reason="missing_partner_parent_id")
        parent = client.get_task_by_internal_id(parent_id.strip())
    if str((parent.get("list") or {}).get("id")) != MAIN_LIST_ID or is_archived(parent):
        return dict(base, action="ignored", reason="invalid_or_archived_partner_parent")
    if not is_enrolled(parent):
        return dict(base, action="ignored", reason="parent_not_enrolled")
    version = configured_store().enqueue(str(parent["id"]))
    return dict(
        base,
        action="queued",
        parent_id=str(parent["id"]),
        store_name=parent.get("name"),
        version=version,
    )
