import pytest

from athena.context.compiler import (
    ContextCompiler,
    _memory_context_needed,
    _research_context_needed,
    _skills_context_needed,
)
from athena.context.blocks import ContextBlock
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import (
    AudioBlock,
    CapabilityResultBlock,
    ImageBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)
from athena.protocol.tasks import ContextRef, ModelPolicy, TaskSpec, WorkspaceSpec


def _task(objective: str = "do the thing", **kw) -> TaskSpec:
    return TaskSpec(id="task-1", objective=objective, **kw)


class _CapRegistry:
    def __init__(self, descriptors):
        self._descriptors = descriptors

    async def list_descriptors(self):
        return list(self._descriptors)


class _SearchCapRegistry(_CapRegistry):
    async def search(self, query, **kwargs):
        return [{"id": "files.read"}]


class _NoMatchCapRegistry(_CapRegistry):
    async def search(self, query, **kwargs):
        return []


class _ActionMissCapRegistry(_NoMatchCapRegistry):
    pass


class _EvidenceCapRegistry(_CapRegistry):
    def __init__(self, descriptors):
        super().__init__(descriptors)
        self.workspace = None

    async def search(self, query, **kwargs):
        self.workspace = kwargs.get("workspace")
        return [
            {
                "id": "files.read",
                "scope": "project",
                "availability": "available",
                "effects": ["READ_LOCAL"],
                "tags": ["evidence"],
                "output_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "proof": {"all_passed": True, "quality_score": 0.9},
                "optimizer": {
                    "dependency_available": True,
                    "environment_compatible": True,
                },
            }
        ]


class _RevisionedCapRegistry(_CapRegistry):
    generation = 1


class _ContextBlockStore:
    async def list(self, *, scopes, attached_only, limit):
        assert attached_only is True
        assert ("task", "task-1") in scopes
        return [
            ContextBlock(
                id="ctx-project-contract",
                label="project-contract",
                content="Always preserve the public API.",
                scope="project",
                scope_id="repo",
                trust=TrustClass.CONFIGURED_INSTRUCTION,
            )
        ]


class _InstructionWorkspaceReader:
    def list_agents_md(self):
        return [("/tmp/repo/AGENTS.md", "Project instruction: preserve the API.")]


@pytest.mark.parametrize(
    "objective",
    [
        "use what we discussed yesterday",
        "apply my preference from last time",
        "follow that decision you said we made",
        "I told you before; do it again",
    ],
)
def test_memory_gate_handles_referential_phrases(objective):
    # Referential turns retrieve memory. Skills also run: the metadata-only
    # selector is cheap and relevance is its decision, not the prompt's
    # vocabulary.
    assert _memory_context_needed(objective)
    assert _skills_context_needed(objective)
    assert not _research_context_needed(objective)


@pytest.mark.parametrize(
    "objective",
    [
        "hello",
        "thanks",
        "tell me a short joke",
    ],
)
def test_trivial_conversation_skips_all_retrieval(objective):
    assert not _memory_context_needed(objective)
    assert not _skills_context_needed(objective)
    assert not _research_context_needed(objective)


@pytest.mark.parametrize(
    "objective",
    [
        # A skill explicitly designed for Kubernetes deployment must be
        # selectable even though the prompt never says "skill".
        "Deploy this service to Kubernetes.",
        "use the same setup as before",
        "do it the way we decided",
    ],
)
def test_work_turns_run_skill_selector_without_prompt_keywords(objective):
    assert _skills_context_needed(objective)
    assert _memory_context_needed(objective)


@pytest.mark.athena_claim("BHV-029")
@pytest.mark.athena_evidence("test")
async def test_compile_minimal_context():
    """Compile a minimal system + user objective context."""
    compiler = ContextCompiler()
    ctx = await compiler.compile(_task())

    assert isinstance(ctx.messages, tuple)
    roles = {m.role for m in ctx.messages}
    assert Role.SYSTEM in roles
    assert Role.USER in roles
    # The user objective is present in the compiled messages.
    user_texts = " ".join(m.text() for m in ctx.messages if m.role == Role.USER)
    assert "do the thing" in user_texts
    assert ctx.requirements is not None


@pytest.mark.asyncio
async def test_attached_context_blocks_are_mandatory_and_provenanced():
    task = _task(workspace=WorkspaceSpec(id="repo", root="/tmp/repo"))
    context = await ContextCompiler(
        context_block_store=_ContextBlockStore(),
    ).compile(task)

    messages = [message for message in context.messages if "project-contract" in message.text()]
    assert len(messages) == 1
    assert messages[0].provenance.source_id == "ctx-project-contract"
    assert messages[0].provenance.trust is TrustClass.CONFIGURED_INSTRUCTION

    rendered = [message.text() for message in context.messages]
    assert rendered.index(messages[0].text()) < rendered.index("do the thing")
    assert context.cache_prefix_messages[-1].text() == messages[0].text()


@pytest.mark.asyncio
async def test_configured_project_instructions_use_the_shared_authority_role():
    context = await ContextCompiler(
        workspace_reader=_InstructionWorkspaceReader(),
        context_block_store=_ContextBlockStore(),
    ).compile(_task(workspace=WorkspaceSpec(id="repo", root="/tmp/repo")))

    project_messages = [
        message
        for message in context.messages
        if "project instruction" in message.text().casefold()
    ]
    assert project_messages
    assert all(message.role is Role.USER for message in project_messages)
    assert all("[project instruction authority]" in message.text() for message in project_messages)


@pytest.mark.asyncio
async def test_runtime_recovery_hint_is_visible_in_resumed_context():
    context = await ContextCompiler().compile(
        _task(
            metadata={
                "_runtime_recovery_hint": {
                    "message": "re-establish state before continuing",
                }
            }
        )
    )

    assert any(
        "Runtime recovery hint: re-establish state before continuing" in message.text()
        for message in context.messages
    )


@pytest.mark.asyncio
async def test_adaptive_recovery_decision_is_visible_as_advisory_context():
    context = await ContextCompiler().compile(
        _task(
            metadata={
                "_adaptive_recovery": {
                    "kind": "implementation_repair",
                    "action": "synthesis.repair",
                    "reason": "implementation_failure",
                    "remaining_attempts": 1,
                }
            }
        )
    )
    assert any(
        "Adaptive recovery decision" in message.text() and "synthesis.repair" in message.text()
        for message in context.messages
    )


@pytest.mark.athena_claim("BHV-029")
@pytest.mark.athena_evidence("test")
async def test_to_request_includes_capabilities_when_registered():
    desc = CapabilityDescriptor(id="files.read", description="read", input_schema={})
    registry = _CapRegistry([desc])
    compiler = ContextCompiler(capability_registry=registry)

    ctx = await compiler.compile(_task())
    req = ctx.to_request(model="m1", provider="fake")

    assert req.capabilities == (desc,)
    assert req.messages == tuple(ctx.messages)


@pytest.mark.asyncio
async def test_fabric_progressively_discloses_relevant_capabilities():
    selected = CapabilityDescriptor(id="files.read", description="read files", input_schema={})
    unrelated = CapabilityDescriptor(
        id="database.query", description="query database", input_schema={}
    )
    compiler = ContextCompiler(capability_registry=_SearchCapRegistry([selected, unrelated]))

    ctx = await compiler.compile(_task(objective="read files"))

    assert [descriptor.id for descriptor in ctx.capability_definitions] == ["files.read"]


@pytest.mark.asyncio
async def test_capability_search_miss_does_not_expand_to_full_inventory():
    compiler = ContextCompiler(
        capability_registry=_NoMatchCapRegistry(
            [
                CapabilityDescriptor(id="files.read", description="read files", input_schema={}),
                CapabilityDescriptor(
                    id="database.query", description="query database", input_schema={}
                ),
            ]
        )
    )

    context = await compiler.compile(_task(objective="tell me a short joke"))

    assert context.capability_definitions == ()
    assert context.requirements.needs_tools is False
    assert context.strategy.route == "respond"


@pytest.mark.asyncio
async def test_action_search_miss_keeps_bounded_discovery_path():
    compiler = ContextCompiler(
        capability_registry=_ActionMissCapRegistry(
            [
                CapabilityDescriptor(
                    id="capabilities", description="reflect and search", input_schema={}
                ),
                CapabilityDescriptor(id="files.read", description="read files", input_schema={}),
                CapabilityDescriptor(
                    id="database.query", description="query database", input_schema={}
                ),
            ]
        )
    )

    context = await compiler.compile(_task(objective="open the configuration"))

    assert [descriptor.id for descriptor in context.capability_definitions] == ["capabilities"]
    assert context.requirements.needs_tools is True
    assert context.strategy.decision == "discover"
    assert context.strategy.completion_mode == "observable_work_required"


@pytest.mark.asyncio
async def test_canonical_user_turn_prevents_duplicate_task_objective():
    task = _task(objective="hello from the canonical intake")
    canonical = Message(
        id="msg_user_task-1",
        role=Role.USER,
        blocks=(TextBlock(text=task.objective),),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.USER, trust=TrustClass.USER_CONTENT),
        metadata={"task_id": task.id, "canonical_user_turn": True},
    )

    context = await ContextCompiler().compile(task, recent_messages=[canonical])

    assert [message.text() for message in context.messages].count(task.objective) == 1


@pytest.mark.asyncio
async def test_strategy_preserves_fabric_proof_and_workspace_scope():
    workspace = WorkspaceSpec(id="repo", root="/tmp/repo")
    registry = _EvidenceCapRegistry(
        [CapabilityDescriptor(id="files.read", description="read files", input_schema={})]
    )
    context = await ContextCompiler(capability_registry=registry).compile(
        _task(objective="inspect evidence", workspace=workspace)
    )

    assert registry.workspace is workspace
    evidence = context.strategy.affordances[0]
    assert evidence.scope == "project"
    assert evidence.proof["all_passed"] is True
    assert evidence.proof["optimizer"]["dependency_available"] is True
    assert evidence.output_schema["type"] == "object"


@pytest.mark.asyncio
async def test_static_context_cache_is_partitioned_by_workspace_revision():
    registry = _RevisionedCapRegistry(
        [CapabilityDescriptor(id="files.read", description="read", input_schema={})]
    )
    compiler = ContextCompiler(capability_registry=registry)
    first = _task(
        workspace=WorkspaceSpec(id="repo", root="/tmp/repo", revision="rev-1"),
        model_policy=ModelPolicy(require_tools=False),
    )
    second = _task(
        workspace=WorkspaceSpec(id="repo", root="/tmp/repo", revision="rev-2"),
        model_policy=ModelPolicy(require_tools=False),
    )

    await compiler.compile(first)
    await compiler.compile(first)
    assert len(compiler._static_cache) == 1  # noqa: SLF001 - cache contract

    await compiler.compile(second)
    assert len(compiler._static_cache) == 2  # noqa: SLF001 - cache contract


@pytest.mark.athena_claim("BHV-029")
@pytest.mark.athena_evidence("test")
async def test_bounded_context_within_token_budget():
    """BHV-029: compiled context stays within a small token budget."""
    compiler = ContextCompiler(
        context_window=2000,
        reserve_output=512,
        recent_verbatim_turns=2,
    )
    ctx = await compiler.compile(_task(objective="objective " + "z" * 20))

    assert ctx.estimated_tokens <= 2000


@pytest.mark.asyncio
async def test_compression_respects_input_budget_after_summary_insertion():
    """Compression may not spend the reserved output budget on input text."""
    compiler = ContextCompiler(
        context_window=700,
        reserve_output=128,
        recent_verbatim_turns=2,
        safety_margin=0,
    )
    history = tuple(
        Message(
            id=f"history-{index}",
            role=Role.USER if index % 2 == 0 else Role.ASSISTANT,
            blocks=(TextBlock(text=f"turn {index} " + ("detail " * 35)),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.SESSION),
        )
        for index in range(12)
    )

    context = await compiler.compile(
        _task(objective="continue the bounded context test"), recent_messages=history
    )

    assert context.compression.occurred
    assert context.estimated_tokens <= 700 - 128


@pytest.mark.asyncio
async def test_context_refs_preserve_multimodal_blocks_and_requirements():
    task = _task(
        model_policy=ModelPolicy(require_tools=False),
        context_refs=(
            ContextRef(kind="image", ref="artifact://sha256/image", mime_type="image/png"),
            ContextRef(kind="audio", ref="artifact://sha256/audio", mime_type="audio/wav"),
        ),
    )

    context = await ContextCompiler().compile(task)

    blocks = [block for message in context.messages for block in message.blocks]
    assert any(isinstance(block, ImageBlock) for block in blocks)
    assert any(isinstance(block, AudioBlock) for block in blocks)
    assert context.requirements.vision is True
    assert context.requirements.audio is True


@pytest.mark.asyncio
async def test_recent_visual_capability_result_requires_vision_capability():
    recent = Message(
        id="m-visual-result",
        role=Role.CAPABILITY,
        blocks=(
            CapabilityResultBlock(
                call_id="call-visual",
                capability_id="computer",
                output='{"model_input":"image"}',
                metadata={"mime_type": "image/png", "artifact_uri": "artifact://frame"},
            ),
        ),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.CAPABILITY),
    )

    context = await ContextCompiler().compile(_task(), recent_messages=(recent,))

    assert context.requirements.vision is True


class _ResearchStore:
    async def search_content(self, query, **kwargs):
        assert query == "what is the protocol"
        assert kwargs["task_id"] == "task-1"
        return [
            {
                "source": {
                    "id": "src-1",
                    "title": "Protocol notes",
                    "canonical_uri": "https://example.test/protocol",
                },
                "snippet": "The protocol uses framed messages.",
            }
        ]


class _RevisionedResearchStore:
    generation = 0

    def __init__(self):
        self.calls = 0

    async def search_content(self, query, **kwargs):
        del query, kwargs
        self.calls += 1
        return []


class _WorkflowStore:
    def __init__(self):
        from athena.protocol.affordances import AffordanceScope
        from athena.workflows.models import Workflow, WorkflowStep

        self.workflow = Workflow.create(
            name="release checks",
            description="Run the repeatable release procedure.",
            steps=(WorkflowStep(id="read", capability_id="fs", arguments={}),),
            scope=AffordanceScope.PROJECT,
            project_scope="repo",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            lifecycle_state="PROMOTED",
        )

    async def list(self, **kwargs):
        del kwargs
        return [self.workflow]


async def test_compiler_offers_relevant_promoted_workflow_without_naming_it():
    workflow_store = _WorkflowStore()
    compiler = ContextCompiler(workflow_store=workflow_store)
    context = await compiler.compile(_task(objective="run the release checks for this repository"))
    text = "\n".join(message.text() for message in context.messages)
    assert "promoted workflow suggestion" in text
    assert workflow_store.workflow.id in text
    assert "Required inputs schema" in text


async def test_compiler_retrieves_scoped_research_as_external_evidence():
    compiler = ContextCompiler(research_store=_ResearchStore())
    context = await compiler.compile(_task(objective="what is the protocol"))

    text = "\n".join(message.text() for message in context.messages)
    assert "The protocol uses framed messages." in text
    research_messages = [
        message
        for message in context.messages
        if message.provenance and message.provenance.source_id == "src-1"
    ]
    assert research_messages


@pytest.mark.asyncio
async def test_compiler_reuses_revisioned_static_context_until_store_changes():
    research = _RevisionedResearchStore()
    compiler = ContextCompiler(research_store=research)
    task = _task(objective="cached research")

    await compiler.compile(task)
    await compiler.compile(task)
    assert research.calls == 1

    research.generation += 1
    await compiler.compile(task)
    assert research.calls == 2


@pytest.mark.asyncio
async def test_compile_exposes_strategy_selection_record_for_kernel_evidence():
    task = _task(
        objective="run a shadow experiment", workspace=WorkspaceSpec(id="repo", root="/tmp/repo")
    )
    context = await ContextCompiler().compile(task)
    record = context.strategy_selection_record
    assert record is not None
    payload = record.to_record()
    assert payload["selected_by"] == "deterministic_advisory_strategy"
    assert payload["workspace_baseline"]["workspace_id"] == "repo"
    assert "sequential" in payload["viable_alternatives"]
