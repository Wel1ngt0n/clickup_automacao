#!/usr/bin/env python3
"""Audita e corrige dependencias entre cards principais e etapas no ClickUp."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.clickup.com/api/v2"
DEFAULT_WORKSPACE_ID = "36953879"
DEFAULT_MAIN_LIST_ID = "211186088"
DEFAULT_FATHER_FIELD_ID = "553cb505-acc0-401e-8760-f73879a3aad7"
DEFAULT_FLOW_STEP_FIELD_ID = "eeb8ae22-76b0-4441-a6e8-d8528998d102"


class ClickUpError(RuntimeError):
    pass


class ClickUpClient:
    def __init__(self, token: str, workspace_id: str) -> None:
        self.token = token
        self.workspace_id = workspace_id

    def request(self, method: str, path: str, *, query=None, body=None):
        url = f"{API_BASE}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise ClickUpError(f"ClickUp retornou HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise ClickUpError(f"Falha de conexao com o ClickUp: {exc.reason}") from exc

    def get_task_by_custom_id(self, custom_id: str):
        return self.request(
            "GET",
            f"/task/{urllib.parse.quote(custom_id)}",
            query={"custom_task_ids": "true", "team_id": self.workspace_id},
        )

    def get_task_by_internal_id(self, task_id: str):
        return self.request("GET", f"/task/{urllib.parse.quote(task_id)}")

    def list_main_tasks(self, list_id: str):
        page = 0
        while True:
            payload = self.request(
                "GET",
                f"/list/{list_id}/task",
                query={"page": page, "include_closed": "true", "subtasks": "true"},
            )
            tasks = payload.get("tasks", [])
            yield from tasks
            if len(tasks) < 100:
                break
            page += 1

    def find_steps(self, father_field_id: str, custom_id: str):
        page = 0
        field_filter = json.dumps(
            [{"field_id": father_field_id, "operator": "==", "value": custom_id}],
            separators=(",", ":"),
        )
        while True:
            payload = self.request(
                "GET",
                f"/team/{self.workspace_id}/task",
                query={
                    "page": page,
                    "include_closed": "true",
                    "subtasks": "true",
                    "custom_fields": field_filter,
                },
            )
            tasks = payload.get("tasks", [])
            yield from tasks
            if payload.get("last_page") is True or len(tasks) < 100:
                break
            page += 1

    def add_waiting_on_dependency(self, parent_internal_id: str, step_internal_id: str):
        return self.request(
            "POST",
            f"/task/{parent_internal_id}/dependency",
            body={"depends_on": step_internal_id},
        )

    def update_task_status(self, task_id: str, status: str):
        return self.request("PUT", f"/task/{task_id}", body={"status": status})


def dependency_pairs(task: dict) -> set[tuple[str, str]]:
    return {
        (str(dep.get("task_id")), str(dep.get("depends_on")))
        for dep in task.get("dependencies", [])
        if dep.get("task_id") and dep.get("depends_on")
    }


def audit_one(client: ClickUpClient, parent: dict, father_field_id: str, apply: bool) -> dict:
    custom_id = parent.get("custom_id")
    if not custom_id:
        return {"custom_id": None, "name": parent.get("name"), "status": "ignorado_sem_custom_id"}

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
    scope.add_argument("--custom-id", help="ID personalizado do card principal, ex.: F0H-797")
    scope.add_argument(
        "--all",
        action="store_true",
        help="Analisa todos os cards da lista principal",
    )
    parser.add_argument("--apply", action="store_true", help="Adiciona dependencias ausentes")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--list-id", default=DEFAULT_MAIN_LIST_ID)
    parser.add_argument("--father-field-id", default=DEFAULT_FATHER_FIELD_ID)
    parser.add_argument("--json", action="store_true", help="Exibe o relatorio como JSON")
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

        report = [audit_one(client, parent, args.father_field_id, args.apply) for parent in parents]
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
