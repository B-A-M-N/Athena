"""Shared root-level fixtures for tests that need a durable (file-backed) DB.

A file-backed :class:`AthenaService` is required wherever state must survive a
service restart/stop (session resume, crash recovery, scheduler recovery),
because :meth:`AthenaService.in_memory` hard-codes ``db_path=":memory:"``.

Athena owns the ``athena_claim``/``athena_evidence`` marker declarations in
its tests; DSH injects the private reporter when it collects proof.

The ``athena_scenario`` marker is metadata-only: it names the 0.1 stable
scenario family (see ``tests/scenarios/registry.py``) a test provides
evidence for.  It never selects or skips tests — ``scripts/scenarios`` binds
scenarios to concrete node IDs and runs them by ID; the marker exists so a
reader of a test file can trace it back to its release-gate scenario.
"""

from __future__ import annotations

import os
import socket
import tempfile

import pytest

from athena.service.service import AthenaService
from athena.service.config import AthenaConfig, ProviderConfig

# Marker registered here (not pyproject.toml) so it stays a tests-tree-only
# concern; registering also silences the strict-marker warning.


RUNNER_CAPABILITIES = frozenset(
    {
        "PURE",
        "FS",
        "PROCESS",
        "LOCAL_SOCKET",
        "TCP_LOOPBACK",
        "NETWORK_EGRESS",
        "DISPLAY",
    }
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "athena_scenario(*scenario_ids): 0.1 stable scenario family evidence "
        "(see tests/scenarios/registry.py); metadata only, never selects tests",
    )
    config.addinivalue_line(
        "markers",
        "athena_capability(*capabilities): host capabilities required by this "
        "test; see docs/scenario-capability-routing.md",
    )


def pytest_collection_modifyitems(config, items):
    """Make a denied host capability explicit instead of a product failure.

    A test declaring ``athena_capability`` is probe-checked before setup.  A
    missing capability emits ``ENVIRONMENT_UNAVAILABLE: <capabilities>`` and
    is skipped; it is never reported as Athena evidence that failed.
    """
    for item in items:
        marker = item.get_closest_marker("athena_capability")
        if marker is None:
            continue
        requested = tuple(str(value) for value in marker.args)
        unknown = set(requested) - RUNNER_CAPABILITIES
        if unknown:
            raise pytest.UsageError(
                f"{item.nodeid}: unknown athena_capability {sorted(unknown)}; "
                f"expected {sorted(RUNNER_CAPABILITIES)}"
            )
        unavailable: list[str] = []
        for capability in requested:
            if capability == "DISPLAY":
                available = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            elif capability == "TCP_LOOPBACK":
                try:
                    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        probe.bind(("127.0.0.1", 0))
                        available = True
                    finally:
                        probe.close()
                except OSError:
                    available = False
            elif capability == "LOCAL_SOCKET":
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.close()
                    available = True
                except OSError:
                    available = False
            elif capability == "NETWORK_EGRESS":
                try:
                    available = socket.getaddrinfo("pypi.org", 443, type=socket.SOCK_STREAM) != []
                except OSError:
                    available = False
            else:
                # PURE, FS, and PROCESS are provided by the ordinary pytest
                # runner/process model and have no extra host precondition.
                available = True
            if not available:
                unavailable.append(capability)
        if unavailable:
            item.add_marker(pytest.mark.skip("ENVIRONMENT_UNAVAILABLE: " + ", ".join(unavailable)))


@pytest.fixture
def durable_db_path():
    """Yield a throwaway .db file path (not :memory:) with cleanup."""
    tmp = tempfile.mkdtemp(prefix="athena-db-")
    path = os.path.join(tmp, "athena.db")
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.unlink(path + "-wal")
    except OSError:
        pass
    try:
        os.unlink(path + "-shm")
    except OSError:
        pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass


@pytest.fixture
async def make_durable_service():
    """Async factory building a started service against a given db path.

    ``make(db_path, scripts=None, **cfg) -> AthenaService``. The service is
    registered for teardown so ``stop()`` is always called.
    """
    started: list[AthenaService] = []

    async def make(db_path, scripts=None, **cfg):
        workspace = cfg.pop("workspace_root", None) or tempfile.mkdtemp(prefix="athena-ws-")
        config = AthenaConfig(
            db_path=db_path,
            workspace_root=workspace,
            artifact_root=os.path.join(workspace, "artifacts"),
            providers=(
                ProviderConfig(
                    kind="fake",
                    name="fake",
                    extra={"scripts": list(scripts or ())},
                ),
            ),
            worker_max_parallel=cfg.get("worker_max_parallel", 4),
            worker_lease_duration_seconds=cfg.get("worker_lease_duration_seconds", 300.0),
            worker_lease_renewal_divisor=cfg.get("worker_lease_renewal_divisor", 3.0),
            scheduler_interval_seconds=cfg.get("scheduler_interval_seconds", 1.0),
            parked_slot_wait_s=cfg.get("parked_slot_wait_s", 300.0),
            cache_namespace=cfg.get("cache_namespace", "athena"),
        )
        svc = AthenaService(config=config)
        await svc.start()
        started.append(svc)
        return svc

    yield make

    for svc in started:
        try:
            if svc._started or svc._db is not None:
                await svc.stop()
        except Exception:
            pass
