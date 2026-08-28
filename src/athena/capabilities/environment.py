"""P1/P2 capability families: service, network, database, workspace, watch.

These complete the deep-capability set from the roadmap. Each is a
capability family registered through the normal registry -> policy ->
executor path; effect envelopes are conservative (mutations gated).

- service    : systemd user/system service control (list/status/start/
               stop/restart/enable/disable/logs). Mutations require approval.
- network    : diagnostics as primitives — http, tcp_connect, dns,
               listeners, connections, ping. Read-only effects.
- database   : SQL against SQLite/Postgres-compatible endpoints with
               schema introspection; writes are policy-gated WRITE_LOCAL.
- workspace  : status/snapshot/restore/diff/changed_files over the task's
               workspace root (checkpoint-manager backed).
- watch      : filesystem/process watchers that push observations into the
               durable event stream while a task runs.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import ipaddress
import inspect
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from athena.network import pinned_async_transport, pinned_sync_transport, resolve_addresses
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ExternalEffectContract,
    ExternalEffectPhase,
    ExternalEffectReceipt,
)
from athena.protocol.ids import new_id
from athena.state.external_effects import (
    ExternalEffectRecoveryRequired,
    ExternalEffectStore,
)

_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _legacy_transaction_result(
    request: CapabilityRequest,
    result: CapabilityResult,
    *,
    operation: str | None = None,
) -> CapabilityResult:
    """Keep the legacy result shape while exposing the durable receipt."""
    if result.status is not CapabilityResultStatus.OK:
        return result
    try:
        receipt = json.loads(result.output)
    except (TypeError, ValueError):
        return result
    response = dict(receipt.get("response") or {})
    if operation is not None:
        response["operation"] = operation
    metadata = dict(result.metadata or {})
    metadata["external_effect"] = receipt
    return _result(
        request,
        output=json.dumps(response),
        meta=metadata,
    )


def _legacy_idempotency_key(
    prefix: str,
    request: CapabilityRequest,
    arguments: Mapping[str, Any],
) -> str:
    digest = hashlib.sha256(
        json.dumps(dict(arguments), sort_keys=True, default=str).encode()
    ).hexdigest()[:24]
    return f"legacy:{prefix}:{request.call_id}:{digest}"


def _has_symlink_component(path: str, root: str) -> bool:
    """Return whether an existing component between root and path is a link."""
    root_real = os.path.realpath(root)
    candidate = os.path.abspath(path)
    try:
        relative = os.path.relpath(candidate, root_real)
    except ValueError:
        return True
    current = root_real
    for component in relative.split(os.sep):
        if component in ("", "."):
            continue
        if component == "..":
            return True
        current = os.path.join(current, component)
        if os.path.lexists(current) and os.path.islink(current):
            return True
    return False


def _run(cmd: list[str] | str, timeout: float = 15.0, shell=False):
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=shell, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"


async def _external_http_request(
    *,
    url: str,
    method: str,
    headers: dict[str, str],
    body: str | None,
    timeout: float,
    follow_redirects: bool,
    policy_name: str | None,
) -> dict[str, Any]:
    """Perform one bounded HTTP request after network policy checks."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must use http or https and include a host")
    hostname = parsed.hostname
    pinned_addresses: tuple[str, ...] = ()
    if policy_name == "restricted":
        candidate = hostname.strip().strip("[]").lower().rstrip(".")
        if not candidate or candidate in {"localhost", "localhost.localdomain"}:
            raise PermissionError("restricted network policy rejects local targets")
        try:
            addresses = {ipaddress.ip_address(candidate)}
            pinned_addresses = (candidate,)
        except ValueError:
            try:
                pinned_addresses = resolve_addresses(candidate, 0)
                addresses = {ipaddress.ip_address(address) for address in pinned_addresses}
            except (OSError, socket.gaierror) as exc:
                raise PermissionError(
                    f"unable to resolve host under restricted network policy: {hostname}"
                ) from exc
        if any(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            for address in addresses
        ):
            raise PermissionError(
                f"restricted network policy rejects private/local host: {hostname}"
            )
        if follow_redirects:
            raise PermissionError("restricted network policy disallows redirects")

    import httpx

    client_args: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
    }
    if policy_name == "restricted":
        client_args["trust_env"] = False
        client_args["transport"] = pinned_async_transport(hostname, pinned_addresses)
    async with httpx.AsyncClient(**client_args) as client:
        async with client.stream(
            method,
            url,
            headers=headers,
            content=body,
        ) as response:
            content = b""
            async for chunk in response.aiter_bytes():
                content += chunk
                if len(content) >= 8192:
                    content = content[:8192]
                    break
            return {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body_head": content.decode(response.encoding or "utf-8", errors="replace"),
                "body_truncated": len(content) >= 8192,
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            }


async def _run_external_http_request(**kwargs: Any) -> dict[str, Any]:
    """Run the async transport, while keeping synchronous test adapters valid."""
    result = _external_http_request(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

_MUTATIONS = {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask"}
_UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$")
_SERVICE_ROLLBACK = {
    "start": "stop",
    "stop": "start",
    "restart": "restart",
    "reload": "reload",
    "enable": "disable",
    "disable": "enable",
    "mask": "unmask",
    "unmask": "mask",
}


async def _service_offload(function, *args, **kwargs):
    """Run one bounded systemd probe without reusing a broken default pool.

    The service transaction is deliberately one synchronous probe/action per
    capability call. A short-lived executor also keeps a previous systemd
    probe from sharing a worker with a later approval/replay path.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="athena-service")
    try:
        return await loop.run_in_executor(
            executor,
            partial(function, *args, **kwargs),
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _service_effects(arguments: Mapping[str, Any]) -> frozenset[EffectClass]:
    """Resolve service authority from the concrete lifecycle phase."""
    operation = str(arguments.get("operation") or "").lower()
    if operation == "service_transaction":
        phase = str(arguments.get("phase") or "").lower()
        service_operation = str(arguments.get("service_operation") or "").lower()
        if service_operation not in _MUTATIONS:
            raise ValueError("service_transaction requires a valid service_operation")
        if phase in {"inspect", "prepare", "dry_run", "verify"}:
            return frozenset(
                {
                    EffectClass.READ_LOCAL,
                    EffectClass.EXECUTE,
                    EffectClass.SPAWN_PROCESS,
                }
            )
        if phase in {"apply", "compensate"}:
            effects = {
                EffectClass.PRIVILEGED,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
            }
            if service_operation in {"enable", "disable", "mask", "unmask"}:
                effects.add(EffectClass.WRITE_LOCAL)
            return frozenset(effects)
        raise ValueError(f"service transaction phase {phase!r} is unsupported")
    direct = {
        "list": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "status": frozenset(
            {EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "logs": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
    }
    if operation in direct:
        return direct[operation]
    if operation in _MUTATIONS:
        effects = {
            EffectClass.PRIVILEGED,
            EffectClass.EXECUTE,
            EffectClass.SPAWN_PROCESS,
        }
        if operation in {"enable", "disable", "mask", "unmask"}:
            effects.add(EffectClass.WRITE_LOCAL)
        return frozenset(effects)
    raise ValueError(f"operation {operation!r} has no service effect contract")


def _service_state(scope: list[str], unit: str) -> dict[str, Any]:
    """Read machine-parseable service state without invoking a shell."""
    rc, out, err = _run(
        [
            "systemctl",
            *scope,
            "show",
            unit,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState",
        ]
    )
    state: dict[str, Any] = {
        "ok": rc == 0,
        "returncode": rc,
    }
    for line in out.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            state[key.casefold()] = value
    if rc != 0:
        state["error"] = (err or out).strip()[:1000]
    return state


def _service_restore_plan(before: Mapping[str, Any], operation: str) -> dict[str, Any]:
    """Build a restore plan from the captured pre-image, never caller input."""
    operation = operation.lower()
    plan: dict[str, Any] = {
        "operation": operation,
        "restorable": bool(before.get("ok")),
        "actions": [],
        "target": {
            key: before.get(key)
            for key in ("loadstate", "activestate", "substate", "unitfilestate")
            if before.get(key) is not None
        },
    }
    if not plan["restorable"]:
        return plan
    if operation in {"restart", "reload"}:
        # A restart/reload cannot restore the prior process identity or
        # in-memory state.  Fail closed instead of pretending its inverse is
        # another restart/reload.
        plan["restorable"] = False
        plan["reason"] = f"{operation} has no exact inverse"
        return plan
    if operation in {"start", "stop"}:
        active = str(before.get("activestate") or "")
        if active == "active" and operation == "start":
            return plan
        if active != "active" and operation == "stop":
            return plan
        if active == "active":
            plan["actions"] = ["start"]
        elif active in {"inactive", "failed", "deactivating"}:
            plan["actions"] = ["stop"]
        else:
            plan["restorable"] = False
            plan["reason"] = f"unknown pre-state ActiveState={active!r}"
        return plan
    if operation in {"enable", "disable", "mask", "unmask"}:
        unit_state = str(before.get("unitfilestate") or "")
        if unit_state == "masked":
            plan["actions"] = ["mask"] if operation == "unmask" else []
        elif unit_state in {"enabled", "enabled-runtime"}:
            plan["actions"] = (
                ["unmask", "enable"]
                if operation == "mask"
                else ["disable"]
                if operation == "disable"
                else []
            )
        elif unit_state == "disabled":
            plan["actions"] = (
                ["unmask", "disable"]
                if operation == "mask"
                else ["disable"]
                if operation == "enable"
                else []
            )
        else:
            plan["restorable"] = False
            plan["reason"] = f"unknown pre-state UnitFileState={unit_state!r}"
        return plan
    plan["restorable"] = False
    plan["reason"] = f"unsupported operation {operation!r}"
    return plan


def _service_state_matches(state: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    """Compare the stable systemd fields captured in a service pre/post-image."""
    return bool(state.get("ok")) and all(
        str(state.get(key) or "") == str(value or "")
        for key, value in target.items()
        if key in {"loadstate", "activestate", "substate", "unitfilestate"}
    )


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
                rc, out, err = _run(
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
                return _run(["systemctl", *scope, "status", unit, "--no-pager", "-l"])

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
                return _run(["journalctl", *scope, "-u", unit, "-n", str(lines), "--no-pager"])

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
                    rc, out, err = _run(
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
                    rc, out, err = _run(
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


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


def _network_effects(arguments) -> frozenset[EffectClass]:
    """Resolve network authority from the concrete operation and method."""
    operation = str(arguments.get("operation") or "").lower()
    if operation == "http_transaction":
        phase = str(arguments.get("phase") or "").lower()
        if phase in {"apply", "compensate"}:
            return frozenset({EffectClass.NETWORK_WRITE})
        if phase in {"inspect", "prepare", "dry_run", "verify"}:
            return frozenset({EffectClass.NETWORK_READ})
        raise ValueError(f"phase {phase!r} has no external effect contract")
    if operation not in {
        "http",
        "tcp_connect",
        "dns",
        "listeners",
        "connections",
        "ping",
    }:
        raise ValueError(f"operation {operation!r} has no network effect contract")
    if operation == "http":
        method = str(arguments.get("method") or "GET").upper()
        if method in _SAFE_HTTP_METHODS:
            return frozenset({EffectClass.NETWORK_READ})
        # A mutating HTTP method is not a read merely because the capability
        # also returns a response body. This is the authority seen by policy.
        return frozenset({EffectClass.NETWORK_WRITE})
    if operation in {"listeners", "connections"}:
        return frozenset({EffectClass.READ_LOCAL})
    return frozenset({EffectClass.NETWORK_READ})


class NetworkCapability:
    """Machine networking diagnostics as first-class primitives."""

    descriptor = CapabilityDescriptor(
        id="network",
        description=(
            "Network diagnostics: HTTP requests (method/headers/body), raw "
            "TCP connect probes, DNS resolution, listening sockets, active "
            "connections, ping. Restricted outbound requests pin the DNS "
            "address checked by policy. http_transaction provides an explicit "
            "prepare/dry_run/apply/verify/compensate protocol for idempotent "
            "external HTTP effects."
        ),
        input_schema={
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "http"},
                        "url": {"type": "string", "minLength": 1},
                        "method": {"type": "string", "minLength": 1, "maxLength": 16},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "body": {"type": "string", "maxLength": 2_000_000},
                        "follow_redirects": {"type": "boolean"},
                    },
                    "required": ["operation", "url"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "tcp_connect"},
                        "host": {"type": "string", "minLength": 1},
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                    },
                    "required": ["operation", "host", "port"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "dns"},
                        "name": {"type": "string", "minLength": 1},
                    },
                    "required": ["operation", "name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"operation": {"const": "listeners"}},
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"operation": {"const": "connections"}},
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "ping"},
                        "host": {"type": "string", "minLength": 1},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                    },
                    "required": ["operation", "host"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "http_transaction"},
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
                        "url": {"type": "string", "minLength": 1},
                        "method": {"type": "string", "minLength": 1, "maxLength": 16},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "body": {"type": "string", "maxLength": 2_000_000},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                        "follow_redirects": {"type": "boolean"},
                        "transaction_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                        "external_identity": {"type": "string", "minLength": 1, "maxLength": 512},
                        "expected_status": {"type": "integer", "minimum": 100, "maximum": 599},
                        "expected_body_contains": {"type": "string", "maxLength": 1024},
                        "verify_url": {"type": "string", "minLength": 1},
                        "verify_method": {"type": "string", "minLength": 1, "maxLength": 16},
                        "verify_headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "verify_body": {"type": "string", "maxLength": 2_000_000},
                        "compensate_url": {"type": "string", "minLength": 1},
                        "compensate_method": {"type": "string", "minLength": 1, "maxLength": 16},
                        "compensate_headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "compensate_body": {"type": "string", "maxLength": 2_000_000},
                    },
                    "required": ["operation", "phase", "url"],
                    "additionalProperties": False,
                },
            ],
        },
        effects=frozenset(
            {EffectClass.READ_LOCAL, EffectClass.NETWORK_READ, EffectClass.NETWORK_WRITE}
        ),
        effect_resolver=_network_effects,
        external_effects={
            "http_transaction": ExternalEffectContract(
                phases=frozenset(ExternalEffectPhase),
                idempotency_required=True,
                # Generic HTTP has no trustworthy inverse.  A caller may
                # supply a compensating request, but that is compensatable
                # behavior, not a proof that the remote resource is restored.
                reversible=False,
                compensatable=True,
                approval_floor="ask",
                identity_fields=("url", "method"),
            ),
        },
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, *, external_store: ExternalEffectStore | None = None) -> None:
        self._external_store = external_store or ExternalEffectStore()

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        timeout = min(float(args.get("timeout") or 10.0), 30.0)
        loop = asyncio.get_running_loop()
        context = kw.get("context")
        network_policy = getattr(getattr(context, "workspace", None), "network_policy", None)
        policy_name = getattr(network_policy, "value", network_policy)

        # Network diagnostics are still network effects.  Enforce the task's
        # workspace policy at the capability boundary for every outbound
        # operation, rather than relying on the HTTP branch alone.
        if (
            op in {"http", "http_transaction", "tcp_connect", "dns", "ping"}
            and policy_name == "deny"
        ):
            return _result(request, ok=False, error="network denied by workspace policy")

        if op == "http_transaction":
            return await self._http_transaction(
                request,
                args,
                policy_name=policy_name,
            )
        if op == "http" and str(args.get("method") or "GET").upper() not in _SAFE_HTTP_METHODS:
            return await self._legacy_http_mutation(
                request,
                args,
                policy_name=policy_name,
            )

        def _restricted_addresses(host: str) -> tuple[str | None, tuple[str, ...]]:
            """Reject localhost/private/metadata targets in restricted mode.

            Resolve names before connecting so a public-looking hostname that
            resolves into a private or link-local address cannot be used as a
            basic SSRF primitive.  The HTTP branch also disables redirects in
            restricted mode because a redirect target is a new destination.
            """
            if policy_name != "restricted":
                return None, ()
            candidate = host.strip().strip("[]").lower().rstrip(".")
            if not candidate or candidate in {"localhost", "localhost.localdomain"}:
                return "restricted network policy rejects local targets", ()
            try:
                addresses = {ipaddress.ip_address(candidate)}
                address_strings: tuple[str, ...] = (candidate,)
            except ValueError:
                try:
                    address_strings = resolve_addresses(candidate, 0)
                    addresses = {ipaddress.ip_address(address) for address in address_strings}
                except (OSError, socket.gaierror):
                    return f"unable to resolve host under restricted network policy: {host}", ()
            if any(
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
                for address in addresses
            ):
                return f"restricted network policy rejects private/local host: {host}", ()
            return None, tuple(address_strings)

        if op == "http":
            url = str(args.get("url") or "")
            method = str(args.get("method") or "GET").upper()
            if not url:
                return _result(request, ok=False, error="url required")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return _result(
                    request, ok=False, error="url must use http or https and include a host"
                )
            hostname = parsed.hostname
            restricted_error, pinned_addresses = _restricted_addresses(hostname)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)
            follow_redirects = bool(args.get("follow_redirects", False))
            if policy_name == "restricted" and follow_redirects:
                return _result(
                    request, ok=False, error="restricted network policy disallows redirects"
                )

            import httpx

            def _http():
                client_args: dict[str, Any] = {
                    "timeout": timeout,
                    "follow_redirects": follow_redirects,
                }
                if policy_name == "restricted":
                    client_args["trust_env"] = False
                    client_args["transport"] = pinned_sync_transport(hostname, pinned_addresses)
                with httpx.Client(**client_args) as c:
                    with c.stream(
                        method,
                        url,
                        headers=dict(args.get("headers") or {}),
                        content=args.get("body"),
                    ) as resp:
                        body = b""
                        for chunk in resp.iter_bytes():
                            body += chunk
                            if len(body) >= 8192:
                                body = body[:8192]
                                break
                        return {
                            "status": resp.status_code,
                            "headers": dict(resp.headers),
                            "body_head": body.decode(resp.encoding or "utf-8", errors="replace"),
                            "body_truncated": len(body) >= 8192,
                            "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
                        }

            try:
                # Use a capability-owned executor.  The process-wide asyncio
                # executor is also used by teardown and can strand the loop
                # in embedded runners after a synchronous HTTP probe.
                info = await _service_offload(_http)
            except Exception as exc:  # noqa: BLE001 - report network failure truthfully
                return _result(request, ok=False, error=str(exc))
            return _result(
                request, output=json.dumps(info, indent=2)[:4000], meta={"status": info["status"]}
            )

        if op == "tcp_connect":
            host = str(args.get("host") or "127.0.0.1")
            port = int(args.get("port") or 0)
            if not (0 < port < 65536):
                return _result(request, ok=False, error="valid port required")
            restricted_error, pinned_addresses = _restricted_addresses(host)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _tcp():
                s = socket.socket()
                s.settimeout(timeout)
                try:
                    s.connect((pinned_addresses[0] if pinned_addresses else host, port))
                    return True
                except OSError:
                    return False
                finally:
                    s.close()

            open_ = await loop.run_in_executor(None, _tcp)
            return _result(
                request,
                output=f"{host}:{port} " + ("open" if open_ else "closed"),
                meta={"open": open_},
            )

        if op == "dns":
            name = str(args.get("name") or "localhost")
            restricted_error, _ = _restricted_addresses(name)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _dns():
                infos = socket.getaddrinfo(name, None)
                uniq = sorted({str(i[4][0]) for i in infos})
                return f"{name} -> {', '.join(uniq)}"

            try:
                text = await loop.run_in_executor(None, _dns)
            except socket.gaierror as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=text)

        if op == "listeners":

            def _ls():
                _rc, out, err = _run(["ss", "-tlnp"])
                return out or err

            return _result(request, output=(await loop.run_in_executor(None, _ls))[:5000])

        if op == "connections":

            def _cx():
                _rc, out, err = _run(["ss", "-tnp"])
                return out or err

            return _result(request, output=(await loop.run_in_executor(None, _cx))[:5000])

        if op == "ping":
            host = str(args.get("host") or "")
            if not host:
                return _result(request, ok=False, error="host required")
            restricted_error, pinned_addresses = _restricted_addresses(host)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _pg():
                target = pinned_addresses[0] if pinned_addresses else host
                rc, out, err = _run(["ping", "-c", "3", "-W", "2", target])
                return rc, out or err

            rc, out = await loop.run_in_executor(None, _pg)
            return _result(request, ok=rc == 0, output=out[:3000])

        return _result(request, ok=False, error=f"unknown operation: {op}")

    async def _legacy_http_mutation(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        *,
        policy_name: str | None,
    ) -> CapabilityResult:
        idempotency_key = _legacy_idempotency_key("http", request, args)
        base = {
            **args,
            "operation": "http_transaction",
            "idempotency_key": idempotency_key,
        }
        prepared = await self._http_transaction(
            request,
            {**base, "phase": "prepare"},
            policy_name=policy_name,
        )
        if prepared.status is not CapabilityResultStatus.OK:
            return prepared
        try:
            transaction_id = str(json.loads(prepared.output)["transaction_id"])
        except (KeyError, TypeError, ValueError):
            return _result(request, ok=False, error="HTTP preparation receipt is malformed")
        applied = await self._http_transaction(
            request,
            {**base, "phase": "apply", "transaction_id": transaction_id},
            policy_name=policy_name,
        )
        return _legacy_transaction_result(request, applied)

    async def _http_transaction(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        *,
        policy_name: str | None,
    ) -> CapabilityResult:
        """Run the explicit external HTTP lifecycle with durable receipts."""
        try:
            phase = ExternalEffectPhase(str(args.get("phase") or "").lower())
        except ValueError:
            return _result(request, ok=False, error="unknown external HTTP phase")
        contract = self.descriptor.resolve_external_effect_contract(args)
        if contract is None or phase not in contract.phases:
            return _result(request, ok=False, error="external HTTP phase is unsupported")
        if phase is not ExternalEffectPhase.INSPECT and request.task_id is None:
            return _result(
                request,
                ok=False,
                error="external transactions require a task-scoped invocation",
            )

        url = str(args.get("url") or "")
        method = str(args.get("method") or "GET").upper()
        if not url:
            return _result(request, ok=False, error="url required")
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("url must use http or https and include a host")
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))
        if bool(args.get("follow_redirects", False)):
            return _result(
                request,
                ok=False,
                error="external transactions do not allow redirects",
            )

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
                error=f"external {phase.value} requires an idempotency_key",
            )
        if phase is not ExternalEffectPhase.INSPECT and not transaction_id:
            if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
                transaction_id = new_id("external-tx")
            else:
                return _result(request, ok=False, error="transaction_id required")

        external_identity = self.descriptor.resolve_external_identity(args)
        if external_identity is None:
            return _result(request, ok=False, error="external HTTP identity unavailable")
        request_digest = _external_request_digest(args)
        if phase is ExternalEffectPhase.INSPECT:
            return _result(
                request,
                output=json.dumps(
                    {
                        "phase": phase.value,
                        "transaction_id": transaction_id or new_id("external-tx"),
                        "external_identity": external_identity,
                        "request_digest": request_digest,
                        "idempotency_required": contract.idempotency_required,
                        "reversible": contract.reversible,
                        "approval_floor": contract.approval_floor,
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
                compensation_digest = _external_compensation_digest(args)
                existing_digest = dict(receipt.get("response") or {}).get(
                    "compensation_plan_digest"
                )
                if existing_digest is not None and existing_digest != compensation_digest:
                    raise ValueError("prepared compensation plan does not match this transaction")
                receipt = await self._external_store.finish(
                    transaction_id,
                    status=("PREPARED" if phase is ExternalEffectPhase.PREPARE else "DRY_RUN"),
                    response={
                        "compensation_plan_digest": compensation_digest,
                    },
                    phase=phase,
                )
            except (ExternalEffectRecoveryRequired, KeyError, ValueError) as exc:
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
                    error="external apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            headers = dict(args.get("headers") or {})
            headers.setdefault("Idempotency-Key", idempotency_key or "")
            try:
                response = await _run_external_http_request(
                    url=url,
                    method=method,
                    headers=headers,
                    body=args.get("body"),
                    timeout=min(float(args.get("timeout") or 10.0), 30.0),
                    follow_redirects=bool(args.get("follow_redirects", False)),
                    policy_name=policy_name,
                )
            except Exception as exc:  # noqa: BLE001 - remote outcome is uncertain
                try:
                    receipt = await self._external_store.finish(
                        transaction_id,
                        status="RECOVERY_REQUIRED",
                        error=str(exc),
                    )
                except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                    receipt = {"transaction_id": transaction_id, "error": str(exc)}
                return _result(
                    request,
                    ok=False,
                    error="external request outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            response_record = {
                **dict(receipt.get("response") or {}),
                **_safe_external_response(response),
            }
            response_status = int(response_record.get("status") or 0)
            apply_status = (
                "COMPLETED"
                if 200 <= response_status < 300
                else "APPLY_REJECTED"
                if 400 <= response_status < 500
                else "APPLY_FAILED"
            )
            receipt = await self._external_store.finish(
                transaction_id,
                status=apply_status,
                response=response_record,
                error=(
                    None
                    if apply_status == "COMPLETED"
                    else f"external HTTP apply returned status {response_status}"
                ),
            )
            return _external_receipt_result(request, receipt)

        if phase is ExternalEffectPhase.COMPENSATE:
            try:
                prepared = await self._external_store.get(transaction_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            prepared_response = dict((prepared or {}).get("response") or {})
            expected_plan = prepared_response.get("compensation_plan_digest")
            actual_plan = _external_compensation_digest(args)
            if not expected_plan or expected_plan != actual_plan:
                return _result(
                    request,
                    ok=False,
                    error=(
                        "compensation plan must be prepared before the external "
                        "HTTP apply and must match exactly"
                    ),
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
        follow_url = url
        follow_method = method
        follow_headers = dict(args.get("headers") or {})
        follow_body = args.get("body")
        if phase is ExternalEffectPhase.VERIFY:
            follow_url = str(args.get("verify_url") or url)
            follow_method = str(args.get("verify_method") or "GET").upper()
            follow_headers = dict(args.get("verify_headers") or {})
            follow_body = args.get("verify_body")
            if follow_method not in _SAFE_HTTP_METHODS:
                return _result(
                    request,
                    ok=False,
                    error="external verification must use a read-only HTTP method",
                )
        else:
            follow_url = str(args.get("compensate_url") or "")
            follow_method = str(args.get("compensate_method") or "DELETE").upper()
            follow_headers = dict(args.get("compensate_headers") or {})
            follow_headers.setdefault(
                "Idempotency-Key",
                f"{idempotency_key}:compensate",
            )
            follow_body = args.get("compensate_body")
            if not follow_url:
                return _result(request, ok=False, error="compensate_url required")
        try:
            response = await _run_external_http_request(
                url=follow_url,
                method=follow_method,
                headers=follow_headers,
                body=follow_body,
                timeout=min(float(args.get("timeout") or 10.0), 30.0),
                follow_redirects=bool(args.get("follow_redirects", False)),
                policy_name=policy_name,
            )
        except Exception as exc:  # noqa: BLE001 - preserve uncertain remote state
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error=str(exc),
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="external follow-up outcome is uncertain; recovery required",
                meta={"external_effect": receipt},
            )

        if phase is ExternalEffectPhase.VERIFY:
            expected_status = args.get("expected_status")
            status_ok = (
                200 <= int(response["status"]) < 300
                if expected_status is None
                else int(response["status"]) == int(expected_status)
            )
            body_match = args.get("expected_body_contains") is None or str(
                args["expected_body_contains"]
            ) in str(response.get("body_head", ""))
            verified = status_ok and body_match
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
                response={
                    **dict(receipt.get("response") or {}),
                    **_safe_external_response(response),
                    "verified": verified,
                },
                phase=phase,
            )
            return _result(
                request,
                ok=verified,
                output=json.dumps(receipt),
                error="external verification failed" if not verified else None,
                meta={"external_effect": receipt},
            )

        response_status = int(response.get("status") or 0)
        compensation_status = (
            "COMPENSATION_SENT"
            if 200 <= response_status < 300
            else "COMPENSATION_REJECTED"
            if 400 <= response_status < 500
            else "COMPENSATION_FAILED"
        )
        receipt = await self._external_store.finish(
            transaction_id,
            status=compensation_status,
            response={
                **dict(receipt.get("response") or {}),
                **_safe_external_response(response),
            },
            error=(
                None
                if compensation_status == "COMPENSATION_SENT"
                else f"external HTTP compensation returned status {response_status}"
            ),
            phase=phase,
        )
        return _external_receipt_result(request, receipt)


def _external_request_digest(arguments: Mapping[str, Any]) -> str:
    """Hash request content while excluding lifecycle and receipt controls."""
    value = {
        key: arguments.get(key)
        for key in (
            "url",
            "method",
            "headers",
            "body",
            "follow_redirects",
        )
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _service_request_digest(unit: str, service_operation: str, user_scope: bool) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "unit": unit,
                "service_operation": service_operation,
                "user_scope": user_scope,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _database_request_digest(path: str, sql: str, params: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "path": path,
                "sql": sql,
                "params": params,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _external_compensation_digest(arguments: Mapping[str, Any]) -> str | None:
    url = str(arguments.get("compensate_url") or "").strip()
    if not url:
        return None
    value = {
        "url": url,
        "method": str(arguments.get("compensate_method") or "DELETE").upper(),
        "headers": arguments.get("compensate_headers") or {},
        "body": arguments.get("compensate_body"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _safe_external_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Keep receipts useful without persisting common response credentials."""
    value = dict(response)
    headers = value.get("headers")
    if isinstance(headers, Mapping):
        value["headers"] = {
            str(key): str(item)
            for key, item in headers.items()
            if str(key).casefold()
            not in {
                "authorization",
                "cookie",
                "set-cookie",
                "proxy-authorization",
                "x-api-key",
                "x-auth-token",
            }
        }
    body = value.get("body_head")
    if isinstance(body, str):
        value["body_head"] = body[:8192]
    return value


def _external_receipt_result(request: CapabilityRequest, receipt: Mapping[str, Any]):
    normalized = ExternalEffectReceipt(
        receipt_id=str(receipt.get("receipt_id") or ""),
        transaction_id=str(receipt.get("transaction_id") or ""),
        capability_id=str(receipt.get("capability_id") or request.capability_id),
        phase=ExternalEffectPhase(str(receipt.get("phase") or ExternalEffectPhase.INSPECT.value)),
        status=str(receipt.get("status") or "UNKNOWN"),
        external_identity=str(receipt.get("external_identity") or ""),
        request_digest=str(receipt.get("request_digest") or ""),
        idempotency_key=(
            str(receipt["idempotency_key"]) if receipt.get("idempotency_key") is not None else None
        ),
        response=dict(receipt.get("response") or {}),
        error=(str(receipt["error"]) if receipt.get("error") is not None else None),
        created_at=(str(receipt["created_at"]) if receipt.get("created_at") is not None else None),
        updated_at=(str(receipt["updated_at"]) if receipt.get("updated_at") is not None else None),
    ).to_record()
    status = normalized["status"]
    failed_statuses = {
        "APPLY_FAILED",
        "APPLY_REJECTED",
        "FAILED",
        "RECOVERY_REQUIRED",
        "VERIFY_FAILED",
        "COMPENSATION_FAILED",
        "COMPENSATION_REJECTED",
        "COMPENSATION_VERIFY_FAILED",
    }
    error = normalized["error"]
    if status in failed_statuses and not error:
        error = f"external transaction is {status.lower()}"
    return _result(
        request,
        output=json.dumps(normalized, sort_keys=True),
        ok=status not in failed_statuses,
        error=error,
        meta={"external_effect": normalized},
    )


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


def _database_effects(arguments) -> frozenset[EffectClass]:
    """Resolve database authority from the operation *and* SQL shape.

    The operation label alone is not a trustworthy security boundary: a
    caller can put an UPDATE inside a ``query`` request. Only a narrow
    read-only SQL prefix is treated as observational; everything else is
    classified as a local mutation before policy evaluation.
    """
    op = str(arguments.get("operation") or "").lower()
    if op == "database_transaction":
        phase = str(arguments.get("phase") or "").lower()
        if phase in {"inspect", "prepare", "dry_run", "verify"}:
            return frozenset({EffectClass.READ_LOCAL})
        if phase in {"apply", "compensate"}:
            return frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL})
        raise ValueError(f"phase {phase!r} has no database effect contract")
    if op not in {"tables", "schema", "query", "explain", "execute"}:
        raise ValueError(f"operation {op!r} has no declared effect classification")
    if op == "execute":
        return frozenset({EffectClass.WRITE_LOCAL})
    if op == "query":
        sql = str(arguments.get("sql") or "").lstrip()
        first = sql.split(None, 1)[0].upper() if sql else ""
        if first not in {"SELECT", "VALUES"}:
            return frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL})
    return frozenset({EffectClass.READ_LOCAL})


class DatabaseCapability:
    """SQL execution with schema introspection (SQLite built-in)."""

    descriptor = CapabilityDescriptor(
        id="database",
        description=(
            "Database access: connect to a SQLite file, inspect schema/tables, "
            "run queries, EXPLAIN, and "
            "execute writes (WRITE_LOCAL, policy-gated). Operations: "
            "tables/schema/query/explain/execute. Results are paginated."
        ),
        input_schema={
            "type": "object",
            "required": ["operation", "path"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "tables",
                        "schema",
                        "query",
                        "explain",
                        "execute",
                        "database_transaction",
                    ],
                },
                "path": {"type": "string"},
                "sql": {"type": "string"},
                "params": {"type": "array"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
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
                "transaction_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "external_identity": {"type": "string", "minLength": 1, "maxLength": 512},
                "verify_sql": {"type": "string", "maxLength": 100_000},
                "verify_params": {"type": "array"},
                "expected_after_hash": {"type": "string", "maxLength": 128},
                "expected_rowcount": {"type": "integer", "minimum": 0},
                "expected_value": {},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "operation": {"const": "database_transaction"},
                        }
                    },
                    "then": {"required": ["phase"]},
                }
            ],
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
            }
        ),
        effect_resolver=_database_effects,
        external_effects={
            "database_transaction": ExternalEffectContract(
                phases=frozenset(ExternalEffectPhase),
                idempotency_required=True,
                reversible=True,
                approval_floor="ask",
                identity_fields=("path", "sql"),
            ),
        },
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        *,
        mutation_store=None,
        artifact_store=None,
        external_store: ExternalEffectStore | None = None,
    ) -> None:
        self._mutations = mutation_store
        self._artifacts = artifact_store
        self._external_store = external_store or ExternalEffectStore()

    @staticmethod
    def _read_only_authorizer(action, arg1, arg2, database, source):
        import sqlite3

        denied = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE,
        }
        # These functions are not part of the normal query surface and may be
        # supplied by an extension.  Keep the read-only contract true even if
        # the host process has loaded SQLite extensions elsewhere.
        if action == getattr(sqlite3, "SQLITE_FUNCTION", -1):
            function_name = str(arg2 or arg1 or "").casefold()
            if function_name in {"load_extension", "readfile", "writefile"}:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

    @classmethod
    def _set_read_only_authorizer(cls, conn) -> None:
        conn.set_authorizer(cls._read_only_authorizer)

    def _connect(self, path: str, *, readonly: bool = False):
        import sqlite3

        if readonly:
            from urllib.parse import quote

            conn = sqlite3.connect(f"file:{quote(path, safe='/')}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _confine_path(path: str, context) -> str:
        root = getattr(getattr(context, "workspace", None), "root", None)
        candidate = (
            os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
            if root
            else os.path.realpath(path)
        )
        if root:
            root = os.path.realpath(root)
            if candidate != root and not candidate.startswith(root + os.sep):
                raise ValueError("database path outside workspace")
            original = path if os.path.isabs(path) else os.path.join(root, path)
            if _has_symlink_component(original, root):
                raise ValueError("database path cannot traverse a symlink")
        return candidate

    async def _snapshot(self, path: str, task_id: str | None, *, persist: bool = True):
        if not os.path.isfile(path):
            return None, None
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        ref = None
        if persist and self._artifacts is not None:
            try:
                saved = await self._artifacts.save(
                    task_id=task_id,
                    content=data,
                    mime_type="application/x-sqlite3",
                    producer="database",
                )
                ref = saved.uri
            except Exception:
                ref = None
        return ref, digest

    async def invoke(self, request: CapabilityRequest, context=None, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        if op == "database_transaction":
            return await self._database_transaction(request, args, context)
        if op == "execute":
            return await self._legacy_database_mutation(request, args, context)
        path = str(args.get("path") or "")
        sql = str(args.get("sql") or "")
        params = list(args.get("params") or [])
        offset = int(args.get("offset") or 0)
        limit = min(int(args.get("limit") or 200), 1000)
        workspace = getattr(context, "workspace", None) if context is not None else None
        if workspace is None or not getattr(workspace, "root", None):
            return _result(
                request,
                ok=False,
                error="database access requires a workspace-bound invocation",
            )
        if not path:
            return _result(request, ok=False, error="path required")
        try:
            path = self._confine_path(path, context)
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))
        if op != "tables" and not path:
            return _result(request, ok=False, error="path required")
        if op in ("query", "explain", "execute") and not sql:
            return _result(request, ok=False, error="sql required")
        readonly = op in {"tables", "schema", "query", "explain"}
        if readonly and not os.path.isfile(path):
            return _result(
                request, ok=False, error=f"database does not exist for read operation: {path}"
            )
        if os.path.isfile(path) and not os.access(path, os.R_OK):
            return _result(request, ok=False, error=f"unreadable: {path}")

        def _rows(cur):
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(offset + limit + 1)
            truncated = len(rows) > offset + limit
            rows = rows[offset : offset + limit]
            return json.dumps(
                {
                    "columns": cols,
                    "rows": [[str(v) for v in r] for r in rows],
                    "offset": offset,
                    "limit": limit,
                    "truncated": truncated,
                },
                default=str,
            )

        def _tables():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name "
                    "LIMIT ? OFFSET ?",
                    (limit + 1, offset),
                )
                rows = cur.fetchall()
                return json.dumps(
                    {
                        "rows": [[r[1], r[0]] for r in rows[:limit]],
                        "offset": offset,
                        "limit": limit,
                        "truncated": len(rows) > limit,
                    }
                )
            finally:
                conn.close()

        def _schema():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "ORDER BY name LIMIT ? OFFSET ?",
                    (limit + 1, offset),
                )
                rows = cur.fetchall()
                return json.dumps(
                    {
                        "statements": [r[0] for r in rows[:limit]],
                        "offset": offset,
                        "limit": limit,
                        "truncated": len(rows) > limit,
                    }
                )
            finally:
                conn.close()

        def _query():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(sql, params)
                return _rows(cur)
            finally:
                conn.close()

        def _explain():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                return _rows(cur)
            finally:
                conn.close()

        def _execute():
            conn = self._connect(path)
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return json.dumps({"rowcount": cur.rowcount})
            finally:
                conn.close()

        handlers = {
            "tables": _tables,
            "schema": _schema,
            "query": _query,
            "explain": _explain,
            "execute": _execute,
        }
        if op not in handlers:
            return _result(request, ok=False, error=f"unknown operation: {op}")
        mutation_id = None
        before_ref = None
        before_hash = None
        if op == "execute":
            before_ref, before_hash = await self._snapshot(path, request.task_id)
            if self._mutations is not None:
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    path,
                    "database.execute",
                    before_ref=before_ref,
                    inverse=(
                        {"op": "restore_from_ref", "target": path, "ref": before_ref}
                        if before_ref
                        else {"op": "delete", "target": path}
                    ),
                )
                await self._mutations.mark_started(mutation_id)
        try:
            await asyncio.sleep(0)
            out = handlers[op]()
        except Exception as exc:  # noqa: BLE001 - report database failure truthfully
            if mutation_id is not None:
                await self._mutations.mark_failed(mutation_id, str(exc))
            return _result(request, ok=False, error=str(exc))
        if mutation_id is not None:
            _after_ref, after_hash = await self._snapshot(path, request.task_id, persist=False)
            reversible = before_ref is not None or before_hash is None
            await self._mutations.complete(
                mutation_id,
                after_hash=after_hash,
                reversible=reversible,
                inverse=(
                    {"op": "restore_from_ref", "target": path, "ref": before_ref}
                    if before_ref
                    else {"op": "delete", "target": path}
                ),
            )
            out_meta = {
                "mutation": {
                    "resource": path,
                    "operation": "database.execute",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "before_ref": before_ref,
                    "reversible": reversible,
                    "mutation_id": mutation_id,
                }
            }
            return _result(request, output=out, meta=out_meta)
        return _result(request, output=out)

    async def _legacy_database_mutation(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        context=None,
    ) -> CapabilityResult:
        """Lower the legacy database execute operation into a durable transaction."""
        base = {
            **args,
            "operation": "database_transaction",
            "idempotency_key": _legacy_idempotency_key(
                "database",
                request,
                args,
            ),
        }
        prepared = await self._database_transaction(
            request,
            {**base, "phase": "prepare"},
            context,
        )
        if prepared.status is not CapabilityResultStatus.OK:
            return prepared
        try:
            transaction_id = str(json.loads(prepared.output)["transaction_id"])
        except (KeyError, TypeError, ValueError):
            return _result(
                request,
                ok=False,
                error="database preparation receipt is malformed",
            )
        applied = await self._database_transaction(
            request,
            {**base, "phase": "apply", "transaction_id": transaction_id},
            context,
        )
        if applied.status is not CapabilityResultStatus.OK:
            return applied
        try:
            receipt = json.loads(applied.output)
            response = dict(receipt.get("response") or {})
        except (TypeError, ValueError):
            return applied

        # Keep the legacy mutation ledger populated for callers that inspect
        # database.execute history. The external receipt remains the source
        # of truth for crash/replay safety.
        metadata = dict(applied.metadata or {})
        if self._mutations is not None:
            try:
                before_ref = response.get("before_ref")
                before_hash = response.get("before_hash")
                after_hash = response.get("after_hash")
                target = str(response.get("path") or args.get("path") or "")
                inverse = (
                    {"op": "restore_from_ref", "target": target, "ref": before_ref}
                    if before_ref
                    else {"op": "delete", "target": target}
                )
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    target,
                    "database.execute",
                    before_ref=before_ref,
                    inverse=inverse,
                )
                await self._mutations.mark_started(mutation_id)
                await self._mutations.complete(
                    mutation_id,
                    after_hash=after_hash,
                    reversible=before_ref is not None or before_hash is None,
                    inverse=inverse,
                )
                metadata["mutation"] = {
                    "resource": target,
                    "operation": "database.execute",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "before_ref": before_ref,
                    "reversible": before_ref is not None or before_hash is None,
                    "mutation_id": mutation_id,
                }
            except Exception as exc:  # noqa: BLE001 - receipt is authoritative
                metadata["mutation_ledger_error"] = str(exc)
        return _result(
            request,
            output=json.dumps({"rowcount": response.get("rowcount", -1)}),
            meta=metadata,
        )

    async def _database_transaction(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        context=None,
    ) -> CapabilityResult:
        """Execute one SQLite mutation through a durable external-effect protocol.

        A SQLite file is local, but it is outside Athena's shadow transaction:
        the database engine commits its own state.  The receipt therefore
        records the exact pre-image, post-image, stable idempotency key, and
        compensation boundary instead of treating a database mutation as an
        ordinary shadowable file write.
        """
        try:
            phase = ExternalEffectPhase(str(args.get("phase") or "").lower())
        except ValueError:
            return _result(request, ok=False, error="unknown database transaction phase")
        if phase is not ExternalEffectPhase.INSPECT and request.task_id is None:
            return _result(
                request,
                ok=False,
                error="database transactions require a task-scoped invocation",
            )
        workspace = getattr(context, "workspace", None) if context else None
        if workspace is None or not getattr(workspace, "root", None):
            return _result(
                request,
                ok=False,
                error="database access requires a workspace-bound invocation",
            )
        raw_path = str(args.get("path") or "")
        if not raw_path:
            return _result(request, ok=False, error="path required")
        try:
            path = self._confine_path(raw_path, context)
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))

        sql = str(args.get("sql") or "").strip()
        if not sql:
            return _result(request, ok=False, error="sql required")
        first = sql.split(None, 1)[0].upper() if sql else ""
        if first in {"SELECT", "VALUES", "EXPLAIN", "PRAGMA"}:
            return _result(
                request,
                ok=False,
                error="database transaction SQL must be mutating",
            )
        params = list(args.get("params") or [])
        request_digest = _database_request_digest(path, sql, params)
        external_identity = self.descriptor.resolve_external_identity(
            args,
            getattr(context, "workspace", None),
        )
        if external_identity is None:
            return _result(request, ok=False, error="database transaction identity unavailable")
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
                error=f"database {phase.value} requires an idempotency_key",
            )
        if phase is not ExternalEffectPhase.INSPECT and not transaction_id:
            if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
                transaction_id = new_id("database-tx")
            else:
                return _result(request, ok=False, error="transaction_id required")

        contract = self.descriptor.resolve_external_effect_contract(args)
        if contract is None or phase not in contract.phases:
            return _result(request, ok=False, error="database transaction phase is unsupported")
        if phase is ExternalEffectPhase.INSPECT:
            return _result(
                request,
                output=json.dumps(
                    {
                        "phase": phase.value,
                        "transaction_id": transaction_id or new_id("database-tx"),
                        "external_identity": external_identity,
                        "request_digest": request_digest,
                        "idempotency_required": contract.idempotency_required,
                        "reversible": contract.reversible,
                        "approval_floor": contract.approval_floor,
                    }
                ),
            )

        if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
            before_exists = os.path.isfile(path)
            before_ref, before_hash = await self._snapshot(
                path,
                request.task_id,
            )
            if before_exists and before_ref is None:
                return _result(
                    request,
                    ok=False,
                    error="database transaction requires an artifact store to preserve its pre-image",
                )
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
                response = {
                    "path": path,
                    "sql_hash": hashlib.sha256(sql.encode()).hexdigest(),
                    "before_exists": before_exists,
                    "before_ref": before_ref,
                    "before_hash": before_hash,
                    "params_digest": hashlib.sha256(
                        json.dumps(
                            params,
                            sort_keys=True,
                            default=str,
                        ).encode()
                    ).hexdigest(),
                }
                receipt = await self._external_store.finish(
                    transaction_id,
                    status=("PREPARED" if phase is ExternalEffectPhase.PREPARE else "DRY_RUN"),
                    response=response,
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
                    error="database apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            try:

                def _apply() -> int:
                    conn = self._connect(path)
                    try:
                        cursor = conn.execute(sql, params)
                        conn.commit()
                        return int(cursor.rowcount)
                    finally:
                        conn.close()

                await asyncio.sleep(0)
                rowcount = _apply()
                _after_ref, after_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
                response = {
                    **dict(receipt.get("response") or {}),
                    "rowcount": rowcount,
                    "after_hash": after_hash,
                }
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="COMPLETED",
                    response=response,
                )
            except Exception as exc:  # noqa: BLE001 - commit outcome may be unknown
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="RECOVERY_REQUIRED",
                    error=str(exc),
                )
                return _result(
                    request,
                    ok=False,
                    error="database apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            return _result(
                request,
                output=json.dumps(receipt),
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
            and receipt.get("status")
            in {"COMPENSATED", "COMPENSATION_SENT", "COMPENSATION_VERIFIED"}
        ):
            return _external_receipt_result(request, receipt)

        payload = dict(receipt.get("response") or {})
        if phase is ExternalEffectPhase.VERIFY:
            compensation_verification = (
                receipt.get("verification_target") == "COMPENSATION_PRESTATE"
            )
            current_hash = None
            if os.path.isfile(path):
                _ref, current_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
            expected_hash = (
                str(payload.get("before_hash") or "")
                if compensation_verification
                else str(args.get("expected_after_hash") or payload.get("after_hash") or "")
            )
            verified = bool(current_hash and expected_hash == current_hash)
            if compensation_verification and not bool(payload.get("before_exists")):
                verified = not os.path.lexists(path)
            query_result: dict[str, Any] | None = None
            verify_sql = str(args.get("verify_sql") or "").strip()
            if verify_sql:
                verify_first = verify_sql.split(None, 1)[0].upper()
                if verify_first not in {"SELECT", "VALUES"}:
                    return _result(
                        request,
                        ok=False,
                        error="database verification SQL must be read-only",
                    )

                def _verify_query() -> dict[str, Any]:
                    conn = self._connect(path, readonly=True)
                    self._set_read_only_authorizer(conn)
                    try:
                        cursor = conn.execute(
                            verify_sql,
                            list(args.get("verify_params") or []),
                        )
                        columns = [item[0] for item in cursor.description or ()]
                        rows = cursor.fetchmany(101)
                        return {
                            "columns": columns,
                            "rows": [[str(value) for value in row] for row in rows[:100]],
                            "truncated": len(rows) > 100,
                        }
                    finally:
                        conn.close()

                try:
                    await asyncio.sleep(0)
                    query_result = _verify_query()
                    expected_rowcount = args.get("expected_rowcount")
                    expected_value = args.get("expected_value")
                    if expected_rowcount is not None:
                        verified = verified and len(query_result["rows"]) == int(expected_rowcount)
                    if expected_value is not None:
                        rows = query_result["rows"]
                        verified = (
                            verified
                            and bool(rows)
                            and bool(rows[0])
                            and rows[0][0] == str(expected_value)
                        )
                except Exception as exc:  # noqa: BLE001 - verification is evidence
                    query_result = {"error": str(exc)}
                    verified = False
            receipt = await self._external_store.finish(
                transaction_id,
                status=(
                    "COMPENSATION_VERIFIED"
                    if compensation_verification and verified
                    else "COMPENSATION_VERIFY_FAILED"
                    if compensation_verification
                    else "VERIFIED"
                    if verified
                    else "VERIFY_FAILED"
                ),
                response={
                    **payload,
                    "current_hash": current_hash,
                    "query": query_result,
                    "verified": verified,
                },
                phase=phase,
            )
            return _result(
                request,
                ok=verified,
                output=json.dumps(receipt),
                error="database verification failed" if not verified else None,
                meta={"external_effect": receipt},
            )

        expected_after = str(payload.get("after_hash") or "")
        _ref, current_hash = await self._snapshot(
            path,
            request.task_id,
            persist=False,
        )
        if expected_after and current_hash != expected_after:
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error="database changed after apply; compensation was not attempted",
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="database changed after apply; recovery required",
                meta={"external_effect": receipt},
            )
        try:
            before_exists = bool(payload.get("before_exists"))
            before_ref = payload.get("before_ref")
            if before_exists:
                if not before_ref or self._artifacts is None:
                    raise ValueError("database pre-image is unavailable for compensation")
                data = await self._artifacts.load(str(before_ref))
                await asyncio.sleep(0)
                self._restore_database_file(path, data)
            elif os.path.lexists(path):
                os.unlink(path)
            restored_hash = None
            if before_exists:
                _ref, restored_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
                restored = restored_hash == str(payload.get("before_hash") or "")
            else:
                restored = not os.path.lexists(path)
            receipt = await self._external_store.finish(
                transaction_id,
                status="COMPENSATION_VERIFIED" if restored else "RECOVERY_REQUIRED",
                response={
                    **payload,
                    "restored_hash": restored_hash,
                    "restored": restored,
                },
                phase=phase,
            )
            if not restored:
                return _result(
                    request,
                    ok=False,
                    output=json.dumps(receipt),
                    error="database compensation was not verified; recovery required",
                    meta={"external_effect": receipt},
                )
        except Exception as exc:  # noqa: BLE001 - restore outcome may be uncertain
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error=str(exc),
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="database compensation outcome is uncertain; recovery required",
                meta={"external_effect": receipt},
            )
        return _result(
            request,
            output=json.dumps(receipt),
            meta={"external_effect": receipt},
        )

    @staticmethod
    def _restore_database_file(path: str, data: bytes) -> None:
        """Restore one captured SQLite image without exposing arbitrary paths."""
        parent = os.path.dirname(path) or "."
        temporary = os.path.join(parent, f".athena-restore-{new_id('db')}")
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


class WorkspaceCapability:
    """Status / snapshot / diff / changed-files for the task workspace."""

    def __init__(
        self,
        checkpoint_manager=None,
        *,
        mutation_store=None,
        mutation_observer=None,
        project_index_store=None,
        project_index_coordinator=None,
    ) -> None:
        from athena.project import ProjectInspector
        from athena.project.index import ProjectIndexBuilder

        self._checkpoints = checkpoint_manager
        self._mutations = mutation_store
        self._mutation_observer = mutation_observer
        self._project_inspector = ProjectInspector()
        self._project_index_builder = ProjectIndexBuilder(
            inspector=self._project_inspector,
        )
        self._project_index_store = project_index_store
        self._project_index_coordinator = project_index_coordinator

    descriptor = CapabilityDescriptor(
        id="workspace",
        description=(
            "Workspace lifecycle: status summary, snapshot (via checkpoint "
            "manager), restore, changed-file listing (git when available, "
            "mtime scan otherwise), project profile, and bounded impact hints. "
            "Operations: status/changed_files/profile/index/impact/snapshot/restore."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "changed_files",
                        "profile",
                        "index",
                        "impact",
                        "snapshot",
                        "restore",
                    ],
                },
                "label": {"type": "string"},
                "checkpoint_id": {"type": "string"},
                "expected_fingerprint": {"type": "string", "minLength": 1},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4096},
                },
                "path": {"type": "string", "maxLength": 4096},
                "refresh": {"type": "boolean"},
            },
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    def _bind_context(self, context) -> str | None:
        return context.workspace.root if context else None

    async def invoke(self, request: CapabilityRequest, context=None, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        root = self._bind_context(context)
        loop = asyncio.get_running_loop()
        refresh_index = bool(args.get("refresh", False))
        if root is None:
            return _result(request, ok=False, error="no workspace bound to this call")

        if op == "status":

            def _st():
                files = sum(len(f) for _, _, f in os.walk(root))
                size = sum(
                    os.path.getsize(os.path.join(d, f))
                    for d, _, fs in os.walk(root)
                    for f in fs
                    if os.path.exists(os.path.join(d, f))
                )
                git = "yes" if os.path.isdir(os.path.join(root, ".git")) else "no"
                return f"root={root}\nfiles={files}\nbytes={size}\ngit={git}"

            return _result(request, output=await loop.run_in_executor(None, _st))

        if op == "changed_files":

            def _changed():
                rc, out, _ = _run(["git", "-C", root, "status", "--porcelain"])
                if rc == 0 and out.strip():
                    return out
                # fallback: newest-modified files
                entries = []
                for d, _, fs in os.walk(root):
                    if ".git" in d:
                        continue
                    for f in fs:
                        p = os.path.join(d, f)
                        try:
                            entries.append((os.path.getmtime(p), os.path.relpath(p, root)))
                        except OSError:
                            pass
                entries.sort(reverse=True)
                return "\n".join(f"{e[1]}" for e in entries[:25])

            return _result(request, output=await loop.run_in_executor(None, _changed))

        if op == "profile":
            index = await self._build_index(root, loop, refresh=refresh_index)
            profile_data = dict(index.profile)
            profile_data["environment"] = dict(index.environment)
            profile_data["index_revision"] = index.index_revision
            return _result(
                request,
                output=json.dumps(profile_data, sort_keys=True),
                meta={"profile": profile_data, "index_revision": index.index_revision},
            )

        if op == "index":
            index = await self._build_index(root, loop, refresh=refresh_index)
            record = index.to_record()
            return _result(
                request,
                output=json.dumps(record, sort_keys=True),
                meta={"index_revision": index.index_revision},
            )

        if op == "impact":
            raw_paths = args.get("paths")
            if raw_paths is None and args.get("path") is not None:
                raw_paths = [args["path"]]
            if not isinstance(raw_paths, list) or not raw_paths:
                return _result(
                    request,
                    ok=False,
                    error="impact requires a non-empty paths list",
                )
            root_real = os.path.realpath(os.path.abspath(root))
            for raw_path in raw_paths:
                candidate = os.path.realpath(
                    os.path.abspath(
                        str(raw_path)
                        if os.path.isabs(str(raw_path))
                        else os.path.join(root, str(raw_path))
                    )
                )
                if candidate != root_real and not candidate.startswith(root_real + os.sep):
                    return _result(
                        request,
                        ok=False,
                        error=f"impact path outside workspace: {raw_path}",
                    )
            try:
                index = await self._build_index(root, loop, refresh=refresh_index)
                impact = index.impact([str(path) for path in raw_paths])
            except ValueError as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(
                request,
                output=json.dumps(impact, sort_keys=True),
                meta={"impact": impact},
            )

        if op == "snapshot":
            mgr = self._checkpoints
            label = str(args.get("label") or "workspace-snapshot")
            manifest = await mgr.capture(
                task_id=request.task_id or "unknown", workspace_root=root, label=label
            )
            cid = manifest.get("checkpoint_id") or manifest.get("id")
            return _result(
                request,
                output=f"snapshot {cid} ({manifest.get('file_count')} files)",
                meta={"checkpoint_id": cid},
            )

        if op == "restore":
            cid = str(args.get("checkpoint_id") or "")
            if not cid or self._checkpoints is None:
                return _result(request, ok=False, error="checkpoint_id required")
            mutation_id = None
            before_checkpoint = None
            task_id = request.task_id or "unknown"
            if self._mutations is not None:
                # A restore is a real workspace mutation. Capture the
                # before-state and write the intent before touching the
                # target, so a partial restore is recoverable and auditable.
                before_checkpoint = await self._checkpoints.capture(
                    task_id=task_id,
                    workspace_root=root,
                    label=f"before-restore-{cid}",
                )
                expected = await self._checkpoints.fingerprint(root)
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    root,
                    "workspace.restore",
                    before_ref=f"checkpoint://{before_checkpoint['id']}",
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                    metadata={"checkpoint_id": cid, "expected_fingerprint": expected},
                )
                await self._mutations.mark_started(mutation_id)
            try:
                outcome = await self._checkpoints.restore(
                    checkpoint_id=cid,
                    workspace_root=root,
                    expected_fingerprint=(
                        str(args.get("expected_fingerprint") or expected)
                        if self._mutations is not None
                        else args.get("expected_fingerprint")
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if mutation_id is not None:
                    await self._mutations.mark_recovery_required(mutation_id)
                return _result(request, ok=False, error=str(exc))
            if mutation_id is not None:
                if before_checkpoint is None:
                    raise RuntimeError("restore mutation has no before checkpoint")
                await self._mutations.complete(
                    mutation_id,
                    after_hash=outcome.get("workspace_fingerprint"),
                    reversible=True,
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                )
            mutation = None
            if mutation_id is not None:
                mutation = {
                    "mutation_id": mutation_id,
                    "resource": root,
                    "operation": "workspace.restore",
                    "after_hash": outcome.get("workspace_fingerprint"),
                    "before_ref": (
                        f"checkpoint://{before_checkpoint['id']}"
                        if before_checkpoint is not None
                        else None
                    ),
                    "reversible": True,
                    "inverse": {"checkpoint_id": before_checkpoint["id"]}
                    if before_checkpoint is not None
                    else None,
                }
            return _result(
                request,
                output=json.dumps(outcome)[:1000],
                meta={"mutation": mutation} if mutation is not None else None,
            )

        return _result(request, ok=False, error=f"unknown operation: {op}")

    async def _build_index(
        self,
        root: str,
        loop,
        *,
        refresh: bool = False,
    ) -> Any:
        """Return the central index, rebuilding only when requested/stale."""
        if self._project_index_coordinator is not None:
            return await self._project_index_coordinator.current(root, refresh=refresh)
        index = await loop.run_in_executor(None, self._project_index_builder.build, root)
        if self._project_index_store is not None:
            await self._project_index_store.save(index)
        return index
