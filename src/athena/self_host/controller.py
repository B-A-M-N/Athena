"""Small, durable PLAN/PATCH/PROVE/PROMOTE coordination helpers.

The controller does not reason, execute, or mutate a workspace.  It only turns
one operator objective plus source-verified evidence into a bounded work item
and advances that record after the canonical ShadowEngine promotion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from athena.protocol.ids import new_id


class SelfHostMissionController:
    """Deterministic mission-state transitions above the ordinary task engine."""

    @staticmethod
    def initial_plan(
        objective: str,
        *,
        index: Any,
        design_bundle_hash: str,
        gate_bundle_hash: str,
        base_fingerprint: str,
    ) -> dict[str, Any]:
        """Create one bounded item grounded in the current source index."""
        item_id = new_id("self-work")
        files = tuple(
            str(item.get("path"))
            for item in (getattr(index, "files", ()) or ())[:64]
            if isinstance(item, Mapping) and item.get("path")
        )
        item = {
            "id": item_id,
            "title": str(objective).strip() or "bounded Athena improvement",
            "objective": str(objective).strip(),
            "affected_files": list(files),
            "affected_invariants": ["candidate-isolated", "proof-before-promotion"],
            "dependencies": [],
            "risk": "pending-diff-classification",
            "status": "current",
            "task_id": None,
            "branch_id": None,
            "certificate_hash": None,
        }
        return {
            "version": 1,
            "bounded": True,
            "phase": "PLAN",
            "current_work_item": item,
            "work_items": [item],
            "completed_work_items": [],
            "blocked_work_items": [],
            "evidence": {
                "index_revision": str(getattr(index, "index_revision", "") or ""),
                "source_revision": str(getattr(index, "source_revision", "") or ""),
                "design_bundle_hash": design_bundle_hash,
                "gate_bundle_hash": gate_bundle_hash,
                "base_fingerprint": base_fingerprint,
            },
            "step": 1,
        }

    @staticmethod
    def task_prompt(plan: Mapping[str, Any]) -> str:
        """Bind the ordinary coding task to the frozen PLAN evidence."""
        item = plan.get("current_work_item") or {}
        evidence = plan.get("evidence") or {}
        return (
            "[ATHENA SELF PLAN]\n"
            f"Bounded work item: {item.get('title') or item.get('objective') or ''}\n"
            f"Source-verified index revision: {evidence.get('index_revision') or 'unknown'}\n"
            f"Frozen design bundle: {evidence.get('design_bundle_hash') or 'unknown'}\n"
            f"Frozen gate bundle: {evidence.get('gate_bundle_hash') or 'unknown'}\n"
            "Modify only the isolated candidate. Do not broaden the objective.\n\n"
            f"Objective:\n{item.get('objective') or ''}"
        )

    @staticmethod
    def mark_task(plan: Mapping[str, Any], task_id: str) -> dict[str, Any]:
        updated = _copy_plan(plan)
        updated.pop("review", None)
        item = dict(updated.get("current_work_item") or {})
        item["task_id"] = task_id
        item["status"] = "in_progress"
        updated["current_work_item"] = item
        updated["work_items"] = [
            item if value.get("id") == item.get("id") else value
            for value in updated.get("work_items", ())
            if isinstance(value, Mapping)
        ]
        updated["phase"] = "PATCH"
        return updated

    @staticmethod
    def mark_promoted(
        plan: Mapping[str, Any],
        *,
        branch_id: str,
        certificate_hash: str | None,
        candidate_fingerprint: str | None,
    ) -> dict[str, Any]:
        updated = _copy_plan(plan)
        item = dict(updated.get("current_work_item") or {})
        item.update(
            {
                "status": "completed",
                "branch_id": branch_id,
                "certificate_hash": certificate_hash,
            }
        )
        completed = [dict(value) for value in updated.get("completed_work_items", ())]
        completed.append(item)
        updated["completed_work_items"] = completed
        updated["work_items"] = [
            item if value.get("id") == item.get("id") else value
            for value in updated.get("work_items", ())
            if isinstance(value, Mapping)
        ]
        updated["current_work_item"] = None
        updated["phase"] = "PROMOTE"
        updated["step"] = int(updated.get("step") or 1) + 1
        evidence = dict(updated.get("evidence") or {})
        if candidate_fingerprint:
            evidence["base_fingerprint"] = candidate_fingerprint
        updated["evidence"] = evidence
        updated["remaining"] = [
            value
            for value in updated.get("work_items", ())
            if isinstance(value, Mapping) and value.get("status") not in {"completed"}
        ]
        return updated

    @staticmethod
    def mark_discarded(plan: Mapping[str, Any]) -> dict[str, Any]:
        updated = _copy_plan(plan)
        item = dict(updated.get("current_work_item") or {})
        item["status"] = "pending"
        updated["current_work_item"] = item
        updated["work_items"] = [
            item if value.get("id") == item.get("id") else value
            for value in updated.get("work_items", ())
            if isinstance(value, Mapping)
        ]
        updated["phase"] = "PATCH"
        updated["attempts"] = int(updated.get("attempts") or 0) + 1
        return updated

    @staticmethod
    def next_work_item(plan: Mapping[str, Any]) -> dict[str, Any] | None:
        """Select the next explicitly planned item, if one remains."""
        updated = _copy_plan(plan)
        for value in updated.get("work_items", ()):
            if not isinstance(value, Mapping) or value.get("status") != "pending":
                continue
            item = dict(value)
            item["status"] = "current"
            updated["current_work_item"] = item
            updated["phase"] = "PLAN"
            updated["work_items"] = [
                item if entry.get("id") == item.get("id") else entry
                for entry in updated.get("work_items", ())
                if isinstance(entry, Mapping)
            ]
            return updated
        return None


def _copy_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Copy JSON-shaped mission state without sharing nested mutable values."""
    import json

    return json.loads(json.dumps(dict(plan), sort_keys=True))


__all__ = ["SelfHostMissionController"]
