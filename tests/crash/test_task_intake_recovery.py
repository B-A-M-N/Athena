"""Ordinary CREATED task intake is reconciled by real service startup."""

from __future__ import annotations

import pytest

from athena.protocol.tasks import AgentRequest, TaskStatus
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import ArtifactRefBlock, FileRefBlock
from athena.protocol.tasks import ContextRef
from athena.state.database import Database
from athena.state.tasks import TaskStore


@pytest.mark.athena_claim("RECOVERY-TASK-INTAKE")
@pytest.mark.athena_evidence("test", "crash-restart")
async def test_created_intake_reconciles_and_quarantines_unsafe_rows(
    make_durable_service, durable_db_path
):
    first = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    request = AgentRequest(prompt="intake survives the restart", session_id="session-intake-real")
    spec = first._build_task_spec(request, request.session_id)
    created = await first._task_manager.create(spec)
    await first._record_canonical_user_turn(request, created)
    await first.stop()  # crash window: durable task/message, no queue transition

    db = Database(durable_db_path)
    await db._ensure_ready()
    try:
        # A task with no durable session cannot reconstruct a causal root and
        # must be quarantined rather than left inert in CREATED.
        await TaskStore(db).insert_task(
            "task-intake-unsafe",
            None,
            None,
            "unsafe intake",
            status=TaskStatus.CREATED,
        )
    finally:
        await db.close()

    second = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    recovered = await second._store_tasks.get(created.id)
    quarantined = await second._store_tasks.get("task-intake-unsafe")
    assert recovered is not None
    assert recovered["status"] != TaskStatus.CREATED.value
    assert recovered["metadata"]["_intake_phase"] == "enqueued"
    assert quarantined is not None
    assert quarantined["status"] == TaskStatus.RECOVERY_REQUIRED.value
    health = second.startup_health()
    assert health["checks"]["task_intake"]["recovered"] == 1
    assert health["checks"]["task_intake"]["quarantined"] == 1


@pytest.mark.athena_claim("RECOVERY-TASK-ATTACHMENT-PROVENANCE")
@pytest.mark.athena_evidence("test", "crash-restart")
async def test_created_intake_recovery_preserves_durable_attachment_provenance(
    make_durable_service, durable_db_path
):
    first = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    attachment = ArtifactRef(
        id="artifact-attachment-1",
        uri="artifact://sha256/attachment-digest",
        hash="attachment-digest",
        mime_type="text/plain",
        size=17,
        producer="upload",
    )
    request = AgentRequest(
        prompt="recover the attached evidence",
        session_id="session-attachment-intake",
        attachments=(attachment,),
    )
    spec = first._build_task_spec(request, request.session_id)
    await first._task_manager.create(spec)
    await first.stop()  # crash before the ephemeral request reaches canonical persistence

    second = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    messages = await second._store_messages.list_session_messages(spec.session_id)
    canonical = next(
        message
        for message in messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    )
    recovered_attachment = next(
        block for block in canonical.blocks if isinstance(block, ArtifactRefBlock)
    )
    assert recovered_attachment.uri == attachment.uri
    assert recovered_attachment.ref is not None
    assert recovered_attachment.ref.id == attachment.id
    assert recovered_attachment.ref.hash == attachment.hash
    assert recovered_attachment.ref.mime_type == attachment.mime_type
    assert recovered_attachment.ref.producer == attachment.producer


def _attachment_signature(message):
    signatures = []
    for block in message.blocks:
        if isinstance(block, ArtifactRefBlock):
            assert block.ref is not None
            signatures.append(
                (
                    "artifact",
                    block.uri,
                    block.ref.id,
                    block.ref.hash,
                    block.ref.mime_type,
                    block.ref.size,
                    block.ref.storage_path,
                    block.ref.producer,
                    dict(block.ref.metadata),
                )
            )
        elif isinstance(block, FileRefBlock):
            signatures.append(("file", block.uri, block.mime_type))
    return tuple(signatures)


@pytest.mark.athena_claim("RECOVERY-TASK-ATTACHMENT-EQUIVALENCE")
@pytest.mark.athena_evidence("test", "crash-restart")
async def test_normal_and_crash_intake_have_identical_attachment_blocks(
    make_durable_service, durable_db_path
):
    first = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    attachment = ArtifactRef(
        id="artifact-equivalence-1",
        uri="artifact://sha256/equivalence-digest",
        hash="equivalence-digest",
        mime_type="application/json",
        size=23,
        storage_path="objects/equivalence.json",
        producer="upload",
        metadata={"source": "test"},
    )
    file_ref = ContextRef(
        kind="file",
        ref="workspace://notes.txt",
        mime_type="text/plain",
    )

    normal_request = AgentRequest(
        prompt="compare ordinary intake",
        session_id="session-attachment-normal",
        attachments=(attachment, file_ref),  # type: ignore[arg-type]
    )
    normal_spec = first._build_task_spec(normal_request, normal_request.session_id)
    await first._task_manager.create(normal_spec)
    await first._record_canonical_user_turn(normal_request, normal_spec)
    normal_messages = await first._store_messages.list_session_messages(normal_spec.session_id)
    normal = next(
        message
        for message in normal_messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    )

    crash_request = AgentRequest(
        prompt="compare recovered intake",
        session_id="session-attachment-crash",
        attachments=(attachment, file_ref),  # type: ignore[arg-type]
    )
    crash_spec = first._build_task_spec(crash_request, crash_request.session_id)
    await first._task_manager.create(crash_spec)
    await first.stop()  # crash before the ephemeral request reaches canonical persistence

    second = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    recovered_messages = await second._store_messages.list_session_messages(crash_spec.session_id)
    recovered = next(
        message
        for message in recovered_messages
        if (message.metadata or {}).get("canonical_user_turn") is True
    )

    assert _attachment_signature(normal) == _attachment_signature(recovered)
