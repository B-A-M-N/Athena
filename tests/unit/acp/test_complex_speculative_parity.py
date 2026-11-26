"""ACP must not bypass canonical complex-coding admission."""

from __future__ import annotations

from athena.acp.adapter import ACPAdapter, ACPRequest
from athena.service.service import AthenaService


def test_acp_provisional_spec_is_normalized_before_intake():
    service = AthenaService.in_memory()
    adapter = ACPAdapter(None, None, service=service)
    provisional = adapter.to_task_spec(
        ACPRequest(
            objective="implement OAuth login with refresh tokens and update tests",
            session_id="acp-session",
            workspace={"root": service.config.workspace_root},
            capability_policy={"allow": ["execute"]},
        )
    )
    assert provisional.metadata.get("origin") == "acp"
    normalized = service.normalize_spec(provisional)
    assert normalized.metadata["_athena_work_class"] == "complex_coding"
    assert normalized.metadata["_athena_speculation_depth"] == "single_candidate"
    assert normalized.workspace is not None
    assert normalized.workspace.mutation_mode.value == "speculative"
