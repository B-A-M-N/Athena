"""Portable, proof-carrying procedure capsules.

Capsules are transport records for declarative workflows and the generated
capabilities they depend on. Import never activates a project or user overlay:
it restores generated machinery into the current task, rechecks source,
dependency, and evidence validity, and persists the workflow as a task-owned
record. Replay then uses the ordinary workflow dispatcher.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.workflows.models import Workflow
from athena.workflows.validation import WorkflowValidator


class ProcedureCapsuleCapability:
    descriptor = CapabilityDescriptor(
        id="capsule",
        description=(
            "Export, inspect, import, and replay a portable procedure capsule. "
            "A capsule carries a declarative workflow, generated source, "
            "dependency locks, evidence dependencies, acceptance proof, and "
            "required native capabilities. Import revalidates it in the current "
            "task before activation. Operations: export/inspect/import/run."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "export",
                        "inspect",
                        "import",
                        "run",
                    ],
                },
                "workflow_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "capsule": {
                    "oneOf": [
                        {"type": "object"},
                        {"type": "string", "minLength": 1, "maxLength": 10_000_000},
                    ],
                },
                "objective": {"type": "string", "maxLength": 2000},
                "inputs": {"type": "object"},
            },
            "oneOf": [
                {"properties": {"operation": {"const": "export"}}, "required": ["workflow_id"]},
                {"properties": {"operation": {"const": "inspect"}}, "required": ["capsule"]},
                {"properties": {"operation": {"const": "import"}}, "required": ["capsule"]},
                {"properties": {"operation": {"const": "run"}}, "required": ["capsule"]},
            ],
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.NETWORK_READ,
                EffectClass.NETWORK_WRITE,
                EffectClass.DELETE,
                EffectClass.PRIVILEGED,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
        tags=frozenset({"workflow", "portable", "proof"}),
    )

    def __init__(
        self, store, fabric, engine, workflow_capability=None, research_store=None, dispatcher=None
    ) -> None:
        self._store = store
        self._fabric = fabric
        self._engine = engine
        self._workflow = workflow_capability
        self._research = research_store
        self._dispatcher = dispatcher

    async def invoke(self, request: CapabilityRequest, *, context=None, **kw):
        if request.task_id is None:
            return _result(request, ok=False, error="capsules require a task scope")
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if operation == "export":
            return await self._export(request, args, context)
        if operation in {"inspect", "import", "run"}:
            try:
                capsule = _decode_capsule(args.get("capsule"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return _result(request, ok=False, error=f"invalid capsule: {exc}")
            if operation == "inspect":
                return _result(request, output=json.dumps(capsule))
            imported = await self._import(request, capsule, context)
            if not imported[0]:
                return _result(request, ok=False, error=imported[1])
            workflow_id = imported[1]
            if operation == "import":
                return _result(
                    request,
                    output=json.dumps(
                        {
                            "capsule_id": capsule["capsule_id"],
                            "workflow_id": workflow_id,
                            "status": "IMPORTED",
                        }
                    ),
                    metadata={"capsule_id": capsule["capsule_id"], "workflow_id": workflow_id},
                )
            if self._workflow is None:
                return _result(request, ok=False, error="workflow executor unavailable")
            workflow_request = CapabilityRequest(
                capability_id="workflow",
                arguments={
                    "operation": "run",
                    "workflow_id": workflow_id,
                    "inputs": dict(args.get("inputs") or {}),
                },
                task_id=request.task_id,
                session_id=request.session_id,
                call_id=new_id("capsule-run"),
                origin=request.origin,
            )
            if self._dispatcher is not None:
                workspace = getattr(context, "workspace", None)
                if workspace is None:
                    return _result(
                        request,
                        ok=False,
                        error="capsule run requires workspace context",
                    )
                return await self._dispatcher.dispatch(
                    workflow_request,
                    workspace=workspace,
                    profile=getattr(context, "autonomy", None),
                    task_policy=getattr(context, "capability_policy", None),
                    task_budget=getattr(context, "resource_budget", None),
                    _generated_call_depth=getattr(context, "generated_call_depth", 0),
                    _generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
                )
            # Compatibility for standalone capsule users. The service injects
            # the dispatcher, so live replay takes the canonical workflow path.
            return await self._workflow.invoke(workflow_request, context=context)
        return _result(request, ok=False, error=f"unknown operation: {operation}")

    async def _export(self, request, args, context):
        workflow_id = str(args.get("workflow_id") or "")
        workspace_id = getattr(getattr(context, "workspace", None), "id", None)
        workflow = await self._store.get(
            workflow_id,
            task_id=request.task_id,
            project_id=workspace_id,
            user_id="athena",
        )
        if workflow is None:
            return _result(request, ok=False, error=f"unknown workflow: {workflow_id}")
        workflows = await self._collect_workflows(
            workflow,
            request.task_id,
            workspace_id,
        )
        capability_ids = sorted(
            {step.capability_id for item in workflows for step in item.steps if step.capability_id}
        )
        capabilities = []
        for capability_id in capability_ids:
            try:
                descriptor = self._fabric.describe(
                    capability_id,
                    task_id=request.task_id,
                    project_id=workspace_id,
                    user_id="athena",
                )
            except Exception as exc:  # noqa: BLE001 - export reports missing dependency
                return _result(
                    request,
                    ok=False,
                    error=f"cannot export capability {capability_id}: {exc}",
                )
            generated = self._fabric.provenance(capability_id)
            capabilities.append(
                {
                    "id": capability_id,
                    "descriptor": descriptor,
                    "generated": generated,
                }
            )
        body = {
            "format": 1,
            "objective": str(args.get("objective") or workflow.description),
            "root_workflow_id": workflow.id,
            "workflows": [item.to_record() for item in workflows],
            "capabilities": capabilities,
            "environment": {
                "workspace_id": workspace_id,
                "runtime": "python",
            },
            "proof": {
                "workflow_id": workflow.id,
                "generated": {
                    item["id"]: dict(item["generated"].get("proof_record") or {})
                    for item in capabilities
                    if item.get("generated")
                },
            },
        }
        capsule = _with_id(body)
        return _result(
            request,
            output=json.dumps(capsule, sort_keys=True),
            metadata={"capsule_id": capsule["capsule_id"], "workflow_id": workflow.id},
        )

    async def _collect_workflows(self, root, task_id, project_id):
        found: dict[str, Workflow] = {}
        pending = [root]
        while pending:
            workflow = pending.pop()
            if workflow.id in found:
                continue
            found[workflow.id] = workflow
            for step in workflow.steps:
                if not step.workflow_id:
                    continue
                nested = await self._store.get(
                    step.workflow_id,
                    task_id=task_id,
                    project_id=project_id,
                    user_id="athena",
                )
                if nested is None:
                    raise ValueError(f"workflow dependency is unavailable: {step.workflow_id}")
                pending.append(nested)
        return [found[key] for key in sorted(found)]

    async def _import(self, request, capsule, context) -> tuple[bool, str]:
        workspace = getattr(context, "workspace", None)
        if workspace is None:
            return False, "capsule import requires workspace context"
        imported_capabilities: list[str] = []
        imported_workflows: list[str] = []
        succeeded = False
        try:
            for item in capsule.get("capabilities") or ():
                capability_id = str(item.get("id") or "")
                generated_record = item.get("generated")
                if not generated_record:
                    # Native dependencies are resolved now so an imported
                    # capsule fails with a useful blocker before replay.
                    self._fabric.executor_for(
                        capability_id,
                        task_id=request.task_id,
                        project_id=workspace.id,
                        user_id="athena",
                    )
                    continue
                generated = GeneratedCapability.from_record(generated_record)
                if generated.lifecycle_state in {
                    "STALE",
                    "REVALIDATION_REQUIRED",
                    "DEPRECATED",
                }:
                    return False, f"generated capability {capability_id} is unavailable"
                if not generated.proof_record.get("all_passed", False):
                    return False, f"generated capability {capability_id} has no passing proof"
                if self._research is not None and generated.evidence_dependencies:
                    evidence = await self._engine.evidence_status(
                        generated,
                        self._research,
                    )
                    if evidence["status"] != "CURRENT":
                        return False, f"generated capability {capability_id} has stale evidence"
                imported_cap = self._engine.synthesize(
                    capability_id=generated.id,
                    name=generated.name,
                    description=generated.description,
                    code=generated.implementation,
                    input_schema=dict(generated.input_schema),
                    output_schema=dict(generated.output_schema or {}) or None,
                    effects=set(generated.declared_effects),
                    task_id=request.task_id,
                    provenance={
                        **dict(generated.provenance),
                        "imported_from_capsule": capsule["capsule_id"],
                        "import_task_id": request.task_id,
                    },
                    validation_cases=[dict(case) for case in generated.validation_cases],
                    required_dependencies=generated.required_dependencies,
                    required_capabilities=generated.required_capabilities,
                    evidence_dependencies=generated.evidence_dependencies,
                    supersedes=generated.supersedes,
                )
                imported_cap = await self._engine.validate(
                    imported_cap,
                    list(generated.validation_cases),
                    tier="task",
                    workspace_root=workspace.root,
                    workspace=workspace,
                    task_id=request.task_id,
                    session_id=request.session_id,
                    profile=getattr(context, "autonomy", None),
                    task_policy=getattr(context, "capability_policy", None),
                    task_budget=getattr(context, "resource_budget", None),
                    generated_call_depth=getattr(context, "generated_call_depth", 0),
                    generated_call_chain=tuple(getattr(context, "generated_call_chain", ())),
                )
                if not imported_cap.validation.get("all_passed"):
                    return False, f"generated capability {capability_id} failed revalidation"
                if not self._engine.register_ephemeral(self._fabric, imported_cap):
                    return False, f"generated capability {capability_id} was not admitted"
                imported_capabilities.append(capability_id)

            records = list(capsule.get("workflows") or ())
            if not records:
                return False, "capsule contains no workflows"
            imported_records: dict[str, Workflow] = {}
            for raw in records:
                record = dict(raw)
                record["scope"] = AffordanceScope.TASK.value
                record["task_scope"] = request.task_id
                record["project_scope"] = None
                record["user_scope"] = None
                record["provenance"] = {
                    **dict(record.get("provenance") or {}),
                    "imported_from_capsule": capsule["capsule_id"],
                }
                workflow = Workflow.from_record(record)
                await self._store.save(workflow)
                imported_workflows.append(workflow.id)
                imported_records[workflow.id] = workflow

            root_id = str(capsule.get("root_workflow_id") or "")
            root = await self._store.get(root_id, task_id=request.task_id)
            if root is None:
                return False, "capsule root workflow was not imported"
            validation = WorkflowValidator(
                lambda identifier: (
                    imported_records.get(identifier)
                    or self._fabric.executor_for(
                        identifier,
                        task_id=request.task_id,
                        project_id=workspace.id,
                        user_id="athena",
                    ).descriptor
                )
            ).validate(root)
            if not validation.ok:
                return False, "capsule validation failed: " + "; ".join(validation.errors)
            succeeded = True
            return True, root.id
        except Exception as exc:  # noqa: BLE001 - imports are an admission boundary
            return False, f"capsule import failed: {exc}"
        finally:
            # If the attempt failed, remove only records introduced by this
            # capsule. Existing task-local machinery remains untouched.
            if not succeeded:
                for workflow_id in imported_workflows:
                    try:
                        await self._store.delete(workflow_id)
                    except Exception:
                        pass
                for capability_id in imported_capabilities:
                    self._fabric.unregister_task_capability(
                        request.task_id,
                        capability_id,
                    )


def _with_id(body: Mapping[str, Any]) -> dict[str, Any]:
    canonical = json.dumps(dict(body), sort_keys=True, separators=(",", ":"))
    value = dict(body)
    value["capsule_id"] = "capsule_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return value


def _decode_capsule(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("capsule must be an object or JSON object string")
    capsule = dict(value)
    if capsule.get("format") != 1:
        raise ValueError("unsupported capsule format")
    supplied_id = str(capsule.pop("capsule_id", None) or "")
    if not supplied_id:
        raise ValueError("capsule_id is required")
    expected = _with_id(capsule)["capsule_id"]
    if supplied_id != expected:
        raise ValueError("capsule content hash does not match capsule_id")
    capsule["capsule_id"] = supplied_id
    return capsule


def _result(request, *, ok=True, output="", error=None, metadata=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["ProcedureCapsuleCapability"]
