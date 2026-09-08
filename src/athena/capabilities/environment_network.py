"""Network capability family: diagnostics as primitives (P1-10).

http, tcp_connect, dns, listeners, connections, ping. Read-only effects."""

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
from typing import Any
from urllib.parse import urlparse
import json
import socket

from athena.capabilities.environment_common import _SAFE_HTTP_METHODS
from athena.capabilities.environment_common import _legacy_idempotency_key
from athena.capabilities.environment_common import _legacy_transaction_result
from athena.capabilities.environment_common import _result
from athena.capabilities.environment_common import _service_offload
from athena.capabilities.environment_common import _external_compensation_digest
from athena.capabilities.environment_common import _external_receipt_result
from athena.capabilities.environment_common import _external_request_digest
from athena.capabilities.environment_common import _safe_external_response
from athena.capabilities.environment_common import _network_effects
from athena.execution.async_call import run_blocking
from athena.network.target_policy import validate_target


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
            validated, error = validate_target(
                host,
                policy_name,
                resolver=_facade.resolve_addresses,
            )
            return error, validated.addresses if validated is not None else ()

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
                    client_args["transport"] = _facade.pinned_sync_transport(
                        hostname, pinned_addresses
                    )
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

            open_ = await run_blocking(_tcp)
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
                text = await run_blocking(_dns)
            except socket.gaierror as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=text)

        if op == "listeners":

            def _ls():
                _rc, out, err = _facade._run(["ss", "-tlnp"])
                return out or err

            return _result(request, output=(await run_blocking(_ls))[:5000])

        if op == "connections":

            def _cx():
                _rc, out, err = _facade._run(["ss", "-tnp"])
                return out or err

            return _result(request, output=(await run_blocking(_cx))[:5000])

        if op == "ping":
            host = str(args.get("host") or "")
            if not host:
                return _result(request, ok=False, error="host required")
            restricted_error, pinned_addresses = _restricted_addresses(host)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _pg():
                target = pinned_addresses[0] if pinned_addresses else host
                rc, out, err = _facade._run(["ping", "-c", "3", "-W", "2", target])
                return rc, out or err

            rc, out = await run_blocking(_pg)
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
                response = await _facade._run_external_http_request(
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
            response = await _facade._run_external_http_request(
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
