"""Hosted/native conformance for shared operator commands."""

from __future__ import annotations

import pytest

from athena.cli.chat import ChatREPL
from athena.cli.native_session import NativeSession, parse_args


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def list_sessions(self):
        self.calls.append(("list_sessions",))
        return [{"id": "session-1", "objective": "objective"}]

    async def operator_permissions(self):
        self.calls.append(("operator_permissions",))
        return {
            "active_grants": [{"approval_id": "grant-1", "capability": "fs", "scope": "call"}],
            "pending": [{"approval_id": "approval-1", "capability_id": "execute"}],
        }

    async def operator_diff(self, *, limit: int):
        self.calls.append(("operator_diff", limit))
        return [{"id": "mutation-1", "status": "done", "operation": "write", "resource": "file"}]

    async def undo_mutation(self, mutation_id: str):
        self.calls.append(("undo_mutation", mutation_id))
        return {"status": "undone", "error": None}

    async def operator_context_summary(self, session_id: str | None):
        self.calls.append(("operator_context_summary", session_id))
        return {
            "window": 100,
            "reserve_output": 10,
            "recent_verbatim_turns": 2,
            "message_count": 4,
        }

    async def list_interrupted(self):
        self.calls.append(("list_interrupted",))
        return [{"id": "task-interrupted", "objective": "resume me"}]

    async def approve(self, approval_id: str, *, granted: bool, scope: str | None = None):
        self.calls.append(("approve", approval_id, granted, scope))

    async def cancel(self, task_id: str):
        self.calls.append(("cancel", task_id))

    async def operator_generated_capabilities(self, task_id: str):
        self.calls.append(("operator_generated_capabilities", task_id))
        return [
            {
                "capability_id": "candidate-1",
                "lifecycle_state": "candidate",
                "description": "generated",
                "proof": {"usage": {"successes": 1, "uses": 2}},
            }
        ]

    async def operator_generated_capability(self, capability_id: str, task_id: str):
        self.calls.append(("operator_generated_capability", capability_id, task_id))
        return {
            "id": capability_id,
            "scope": "task",
            "lifecycle_state": "candidate",
            "description": "generated",
            "code_hash": "code-hash",
            "schema_hash": "schema-hash",
            "proof_record": {"usage": {"successes": 1, "uses": 2}},
        }

    async def operator_promote_generated_capability(
        self, capability_id: str, scope: str, task_id: str
    ):
        self.calls.append(("operator_promote_generated_capability", capability_id, scope, task_id))
        return {"value": {"project_id": "project-1"}}

    async def operator_deprecate_generated_capability(self, capability_id: str, task_id: str):
        self.calls.append(("operator_deprecate_generated_capability", capability_id, task_id))


def _controllers() -> tuple[ChatREPL, NativeSession, _RecordingService, _RecordingService]:
    chat_service = _RecordingService()
    native_service = _RecordingService()
    chat = ChatREPL(chat_service)
    native = NativeSession(parse_args([]))
    native.service = native_service
    chat.session_id = native.session_id = "session-shared"
    chat._last_task_id = native._last_task_id = "task-shared"
    chat_output: list[str] = []
    native_output: list[str] = []
    chat.surface.render_notice = lambda message, **_kwargs: chat_output.append(message)
    chat._operator_router._emit = chat_output.append
    native._operator_router._emit = native_output.append
    # Expose outputs on the instances for the assertion helper without making
    # them part of either controller's production API.
    chat._test_output = chat_output  # type: ignore[attr-defined]
    native._test_output = native_output  # type: ignore[attr-defined]
    return chat, native, chat_service, native_service


@pytest.mark.asyncio
async def test_shared_operator_commands_have_hosted_native_service_parity():
    chat, native, chat_service, native_service = _controllers()
    commands = (
        "/sessions",
        "/permissions",
        "/diff 7",
        "/undo mutation-1",
        "/context",
        "/compact",
        "/interrupted",
        "/approve approval-1 task",
        "/deny approval-2",
        "/cancel",
        "/candidates",
        "/candidate candidate-1",
        "/promote candidate-1 project",
        "/deprecate candidate-1",
    )

    for command in commands:
        assert await chat._operator_router.dispatch(command), command
        assert await native._operator_router.dispatch(command), command
        assert chat_service.calls == native_service.calls, command
        assert chat._test_output == native._test_output, command  # type: ignore[attr-defined]

    for command in ("/model model-x", "/autonomy coding", "/criteria one;two", "/new"):
        assert await chat._operator_router.dispatch(command), command
        assert await native._operator_router.dispatch(command), command
        assert chat_service.calls == native_service.calls, command
        assert chat._test_output == native._test_output, command  # type: ignore[attr-defined]

    assert chat.model_policy == "model-x"
    assert native._model_name == "model-x"
    assert chat.autonomy.value == "coding"
    assert native._autonomy_level.value == "coding"
    assert chat.criteria == ["one", "two"]
    assert native._criteria == ["one", "two"]
    assert chat.session_id is None and native.session_id is None
    assert chat._last_task_id is None and native._last_task_id is None

    assert chat_service.calls == [
        ("list_sessions",),
        ("operator_permissions",),
        ("operator_diff", 7),
        ("undo_mutation", "mutation-1"),
        ("operator_context_summary", "session-shared"),
        ("operator_context_summary", "session-shared"),
        ("list_interrupted",),
        ("approve", "approval-1", True, "task"),
        ("approve", "approval-2", False, None),
        ("cancel", "task-shared"),
        ("operator_generated_capabilities", "task-shared"),
        ("operator_generated_capability", "candidate-1", "task-shared"),
        ("operator_promote_generated_capability", "candidate-1", "project", "task-shared"),
        ("operator_deprecate_generated_capability", "candidate-1", "task-shared"),
    ]
