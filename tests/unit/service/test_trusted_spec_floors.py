"""Trusted intake must apply deterministic floors, not bypass them."""

from __future__ import annotations

from athena.protocol.tasks import MutationMode, TaskSpec, WorkspaceSpec
from athena.service.service import AthenaService
from athena.service.work_classification import SpeculationDepth, WorkClass


def test_trusted_prebuilt_complex_spec_cannot_bypass_speculative_floor():
    svc = AthenaService.in_memory()
    spec = TaskSpec(
        id="trusted-task",
        objective="refactor authentication and update all callers",
        workspace=WorkspaceSpec(
            id="workspace",
            root="/tmp",
            mutation_mode=MutationMode.DIRECT,
        ),
    )
    normalized = svc.normalize_spec(spec, trusted=True)

    assert normalized.workspace is not None
    assert normalized.workspace.mutation_mode is MutationMode.SPECULATIVE
    assert normalized.metadata["_athena_work_class"] == WorkClass.COMPLEX_CODING.value
    assert (
        normalized.metadata["_athena_speculation_depth"] == SpeculationDepth.SINGLE_CANDIDATE.value
    )


def test_trusted_prebuilt_noncoding_spec_keeps_direct_workspace():
    svc = AthenaService.in_memory()
    spec = TaskSpec(
        id="trusted-noncoding",
        objective="tell me a joke",
        workspace=WorkspaceSpec(
            id="workspace",
            root="/tmp",
            mutation_mode=MutationMode.DIRECT,
        ),
    )
    normalized = svc.normalize_spec(spec, trusted=True)

    assert normalized.workspace is not None
    assert normalized.workspace.mutation_mode is MutationMode.DIRECT
    assert normalized.metadata["_athena_work_class"] == WorkClass.NON_CODING.value
    assert normalized.metadata["_athena_speculation_depth"] == SpeculationDepth.NONE.value
