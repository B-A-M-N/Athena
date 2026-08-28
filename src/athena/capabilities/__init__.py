"""Capabilities subsystem (BUILDSPEC section 9).

The capability bus: registry (single entry point, INV-004), dispatcher
(canonical invocation lifecycle), and the core capability set:

    fs | execute | memory | skills | delegate

Every model-requested action flows CapabilityRegistry -> PolicyEngine ->
executor. Schema validation precedes policy evaluation (BHV-040); denial has
no effect (BHV-043).
"""

from __future__ import annotations

from athena.capabilities.registry import CapabilityRegistry, validate_schema
from athena.capabilities.dispatcher import (
    CapabilityDispatcher,
    SuspendedCall,
    WAITING_APPROVAL,
)
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.skills import SkillsCapability
from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.dependency import DependencyCapability
from athena.capabilities.reflection import CapabilityReflection
from athena.capabilities.truth import TruthCapability
from athena.capabilities.artifacts import ArtifactCapability
from athena.capabilities.workflow import WorkflowCapability
from athena.capabilities.synthesis import SynthesisCapability
from athena.capabilities.scratch import ScratchCapability
from athena.capabilities.research import ResearchCapability
from athena.capabilities.maintain import MaintenanceCapability
from athena.capabilities.git import GitCapability
from athena.capabilities.observer import ObserverCapability
from athena.capabilities.capsule import ProcedureCapsuleCapability
from athena.capabilities.diagnostics import DiagnosticsCapability

__all__ = [
    "CapabilityRegistry",
    "validate_schema",
    "CapabilityDispatcher",
    "SuspendedCall",
    "WAITING_APPROVAL",
    "FilesystemCapability",
    "ExecuteCapability",
    "MemoryCapability",
    "SkillsCapability",
    "DelegateCapability",
    "DependencyCapability",
    "CapabilityReflection",
    "TruthCapability",
    "ArtifactCapability",
    "WorkflowCapability",
    "SynthesisCapability",
    "ScratchCapability",
    "ResearchCapability",
    "MaintenanceCapability",
    "GitCapability",
    "ObserverCapability",
    "ProcedureCapsuleCapability",
    "DiagnosticsCapability",
]
