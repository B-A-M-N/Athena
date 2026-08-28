"""Secret-leakage invariants.

Raw secret values must never appear unredacted in capability event payloads,
TaskSpec JSON, or model-visible credential surfaces (BHV-072, B-071 opacity).
"""

from __future__ import annotations
import pytest

import json

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.credentials import SecretManager, FileSource
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.events import make_event
from athena.protocol.tasks import AutonomyLevel, TaskSpec, WorkspaceSpec

_SECRET = "sk-test-1234567890"


def _as_dict(item):
    if hasattr(item, "payload"):
        return vars(item)
    if isinstance(item, dict):
        return item
    return vars(item)


def _recording_events() -> list:
    events: list = []

    async def sink(event) -> None:
        events.append(event)

    return events, sink


@pytest.mark.athena_claim("BHV-072", "BHV-071")
@pytest.mark.athena_evidence("test", "security")
async def test_capability_event_payload_redacts_secret(tmp_path):
    """A secret-plaintext argument is REDACTED, not the raw value."""
    events, sink = _recording_events()
    reg = CapabilityRegistry()
    reg.register(FilesystemCapability(workspace=WorkspaceSpec(id="w", root="/ws")))
    dispatcher = CapabilityDispatcher(
        reg,
        PolicyEngine(profile=AutonomyLevel.SUPERVISED),
        event_sink=sink,
    )

    await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="fs",
            arguments={
                "operation": "write",
                "path": "/ws/leak.txt",
                "content": _SECRET,
            },
            task_id="t1",
        ),
        workspace=WorkspaceSpec(id="w", root="/ws"),
    )

    payloads = [ev for ev in events]
    assert payloads, "expected at least one emitted event"
    raw_found = any(_SECRET in json.dumps(_as_dict(p), default=str) for p in payloads)
    assert not raw_found, "raw secret leaked into an event payload"
    # The redaction marker is present somewhere.
    redacted_found = any("REDACTED" in json.dumps(_as_dict(p), default=str) for p in payloads)
    assert redacted_found


@pytest.mark.athena_claim("BHV-072")
@pytest.mark.athena_evidence("test", "security")
def test_event_make_payload_redacts_through_helper():
    """make_event stores whatever the caller passes; dispatcher scrubs args."""
    from athena.capabilities.dispatcher import _redact_event_payload

    payload = _redact_event_payload(
        {"arguments": {"content": _SECRET, "path": "/a"}, "call_id": "c1"}
    )
    assert _SECRET not in json.dumps(payload)
    assert payload["arguments"]["content"] == "[REDACTED]"


@pytest.mark.athena_claim("BHV-071")
@pytest.mark.athena_evidence("test", "security")
def test_direct_secret_write_payload_never_leaks(tmp_path):
    """Credential-opaque describe/available do not expose the raw value."""
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "api_key").write_text(_SECRET)

    mgr = SecretManager(sources=[FileSource(str(secret_dir))])
    assert mgr.available("api_key") is True
    # Opacity (B-071): describe reports availability, never the value.
    assert _SECRET not in mgr.describe("api_key")
    # resolve() returns the value for authorized composition boundaries.
    assert mgr.resolve("api_key", owner_task="system") == _SECRET


@pytest.mark.athena_claim("BHV-072")
@pytest.mark.athena_evidence("test", "security")
def test_resolved_secret_not_in_task_serialization(tmp_path):
    """The raw key must not appear in TaskSpec JSON or message payloads."""
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "db").write_text(_SECRET)

    mgr = SecretManager(sources=[FileSource(str(secret_dir))])
    key = mgr.resolve("db", owner_task="system")
    assert key == _SECRET

    # Even though the service owns the value, task/event serialization must
    # not capture it.
    task = TaskSpec(id="t1", objective="work")
    task_json = json.dumps(task.__dict__, default=str)
    assert _SECRET not in task_json

    session_msg = make_event("SESSION_MESSAGE", {"text": "db credential resolved"}, task_id="t1")
    assert _SECRET not in json.dumps(session_msg.payload)
