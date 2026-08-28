import pytest

from athena.context.compiler import ContextCompiler
from athena.context.blocks import ContextBlock
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import AudioBlock, ImageBlock, Role, TrustClass
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
