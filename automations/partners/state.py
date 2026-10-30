"""Durable queue and creation journal for the ClickUp partner workflow."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class PartnerStateError(RuntimeError):
    """A database failure with connection details deliberately omitted."""


def _lock_id(parent_id: str) -> int:
    value = f"clickup-partner:{parent_id}".encode()
    return int.from_bytes(
        hashlib.blake2b(value, digest_size=8).digest(), "big", signed=True
    )


class PartnerStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.strip()
        if not self.database_url.startswith(("postgres://", "postgresql://")):
            raise PartnerStateError(
                "Configure uma URL PostgreSQL para o fluxo de parceiros."
            )

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise PartnerStateError(
                "Instale a dependencia PostgreSQL do projeto."
            ) from None
        try:
            with psycopg.connect(
                self.database_url,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=10,
                options="-c statement_timeout=15000 -c lock_timeout=10000",
            ) as connection:
                yield connection
        except psycopg.Error:
            raise PartnerStateError(
                "Falha no armazenamento do fluxo de parceiros."
            ) from None

    def initialize(self) -> None:
        with self._connection() as connection:
            lock_id = _lock_id("__schema__")
            connection.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS clickup_partner_jobs (
                        parent_id TEXT PRIMARY KEY,
                        requested_version BIGINT NOT NULL DEFAULT 0,
                        processed_version BIGINT NOT NULL DEFAULT 0,
                        retry_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_error_code TEXT,
                        context_json JSONB
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS clickup_partner_operations (
                        parent_id TEXT NOT NULL,
                        operation_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'done')),
                        result_id TEXT,
                        PRIMARY KEY (parent_id, operation_key)
                    )"""
                )
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    def _one(self, query: str, parameters: tuple = ()) -> dict | None:
        with self._connection() as connection:
            return connection.execute(query, parameters).fetchone()

    def enqueue(self, parent_id: str) -> int:
        row = self._one(
            """INSERT INTO clickup_partner_jobs (parent_id, requested_version)
               VALUES (%s, 1)
               ON CONFLICT (parent_id) DO UPDATE SET
                   requested_version = clickup_partner_jobs.requested_version + 1,
                   retry_at = CURRENT_TIMESTAMP
               RETURNING requested_version""",
            (parent_id,),
        )
        if row is None:
            raise PartnerStateError("Não foi possível registrar o evento de parceiros.")
        return row["requested_version"]

    def pending_jobs(self, limit: int = 10) -> list[dict]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit deve ser um inteiro de 1 a 1000.")
        with self._connection() as connection:
            return connection.execute(
                """SELECT parent_id, requested_version AS version
                   FROM clickup_partner_jobs
                   WHERE requested_version > processed_version
                     AND retry_at <= CURRENT_TIMESTAMP
                   ORDER BY retry_at, parent_id LIMIT %s""",
                (limit,),
            ).fetchall()

    @contextmanager
    def store_lock(self, parent_id: str) -> Iterator[bool]:
        with self._connection() as connection:
            lock_id = _lock_id(parent_id)
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_id,)
            ).fetchone()
            acquired = bool(row["acquired"])
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    def job_context(self, parent_id: str, snapshot: dict) -> dict:
        if not isinstance(snapshot, dict):
            raise TypeError("snapshot deve ser um objeto.")
        row = self._one(
            """INSERT INTO clickup_partner_jobs (parent_id, context_json)
               VALUES (%s, %s::jsonb)
               ON CONFLICT (parent_id) DO UPDATE SET
                   context_json = COALESCE(clickup_partner_jobs.context_json,
                                           EXCLUDED.context_json)
               RETURNING context_json""",
            (parent_id, json.dumps(snapshot, ensure_ascii=False)),
        )
        if row is None:
            raise PartnerStateError(
                "Não foi possível registrar o contexto de parceiros."
            )
        return row["context_json"]

    def operation(self, parent_id: str, key: str) -> dict | None:
        return self._one(
            """SELECT state, result_id FROM clickup_partner_operations
               WHERE parent_id = %s AND operation_key = %s""",
            (parent_id, key),
        )

    def reserve_operation(self, parent_id: str, key: str) -> bool:
        row = self._one(
            """INSERT INTO clickup_partner_operations (parent_id, operation_key, state)
               VALUES (%s, %s, 'pending') ON CONFLICT DO NOTHING
               RETURNING operation_key""",
            (parent_id, key),
        )
        return row is not None

    def complete_operation(self, parent_id: str, key: str, result_id: str) -> None:
        if not isinstance(result_id, str) or not result_id.strip():
            raise ValueError("result_id deve identificar o objeto criado.")
        row = self._one(
            """UPDATE clickup_partner_operations SET state = 'done', result_id = %s
               WHERE parent_id = %s AND operation_key = %s
                 AND (result_id IS NULL OR result_id = %s)
               RETURNING operation_key""",
            (result_id, parent_id, key, result_id),
        )
        if row is None:
            raise PartnerStateError(
                "Reserva inexistente ou resultado conflitante da operacao."
            )

    def finish(self, parent_id: str, version: int) -> None:
        self._one(
            """UPDATE clickup_partner_jobs SET
                   processed_version = GREATEST(processed_version,
                                               LEAST(requested_version, %s)),
                   last_error_code = NULL, retry_at = CURRENT_TIMESTAMP
               WHERE parent_id = %s RETURNING parent_id""",
            (version, parent_id),
        )

    def retry(self, parent_id: str, delay_seconds: int, error_code: str) -> None:
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, int)
            or not 0 <= delay_seconds <= 86400
        ):
            raise ValueError("delay_seconds deve ser um inteiro de 0 a 86400.")
        safe_code = (
            error_code
            if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", error_code)
            else "processing_error"
        )
        self._one(
            """UPDATE clickup_partner_jobs SET
                   retry_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                   last_error_code = %s
               WHERE parent_id = %s RETURNING parent_id""",
            (delay_seconds, safe_code, parent_id),
        )
