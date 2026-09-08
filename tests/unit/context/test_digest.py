from athena.context.compiler import ContextCompiler, _message_entry
from athena.context.digest_builder import ContextDigestBuilder
from athena.context.digest import ContextDigest, ContextDigestStore
from athena.protocol.messages import (
    CapabilityResultBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    utcnow,
)
from athena.protocol.tasks import TaskSpec
from athena.state.database import Database


async def test_context_digest_round_trips_structured_state_and_is_principal_scoped():
    db = Database(":memory:")
    await db._ensure_ready()
    store = ContextDigestStore(db)
    digest = await store.save(
        ContextDigest(
            task_id="task-1",
            session_id="session-1",
            principal_id="principal-1",
            level=2,
            fields={
                "objective": "ship the release",
                "decisions": ["retain the frozen artifact identity"],
                "pending_work": ["run the final gate"],
            },
            transcript_anchors=("msg-old",),
            recovery_queries=("release artifact",),
        )
    )

    assert (await store.latest_for_task("task-1", "principal-1")).id == digest.id
    assert await store.latest_for_task("task-1", "other-principal") is None
    listed = await store.list_for_session("session-1", "principal-1")
    assert listed[0].normalized_fields()["pending_work"] == ["run the final gate"]
    await db.close()


async def test_context_compiler_persists_and_rehydrates_digest_before_history_tail():
    db = Database(":memory:")
    await db._ensure_ready()
    store = ContextDigestStore(db)
    compiler = ContextCompiler(
        context_digest_store=store,
        context_window=700,
        reserve_output=128,
        recent_verbatim_turns=2,
        safety_margin=0,
    )
    history = tuple(
        Message(
            id=f"msg-{index}",
            role=Role.USER if index % 2 == 0 else Role.ASSISTANT,
            blocks=(TextBlock(text=f"decision {index} " + ("detail " * 35)),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.SESSION),
        )
        for index in range(12)
    )
    task = TaskSpec(id="task-1", session_id="session-1", objective="ship the release")

    first = await compiler.compile(task, recent_messages=history)

    assert first.compression.occurred
    assert await store.latest_for_task("task-1", "athena") is not None

    second_task = TaskSpec(id="task-2", session_id="session-1", objective="continue the release")
    second = await compiler.compile(second_task, recent_messages=history[-2:])
    assert any("durable context digest" in message.text() for message in second.messages)
    await db.close()


def test_digest_builder_requires_evidence_for_decisions_and_completed_work():
    task = TaskSpec(id="task-1", session_id="session-1", objective="inspect the project")
    fluent = Message(
        id="assistant-fluent",
        role=Role.ASSISTANT,
        blocks=(TextBlock(text="The project is healthy and ready to ship."),),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.SESSION),
    )
    result = Message(
        id="result-message",
        role=Role.CAPABILITY,
        blocks=(
            CapabilityResultBlock(
                call_id="call-1",
                capability_id="fs",
                output="README.md",
                metadata={
                    "operation": "read",
                    "resolved_effects": ["READ_LOCAL"],
                    "result_id": "result-1",
                },
            ),
        ),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.CAPABILITY),
    )
    conclusion = Message(
        id="assistant-conclusion",
        role=Role.ASSISTANT,
        blocks=(TextBlock(text="README.md is the project entry point."),),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.SESSION),
    )

    digest = ContextDigestBuilder().build(
        task,
        required=(),
        corpus=tuple(
            _message_entry(message, is_last=False) for message in (fluent, result, conclusion)
        ),
        previous=None,
        principal_id="athena",
    )

    assert digest.fields["decisions"] == ["README.md is the project entry point."]
    assert digest.fields["completed_work"] == [
        {
            "kind": "observation",
            "capability_id": "fs",
            "status": "completed",
            "proof_reference": "result-1",
        }
    ]
