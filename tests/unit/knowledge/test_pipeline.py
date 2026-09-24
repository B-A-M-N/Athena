from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.knowledge.pipeline import KnowledgePipeline
from athena.protocol.messages import CapabilityCallBlock, CapabilityResultBlock
from athena.protocol.tasks import TaskStatus
from athena.skills.models import Skill
from athena.workflows.mining import merge_observation
from athena.workflows.models import Workflow, WorkflowStep


class _Messages:
    def __init__(self, messages):
        self._messages = messages

    async def list_session_messages(self, session_id):
        return self._messages


class _Workflows:
    def __init__(self):
        self.saved = []

    async def save(self, workflow):
        self.saved.append(workflow)


class _CandidateWorkflows(_Workflows):
    async def save(self, workflow):
        for index, existing in enumerate(self.saved):
            if existing.id == workflow.id:
                self.saved[index] = workflow
                return
        self.saved.append(workflow)

    async def find_candidate_by_signature(self, signature):
        for workflow in self.saved:
            if workflow.provenance.get("trace_signature") == signature:
                return workflow
        return None

    async def record_candidate_observation(
        self,
        workflow_id,
        *,
        task_id,
        steps=(),
        workspace_id=None,
        workspace_revision=None,
        verification=None,
        observed_at=None,
    ):
        updated = None
        for workflow in self.saved:
            if workflow.id == workflow_id:
                updated = merge_observation(workflow, task_id=task_id, steps=steps)
                await self.save(updated)
                return updated
        return None


class _SkillEvents:
    def __init__(self, events):
        self._events = events

    async def list_for_task(self, task_id):
        return self._events


class _OutcomeSkills:
    def __init__(self):
        self.skill = Skill(
            id="skill-release",
            name="release-checks",
            description="Run release checks",
            body="Run the checks and verify the receipt.",
            triggers=("release", "checks"),
            version=1,
        )
        self.outcomes = []
        self.candidates = []

    async def get(self, skill_id):
        return self.skill if skill_id == self.skill.id else None

    async def record_outcome(self, skill_id, **kwargs):
        self.outcomes.append((skill_id, kwargs))
        return {"status": "recorded"}

    async def promote(self, candidate, **kwargs):
        self.candidates.append((candidate, kwargs))
        return None


@pytest.mark.asyncio
async def test_failed_skill_reuse_is_attributed_and_targets_exact_revision():
    messages = [
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="read",
                    capability_id="fs",
                    arguments={"operation": "read", "path": "release.toml"},
                ),
                CapabilityResultBlock(call_id="read", capability_id="fs", ok=True, output="config"),
                CapabilityCallBlock(
                    call_id="check",
                    capability_id="execute",
                    arguments={"command": "pytest -q"},
                ),
                CapabilityResultBlock(
                    call_id="check", capability_id="execute", ok=True, output="passed"
                ),
            )
        )
    ]
    skills = _OutcomeSkills()
    pipeline = KnowledgePipeline(
        messages=_Messages(messages),
        skill_lifecycle=skills,
        events=_SkillEvents(
            [
                SimpleNamespace(
                    type="SkillContextSelected",
                    payload={"skills": [{"skill_id": "skill-release", "version": 1}]},
                ),
                SimpleNamespace(
                    type="SkillApplied",
                    payload={"skill_id": "skill-release", "version": 1},
                ),
            ]
        ),
    )

    await pipeline(
        SimpleNamespace(
            id="task-failed-reuse",
            session_id="session-failed-reuse",
            objective="follow these repeatable release steps and verify the project",
        ),
        SimpleNamespace(
            status=TaskStatus.FAILED,
            summary="the release check selected the wrong scope",
            unresolved=("release scope",),
        ),
    )

    assert skills.outcomes == [
        (
            "skill-release",
            {
                "version": 1,
                "passed": False,
                "task_id": "task-failed-reuse",
                "failure": {
                    "status": "FAILED",
                    "summary": "the release check selected the wrong scope",
                    "unresolved": ["release scope"],
                },
            },
        )
    ]
    assert len(skills.candidates) == 1
    candidate, kwargs = skills.candidates[0]
    assert kwargs["authorized"] is False
    assert candidate.target_skill == "skill-release"
    assert candidate.target_skill_version == 1
    assert candidate.draft.id == "skill-release"
    assert candidate.draft.version == 1


@pytest.mark.asyncio
async def test_single_successful_trace_waits_for_repeatability():
    messages = [
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="call-1",
                    capability_id="fs",
                    arguments={"operation": "read", "path": "pyproject.toml"},
                ),
                CapabilityResultBlock(
                    call_id="call-1",
                    capability_id="fs",
                    ok=True,
                    output="{}",
                ),
            )
        ),
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="call-2",
                    capability_id="execute",
                    arguments={"command": "pytest -q"},
                ),
                CapabilityResultBlock(
                    call_id="call-2",
                    capability_id="execute",
                    ok=True,
                    output="passed",
                ),
            )
        ),
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="call-3",
                    capability_id="fs",
                    arguments={"operation": "read", "path": "ignored"},
                ),
                CapabilityResultBlock(
                    call_id="call-3",
                    capability_id="fs",
                    ok=False,
                    output="",
                    error="missing",
                ),
            )
        ),
    ]
    workflows = _Workflows()
    pipeline = KnowledgePipeline(
        messages=_Messages(messages),
        workflow_store=workflows,
    )

    await pipeline(
        SimpleNamespace(
            id="task-procedure",
            session_id="session-procedure",
            objective="follow these repeatable steps and verify the project",
        ),
        SimpleNamespace(status=TaskStatus.COMPLETE),
    )

    assert workflows.saved == []

    await pipeline(
        SimpleNamespace(
            id="task-procedure-2",
            session_id="session-procedure",
            objective="follow these repeatable steps and verify the project",
        ),
        SimpleNamespace(status=TaskStatus.COMPLETE),
    )

    assert len(workflows.saved) == 1
    workflow = workflows.saved[0]
    assert workflow.scope.value == "candidate"
    assert workflow.task_scope == "task-procedure"
    assert [step.capability_id for step in workflow.steps] == ["fs", "execute"]
    assert workflow.provenance["origin"] == "successful_task_trace"
    assert workflow.provenance["successful_observations"] == 2


@pytest.mark.asyncio
async def test_procedure_learning_excludes_capsule_transport_calls():
    messages = [
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="capsule-call",
                    capability_id="capsule",
                    arguments={"operation": "run"},
                ),
                CapabilityResultBlock(
                    call_id="capsule-call",
                    capability_id="capsule",
                    ok=True,
                    output="completed",
                ),
            )
        ),
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="check-call",
                    capability_id="execute",
                    arguments={"command": "pytest -q"},
                ),
                CapabilityResultBlock(
                    call_id="check-call",
                    capability_id="execute",
                    ok=True,
                    output="passed",
                ),
            )
        ),
    ]
    workflows = _Workflows()
    pipeline = KnowledgePipeline(
        messages=_Messages(messages),
        workflow_store=workflows,
    )

    await pipeline(
        SimpleNamespace(id="task-capsule", session_id="session-capsule"),
        SimpleNamespace(status=TaskStatus.COMPLETE),
    )

    assert len(workflows.saved) == 0


@pytest.mark.asyncio
async def test_partial_task_does_not_create_workflow_candidate():
    messages = [
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="read-call",
                    capability_id="fs",
                    arguments={"operation": "read", "path": "README.md"},
                ),
                CapabilityResultBlock(
                    call_id="read-call",
                    capability_id="fs",
                    ok=True,
                    output="{}",
                ),
            )
        ),
        SimpleNamespace(
            blocks=(
                CapabilityCallBlock(
                    call_id="check-call",
                    capability_id="execute",
                    arguments={"command": "pytest -q"},
                ),
                CapabilityResultBlock(
                    call_id="check-call",
                    capability_id="execute",
                    ok=True,
                    output="passed",
                ),
            )
        ),
    ]
    workflows = _Workflows()
    pipeline = KnowledgePipeline(
        messages=_Messages(messages),
        workflow_store=workflows,
    )

    await pipeline(
        SimpleNamespace(id="task-partial", session_id="session-partial"),
        SimpleNamespace(status=TaskStatus.PARTIAL),
    )

    assert workflows.saved == []


@pytest.mark.asyncio
async def test_shared_session_learning_stops_at_next_canonical_turn():
    messages = [
        SimpleNamespace(
            metadata={"canonical_user_turn": True, "task_id": "task-a"},
            blocks=(),
        ),
        SimpleNamespace(
            metadata={},
            blocks=(
                CapabilityCallBlock(
                    call_id="call-a", capability_id="fs", arguments={"operation": "read"}
                ),
                CapabilityResultBlock(call_id="call-a", capability_id="fs", ok=True, output="a"),
                CapabilityCallBlock(
                    call_id="call-a-check", capability_id="execute", arguments={"command": "a"}
                ),
                CapabilityResultBlock(
                    call_id="call-a-check",
                    capability_id="execute",
                    ok=True,
                    output="a-check",
                ),
            ),
        ),
        SimpleNamespace(
            metadata={"canonical_user_turn": True, "task_id": "task-b"},
            blocks=(),
        ),
        SimpleNamespace(
            metadata={},
            blocks=(
                CapabilityCallBlock(
                    call_id="call-b", capability_id="execute", arguments={"command": "b"}
                ),
                CapabilityResultBlock(
                    call_id="call-b", capability_id="execute", ok=True, output="b"
                ),
            ),
        ),
    ]
    workflows = _Workflows()
    pipeline = KnowledgePipeline(messages=_Messages(messages), workflow_store=workflows)

    await pipeline(
        SimpleNamespace(
            id="task-a",
            session_id="shared-session",
            objective="follow these repeatable steps and verify the project",
        ),
        SimpleNamespace(status=TaskStatus.COMPLETE),
    )

    assert workflows.saved == []


@pytest.mark.asyncio
async def test_successful_workflow_execution_enters_same_learning_store():
    workflows = _Workflows()
    pipeline = KnowledgePipeline(workflow_store=workflows)
    workflow = Workflow.create(
        name="verified procedure",
        description="procedure",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read", "path": "README.md"},
            ),
            WorkflowStep(
                id="check",
                capability_id="execute",
                arguments={"command": "python -m compileall src"},
            ),
        ),
    )

    await pipeline.observe_workflow_execution(
        task_id="task-verified",
        workflow=workflow,
        outcome=SimpleNamespace(
            status="completed",
            run_id="run-verified",
            outputs={"check": "passed"},
        ),
    )

    assert len(workflows.saved) == 1
    candidate = workflows.saved[0]
    assert candidate.provenance["origin"] == "successful_workflow_execution"
    assert candidate.provenance["source_workflow_id"] == workflow.id
    assert candidate.provenance["source_run_id"] == "run-verified"
    assert candidate.provenance["verification"] == {
        "status": "completed",
        "run_id": "run-verified",
        "output_keys": ["check"],
    }
    assert candidate.provenance["successful_observations"] == 1


@pytest.mark.asyncio
async def test_repeated_workflow_observations_merge_through_learning_store():
    workflows = _CandidateWorkflows()
    pipeline = KnowledgePipeline(workflow_store=workflows)

    def source_workflow(path):
        return Workflow.create(
            name="verified procedure",
            description="procedure",
            steps=(
                WorkflowStep(
                    id="read",
                    capability_id="fs",
                    arguments={"operation": "read", "path": path},
                ),
            ),
        )

    await pipeline.observe_workflow_execution(
        task_id="task-one",
        workflow=source_workflow("src/one.py"),
        outcome=SimpleNamespace(status="completed", run_id="run-one", outputs={}),
    )
    await pipeline.observe_workflow_execution(
        task_id="task-two",
        workflow=source_workflow("src/two.py"),
        outcome=SimpleNamespace(status="completed", run_id="run-two", outputs={}),
    )

    assert len(workflows.saved) == 1
    candidate = workflows.saved[0]
    assert candidate.provenance["successful_observations"] == 2
    assert candidate.provenance["observed_task_ids"] == ["task-one", "task-two"]
    assert candidate.steps[0].arguments["path"] == "$input.path"
    assert len(candidate.provenance["observations"]) == 2


@pytest.mark.asyncio
async def test_recovery_workflow_does_not_enter_learning_store():
    workflows = _Workflows()
    pipeline = KnowledgePipeline(workflow_store=workflows)
    workflow = Workflow.create(
        name="unresolved procedure",
        description="procedure",
        steps=(
            WorkflowStep(
                id="read",
                capability_id="fs",
                arguments={"operation": "read", "path": "README.md"},
            ),
        ),
    )

    await pipeline.observe_workflow_execution(
        task_id="task-recovery",
        workflow=workflow,
        outcome=SimpleNamespace(status="recovery_required"),
    )

    assert workflows.saved == []


def test_repeated_trace_generalizes_only_values_that_changed():
    from athena.affordances.models import AffordanceScope

    first = (
        WorkflowStep(
            id="read",
            capability_id="fs",
            arguments={"operation": "read", "path": "src/one.py"},
        ),
        WorkflowStep(
            id="test",
            capability_id="execute",
            arguments={"command": "pytest tests/one_test.py"},
        ),
    )
    workflow = Workflow.create(
        name="candidate",
        description="candidate",
        steps=first,
        scope=AffordanceScope.CANDIDATE,
        task_scope="task-1",
        provenance={
            "origin": "successful_task_trace",
            "task_id": "task-1",
            "observed_task_ids": ["task-1"],
            "successful_observations": 1,
            "observations": [
                {
                    "task_id": "task-1",
                    "steps": [step.to_record() for step in first],
                }
            ],
        },
    )
    second = (
        WorkflowStep(
            id="read",
            capability_id="fs",
            arguments={"operation": "read", "path": "src/two.py"},
        ),
        WorkflowStep(
            id="test",
            capability_id="execute",
            arguments={"command": "pytest tests/two_test.py"},
        ),
    )

    merged = merge_observation(workflow, task_id="task-2", steps=second)

    assert merged.provenance["successful_observations"] == 2
    assert merged.input_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}, "command": {"type": "string"}},
        "required": ["command", "path"],
    }
    assert merged.steps[0].arguments == {"operation": "read", "path": "$input.path"}
    assert merged.steps[1].arguments == {"command": "$input.command"}
    assert merged.provenance["representative_inputs"] == {
        "path": "src/one.py",
        "command": "pytest tests/one_test.py",
    }
    assert merged.provenance["parameter_bindings"] == {
        "path": {"source_path": "read.arguments.path"},
        "command": {"source_path": "test.arguments.command"},
    }
