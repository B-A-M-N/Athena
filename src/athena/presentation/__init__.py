"""Interface-neutral presentation projection and bridge contracts."""

from athena.presentation.projection import OperationNode, ProjectionState
from athena.presentation.schema import (
    LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION,
    NATIVE_BRIDGE_SCHEMA_VERSION,
    ProjectionFailure,
    ProjectionFailurePayload,
    ProjectionFrame,
    FailureInfo,
)

__all__ = [
    "NATIVE_BRIDGE_SCHEMA_VERSION",
    "LEGACY_NATIVE_BRIDGE_SCHEMA_VERSION",
    "OperationNode",
    "ProjectionFailure",
    "ProjectionFailurePayload",
    "ProjectionFrame",
    "FailureInfo",
    "ProjectionState",
]
