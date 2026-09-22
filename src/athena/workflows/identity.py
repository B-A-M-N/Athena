"""Immutable workflow-run identity and nested-run key mechanics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.ids import new_id


def canonical_hash(value: Any) -> str:
    """Encode one workflow identity value deterministically."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def workflow_run_id(workflow_id: str, task_id: str | None, parent_call_id: str) -> str:
    suffix = canonical_hash(
        {"workflow_id": workflow_id, "task_id": task_id or "", "parent_call_id": parent_call_id}
    )[:32]
    return f"workflow-run-{suffix}"


class WorkflowIdentity:
    """Pure identity codec used by the durable run transaction owner."""

    @staticmethod
    def new_run(workflow_id: str, task_id: str | None, parent_call_id: str | None) -> str:
        return (
            workflow_run_id(workflow_id, task_id, parent_call_id)
            if parent_call_id
            else new_id("workflow-run")
        )

    @staticmethod
    def expected(
        *,
        definition_hash: str | None,
        input_hash: str,
        workspace_identity: str,
        workspace_revision: str | None,
        environment_identity: str,
    ) -> dict[str, str]:
        return {
            "definition_hash": definition_hash or "",
            "input_hash": input_hash,
            "workspace_identity": workspace_identity,
            "workspace_revision": workspace_revision or "",
            "environment_identity": environment_identity,
        }

    @staticmethod
    def validate(run_id: str, row: Mapping[str, Any], expected: Mapping[str, str]) -> None:
        for field in (
            "definition_hash",
            "input_hash",
            "workspace_identity",
            "workspace_revision",
            "environment_identity",
        ):
            actual = str(row.get(field) or "")
            wanted = str(expected.get(field) or "")
            if not actual or actual != wanted:
                raise ValueError(f"workflow run {run_id} {field} does not match immutable identity")


__all__ = ["WorkflowIdentity", "canonical_hash", "workflow_run_id"]
