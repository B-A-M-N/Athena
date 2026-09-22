"""Small helpers for the operator-facing backend readiness matrix."""

from __future__ import annotations

from typing import Any

from athena.service.operational_matrix_ports import OperationalMatrixPorts


def behavioral_proof(passport: Any, runtime: Any) -> dict[str, Any]:
    if not isinstance(passport, dict) or not isinstance(passport.get("cells"), list):
        return {"status": "not_run"}
    for cell in passport["cells"]:
        if isinstance(cell, dict) and cell.get("runtime") == runtime:
            return {
                "status": "passed" if cell.get("passed") else "failed",
                "release_sha": passport.get("release_sha"),
                "unverified_claims": list(cell.get("unverified_claims") or ()),
            }
    return {"status": "not_run"}

def build_operational_matrix(service: Any) -> dict[str, Any]:
    ports = OperationalMatrixPorts(service)
    execution = ports.execution
    backend_rows = execution.backend_status() if execution is not None else []
    cells: list[dict[str, Any]] = []
    for backend in backend_rows:
        if not isinstance(backend, dict) or not isinstance(backend.get("capabilities"), dict):
            continue
        capabilities = backend["capabilities"]
        passport = backend.get("passport")
        runtime_caps = capabilities.get("runtime_capabilities") or {}
        for runtime in capabilities.get("supported_runtimes") or ():
            cell = runtime_caps.get(runtime) if isinstance(runtime_caps, dict) else {}
            cell = cell if isinstance(cell, dict) else {}
            cells.append(
                {
                    "backend": backend.get("id"),
                    "runtime": runtime,
                    "available": bool(backend.get("available")),
                    "persistent_session": bool(
                        cell.get("persistent_sessions", capabilities.get("persistent_sessions"))
                    ),
                    "persistent_runtime_state": bool(
                        cell.get(
                            "persistent_runtime_state",
                            capabilities.get("persistent_runtime_state"),
                        )
                    ),
                    "reattach": bool(cell.get("reattach", capabilities.get("reattach"))),
                    "dependency_installation": list(
                        capabilities.get("dependency_installation") or ()
                    ),
                    "network_modes": list(capabilities.get("network_modes") or ()),
                    "filesystem_containment": bool(
                        cell.get(
                            "filesystem_containment", capabilities.get("filesystem_containment")
                        )
                    ),
                    "network_containment": bool(
                        cell.get("network_containment", capabilities.get("network_containment"))
                    ),
                    "behavioral_proof": behavioral_proof(passport, runtime),
                }
            )
    return {
        "execution": cells,
        "mcp": ports.mcp_status(),
        "memory": ports.runtime_health().get("memory_embeddings", {}),
    }
