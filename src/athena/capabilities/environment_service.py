"""Service capability family: systemd user/system service control (P1-10).

Mutations require approval; recovery contracts live with the family."""

from athena.capabilities import environment as _facade
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.capabilities import CapabilityOrigin
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.capabilities import CapabilityResult
from athena.protocol.capabilities import CapabilityResultStatus
from athena.protocol.capabilities import EffectClass
from athena.protocol.capabilities import ExternalEffectContract
from athena.protocol.capabilities import ExternalEffectPhase
from athena.protocol.ids import new_id
from athena.state.external_effects import ExternalEffectRecoveryRequired
from athena.state.external_effects import ExternalEffectStore
from collections.abc import Mapping
from typing import Any
import json

from athena.capabilities.environment_common import _MUTATIONS
from athena.capabilities.environment_common import _SERVICE_ROLLBACK
from athena.capabilities.environment_common import _UNIT_NAME
from athena.capabilities.environment_common import _legacy_idempotency_key
from athena.capabilities.environment_common import _legacy_transaction_result
from athena.capabilities.environment_common import _result
from athena.capabilities.environment_common import _service_effects
from athena.capabilities.environment_common import _service_offload
from athena.capabilities.environment_common import _service_restore_plan
from athena.capabilities.environment_common import _service_state
from athena.capabilities.environment_common import _service_state_matches
from athena.capabilities.environment_common import _external_receipt_result
from athena.capabilities.environment_common import _service_request_digest


class ServiceCapability:
    """systemd service control (user + system scopes)."""

    descriptor = CapabilityDescriptor(
        id="service",
        description=(
            "Operating-system service control via systemd: list services, "
            "status, start/stop/restart/reload, enable/disable, journal "
            "logs. Mutating operations are policy-gated (PRIVILEGED). "
            "Operations: list/status/logs plus the mutation verbs."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "list",
                        "status",
                        "logs",
                        "start",
                        "stop",
                        "restart",
                        "reload",
                        "enable",
                        "disable",
                        "mask",
                        "unmask",
                        "service_transaction",
                    ],
                },
                "unit": {
                    "type": "string",
                    "maxLength": 255,
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$",
                },
                "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                "user_scope": {"type": "boolean"},
                "phase": {
                    "type": "string",
                    "enum": [
                        "inspect",
                        "prepare",
                        "dry_run",
                        "apply",
                        "verify",
                        "compensate",
                    ],
                },
                "service_operation": {"type": "string", "enum": sorted(_MUTATIONS)},
                "transaction_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "external_identity": {"type": "string", "minLength": 1, "maxLength": 512},
                "expected_active_state": {"type": "string", "maxLength": 64},
                "compensate_operation": {"type": "string", "enum": sorted(_MUTATIONS)},
            },
            "additionalProperties": False,
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.PRIVILEGED,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        effect_resolver=_service_effects,
        external_effects={
            "service_transaction": ExternalEffectContract(
                phases=frozenset(ExternalEffectPhase),
                idempotency_required=True,
                reversible=False,
                compensatable=True,
                approval_floor="ask",
                identity_fields=("unit", "service_operation", "user_scope"),
            ),
        },
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, *, external_store: ExternalEffectStore | None = None) -> None:
        self._external_store = external_store or ExternalEffectStore()

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        if op == "service_transaction":
            return await self._service_transaction(request, args)
        unit = str(args.get("unit") or "").strip()
        scope = ["--user"] if args.get("user_scope") else []

        if op == "list":

            def _ls():
                rc, out, err = _facade._run(
                    [
                        "systemctl",
                        *scope,
                        "list-units",
                        "--type=service",
                        "--no-pager",
                        "--no-legend",
                    ]
                )
                return rc, out, err

            rc, out, err = await _service_offload(_ls)
            return _result(
                request,
                ok=rc == 0,
                output=(out or err)[:6000],
                error=err if rc else None,
                meta={"rc": rc},
            )

        if not unit:
            return _result(request, ok=False, error="unit required")
        if not _UNIT_NAME.fullmatch(unit):
            return _result(request, ok=False, error="invalid systemd unit name")

        if op == "status":

            def _st():
                return _facade._run(["systemctl", *scope, "status", unit, "--no-pager", "-l"])

            rc, out, err = await _service_offload(_st)
            state = await _service_offload(_service_state, scope, unit)
            payload = {
                "unit": unit,
                "scope": "user" if scope else "system",
                "state": state,
                "detail": (out or err)[:6000],
            }
            return _result(
                request,
                ok=rc == 0,
                output=json.dumps(payload),
                error=err if rc else None,
                meta=payload,
            )

        if op == "logs":
            lines = max(int(args.get("lines") or 50), 1)

            def _lg():
                return _facade._run(
                    ["journalctl", *scope, "-u", unit, "-n", str(lines), "--no-pager"]
                )

            rc, out, err = await _service_offload(_lg)
            return _result(
                request,
                ok=rc == 0,
                output=(out or err)[:8000],
                error=err if rc else None,
                meta={"rc": rc},
            )

        if op in _MUTATIONS:
            # Preserve the legacy verbs, but lower their mutation through the
            # same durable prepare/apply receipt used by the explicit API.
            return await self._legacy_service_mutation(request, args, op)

        return _result(request, ok=False, error=f"unknown operation: {op}")

    async def _legacy_service_mutation(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        operation: str,
    ) -> CapabilityResult:
        idempotency_key = str(
            args.get("idempotency_key") or _legacy_idempotency_key(operation, request, args)
        )
        base = {
            **args,
            "operation": "service_transaction",
            "service_operation": operation,
            "idempotency_key": idempotency_key,
        }
        prepared = await self._service_transaction(
            request,
            {**base, "phase": "prepare"},
        )
        if prepared.status is not CapabilityResultStatus.OK:
            return prepared
        try:
            transaction_id = str(json.loads(prepared.output)["transaction_id"])
        except (KeyError, TypeError, ValueError):
            return _result(request, ok=False, error="service preparation receipt is malformed")
        applied = await self._service_transaction(
            request,
            {**base, "phase": "apply", "transaction_id": transaction_id},
        )
        return _legacy_transaction_result(request, applied, operation=operation)

    async def _service_transaction(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
    ) -> CapabilityResult:
        """Run service control through a durable, reversible lifecycle."""
        try:
            phase = ExternalEffectPhase(str(args.get("phase") or "").lower())
        except ValueError:
            return _result(request, ok=False, error="unknown service transaction phase")
        service_operation = str(args.get("service_operation") or "").lower()
        unit = str(args.get("unit") or "").strip()
        if service_operation not in _MUTATIONS:
            return _result(request, ok=False, error="service_operation required")
        if not unit or not _UNIT_NAME.fullmatch(unit):
            return _result(request, ok=False, error="valid service unit required")
        if phase is not ExternalEffectPhase.INSPECT and request.task_id is None:
            return _result(
                request,
                ok=False,
                error="service transactions require a task-scoped invocation",
            )
        user_scope = bool(args.get("user_scope"))
        scope = ["--user"] if user_scope else []
        external_identity = self.descriptor.resolve_external_identity(args)
        if external_identity is None:
            return _result(request, ok=False, error="service transaction identity unavailable")
        request_digest = _service_request_digest(unit, service_operation, user_scope)
        transaction_id = str(args.get("transaction_id") or "")
        idempotency_key = str(args.get("idempotency_key") or "") or None
        if (
            phase
            in {
                ExternalEffectPhase.PREPARE,
                ExternalEffectPhase.DRY_RUN,
                ExternalEffectPhase.APPLY,
                ExternalEffectPhase.COMPENSATE,
            }
            and not idempotency_key
        ):
            return _result(
                request,
                ok=False,
                error=f"service {phase.value} requires an idempotency_key",
            )
        if phase is not ExternalEffectPhase.INSPECT and not transaction_id:
            if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
                transaction_id = new_id("service-tx")
            else:
                return _result(request, ok=False, error="transaction_id required")
        if phase is ExternalEffectPhase.INSPECT:
            return _result(
                request,
                output=json.dumps(
                    {
                        "phase": phase.value,
                        "transaction_id": transaction_id or new_id("service-tx"),
                        "external_identity": external_identity,
                        "request_digest": request_digest,
                        "idempotency_required": True,
                        "reversible": True,
                        "approval_floor": "ask",
                    }
                ),
            )

        if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
            try:
                receipt = await self._external_store.prepare(
                    transaction_id=transaction_id,
                    task_id=request.task_id,
                    capability_id=request.capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key,
                    phase=phase,
                )
                before = await _service_offload(_service_state, scope, unit)
                restore_plan = _service_restore_plan(before, service_operation)
                receipt = await self._external_store.finish(
                    transaction_id,
                    status=("PREPARED" if phase is ExternalEffectPhase.PREPARE else "DRY_RUN"),
                    response={
                        "unit": unit,
                        "service_operation": service_operation,
                        "user_scope": user_scope,
                        "before": before,
                        "compensate_operation": _SERVICE_ROLLBACK[service_operation],
                        "restore_plan": restore_plan,
                    },
                    phase=phase,
                )
            except (ExternalEffectRecoveryRequired, KeyError, OSError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            return _external_receipt_result(request, receipt)

        if phase is ExternalEffectPhase.APPLY:
            try:
                receipt, replay = await self._external_store.begin_apply(
                    transaction_id=transaction_id,
                    task_id=request.task_id,
                    capability_id=request.capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key or "",
                )
            except (ExternalEffectRecoveryRequired, KeyError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            if replay:
                return _external_receipt_result(request, receipt)
            if receipt.get("status") != "APPLYING":
                return _result(
                    request,
                    ok=False,
                    error="service apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            try:

                def _apply():
                    before = _service_state(scope, unit)
                    rc, out, err = _facade._run(
                        ["systemctl", *scope, service_operation, unit],
                        timeout=30,
                    )
                    after = _service_state(scope, unit)
                    return rc, out, err, before, after

                rc, out, err, before, after = await _service_offload(_apply)
                payload = {
                    "unit": unit,
                    "service_operation": service_operation,
                    "user_scope": user_scope,
                    "before": before,
                    "after": after,
                    "returncode": rc,
                    "detail": (out or err).strip()[:4000],
                    "compensate_operation": _SERVICE_ROLLBACK[service_operation],
                    "restore_plan": _service_restore_plan(before, service_operation),
                }
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="COMPLETED" if rc == 0 else "FAILED",
                    response=payload,
                )
            except Exception as exc:  # noqa: BLE001 - remote state may be uncertain
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="RECOVERY_REQUIRED",
                    error=str(exc),
                )
                return _result(
                    request,
                    ok=False,
                    error="service apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            return _result(
                request,
                ok=rc == 0,
                output=json.dumps(receipt),
                error=(err or "service operation failed") if rc else None,
                meta={"external_effect": receipt},
            )

        try:
            receipt = await self._external_store.begin_followup(
                transaction_id,
                task_id=request.task_id,
                capability_id=request.capability_id,
                request_digest=(request_digest if phase is ExternalEffectPhase.VERIFY else None),
                phase=phase,
            )
        except (ExternalEffectRecoveryRequired, KeyError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        if (
            phase is ExternalEffectPhase.VERIFY
            and receipt.get("status") in {"VERIFIED", "COMPENSATION_VERIFIED"}
        ) or (
            phase is ExternalEffectPhase.COMPENSATE
            and receipt.get("status") in {"COMPENSATED", "COMPENSATION_SENT"}
        ):
            return _external_receipt_result(request, receipt)
        payload = receipt.get("response") or {}
        original_operation = str(payload.get("service_operation") or service_operation)
        if phase is ExternalEffectPhase.VERIFY:
            state = await _service_offload(_service_state, scope, unit)
            expected = str(args.get("expected_active_state") or "")
            target = (
                (
                    payload.get("before")
                    if receipt.get("verification_target") == "COMPENSATION_PRESTATE"
                    else payload.get("after")
                )
                or (payload.get("restore_plan") or {}).get("target")
                or {}
            )
            verified = _service_state_matches(state, target) and (
                not expected or str(state.get("activestate") or "") == expected
            )
            compensation_verification = receipt.get(
                "verification_target"
            ) == "COMPENSATION_PRESTATE" or receipt.get("previous_status") in {
                "COMPENSATION_SENT",
                "COMPENSATION_VERIFY_FAILED",
            }
            verification_status = (
                "COMPENSATION_VERIFIED"
                if compensation_verification and verified
                else "COMPENSATION_VERIFY_FAILED"
                if compensation_verification
                else "VERIFIED"
                if verified
                else "VERIFY_FAILED"
            )
            receipt = await self._external_store.finish(
                transaction_id,
                status=verification_status,
                response={**dict(payload), "state": state, "verified": verified},
                phase=phase,
            )
            return _result(
                request,
                ok=verified,
                output=json.dumps(receipt),
                error="service verification failed" if not verified else None,
                meta={"external_effect": receipt},
            )

        prepared_restore_plan: Any = payload.get("restore_plan")
        if not isinstance(prepared_restore_plan, Mapping):
            return _result(
                request,
                ok=False,
                error="service compensation requires the prepared restore plan",
            )
        if not bool(prepared_restore_plan.get("restorable")):
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error=str(
                    prepared_restore_plan.get("reason") or "service pre-state is not restorable"
                ),
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="service pre-state cannot be restored exactly; recovery required",
                meta={"external_effect": receipt},
            )
        requested_operation = str(args.get("compensate_operation") or "").lower()
        actions = [str(action).lower() for action in prepared_restore_plan.get("actions") or ()]
        if requested_operation and requested_operation not in actions:
            return _result(
                request,
                ok=False,
                error="compensation operation does not match the prepared restore plan",
            )
        try:

            def _compensate():
                outputs = []
                rc = 0
                out = ""
                err = ""
                for action in actions:
                    rc, out, err = _facade._run(
                        ["systemctl", *scope, action, unit],
                        timeout=30,
                    )
                    outputs.append((out or err).strip())
                    if rc != 0:
                        break
                return (
                    rc,
                    "\n".join(item for item in outputs if item),
                    err,
                    _service_state(scope, unit),
                )

            rc, out, err, state = await _service_offload(_compensate)
            restored = _service_state_matches(state, prepared_restore_plan.get("target") or {})
            response = {
                "unit": unit,
                "service_operation": original_operation,
                "user_scope": user_scope,
                "returncode": rc,
                "state": state,
                "restore_plan": prepared_restore_plan,
                "restored": restored,
                "detail": (out or err).strip()[:4000],
            }
            receipt = await self._external_store.finish(
                transaction_id,
                status="COMPENSATION_VERIFIED" if rc == 0 and restored else "RECOVERY_REQUIRED",
                response=response,
                phase=phase,
            )
        except Exception as exc:  # noqa: BLE001 - preserve uncertain state
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error=str(exc),
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="service compensation outcome is uncertain; recovery required",
                meta={"external_effect": receipt},
            )
        return _result(
            request,
            ok=rc == 0 and restored,
            output=json.dumps(receipt),
            error=(err or "service compensation was not verified")
            if not (rc == 0 and restored)
            else None,
            meta={"external_effect": receipt},
        )
