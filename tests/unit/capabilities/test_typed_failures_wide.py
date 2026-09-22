"""Migrated native capabilities carry typed failure metadata on FAILED results."""

from __future__ import annotations

import pytest

from athena.capabilities.context_blocks import ContextBlocksCapability
from athena.capabilities.dependency import DependencyCapability
from athena.capabilities.git import GitCapability
from athena.capabilities.watch import WatchCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus


class _NullStore:
    """Minimal store double: only the failure path is exercised."""


def _request(capability_id):
    return CapabilityRequest(
        capability_id,
        {"operation": "definitely-not-an-operation"},
        task_id="typed-failures",
        call_id="typed-failure-1",
    )


@pytest.mark.parametrize(
    "executor",
    [
        ContextBlocksCapability(_NullStore()),
        DependencyCapability(),
        GitCapability(),
        WatchCapability(),
    ],
    ids=lambda e: e.descriptor.id,
)
async def test_failed_operation_carries_typed_failure_code(executor):
    result = await executor.invoke(_request(executor.descriptor.id))
    assert result.status is CapabilityResultStatus.FAILED
    assert result.metadata is not None, f"{executor.descriptor.id}: no metadata"
    assert "failure_code" in result.metadata, (
        f"{executor.descriptor.id}: FAILED result lacks typed failure_code"
    )
    assert result.metadata["failure_code"]
