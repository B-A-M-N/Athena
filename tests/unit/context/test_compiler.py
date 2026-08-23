
import pytest
from athena.context.compiler import ContextCompiler
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.messages import Role
from athena.protocol.tasks import TaskSpec


def _task(objective: str = "do the thing", **kw) -> TaskSpec:
    return TaskSpec(id="task-1", objective=objective, **kw)


class _CapRegistry:
    def __init__(self, descriptors):
        self._descriptors = descriptors

    async def list_descriptors(self):
        return list(self._descriptors)


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