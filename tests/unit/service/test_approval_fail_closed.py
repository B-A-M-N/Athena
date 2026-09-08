"""Fault-injection coverage for the durable approval authority boundary."""

from __future__ import annotations

import pytest

from athena.protocol.tasks import TaskStatus
from athena.service.service import AthenaService


_METADATA = {
    "capability_id": "execute",
    "call_id": "call-1",
    "args_digest": "digest-1",
    "requested_scope": ["call", "task", "session"],
    "scope": "call",
}


class _Approvals:
    def __init__(self, *, status: str = "PENDING", error: BaseException | None = None) -> None:
        self.recorded: list[tuple[str, str]] = []
        self.status = status
        self.error = error

    async def get(self, approval_id: str) -> dict:
        return {
            "id": approval_id,
            "status": self.status,
            "task_id": "task-1",
            "metadata": dict(_METADATA),
        }

    async def record_grant(self, approval_id: str, **_kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.recorded.append((approval_id, "granted"))

    async def record_deny(self, approval_id: str, **_kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.recorded.append((approval_id, "denied"))


class _Continuations:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.resolved: list[tuple[str, str]] = []

    async def pending(self, _task_id: str | None) -> list[dict]:
        return [{"id": "continuation-1", "call_id": "call-1"}]

    async def mark_resolved(self, continuation_id: str, decision: str) -> None:
        if self.error is not None:
            raise self.error
        self.resolved.append((continuation_id, decision))


class _Tasks:
    async def get(self, task_id: str) -> dict:
        return {"id": task_id, "status": TaskStatus.WAITING_APPROVAL.value}


class _TaskManager:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, TaskStatus, str]] = []

    async def transition(self, task_id: str, status: TaskStatus, *, reason: str = "") -> None:
        self.transitions.append((task_id, status, reason))


class _Kernel:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self._runs = {"task-1": object()}
        self.error = error
        self.notifications: list[tuple[str, str]] = []

    async def notify_approval_resolved(self, task_id: str, decision: str) -> None:
        if self.error is not None:
            raise self.error
        self.notifications.append((task_id, decision))


class _RuntimeApprovals:
    def __init__(self, *, install_error: BaseException | None = None) -> None:
        self.install_error = install_error
        self.created = False
        self.granted = False

    def state(self, _approval_id: str):
        return None

    def create_request(self, *_args, **_kwargs) -> None:
        self.created = True

    def grant(self, _approval_id: str, *, resolver: str) -> None:
        assert resolver == "user"
        if self.install_error is not None:
            raise self.install_error
        self.granted = True


def _service(
    approvals: _Approvals,
    *,
    continuations: _Continuations | None = None,
    runtime_error: BaseException | None = None,
    kernel_error: BaseException | None = None,
) -> AthenaService:
    service = AthenaService.__new__(AthenaService)
    service._store_approvals = approvals
    service._store_continuations = continuations
    service._store_tasks = _Tasks()
    service._task_manager = _TaskManager()
    service._kernel = _Kernel(error=kernel_error)
    service._policy = type(
        "Policy",
        (),
        {"approvals": _RuntimeApprovals(install_error=runtime_error)},
    )()
    return service


@pytest.mark.asyncio
async def test_database_failure_leaves_approval_parked_and_does_not_wake() -> None:
    approvals = _Approvals(error=OSError("database unavailable"))
    service = _service(approvals)

    with pytest.raises(OSError, match="database unavailable"):
        await service.approve("apr-1", granted=True, scope="call")

    assert approvals.recorded == []
    assert service._policy.approvals.granted is False
    assert service._kernel.notifications == []
    assert service._task_manager.transitions == []


@pytest.mark.asyncio
async def test_continuation_failure_after_persistence_requires_recovery() -> None:
    approvals = _Approvals()
    continuations = _Continuations(error=RuntimeError("continuation store failed"))
    service = _service(approvals, continuations=continuations)

    with pytest.raises(RuntimeError, match="continuation store failed"):
        await service.approve("apr-1", granted=True, scope="call")

    assert approvals.recorded == [("apr-1", "granted")]
    transition = service._task_manager.transitions[-1]
    assert transition[0:2] == ("task-1", TaskStatus.RECOVERY_REQUIRED)
    assert service._policy.approvals.granted is False
    assert service._kernel.notifications == []


@pytest.mark.asyncio
async def test_runtime_grant_install_failure_requires_recovery() -> None:
    approvals = _Approvals()
    continuations = _Continuations()
    service = _service(
        approvals,
        continuations=continuations,
        runtime_error=RuntimeError("runtime grant unavailable"),
    )

    with pytest.raises(RuntimeError, match="runtime grant unavailable"):
        await service.approve("apr-1", granted=True, scope="call")

    assert approvals.recorded == [("apr-1", "granted")]
    assert continuations.resolved == [("continuation-1", "granted")]
    transition = service._task_manager.transitions[-1]
    assert transition[0:2] == ("task-1", TaskStatus.RECOVERY_REQUIRED)
    assert service._kernel.notifications == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "approval_id", "scope", "message"),
    [
        ("GRANTED", "apr-duplicate", "call", "already resolved"),
        ("PENDING", "apr-expired", "call", "database expired"),
    ],
)
async def test_duplicate_and_expired_resolution_never_installs_authority(
    status: str, approval_id: str, scope: str, message: str
) -> None:
    error = ValueError(message) if status == "PENDING" else None
    approvals = _Approvals(status=status, error=error)
    service = _service(approvals)

    with pytest.raises(ValueError):
        await service.approve(approval_id, granted=True, scope=scope)

    assert approvals.recorded == []
    assert service._policy.approvals.granted is False
    assert service._kernel.notifications == []


@pytest.mark.asyncio
async def test_unknown_approval_id_is_fail_closed() -> None:
    class _Unknown(_Approvals):
        async def get(self, _approval_id: str):
            return None

    approvals = _Unknown()
    service = _service(approvals)

    with pytest.raises(KeyError, match="Approval not found"):
        await service.approve("missing", granted=True, scope="call")

    assert service._policy.approvals.granted is False
    assert service._kernel.notifications == []


@pytest.mark.asyncio
async def test_unsupported_scope_is_rejected_before_durable_resolution() -> None:
    approvals = _Approvals()
    service = _service(approvals)

    with pytest.raises(ValueError, match="Unsupported approval scope"):
        await service.approve("apr-unsupported", granted=True, scope="profile")

    assert approvals.recorded == []
    assert service._policy.approvals.granted is False
    assert service._kernel.notifications == []


@pytest.mark.asyncio
async def test_wake_failure_after_install_requires_recovery() -> None:
    approvals = _Approvals()
    continuations = _Continuations()
    service = _service(
        approvals,
        continuations=continuations,
        kernel_error=RuntimeError("kernel wake failed"),
    )

    with pytest.raises(RuntimeError, match="kernel wake failed"):
        await service.approve("apr-1", granted=True, scope="call")

    assert service._policy.approvals.granted is True
    transition = service._task_manager.transitions[-1]
    assert transition[0:2] == ("task-1", TaskStatus.RECOVERY_REQUIRED)
