"""Behavioral probes for execution-boundary conformance.

This module owns only the controlled filesystem/network containment proof used
by the backend passport.  It does not select backends or persist certification
state; :mod:`athena.execution.conformance` remains the passport authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Any, Mapping

from athena.protocol.execution import ExecutionRequest
from athena.protocol.tasks import NetworkPolicy


@dataclass(frozen=True)
class ContainmentProof:
    checks: tuple[str, ...] = ()
    proven: Mapping[str, bool] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    contract_checks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


def containment_source(
    runtime: str,
    outside_path: str,
    *,
    network_host: str | None = None,
    network_port: int | None = None,
) -> str:
    """Return a hostile probe using only the supplied controlled endpoint."""
    if network_host is None or network_port is None:
        python_network = "net='NOT_TESTED'"
        shell_network = "net=NOT_TESTED"
    else:
        python_network = (
            "net='DENIED'\n"
            "try:\n"
            f"    socket.create_connection(({network_host!r}, {network_port}), timeout=0.5).close()\n"
            "    net='ALLOWED'\n"
            "except OSError:\n"
            "    pass"
        )
        shell_network = (
            f"if timeout 1 bash -c '</dev/tcp/{network_host}/{network_port}' 2>/dev/null; "
            "then net=ALLOWED; else net=DENIED; fi"
        )
    if runtime == "python":
        return (
            "from pathlib import Path\n"
            "import socket\n"
            f"p=Path({outside_path!r})\n"
            "fs='ALLOWED' if p.exists() and p.read_text() == 'outside' else 'DENIED'\n"
            f"{python_network}\n"
            "print('FS=' + fs + ' NET=' + net)"
        )
    if runtime == "shell":
        return (
            f'if [ -f {outside_path!r} ] && [ "$(cat {outside_path!r})" = outside ]; '
            "then fs=ALLOWED; else fs=DENIED; fi; "
            f"{shell_network}; "
            'printf \'FS=%%s NET=%%s\' "$fs" "$net"'
        )
    raise ValueError(f"containment probe unavailable for runtime {runtime!r}")


async def _start_network_control() -> tuple[asyncio.AbstractServer, str, int]:
    async def accept_and_close(_reader, writer) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept_and_close, "127.0.0.1", 0)
    sockets = server.sockets or ()
    if not sockets:
        server.close()
        await server.wait_closed()
        raise RuntimeError("network containment control did not bind a socket")
    host, port = sockets[0].getsockname()[:2]
    return server, str(host), int(port)


async def _prove_network_positive_control(
    manager: Any,
    *,
    backend: str,
    runtime: str,
    task_id: str,
    workspace_id: str,
    workspace_root: str,
    cwd: str | None,
    source: str,
) -> tuple[bool, str]:
    """Prove the test endpoint is reachable before testing denial."""
    session_id: str | None = None
    try:
        session_id = await manager.create_session(
            task_id=task_id,
            runtime=runtime,
            backend=backend,
            cwd=cwd,
            workspace_root=workspace_root,
            network_policy=NetworkPolicy.ALLOW,
        )
        result = await manager.execute(
            ExecutionRequest(
                runtime=runtime,
                source=source,
                task_id=task_id,
                workspace_id=workspace_id,
                backend=backend,
                runtime_session_id=session_id,
                cwd=cwd,
                workspace_root=workspace_root,
                network_policy=NetworkPolicy.ALLOW,
            )
        )
        if result.exit_code == 0 and "NET=ALLOWED" in result.stdout:
            return True, "controlled endpoint was reachable under allow policy"
        return False, f"positive network control failed: {result.stdout[-500:]}"
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, f"positive network control failed: {exc}"
    finally:
        if session_id is not None:
            try:
                await manager.destroy_session(session_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass


async def prove_containment(
    manager: Any,
    *,
    backend: str,
    runtime: str,
    workspace_id: str,
    workspace_root: str,
    cwd: str | None,
    advertised: Mapping[str, bool],
    require_all_claims: bool,
) -> ContainmentProof:
    claims = {"filesystem_containment": "FS=DENIED", "network_containment": "NET=DENIED"}
    proven = {name: False for name in claims}
    contract_checks: dict[str, Mapping[str, str]] = {
        name: {"status": "unverified" if advertised[name] else "not_advertised"}
        for name in claims
    }
    checks: list[str] = []
    failures: list[str] = []
    if not any(advertised.values()):
        return ContainmentProof(proven=proven, contract_checks=contract_checks)

    with tempfile.TemporaryDirectory(prefix="athena-conformance-") as root:
        outside = Path(root).parent / f"{Path(root).name}-outside"
        outside.write_text("outside", encoding="utf-8")
        containment_session: str | None = None
        control_server: asyncio.AbstractServer | None = None
        control_host: str | None = None
        control_port: int | None = None
        try:
            if advertised["network_containment"]:
                control_server, control_host, control_port = await _start_network_control()
                positive_source = containment_source(
                    runtime,
                    str(outside),
                    network_host=control_host,
                    network_port=control_port,
                )
                positive_ok, positive_detail = await _prove_network_positive_control(
                    manager,
                    backend=backend,
                    runtime=runtime,
                    task_id=f"conformance-network-positive-{runtime}",
                    workspace_id=workspace_id,
                    workspace_root=root,
                    cwd=cwd,
                    source=positive_source,
                )
                if not positive_ok:
                    contract_checks["network_containment"] = {
                        "status": "failed",
                        "detail": positive_detail,
                    }
                    if require_all_claims:
                        failures.append(
                            "network containment positive control failed: " + positive_detail
                        )
            containment_session = await manager.create_session(
                task_id=f"conformance-boundary-{runtime}",
                runtime=runtime,
                backend=backend,
                workspace_root=root,
                network_policy=NetworkPolicy.DENY,
            )
            containment_result = await manager.execute(
                ExecutionRequest(
                    runtime=runtime,
                    source=containment_source(
                        runtime,
                        str(outside),
                        network_host=control_host,
                        network_port=control_port,
                    ),
                    task_id=f"conformance-boundary-{runtime}",
                    workspace_id=workspace_id,
                    backend=backend,
                    runtime_session_id=containment_session,
                    workspace_root=root,
                    network_policy=NetworkPolicy.DENY,
                )
            )
            for name, marker in claims.items():
                if not advertised[name]:
                    continue
                if name == "network_containment" and contract_checks[name].get("status") == "failed":
                    continue
                ok = containment_result.exit_code == 0 and marker in containment_result.stdout
                contract_checks[name] = {
                    "status": "passed" if ok else "failed",
                    "detail": containment_result.stdout[-500:],
                }
                if ok:
                    checks.append(name)
                    proven[name] = True
                elif require_all_claims:
                    failures.append(f"{name} proof failed")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            for name in claims:
                if advertised[name]:
                    contract_checks[name] = {"status": "failed", "detail": str(exc)}
            if require_all_claims:
                failures.append(f"containment proof failed: {exc}")
        finally:
            if containment_session is not None:
                await manager.destroy_session(containment_session)
            if control_server is not None:
                control_server.close()
                await control_server.wait_closed()
        outside.unlink(missing_ok=True)
    return ContainmentProof(
        checks=tuple(checks),
        proven=proven,
        failures=tuple(failures),
        contract_checks=contract_checks,
    )


__all__ = ["ContainmentProof", "containment_source", "prove_containment"]
