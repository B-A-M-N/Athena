"""Shared plumbing for the environment capability families (P1-10).

Single source for the helpers the service/network/database/workspace
capability modules all use (bounded subprocess runs, pinned external HTTP,
effect/digest/receipt helpers). Module-level patch seams for tests live
here — the family modules resolve these helpers through this module at
call time, so patching ``environment_common.<name>`` propagates.
"""

from __future__ import annotations


import hashlib
import ipaddress
import inspect
import json
import os
import re
import socket
import subprocess
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from athena.network import (  # noqa: F401 (patch seams for family modules)
    pinned_async_transport,
    pinned_sync_transport,
    resolve_addresses,
)
from athena.execution.async_call import run_blocking
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ExternalEffectPhase,
    ExternalEffectReceipt,
)


def _facade():
    # Patch seams: tests patch ``athena.capabilities.environment.<name>``.
    # Family modules and this module resolve those helpers through the facade
    # module lazily to avoid the facade <-> common import cycle.
    from athena.capabilities import environment

    return environment


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
                pinned_addresses = _facade().resolve_addresses(candidate, 0)
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
        client_args["transport"] = _facade().pinned_async_transport(hostname, pinned_addresses)
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
    result = _facade()._external_http_request(**kwargs)
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
    """Run one bounded systemd probe on an owned daemon thread.

    The service transaction is deliberately one synchronous probe/action per
    capability call. The owned bridge keeps a stuck systemd operation from
    sharing a process-lifetime executor with a later approval/replay path.
    """
    return await run_blocking(function, *args, **kwargs)


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
    rc, out, err = _facade()._run(
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
