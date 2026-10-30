"""Auditoria manual de dependências do quadro principal."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from automations.integration_repair.config import (
    DEFAULT_FATHER_FIELD_ID,
    DEFAULT_MAIN_LIST_ID,
)
from common.clickup import (
    DEFAULT_WORKSPACE_ID,
    ClickUpClient,
    ClickUpError,
    dependency_pairs,
)


def audit_one(
    client: ClickUpClient, parent: dict, father_field_id: str, apply: bool
) -> dict:
    custom_id = parent.get("custom_id")
    if not custom_id:
        return {
            "custom_id": None,
            "name": parent.get("name"),
            "status": "ignorado_sem_custom_id",
        }

    # A resposta da listagem pode ser resumida; GET Task garante as dependencias atuais.
    full_parent = client.get_task_by_custom_id(custom_id)
    parent_id = str(full_parent["id"])
    existing = dependency_pairs(full_parent)
    steps = list(client.find_steps(father_field_id, custom_id))
    results = []

    for step in sorted(steps, key=lambda item: item.get("name", "")):
        step_id = str(step["id"])
        pair = (parent_id, step_id)
        if pair in existing:
            state = "ok"
        elif apply:
            client.add_waiting_on_dependency(parent_id, step_id)
            state = "adicionada"
            time.sleep(0.12)
        else:
            state = "faltando"
        results.append(
            {
                "task_id": step_id,
                "custom_id": step.get("custom_id"),
                "name": step.get("name"),
                "status": state,
            }
        )

    return {
        "custom_id": custom_id,
        "task_id": parent_id,
        "name": full_parent.get("name"),
        "status": "analisado",
        "steps_found": len(steps),
        "dependencies_ok": sum(item["status"] == "ok" for item in results),
        "dependencies_missing": sum(item["status"] == "faltando" for item in results),
        "dependencies_added": sum(item["status"] == "adicionada" for item in results),
        "steps": results,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Audita dependencias das etapas usando o campo _father_task_id."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--custom-id", help="ID personalizado do card principal, ex.: F0H-797"
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="Analisa todos os cards da lista principal",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Adiciona dependencias ausentes"
    )
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--list-id", default=DEFAULT_MAIN_LIST_ID)
    parser.add_argument("--father-field-id", default=DEFAULT_FATHER_FIELD_ID)
    parser.add_argument(
        "--json", action="store_true", help="Exibe o relatorio como JSON"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        print("Erro: defina a variavel CLICKUP_API_TOKEN.", file=sys.stderr)
        return 2

    client = ClickUpClient(token, args.workspace_id)
    try:
        if args.custom_id:
            parents = [client.get_task_by_custom_id(args.custom_id)]
        else:
            parents = list(client.list_main_tasks(args.list_id))

        report = [
            audit_one(client, parent, args.father_field_id, args.apply)
            for parent in parents
        ]
    except ClickUpError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "CORRECAO" if args.apply else "SOMENTE ANALISE"
        print(f"Modo: {mode}")
        for item in report:
            if item["status"] != "analisado":
                print(f"- {item.get('name')}: {item['status']}")
                continue
            print(
                f"- {item['custom_id']} | {item['name']}: "
                f"{item['dependencies_ok']} ok, "
                f"{item['dependencies_missing']} faltando, "
                f"{item['dependencies_added']} adicionadas"
            )
            for step in item["steps"]:
                print(f"    [{step['status']}] {step['name']} ({step['task_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
