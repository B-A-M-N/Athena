import pytest

from athena.capabilities.registry import CapabilityRegistry
from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.errors import CapabilityUnavailable, CapabilityValidationError


class _Executor:
    """Minimal executor exposing a CapabilityDescriptor attribute."""

    def __init__(self, descriptor: CapabilityDescriptor):
        self.descriptor = descriptor

    async def invoke(self, request, *, output_accumulator=None):
        return CapabilityResult(
            "call-1", request.capability_id, CapabilityResultStatus.OK, output="done"
        )


def _writer_descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id="files.write",
        description="write a file",
        input_schema={
            "allow_extra": False,
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        effects=frozenset({EffectClass.WRITE_LOCAL}),
    )


def test_register_and_resolve_by_id():
    desc = _writer_descriptor()
    reg = CapabilityRegistry()
    reg.register(_Executor(desc))

    assert reg.resolve("files.write") is desc


def test_resolve_unknown_raises():
    reg = CapabilityRegistry()
    with pytest.raises(CapabilityUnavailable):
        reg.resolve("does.not.exist")


def test_validate_rejects_missing_required_args():
    reg = CapabilityRegistry()
    reg.register(_Executor(_writer_descriptor()))

    with pytest.raises(CapabilityValidationError):
        reg.validate("files.write", {"path": "/tmp/x.txt"})


def test_validate_rejects_unknown_keys_when_not_allowed():
    reg = CapabilityRegistry()
    reg.register(_Executor(_writer_descriptor()))

    with pytest.raises(CapabilityValidationError):
        reg.validate(
            "files.write",
            {"path": "/tmp/x.txt", "content": "hi", "bogus": 1},
        )


def test_list_available_returns_registered_descriptors():
    a = CapabilityDescriptor(id="files.read", description="read", input_schema={})
    b = CapabilityDescriptor(id="files.write", description="write", input_schema={})
    reg = CapabilityRegistry()
    reg.register(_Executor(a))
    reg.register(_Executor(b))

    ids = [d.id for d in reg.list_available()]
    assert sorted(ids) == ["files.read", "files.write"]

    unavailable = CapabilityDescriptor(
        id="gone",
        description="unavailable",
        input_schema={},
        availability=Availability("unavailable"),
    )
    reg.register(_Executor(unavailable))
    # Unfiltered returns everything; filtered on availability excludes "gone".
    assert any(d.id == "gone" for d in reg.list_available())
    assert reg.list_available(availability=unavailable.availability) == [unavailable]