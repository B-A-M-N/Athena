"""P1/P2 capability families: service, network, database, workspace.

Compatibility facade (P1-10): the families now live in focused modules and
the shared plumbing in :mod:`athena.capabilities.environment_common`:

- :mod:`environment_service`   — systemd user/system service control.
- :mod:`environment_network`   — diagnostics as primitives (http, dns, ...).
- :mod:`environment_database`  — SQL with schema introspection.
- :mod:`environment_workspace` — workspace status/snapshot/restore/diff.

Every public name (and every test patch seam, e.g. ``environment._run``)
remains importable from here. Patch seams resolve through
``environment_common`` at call time inside the family modules, so patching
``environment.<name>`` mutates the shared binding the families actually
call — module attribute re-export keeps one object identity.
"""

from __future__ import annotations

from athena.network import (  # noqa: F401 (patch seams)
    pinned_async_transport,
    pinned_sync_transport,
    resolve_addresses,
)
from athena.capabilities.environment_common import (  # noqa: F401 (facade re-exports)
    _MUTATIONS,
    _SAFE_HTTP_METHODS,
    _SERVICE_ROLLBACK,
    _UNIT_NAME,
    _database_effects,
    _database_request_digest,
    _external_compensation_digest,
    _external_http_request,
    _external_receipt_result,
    _external_request_digest,
    _has_symlink_component,
    _legacy_idempotency_key,
    _legacy_transaction_result,
    _network_effects,
    _result,
    _run,
    _run_external_http_request,
    run_external_http_request,
    _safe_external_response,
    _service_effects,
    _service_offload,
    _service_request_digest,
    _service_restore_plan,
    _service_state,
    _service_state_matches,
)
from athena.capabilities.environment_database import DatabaseCapability
from athena.capabilities.environment_network import NetworkCapability
from athena.capabilities.environment_service import ServiceCapability
from athena.capabilities.environment_workspace import WorkspaceCapability

__all__ = [
    "DatabaseCapability",
    "NetworkCapability",
    "ServiceCapability",
    "WorkspaceCapability",
]
