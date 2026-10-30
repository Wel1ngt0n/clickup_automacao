"""Fila durável e acompanhamento local dos projetos de parceiros."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from automations.partners.config import (
    MAIN_LIST_ID,
    build_snapshot,
    existing_client,
    load_config,
)
from automations.partners.events import configured_store, partner_enabled
from automations.partners.service import is_archived, is_enrolled, reconcile_partner
from automations.partners.state import PartnerStateError, PartnerStore
from common.clickup import DEFAULT_WORKSPACE_ID, ClickUpClient, ClickUpError

logger = logging.getLogger(__name__)


def poll_parents(client: ClickUpClient, store: PartnerStore, config: dict) -> int:
    count = 0
    for parent in client.list_main_tasks(config["main_list_id"]):
        if not parent.get("parent") and not is_archived(parent) and is_enrolled(parent):
            store.enqueue(str(parent["id"]))
            count += 1
    return count


def run_pending(client: ClickUpClient, store: PartnerStore, config: dict) -> int:
    count = 0
    for job in store.pending_jobs(limit=10):
        parent_id = job["parent_id"]
        with store.store_lock(parent_id) as acquired:
            if not acquired:
                continue
            try:
                result = reconcile_partner(client, store, parent_id, config)
                if result["action"] == "blocked":
                    logger.warning(
                        "Fluxo=parceiros | loja=%r | pai=%s | pendencia=%s",
                        result.get("store_name"),
                        parent_id,
                        result["reason"],
                    )
                    store.retry(parent_id, 60, result["reason"])
                else:
                    store.finish(parent_id, job["version"])
                count += 1
            except ClickUpError as exc:
                code = f"clickup_http_{exc.status_code or 'network'}"
                logger.warning(
                    "Fluxo=parceiros | pai=%s | erro=%s | retomada_pendente",
                    parent_id,
                    code,
                )
                store.retry(parent_id, 60, code)
    return count


def preflight(client: ClickUpClient, config: dict) -> None:
    main = client.request("GET", f"/list/{config['main_list_id']}")
    statuses = {s["status"] for s in main.get("statuses", [])}
    required = {s["parent_status"] for s in config["stages"]} | {"implantado"}
    if required - statuses:
        raise ValueError("Há status obrigatórios ausentes no quadro de parceiros")
    for stage in config["stages"]:
        destination = client.request("GET", f"/list/{stage['list_id']}")
        if (
            str((destination.get("folder") or {}).get("id"))
            != config["stage_folder_id"]
        ):
            raise ValueError(f"Lista fora da pasta autorizada: {stage['key']}")
        initial_status = stage.get(
            "initial_status", config["defaults"]["initial_status"]
        )
        allowed_statuses = {
            s["status"]
            for s in destination.get("statuses", [])
            if s.get("type") in {"open", "custom"}
        }
        if initial_status not in allowed_statuses:
            raise ValueError(f"Status inicial ausente: {stage['key']}")
        available = client.request("GET", f"/list/{stage['list_id']}/field")
        field_ids = {f["id"] for f in available.get("fields", [])}
        if {
            config["fields"]["parent_task_id"],
            config["fields"]["stage_order"],
        } - field_ids:
            raise ValueError(f"Campos de vínculo/ordem ausentes: {stage['key']}")


def run_cycle(
    client, store, config: dict, next_poll: float, poll_seconds: int
) -> float:
    now = time.monotonic()
    if poll_seconds and now >= next_poll:
        poll_parents(client, store, config)
        next_poll = time.monotonic() + poll_seconds
    # Eventos enfileirados não aguardam o intervalo da consulta de recuperação.
    run_pending(client, store, config)
    return next_poll


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Executa um ciclo e encerra"
    )
    parser.add_argument(
        "--validate", action="store_true", help="Valida modelos sem rede ou banco"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Confere acesso, listas e campos, sem criar cards",
    )
    parser.add_argument(
        "--preview-parent",
        help="Simula uma loja pelo ID interno, sem escrever no ClickUp ou banco",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="Intervalo da consulta de recuperação ao ClickUp; 0 desabilita essa consulta",
    )
    parser.add_argument(
        "--queue-seconds",
        type=float,
        default=2,
        help="Intervalo da leitura da fila de eventos; padrão 2 segundos",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config = load_config(require_ready=True)
        if args.validate:
            logger.info(
                "Modelos validados: %d etapas, incluindo Financeiro",
                len(config["stages"]),
            )
            return 0
        if not partner_enabled():
            raise ValueError(
                "Habilite CLICKUP_PARTNER_ENABLED para iniciar o processo local"
            )
        token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "Preencha CLICKUP_API_TOKEN no arquivo .env local antes de iniciar"
            )
        if args.poll_seconds != 0 and args.poll_seconds < 15:
            raise ValueError(
                "Intervalo de consulta deve ser 0 ou pelo menos 15 segundos"
            )
        if not 0.5 <= args.queue_seconds <= 60:
            raise ValueError("Intervalo de fila deve estar entre 0.5 e 60 segundos")
        client = ClickUpClient(
            token, os.environ.get("CLICKUP_WORKSPACE_ID", DEFAULT_WORKSPACE_ID).strip()
        )
        client.min_request_interval = 1.0
        if args.preview_parent:
            parent = client.get_task_by_internal_id(args.preview_parent)
            answer = existing_client(parent, config["fields"]["existing_client"])
            if (
                str((parent.get("list") or {}).get("id")) != MAIN_LIST_ID
                or is_archived(parent)
                or answer is None
            ):
                raise ValueError(
                    "Loja fora do escopo ou sem resposta válida no formulário"
                )
            snapshot = build_snapshot(config, parent, answer)
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "store_name": snapshot["store_name"],
                        "existing_client": answer,
                        "stages": [
                            {
                                "key": s["key"],
                                "name": s["payload"]["name"],
                                "list_id": s["list_id"],
                                "checklists": s["checklists"],
                            }
                            for s in snapshot["stages"]
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        preflight(client, config)
        if args.check:
            logger.info("Acesso e estrutura ClickUp validados; nenhuma tarefa alterada")
            return 0
        store = configured_store()
        enrollment = store.job_context(
            "__partner_enrollment__", {"start_after_ms": int(time.time() * 1000)}
        )
        if not os.environ.get("CLICKUP_PARTNER_START_AFTER", "").strip():
            os.environ["CLICKUP_PARTNER_START_AFTER"] = str(
                enrollment["start_after_ms"]
            )
        logger.info(
            "Fluxo parceiros iniciado: consulta=%ds | fila=%.1fs | novos projetos após=%s | limite_por_ids=%s",
            args.poll_seconds,
            args.queue_seconds,
            os.environ["CLICKUP_PARTNER_START_AFTER"],
            bool(os.environ.get("CLICKUP_PARTNER_ALLOWED_PARENT_IDS", "").strip()),
        )
        next_poll = 0.0
        while True:
            config = load_config(require_ready=True)
            try:
                next_poll = run_cycle(
                    client, store, config, next_poll, args.poll_seconds
                )
            except ClickUpError as exc:
                logger.warning(
                    "Consulta ClickUp falhou: HTTP=%s; tentando no próximo ciclo",
                    exc.status_code or "network",
                )
            if args.once:
                return 0
            time.sleep(args.queue_seconds)
    except ClickUpError as exc:
        logger.error(
            "Falha no acesso inicial ao ClickUp: HTTP=%s", exc.status_code or "network"
        )
        return 1
    except (ValueError, OSError, PartnerStateError) as exc:
        logger.error("Não foi possível iniciar/manter parceiros: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
