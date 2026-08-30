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
        item: dict[str, Any] = {
            "id": item_id,
            "title": str(objective).strip() or "bounded Athena improvement",
            "objective": str(objective).strip(),
            "affected_files": [],
            "affected_invariants": [],
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
                "indexed_files": [
                    str(value.get("path"))
                    for value in (getattr(index, "files", ()) or ())[:128]
                    if isinstance(value, Mapping) and value.get("path")
                ],
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
    def planner_prompt(
        mission: Mapping[str, Any],
        *,
        current_index: Any,
        design_context: str,
        current_release_evidence: Mapping[str, Any] | None = None,
    ) -> str:
        """Build a bounded planning prompt from current trusted evidence."""
        plan = mission.get("plan") or {}
        evidence = plan.get("evidence") or {}
        completed = plan.get("completed_work_items") or []
        index_files = [
            str(value.get("path"))
            for value in (getattr(current_index, "files", ()) or ())[:128]
            if isinstance(value, Mapping) and value.get("path")
        ]
        return (
            "Plan exactly one bounded next work item for Athena. Return JSON only.\n"
            "Do not edit files, install dependencies, or approve promotion.\n"
            f"Mission objective: {str(mission.get('objective') or '')[:4000]}\n"
            f"Completed work: {_json_limit(completed)}\n"
            f"Current index revision: {getattr(current_index, 'index_revision', '')}\n"
            f"Current source revision: {getattr(current_index, 'source_revision', '')}\n"
            f"Current release evidence: {_json_limit(current_release_evidence or {})}\n"
            f"Frozen authority hashes: {_json_limit({key: evidence.get(key) for key in ('design_bundle_hash', 'gate_bundle_hash')})}\n"
            f"Indexed paths: {_json_limit(index_files)}\n"
            "Frozen design context:\n"
            f"{design_context[:24000]}\n"
            'Return either {"done":true,"reason":"..."} only when the objective is demonstrably complete, '
            'or {"done":false,"work_item":{"title":"...","objective":"...",'
            '"affected_files":[],"affected_invariants":[],"dependencies":[]},"reason":"..."}.'
        )

    @staticmethod
    def parse_planner_output(
        raw: str | None,
        *,
        indexed_files: set[str],
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """Validate planner JSON; return ``(item, reason, error)``."""
        if not raw:
            return None, None, "planner returned no output"
        import json

        text = str(raw).strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("```")
            )
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None, None, "planner returned invalid JSON"
        if not isinstance(parsed, Mapping):
            return None, None, "planner output must be an object"
        reason = str(parsed.get("reason") or "").strip()[:1000]
        done = parsed.get("done")
        if done is True:
            return None, reason or "planner says the mission is complete", None
        if done is not False:
            return None, None, "planner done must be boolean"
        raw_item = parsed.get("work_item")
        if not isinstance(raw_item, Mapping):
            return None, None, "planner omitted work_item"
        title = str(raw_item.get("title") or "").strip()[:300]
        objective = str(raw_item.get("objective") or "").strip()[:4000]
        if not title or not objective:
            return None, None, "planner work item needs title and objective"
        files = _safe_paths(raw_item.get("affected_files"), limit=64)
        if files is None:
            return None, None, "planner work item has invalid affected_files"
        invariants = _bounded_list(raw_item.get("affected_invariants"), limit=16)
        dependencies = _bounded_list(raw_item.get("dependencies"), limit=16)
        if not invariants:
            return None, None, "planner work item needs affected_invariants"
        if any(path == ".git" or path.startswith(".git/") for path in files):
            return None, None, "planner selected a Git metadata path"
        return (
            {
                "id": new_id("self-work"),
                "title": title,
                "objective": objective,
                "affected_files": files,
                "affected_invariants": invariants,
                "dependencies": dependencies,
                "risk": "pending-diff-classification",
                "status": "current",
                "task_id": None,
                "branch_id": None,
                "certificate_hash": None,
            },
            reason or "planner selected the next bounded item",
            None,
        )

    @staticmethod
    def add_planned_item(
        plan: Mapping[str, Any], item: Mapping[str, Any], *, reason: str
    ) -> dict[str, Any]:
        updated = _copy_plan(plan)
        work_item = dict(item)
        work_item["status"] = "current"
        updated["current_work_item"] = work_item
        updated["work_items"] = [*updated.get("work_items", ()), work_item]
        updated["phase"] = "PLAN"
        updated["plan_reason"] = str(reason)[:1000]
        updated["step"] = int(updated.get("step") or 1) + 1
        return updated

    @staticmethod
    def replace_current_item(
        plan: Mapping[str, Any], item: Mapping[str, Any], *, reason: str
    ) -> dict[str, Any]:
        """Install the planner's bounded item in the one-item planning slot."""
        updated = _copy_plan(plan)
        work_item = dict(item)
        work_item["status"] = "current"
        current = updated.get("current_work_item") or {}
        current_id = current.get("id") if isinstance(current, Mapping) else None
        replaced = False
        values: list[dict[str, Any]] = []
        for value in updated.get("work_items", ()):
            if not isinstance(value, Mapping):
                continue
            if current_id and value.get("id") == current_id:
                values.append(work_item)
                replaced = True
            else:
                values.append(dict(value))
        if not replaced:
            values.append(work_item)
        updated["work_items"] = values
        updated["current_work_item"] = work_item
        updated["phase"] = "PLAN"
        updated["plan_reason"] = str(reason)[:1000]
        return updated

    @staticmethod
    def completion_proof(
        plan: Mapping[str, Any],
        *,
        objective: str,
        reason: str,
        authority: Mapping[str, Any],
        base_fingerprint: str,
        release_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create durable evidence required before declaring completion."""
        import hashlib
        import json

        proof = {
            "kind": "self_host_mission_completion",
            "objective": str(objective)[:4000],
            "reason": str(reason)[:1000],
            "completed_work_items": list(plan.get("completed_work_items") or ()),
            "authority": {
                key: str(authority.get(key) or "")
                for key in ("source_revision", "design_bundle_hash", "gate_bundle_hash")
            },
            "base_fingerprint": base_fingerprint,
            "release_evidence": {
                "task_status": str(release_evidence.get("task_status") or ""),
                "base_fingerprint": str(release_evidence.get("base_fingerprint") or ""),
                "certificate_hash": str(
                    (release_evidence.get("review") or {}).get("certificate_hash") or ""
                )
                if isinstance(release_evidence.get("review"), Mapping)
                else "",
                "review_eligible": (release_evidence.get("review") or {}).get("eligible") is True
                if isinstance(release_evidence.get("review"), Mapping)
                else False,
            },
        }
        proof["proof_hash"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return proof

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


def _bounded_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip()[:300] for item in value[:limit] if str(item).strip()]


def _safe_paths(value: Any, *, limit: int) -> list[str] | None:
    values = _bounded_list(value, limit=limit)
    for path in values:
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            return None
    return values


def _json_limit(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))[:12000]


__all__ = ["SelfHostMissionController"]
