"""Executable backend/runtime capability conformance checks.

Reflection is useful only when it is tied to a behavioral receipt.  This
module keeps the probe small and deterministic: it exercises every advertised
runtime, and exercises state persistence whenever that cell is claimed.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from athena.execution.conformance_probes import prove_containment
from athena.protocol.execution import ExecutionRequest
from athena.protocol.tasks import NetworkPolicy


@dataclass(frozen=True)
class ConformanceReceipt:
    backend: str
    runtime: str
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # A receipt with no executed checks is not evidence.  In particular,
        # an advertised runtime cannot earn PASS from a vacuous probe.
        return bool(self.checks) and "execution" in self.checks and not self.failures

    @property
    def unverified_claims(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.metadata.get("unverified_claims", ()))

    def to_record(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "runtime": self.runtime,
            "checks": list(self.checks),
            "failures": list(self.failures),
            "unverified_claims": list(self.unverified_claims),
            "metadata": dict(self.metadata),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class BackendPassport:
    """Release-bound behavioral proof for one advertised backend.

    Reflection may describe a capability, but only receipts from the
    conformance runner can promote that claim to ``verified``. Unverified
    claims remain visible in the passport and keep its status non-PASS.
    """

    backend: str
    release_sha: str
    environment: Mapping[str, Any] = field(default_factory=dict)
    receipts: tuple[ConformanceReceipt, ...] = ()
    # ``None`` means the caller did not supply an advertised runtime contract.
    # An explicit empty tuple means the backend advertised no runnable cells
    # and must therefore fail certification.
    expected_runtimes: tuple[str, ...] | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    release_run_id: str | None = None
    schema_version: int = 1

    @property
    def unverified_claims(self) -> tuple[str, ...]:
        return tuple(
            f"{receipt.runtime}:{claim}"
            for receipt in self.receipts
            for claim in receipt.unverified_claims
        )

    @property
    def status(self) -> str:
        observed = tuple(receipt.runtime for receipt in self.receipts)
        observed_unique = len(observed) == len(set(observed))
        if self.expected_runtimes is None:
            complete = bool(observed) and observed_unique
        else:
            expected = tuple(self.expected_runtimes)
            complete = (
                bool(expected)
                and len(expected) == len(set(expected))
                and observed_unique
                and set(observed) == set(expected)
                and len(observed) == len(expected)
            )
        return (
            "PASS"
            if complete
            and all(receipt.passed for receipt in self.receipts)
            and not self.unverified_claims
            else "FAIL"
        )

    @classmethod
    def from_receipts(
        cls,
        receipts: tuple[ConformanceReceipt, ...] | list[ConformanceReceipt],
        *,
        release_sha: str,
        environment: Mapping[str, Any] | None = None,
        release_run_id: str | None = None,
        expected_runtimes: tuple[str, ...] | list[str] | None = None,
    ) -> "BackendPassport":
        values = tuple(receipts)
        backend = values[0].backend if values else "unknown"
        return cls(
            backend=backend,
            release_sha=str(release_sha),
            environment=dict(environment or {}),
            receipts=values,
            expected_runtimes=(
                tuple(str(runtime) for runtime in expected_runtimes)
                if expected_runtimes is not None
                else None
            ),
            release_run_id=release_run_id,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": "athena_backend_passport",
            "schema_version": self.schema_version,
            "backend": self.backend,
            "release_sha": self.release_sha,
            "release_run_id": self.release_run_id,
            "generated_at": self.generated_at,
            "environment": dict(self.environment),
            "status": self.status,
            "expected_runtimes": (
                list(self.expected_runtimes) if self.expected_runtimes is not None else None
            ),
            "unverified_claims": list(self.unverified_claims),
            "cells": [receipt.to_record() for receipt in self.receipts],
        }


def _state_sources(runtime: str) -> tuple[str, str, str]:
    if runtime == "python":
        return (
            "import os; athena_conformance_value = 41; print('SECRET=' + os.environ.get('ATHENA_CONFORMANCE_SECRET', '')); print('HOST=' + os.environ.get('ATHENA_CONFORMANCE_HOST_ONLY', ''))",
            "print(athena_conformance_value + 1)",
            "42",
        )
    if runtime == "node":
        return (
            "globalThis.athenaConformanceValue = 41; console.log('SECRET=' + (process.env.ATHENA_CONFORMANCE_SECRET || '')); console.log('HOST=' + (process.env.ATHENA_CONFORMANCE_HOST_ONLY || ''))",
            "console.log(athenaConformanceValue + 1)",
            "42",
        )
    if runtime == "shell":
        return (
            'export ATHENA_CONFORMANCE_VALUE=41; printf "SECRET=%%s\\nHOST=%%s\\n" "$ATHENA_CONFORMANCE_SECRET" "$ATHENA_CONFORMANCE_HOST_ONLY"',
            'echo -n "$ATHENA_CONFORMANCE_VALUE"',
            "41",
        )
    raise ValueError(f"no conformance probe for runtime {runtime!r}")


def _cancellation_source(runtime: str) -> str:
    if runtime == "python":
        return "import time; time.sleep(60)"
    if runtime == "node":
        return "setTimeout(() => console.log('late'), 60000)"
    if runtime == "shell":
        return "sleep 60"
    raise ValueError(f"cancellation probe unavailable for runtime {runtime!r}")


def _capability_value(
    capabilities: Any,
    runtime_cell: Mapping[str, Any],
    name: str,
) -> bool:
    if name in runtime_cell:
        return bool(runtime_cell[name])
    if isinstance(capabilities, Mapping):
        return bool(capabilities.get(name, False))
    return bool(getattr(capabilities, name, False))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _prove_reattach(
    manager: Any,
    *,
    backend_name: str,
    runtime: str,
    task_id: str,
    session_id: str,
) -> tuple[bool, str]:
    """Prove backend identity/reattach using the real manager adoption path."""
    backend_lookup = getattr(manager, "backend", None)
    reattach_manager = getattr(manager, "reattach_session", None)
    if not callable(backend_lookup) or not callable(reattach_manager):
        return False, "manager does not expose backend reattachment"
    backend = backend_lookup(backend_name)
    if backend is None:
        return False, f"backend {backend_name!r} is not selected"
    describe = getattr(backend, "describe_session", None)
    if not callable(describe):
        return False, "backend does not expose durable session identity"
    metadata = dict(await _maybe_await(describe(session_id)))
    record = {
        "id": session_id,
        "task_id": task_id,
        "backend": backend_name,
        "runtime": runtime,
        "metadata": metadata,
        **metadata,
    }
    if not await reattach_manager(record):
        return False, "manager declined reattachment"
    return True, "durable identity matched and manager adopted session"


async def _prove_cancellation(
    manager: Any,
    *,
    backend: str,
    runtime: str,
    workspace_id: str,
    workspace_root: str | None,
) -> tuple[bool, str]:
    cancel = getattr(manager, "cancel_task", None)
    if not callable(cancel):
        return False, "manager does not expose task cancellation"
    task_id = f"conformance-cancel-{runtime}"
    session_id = await manager.create_session(
        task_id=task_id,
        runtime=runtime,
        backend=backend,
        workspace_root=workspace_root,
        network_policy=NetworkPolicy.DENY,
    )
    execution = asyncio.create_task(
        manager.execute(
            ExecutionRequest(
                runtime=runtime,
                source=_cancellation_source(runtime),
                task_id=task_id,
                workspace_id=workspace_id,
                backend=backend,
                runtime_session_id=session_id,
                workspace_root=workspace_root,
                network_policy=NetworkPolicy.DENY,
            )
        )
    )
    try:
        await asyncio.sleep(0.15)
        result = await cancel(task_id)
        if not getattr(result, "confirmed", False):
            return False, f"cancellation was not confirmed: {result!r}"
        try:
            await asyncio.wait_for(execution, timeout=5)
        except asyncio.TimeoutError:
            return False, "execution remained live after task cancellation"
        task_live = getattr(manager, "has_live_runtime", lambda _task_id: False)(task_id)
        if task_live:
            return False, (
                "live execution resources remained after cancellation"
                f" (task={task_id}, sessions={getattr(manager, '_task_sessions', {}).get(task_id)!r})"
            )
        return True, "task cancellation confirmed process-tree cleanup"
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


def _canonical_runtime(runtime: str) -> str:
    aliases = {
        "py": "python",
        "python3": "python",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
        "js": "node",
        "nodejs": "node",
        "javascript": "node",
    }
    return aliases.get(runtime.casefold(), runtime.casefold())


def _advertised_runtimes(capabilities: Any) -> tuple[str, ...]:
    raw = (
        capabilities.get("supported_runtimes", ())
        if isinstance(capabilities, Mapping)
        else getattr(capabilities, "supported_runtimes", ())
    )
    return tuple(dict.fromkeys(_canonical_runtime(str(item)) for item in raw))


async def run_backend_conformance(
    manager: Any,
    *,
    backend: str,
    task_id: str = "conformance",
    workspace_id: str = "conformance",
    cwd: str | None = None,
    workspace_root: str | None = None,
    require_all_claims: bool = False,
) -> tuple[ConformanceReceipt, ...]:
    """Run behavioral probes for every runtime advertised by ``backend``.

    The function fails closed when a backend advertises a runtime that cannot
    be exercised.  It is intentionally usable by tests, ``doctor``, and
    release qualification without creating a second execution path.
    """
    capabilities = manager.backend_capabilities(backend)
    advertised = _advertised_runtimes(capabilities)
    runtime_caps = (
        capabilities.get("runtime_capabilities", {})
        if isinstance(capabilities, Mapping)
        else getattr(capabilities, "runtime_capabilities", {})
    )
    receipts: list[ConformanceReceipt] = []
    host_marker = "athena-conformance-host-only"
    prior_host_marker = os.environ.get("ATHENA_CONFORMANCE_HOST_ONLY")
    os.environ["ATHENA_CONFORMANCE_HOST_ONLY"] = host_marker
    claims = (
        "persistent_runtime_state",
        "reattach",
        "process_signals",
        "secret_materialization",
        "filesystem_containment",
        "network_containment",
    )
    try:
        for runtime in advertised:
            checks: list[str] = []
            failures: list[str] = []
            contract_checks: dict[str, dict[str, Any]] = {}
            session_id: str | None = None
            unverified: tuple[str, ...] = ()
            cell = runtime_caps.get(runtime, {}) if isinstance(runtime_caps, Mapping) else {}
            if not isinstance(cell, Mapping):
                cell = {}
            advertised_contract = {
                name: _capability_value(capabilities, cell, name) for name in claims
            }
            proven_contract = {name: False for name in claims}
            for name in claims:
                contract_checks[name] = (
                    {"status": "unverified"}
                    if advertised_contract[name]
                    else {"status": "not_advertised"}
                )
            try:
                first, second, expected = _state_sources(str(runtime))
                session_id = await manager.create_session(
                    task_id=task_id,
                    runtime=str(runtime),
                    backend=backend,
                    cwd=cwd,
                    env={"ATHENA_CONFORMANCE_SECRET": "conformance-secret"},
                    workspace_root=workspace_root,
                    network_policy=NetworkPolicy.DENY,
                )
                first_result = await manager.execute(
                    ExecutionRequest(
                        runtime=str(runtime),
                        source=first,
                        task_id=task_id,
                        workspace_id=workspace_id,
                        backend=backend,
                        runtime_session_id=session_id,
                        cwd=cwd,
                        env={"ATHENA_CONFORMANCE_SECRET": "conformance-secret"},
                        workspace_root=workspace_root,
                        network_policy=NetworkPolicy.DENY,
                    )
                )
                if first_result.exit_code != 0:
                    failures.append(f"initial execution failed: {first_result.stderr[-500:]}")
                else:
                    checks.append("execution")
                if advertised_contract["secret_materialization"]:
                    if "conformance-secret" not in first_result.stdout:
                        failures.append("advertised secret materialization was not observed")
                    else:
                        checks.append("secret_materialization")
                        proven_contract["secret_materialization"] = True
                        contract_checks["secret_materialization"] = {
                            "status": "passed",
                            "detail": "explicit secret reached the runtime",
                        }
                if host_marker in first_result.stdout:
                    failures.append("host-only environment marker escaped into runtime")
                else:
                    checks.append("environment_handling")
                second_result = await manager.execute(
                    ExecutionRequest(
                        runtime=str(runtime),
                        source=second,
                        task_id=task_id,
                        workspace_id=workspace_id,
                        backend=backend,
                        runtime_session_id=session_id,
                        cwd=cwd,
                        workspace_root=workspace_root,
                        network_policy=NetworkPolicy.DENY,
                    )
                )
                if advertised_contract["persistent_runtime_state"]:
                    if second_result.exit_code != 0 or expected not in second_result.stdout:
                        failures.append(
                            "advertised persistent runtime state did not survive the next call"
                        )
                    else:
                        checks.append("persistent_runtime_state")
                        proven_contract["persistent_runtime_state"] = True
                        contract_checks["persistent_runtime_state"] = {
                            "status": "passed",
                            "detail": "state survived the subsequent execution",
                        }
                if hasattr(first_result, "status") and hasattr(first_result, "exit_code"):
                    checks.append("stdout_stderr_framing")

                if advertised_contract["reattach"]:
                    try:
                        ok, detail = await _prove_reattach(
                            manager,
                            backend_name=backend,
                            runtime=str(runtime),
                            task_id=task_id,
                            session_id=session_id,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        ok, detail = False, str(exc)
                    contract_checks["reattach"] = {
                        "status": "passed" if ok else "failed",
                        "detail": detail,
                    }
                    if ok:
                        checks.append("reattach")
                        proven_contract["reattach"] = True
                    elif require_all_claims:
                        failures.append(f"reattach proof failed: {detail}")

                if advertised_contract["process_signals"]:
                    try:
                        ok, detail = await _prove_cancellation(
                            manager,
                            backend=backend,
                            runtime=str(runtime),
                            workspace_id=workspace_id,
                            workspace_root=workspace_root,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        ok, detail = False, str(exc)
                    contract_checks["process_signals"] = {
                        "status": "passed" if ok else "failed",
                        "detail": detail,
                    }
                    if ok:
                        checks.append("process_tree_cancellation")
                        proven_contract["process_signals"] = True
                    elif require_all_claims:
                        failures.append(f"process-tree cancellation proof failed: {detail}")

                containment_claims = {
                    "filesystem_containment": advertised_contract["filesystem_containment"],
                    "network_containment": advertised_contract["network_containment"],
                }
                if any(containment_claims.values()):
                    proof = await prove_containment(
                        manager,
                        backend=backend,
                        runtime=str(runtime),
                        workspace_id=workspace_id,
                        cwd=cwd,
                        advertised=containment_claims,
                        require_all_claims=require_all_claims,
                    )
                    checks.extend(proof.checks)
                    failures.extend(proof.failures)
                    proven_contract.update(proof.proven)
                    contract_checks.update(proof.contract_checks)
                unverified = tuple(
                    name
                    for name, claimed in advertised_contract.items()
                    if claimed and not proven_contract[name]
                )
                if require_all_claims and unverified:
                    failures.append(
                        "advertised capability claims lack behavioral proof: "
                        + ", ".join(unverified)
                    )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                failures.append(str(exc))
                unverified = tuple(name for name, claimed in advertised_contract.items() if claimed)
            finally:
                if session_id is not None:
                    try:
                        await manager.destroy_session(session_id)
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        failures.append(f"session cleanup failed: {exc}")
            receipts.append(
                ConformanceReceipt(
                    backend=backend,
                    runtime=str(runtime),
                    checks=tuple(checks),
                    failures=tuple(failures),
                    metadata={
                        "advertised": True,
                        "unverified_claims": unverified,
                        "contract_checks": contract_checks,
                    },
                )
            )
    finally:
        if prior_host_marker is None:
            os.environ.pop("ATHENA_CONFORMANCE_HOST_ONLY", None)
        else:
            os.environ["ATHENA_CONFORMANCE_HOST_ONLY"] = prior_host_marker
    return tuple(receipts)


async def run_backend_passport(
    manager: Any,
    *,
    backend: str,
    release_sha: str,
    environment: Mapping[str, Any] | None = None,
    task_id: str = "conformance",
    workspace_id: str = "conformance",
    cwd: str | None = None,
    workspace_root: str | None = None,
    require_all_claims: bool = False,
    release_run_id: str | None = None,
) -> BackendPassport:
    receipts = await run_backend_conformance(
        manager,
        backend=backend,
        task_id=task_id,
        workspace_id=workspace_id,
        cwd=cwd,
        workspace_root=workspace_root,
        require_all_claims=require_all_claims,
    )
    expected_runtimes = _advertised_runtimes(manager.backend_capabilities(backend))
    return BackendPassport.from_receipts(
        receipts,
        release_sha=release_sha,
        environment=environment,
        release_run_id=release_run_id,
        expected_runtimes=expected_runtimes,
    )


__all__ = [
    "BackendPassport",
    "ConformanceReceipt",
    "run_backend_conformance",
    "run_backend_passport",
]
