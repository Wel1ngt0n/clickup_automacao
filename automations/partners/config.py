"""Modelos editáveis e regras de entrada da implantação de parceiros."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "templates" / "modelos.json"
MAIN_LIST_ID = "901327379509"
STAGE_LIST_IDS = {
    "901328073754",
    "901327577128",
    "901327577146",
    "901328325681",
    "901327577197",
    "901327577154",
    "901327577160",
    "1400600000003634",
    "901328325698",
}
STAGE_DESTINATIONS = dict(
    zip(
        (
            "onboarding",
            "criar_loja",
            "integracao_erp",
            "integracao_marketplace",
            "cadastro_produtos",
            "qualidade",
            "treinamento",
            "configuracao_financeira",
            "pos_implantacao",
        ),
        (
            "901328073754",
            "901327577128",
            "901327577146",
            "901328325681",
            "901327577197",
            "901327577154",
            "901327577160",
            "1400600000003634",
            "901328325698",
        ),
    )
)
EXISTING_CLIENT_OPTIONS = {
    "0144f848-2585-46d1-a966-daceaf49108d": True,
    "f93c28ca-f8e1-4814-9d48-6f1d1092ae0d": False,
    "0": True,
    "1": False,
    "sim": True,
    "não": False,
    "nao": False,
}


def field_value(task: dict, field_id: str):
    return next(
        (
            f.get("value")
            for f in task.get("custom_fields", [])
            if f.get("id") == field_id
        ),
        None,
    )


def existing_client(task: dict, field_id: str) -> bool | None:
    value = field_value(task, field_id)
    if isinstance(value, dict):
        values = [
            existing_client(
                {"custom_fields": [{"id": field_id, "value": value[k]}]}, field_id
            )
            for k in ("id", "orderindex", "name")
            if k in value
        ]
        known = [item for item in values if item is not None]
        return known[0] if known and len(set(known)) == 1 else None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value not in (0, 1):
            return None
        value = str(int(value))
    return EXISTING_CLIENT_OPTIONS.get(str(value).strip().lower())


def _unique(values: list, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Valores repetidos: {label}")


def load_config(path: Path | str | None = None, *, require_ready: bool = False) -> dict:
    catalog_path = Path(path or CATALOG_PATH).resolve()
    config = json.loads(catalog_path.read_text(encoding="utf-8"))
    stages = config["stages"]
    if config.get("schema_version") != 1 or config.get("main_list_id") != MAIN_LIST_ID:
        raise ValueError("Catálogo de parceiros inválido")
    if [s["order"] for s in stages] != list(range(1, 10)):
        raise ValueError("As nove etapas devem estar na ordem 1–9")
    if [s["key"] for s in stages] != list(STAGE_DESTINATIONS):
        raise ValueError("Chaves ou ordem das etapas fora do fluxo de parceiros")
    if type(config.get("enabled")) is not bool:
        raise ValueError("enabled deve ser booleano")
    for key in ("key", "parent_status"):
        _unique([s[key] for s in stages], key)
    _unique([s["list_id"] for s in stages if s.get("list_id")], "list_id")
    for stage in stages:
        if (
            stage.get("list_id") is not None
            and stage["list_id"] != STAGE_DESTINATIONS[stage["key"]]
        ):
            raise ValueError("Destino da etapa fora da lista autorizada")
        _validate_stage(stage, catalog_path.parent)
    if require_ready and (
        not config.get("enabled")
        or config.get("review_status") != "approved"
        or any(
            not s.get("list_id") or s.get("review_status") != "approved" for s in stages
        )
    ):
        raise ValueError(
            "Modelos precisam estar aprovados, habilitados e com todas as listas definidas"
        )
    return config


def task_assignee_ids(task: dict) -> list[int]:
    ids = [
        item.get("id") if isinstance(item, dict) else item
        for item in task.get("assignees", [])
    ]
    return sorted(
        {
            int(str(item))
            for item in ids
            if re.fullmatch(r"[0-9]+", str(item)) and int(str(item)) > 0
        }
    )


def stage_assignees(config: dict, stage: dict, parent: dict) -> list[int]:
    if stage.get("assignee_source", "fixed") == "parent":
        return task_assignee_ids(parent)
    return sorted(
        {
            int(item)
            for item in stage.get("assignee_ids", config["defaults"]["assignee_ids"])
        }
    )


def assignment_allowed(assignee_ids: list[int]) -> bool:
    restriction = os.environ.get("CLICKUP_PARTNER_ALLOWED_ASSIGNEE_IDS", "").strip()
    if not restriction:
        return True
    allowed = {item.strip() for item in restriction.split(",") if item.strip()}
    return all(str(user_id) in allowed for user_id in assignee_ids)


def _validate_stage(stage: dict, root: Path) -> None:
    expected = "new_clients" if stage["key"] == "cadastro_produtos" else "all"
    if stage["applies_to"] != expected:
        raise ValueError("Somente Cadastro de produtos pode ser condicional")
    path = (root / stage["description_file"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Arquivo de descrição inválido")
    stage["description_template"] = path.read_text(encoding="utf-8")
    allowed = {"nome_loja", "id_tarefa_pai", "url_card_principal"}
    markers = re.findall(
        r"\{\{(.*?)\}\}", stage["name_template"] + stage["description_template"]
    )
    if set(markers) - allowed:
        raise ValueError("Marcador desconhecido no modelo")
    checklists = stage["checklists"]
    if not checklists or any(
        not c["name"].strip() or not c["items"] for c in checklists
    ):
        raise ValueError("Checklists não podem estar vazios")
    _unique([c["name"] for c in checklists], "checklists")
    for checklist in checklists:
        if any(not isinstance(i, str) or not i.strip() for i in checklist["items"]):
            raise ValueError("Item de checklist inválido")
        _unique(checklist["items"], "itens do checklist")
    days = stage.get("due_after_days")
    if days is not None and (type(days) is not int or days < 0):
        raise ValueError("Prazo deve ser um inteiro não negativo ou null")
    if stage.get("assignee_source", "fixed") not in {"fixed", "parent"}:
        raise ValueError("Origem de responsável deve ser fixed ou parent")
    if stage.get("assignee_source") == "parent" and stage.get("assignee_ids"):
        raise ValueError("Etapa que herda responsável não pode ter IDs fixos")
    if any(
        not re.fullmatch(r"[0-9]+", str(i)) or int(i) <= 0
        for i in stage.get("assignee_ids", [])
    ):
        raise ValueError("Responsáveis devem ser IDs numéricos do ClickUp")


def build_snapshot(config: dict, parent: dict, client_exists: bool) -> dict:
    store_name = str(
        field_value(parent, config["fields"]["store_name"]) or parent.get("name") or ""
    ).strip()
    if not store_name:
        raise ValueError("Nome da loja ausente")
    values = {
        "nome_loja": store_name,
        "id_tarefa_pai": str(parent["id"]),
        "url_card_principal": parent.get("url")
        or f"https://app.clickup.com/t/{parent['id']}",
    }
    snapshot: dict = {
        "existing_client": client_exists,
        "store_name": store_name,
        "stages": [],
    }
    for original in config["stages"]:
        if original["applies_to"] == "new_clients" and client_exists:
            continue
        stage = copy.deepcopy(original)
        description = _render(stage.pop("description_template"), values)
        marker = f"Automação parceiros: parent={parent['id']}; etapa={stage['key']}"
        stage["payload"] = {
            "name": _render(stage["name_template"], values),
            "markdown_content": description + "\n\n" + marker,
            "status": stage.get("initial_status", config["defaults"]["initial_status"]),
            "assignees": stage_assignees(config, stage, parent),
            "notify_all": False,
            "custom_fields": [
                {"id": config["fields"]["parent_task_id"], "value": str(parent["id"])},
                {"id": config["fields"]["stage_order"], "value": stage["order"]},
            ],
        }
        days = stage.get("due_after_days", config["defaults"]["due_after_days"])
        if days is not None:
            stage["payload"]["due_date"] = int(time.time() * 1000) + days * 86_400_000
        snapshot["stages"].append(stage)
    return snapshot


def _render(template: str, values: dict) -> str:
    return re.sub(r"\{\{(.*?)\}\}", lambda m: values[m.group(1)], template)
