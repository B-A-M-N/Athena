from athena.memory.candidates import candidates_from_task
from athena.protocol.memory import MemoryKind
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