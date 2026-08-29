"""Tests for the stable OI-inspired operator surface."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

import pytest

from athena.cli.chat import stream_task
from athena.cli.chat import ChatREPL
from athena.cli.surface import OperatorSurface
from athena.protocol.events import make_event


@dataclass
class _Service:
    events: list
    approved: list[tuple] | None = None

    def __post_init__(self):
        self.approved = []

    async def stream_events(self, task_id, after_sequence=0):
        for event in self.events:
            yield event

    async def approve(self, approval_id, *, granted, scope=None):
        self.approved.append((approval_id, granted, scope))

    async def get_result(self, task_id):
        return "result"


@pytest.mark.asyncio
async def test_surface_groups_code_and_runtime_output():
    output = StringIO()
    surface = OperatorSurface(output=output, interactive=False)
    await surface.render_event(
        make_event(
            "CapabilityRequested",
            {
                "capability_id": "execute",
                "arguments": {"language": "python", "code": "print(2 + 2)"},
            },
        )
    )
    await surface.render_event(make_event("StdoutChunk", {"data": "4\n"}))
    await surface.render_event(make_event("CapabilityCompleted", {"capability_id": "execute"}))
    rendered = output.getvalue()
    assert "python" in rendered
    assert "print(2 + 2)" in rendered
    assert "4" in rendered
    assert "completed" in rendered


@pytest.mark.asyncio
async def test_surface_selects_approval_scope_and_routes_to_service():
    approval = make_event(
        "ApprovalRequested",
        {"approval_id": "apr-1", "capability_id": "execute", "scopes": ["call", "task"]},
    )
    service = _Service([approval])
    output = StringIO()
    surface = OperatorSurface(
        output=output,
        interactive=True,
        input_fn=lambda prompt: "2",
    )
    result = await stream_task(service, "task-1", surface=surface)
    assert result == "result"
    assert service.approved == [("apr-1", True, "task")]
    assert "approval required" in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.athena_scenario("PROJECTION-002")
async def test_direct_shell_escape_uses_the_same_execution_surface():
    class DirectService:
        async def execute_direct(
            self,
            source,
            *,
            language,
            session_id,
            inject_into_context,
            on_approval,
        ):
            assert language == "shell"
            assert session_id.startswith("session_")
            assert inject_into_context is False
            assert on_approval is not None
            return {
                "exit_code": 0,
                "stdout": "hello\n",
                "stderr": "",
                "status": "completed",
            }

    output = StringIO()
    repl = ChatREPL(DirectService())
    repl.surface = OperatorSurface(output=output, error=output, interactive=False)
    await repl._shell_escape("printf hello", inject=False)
    rendered = output.getvalue()
    assert "execute" in rendered
    assert "printf hello" in rendered
    assert "hello" in rendered
    assert "displayed only" in rendered


@pytest.mark.asyncio
async def test_generated_candidate_commands_route_through_service():
    class OperatorService:
        def __init__(self):
            self.candidates_task = None
            self.inspect_args = None
            self.promote_args = None
            self.deprecate_args = None

        async def operator_generated_capabilities(self, task_id):
            self.candidates_task = task_id
            return [
                {
                    "capability_id": "synth_1",
                    "lifecycle_state": "CANDIDATE",
                    "description": "reviewable helper",
                    "proof": {"usage": {"uses": 3, "successes": 3}},
                }
            ]

        async def operator_generated_capability(self, capability_id, task_id):
            self.inspect_args = (capability_id, task_id)
            return {
                "id": capability_id,
                "scope": "candidate",
                "lifecycle_state": "CANDIDATE",
                "description": "reviewable helper",
                "code_hash": "code-hash",
                "schema_hash": "schema-hash",
                "proof_record": {
                    "usage": {"uses": 3, "successes": 3},
                    "quality_score": 0.75,
                },
                "required_dependencies": [],
            }

        async def operator_promote_generated_capability(self, capability_id, scope, task_id):
            self.promote_args = (capability_id, scope, task_id)
            return {"value": {"capability_id": capability_id, "project_id": "root"}}

        async def operator_deprecate_generated_capability(self, capability_id, task_id):
            self.deprecate_args = (capability_id, task_id)
            return {"value": {"status": "deprecated"}}

    service = OperatorService()
    output = StringIO()
    repl = ChatREPL(service)
    repl.surface = OperatorSurface(output=output, error=output, interactive=False)
    repl._last_task_id = "task-1"

    assert await repl._dispatch_meta("/candidates")
    assert service.candidates_task == "task-1"
    assert "synth_1" in output.getvalue()
    assert "3/3 successful" in output.getvalue()

    assert await repl._dispatch_meta("/candidate synth_1")
    assert service.inspect_args == ("synth_1", "task-1")
    assert "quality: 0.75" in output.getvalue()

    assert await repl._dispatch_meta("/promote synth_1 project")
    assert service.promote_args == ("synth_1", "project", "task-1")
    assert "promoted synth_1 to project root" in output.getvalue()

    assert await repl._dispatch_meta("/deprecate synth_1")
    assert service.deprecate_args == ("synth_1", "task-1")
    assert "deprecated synth_1" in output.getvalue()


@pytest.mark.asyncio
async def test_stream_flushes_buffered_text_when_event_source_fails():
    class BrokenService:
        async def stream_events(self, task_id, after_sequence=0):
            yield make_event("ModelDelta", {"text": "partial response"})
            raise RuntimeError("event source disconnected")

    output = StringIO()
    surface = OperatorSurface(output=output, interactive=False)
    with pytest.raises(RuntimeError, match="event source disconnected"):
        await stream_task(BrokenService(), "task-2", surface=surface)
    assert "assistant> partial response" in output.getvalue()
