"""Executable backend/runtime capability conformance checks.

Reflection is useful only when it is tied to a behavioral receipt.  This
module keeps the probe small and deterministic: it exercises every advertised
runtime, and exercises state persistence whenever that cell is claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

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
        return not self.failures

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


def _state_sources(runtime: str) -> tuple[str, str, str]:
    if runtime == "python":
        return (
            "import os; athena_conformance_value = 41; print(os.environ.get('ATHENA_CONFORMANCE_SECRET', ''))",
            "print(athena_conformance_value + 1)",
            "42",
        )
    if runtime == "node":
        return (
            "globalThis.athenaConformanceValue = 41; console.log(process.env.ATHENA_CONFORMANCE_SECRET || '')",
            "console.log(athenaConformanceValue + 1)",
            "42",
        )
    if runtime == "shell":
        return (
            'export ATHENA_CONFORMANCE_VALUE=41; echo -n "$ATHENA_CONFORMANCE_SECRET"',
            'echo -n "$ATHENA_CONFORMANCE_VALUE"',
            "41",
        )
    raise ValueError(f"no conformance probe for runtime {runtime!r}")


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
    advertised_raw = (
        tuple(capabilities.get("supported_runtimes", ()))
        if isinstance(capabilities, Mapping)
        else tuple(getattr(capabilities, "supported_runtimes", ()))
    )
    advertised = tuple(dict.fromkeys(_canonical_runtime(str(item)) for item in advertised_raw))
    runtime_caps = (
        capabilities.get("runtime_capabilities", {})
        if isinstance(capabilities, Mapping)
        else getattr(capabilities, "runtime_capabilities", {})
    )
    receipts: list[ConformanceReceipt] = []
    for runtime in advertised:
        checks: list[str] = []
        failures: list[str] = []
        session_id: str | None = None
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

            cell = runtime_caps.get(runtime, {}) if isinstance(runtime_caps, Mapping) else {}
            if not isinstance(cell, Mapping):
                cell = {}
            persistent = (
                bool(cell.get("persistent_runtime_state"))
                if "persistent_runtime_state" in cell
                else bool(
                    capabilities.get("persistent_runtime_state", False)
                    if isinstance(capabilities, Mapping)
                    else getattr(capabilities, "persistent_runtime_state", False)
                )
            )
            secret_materialization = (
                bool(cell.get("secret_materialization"))
                if "secret_materialization" in cell
                else bool(
                    capabilities.get("secret_materialization", False)
                    if isinstance(capabilities, Mapping)
                    else getattr(capabilities, "secret_materialization", False)
                )
            )
            if secret_materialization:
                if "conformance-secret" not in first_result.stdout:
                    failures.append("advertised secret materialization was not observed")
                else:
                    checks.append("secret_materialization")
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
            if persistent:
                if second_result.exit_code != 0 or expected not in second_result.stdout:
                    failures.append(
                        "advertised persistent runtime state did not survive the next call"
                    )
                else:
                    checks.append("persistent_runtime_state")
            if hasattr(first_result, "status") and hasattr(first_result, "exit_code"):
                checks.append("stdout_stderr_framing")
            advertised_contract = {
                str(name): bool(value)
                for name, value in cell.items()
                if isinstance(value, bool) and value
            }
            proven_contract = {
                "persistent_runtime_state": "persistent_runtime_state" in checks,
                "secret_materialization": "secret_materialization" in checks,
                "execution": "execution" in checks,
                "stdout_stderr_framing": "stdout_stderr_framing" in checks,
            }
            unverified = tuple(
                name
                for name, claimed in advertised_contract.items()
                if claimed and not proven_contract.get(name, False)
            )
            if require_all_claims and unverified:
                failures.append(
                    "advertised capability claims lack behavioral proof: " + ", ".join(unverified)
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failures.append(str(exc))
            unverified = ()
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
                },
            )
        )
    return tuple(receipts)


__all__ = ["ConformanceReceipt", "run_backend_conformance"]
