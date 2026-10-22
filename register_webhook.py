"""Registra no ClickUp o endpoint ja publicado no Render."""

from __future__ import annotations

import argparse
import json
import os
import sys

from clickup_dependency_audit import ClickUpClient, ClickUpError, DEFAULT_WORKSPACE_ID


def main() -> int:
    parser = argparse.ArgumentParser(description="Registra o webhook no ClickUp.")
    parser.add_argument("--url", required=True, help="Ex.: https://servico.onrender.com/webhook/clickup")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    args = parser.parse_args()

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        print("Erro: defina CLICKUP_API_TOKEN.", file=sys.stderr)
        return 2

    client = ClickUpClient(token, args.workspace_id)
    try:
        result = client.request(
            "POST",
            f"/team/{args.workspace_id}/webhook",
            body={
                "endpoint": args.url,
                "events": ["taskCreated", "taskUpdated", "taskMoved"],
            },
        )
    except ClickUpError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\nCopie o campo secret para CLICKUP_WEBHOOK_SECRET no Render.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
