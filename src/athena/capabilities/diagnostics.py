"""First-class diagnostic normalization and repair-memory access."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.execution.diagnostics import normalize_diagnostics, normalize_diagnostics_payload
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


class DiagnosticsCapability:
    descriptor = CapabilityDescriptor(
        id="diagnostics",
        description=(
            "Normalize native or textual diagnostics and retrieve advisory, "
            "project/environment-scoped repair memories. Operations: normalize, "
            "failures, remember."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["normalize", "failures", "remember"]},
                "text": {"type": "string", "maxLength": 1_000_000},
                "payload": {},
                "tool": {"type": "string", "maxLength": 128},
                "source_tool_version": {"type": "string", "maxLength": 128},
                "signature_fingerprint": {"type": "string", "maxLength": 128},
                "capability_id": {"type": "string", "maxLength": 128},
                "environment_fingerprint": {"type": "string", "maxLength": 256},
                "strategy": {},
                "remediation": {},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "success": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, memory) -> None:
        self._memory = memory

    async def invoke(self, request: CapabilityRequest, *, context=None, **_) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if operation == "normalize":
            tool = str(args.get("tool") or "unknown")
            version = args.get("source_tool_version")
            if "payload" in args:
                diagnostics = normalize_diagnostics_payload(
                    args.get("payload"),
                    tool=tool,
                    source_tool_version=str(version) if version else None,
                )
            else:
                diagnostics = normalize_diagnostics(
                    str(args.get("text") or ""),
                    tool=tool,
                    source_tool_version=str(version) if version else None,
                )
            return _result(request, output=json.dumps([item.to_dict() for item in diagnostics]))
        project = getattr(getattr(context, "workspace", None), "id", None)
        environment = str(args.get("environment_fingerprint") or "")
        if operation == "failures":
            records = await self._memory.retrieve(
                signature_fingerprint=(
                    str(args["signature_fingerprint"])
                    if args.get("signature_fingerprint")
                    else None
                ),
                capability_id=(str(args["capability_id"]) if args.get("capability_id") else None),
                environment_fingerprint=environment,
                project_scope=project,
                limit=int(args.get("limit") or 20),
            )
            return _result(request, output=json.dumps(records))
        if operation == "remember":
            signature = str(args.get("signature_fingerprint") or "")
            capability = str(args.get("capability_id") or request.capability_id)
            if not signature:
                return _result(request, ok=False, error="signature_fingerprint is required")
            record_id = await self._memory.record(
                signature_fingerprint=signature,
                capability_id=capability,
                environment_fingerprint=environment,
                project_scope=project,
                strategy=args.get("strategy") or "",
                remediation=args.get("remediation"),
                evidence_ids=tuple(str(value) for value in args.get("evidence_ids") or ()),
                success=bool(args.get("success", False)),
            )
            return _result(request, output=record_id, metadata={"record_id": record_id})
        return _result(request, ok=False, error=f"unknown diagnostics operation: {operation}")


def _result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["DiagnosticsCapability"]
