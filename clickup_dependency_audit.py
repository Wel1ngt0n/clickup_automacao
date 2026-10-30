"""Entrada compatível; a implementação está em automations.integration_repair.audit."""

from automations.integration_repair.audit import audit_one, main, parse_args
from automations.integration_repair.config import (
    DEFAULT_FATHER_FIELD_ID,
    DEFAULT_FLOW_STEP_FIELD_ID,
    DEFAULT_MAIN_LIST_ID,
)
from common.clickup import (
    DEFAULT_WORKSPACE_ID,
    ClickUpClient,
    ClickUpError,
    dependency_pairs,
)

__all__ = [
    "DEFAULT_FATHER_FIELD_ID",
    "DEFAULT_FLOW_STEP_FIELD_ID",
    "DEFAULT_MAIN_LIST_ID",
    "DEFAULT_WORKSPACE_ID",
    "ClickUpClient",
    "ClickUpError",
    "audit_one",
    "dependency_pairs",
    "main",
    "parse_args",
]

if __name__ == "__main__":
    raise SystemExit(main())
