from athena.memory.candidates import candidates_from_task
from athena.protocol.memory import MemoryKind
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    Message,
    Provenance,
    ReasoningBlock,
    Role,
    SourceType,
    TextBlock,
    utcnow,
)
from athena.protocol.tasks import TaskResult, TaskSpec, TaskStatus


async def test_candidates_from_task_mark_promotion_required():
    task = TaskSpec(id="task-1", objective="refactor the retry logic", session_id="sess-1")
    result = TaskResult(task_id="task-1", status=TaskStatus.COMPLETE)
    candidates = await candidates_from_task(
        task,
        transcript=[
            "always verify the retry budget before calling a remote service",
        ],
        result=result,
    )
    assert candidates
    for candidate in candidates:
        assert candidate.metadata.get("promotion") == "required"


async def test_episodic_candidate_has_promotion_flag():
    task = TaskSpec(id="task-2", objective="migrate the config store", session_id="sess-2")
    result = TaskResult(task_id="task-2", status=TaskStatus.COMPLETE)
    candidates = await candidates_from_task(task, transcript=[], result=result)

    episodic = [c for c in candidates if c.kind is MemoryKind.EPISODIC]
    assert len(episodic) == 1
    record = episodic[0]
    assert record.metadata.get("promotion") == "required"
    assert record.metadata.get("origin") == "episodic"
    assert record.metadata.get("task_id") == "task-2"
    assert record.scope.value == "task"


async def test_greeting_and_hidden_reasoning_produce_no_memory_candidates():
    task = TaskSpec(id="task-greeting", objective="hello", session_id="sess-greeting")
    message = Message(
        id="msg-greeting",
        role=Role.ASSISTANT,
        blocks=(
            ReasoningBlock(text="The user greeted me; perhaps save this."),
            TextBlock(text="Hello!"),
        ),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.GENERATED),
    )

    candidates = await candidates_from_task(task, [message], None)

    assert candidates == []


async def test_agent_lesson_requires_later_conclusion_linked_to_successful_result():
    task = TaskSpec(id="task-evidence", session_id="sess-evidence", objective="inspect config")
    transcript = [
        Message(
            id="call-message",
            role=Role.ASSISTANT,
            blocks=(CapabilityCallBlock(call_id="call-1", capability_id="files.read"),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.GENERATED),
        ),
        Message(
            id="result-message",
            role=Role.CAPABILITY,
            blocks=(
                CapabilityResultBlock(
                    call_id="call-1",
                    capability_id="files.read",
                    ok=True,
                    output="config uses bounded retries",
                    ref_uri="evidence:config-1",
                    metadata={"result_id": "result-1"},
                ),
            ),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.CAPABILITY),
        ),
        Message(
            id="conclusion-message",
            role=Role.ASSISTANT,
            blocks=(TextBlock(text="The service uses bounded retries for remote calls."),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.GENERATED),
        ),
    ]

    candidates = await candidates_from_task(task, transcript, None, principal_id="alice")

    lessons = [c for c in candidates if c.kind is MemoryKind.SEMANTIC]
    assert len(lessons) == 1
    assert lessons[0].source_refs == ("result-1", "evidence:config-1", "call-1")
    assert lessons[0].metadata["candidate_type"] == "evidence_linked_lesson"
    assert lessons[0].metadata["session_id"] == "sess-evidence"


async def test_failed_result_or_unlinked_assistant_prose_cannot_create_lesson():
    task = TaskSpec(id="task-unproven", objective="inspect config")
    failed = Message(
        id="failed-result",
        role=Role.CAPABILITY,
        blocks=(
            CapabilityResultBlock(
                call_id="call-failed", capability_id="files.read", ok=False, error="denied"
            ),
        ),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.CAPABILITY),
    )
    conclusion = Message(
        id="unlinked-conclusion",
        role=Role.ASSISTANT,
        blocks=(TextBlock(text="The service always uses safe bounded retries."),),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.GENERATED),
    )

    candidates = await candidates_from_task(task, [failed, conclusion], None)

    assert not [c for c in candidates if c.kind is MemoryKind.SEMANTIC]
