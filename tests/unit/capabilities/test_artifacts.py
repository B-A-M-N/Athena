from __future__ import annotations

import json
from contextlib import asynccontextmanager
import hashlib

import pytest

from athena.artifacts.store import ArtifactStore
from athena.capabilities.artifacts import ArtifactCapability
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus


class _MemoryArtifacts:
    def __init__(self) -> None:
        owned_uri = (
            "artifact://sha256/"
            + hashlib.sha256(b"first line\nimportant finding\nlast line\n").hexdigest()
        )
        secret_uri = "artifact://sha256/" + hashlib.sha256(b"secret").hexdigest()
        self.content = {
            owned_uri: b"first line\nimportant finding\nlast line\n",
            secret_uri: b"secret",
        }
        self.load_calls = 0
        self.stream_calls = 0
        self.refs = {
            "task-1": [
                ArtifactRef(
                    id=owned_uri,
                    uri=owned_uri,
                    hash=hashlib.sha256(self.content[owned_uri]).hexdigest(),
                    mime_type="text/plain",
                    size=len(self.content[owned_uri]),
                    producer="execute",
                    task_id="task-1",
                )
            ],
            "task-owner": [
                ArtifactRef(
                    id=secret_uri,
                    uri=secret_uri,
                    hash=hashlib.sha256(self.content[secret_uri]).hexdigest(),
                    size=len(self.content[secret_uri]),
                    task_id="task-owner",
                )
            ],
        }

    async def list(self, *, task_id=None, limit=100):
        return self.refs.get(task_id, [])[:limit]

    async def load(self, uri):
        self.load_calls += 1
        return self.content[uri]

    @asynccontextmanager
    async def open_stream(self, uri):
        self.stream_calls += 1

        async def chunks():
            data = self.content[uri]
            for index in range(0, len(data), 3):
                yield data[index : index + 3]

        yield chunks()


@pytest.mark.asyncio
async def test_artifact_capability_lists_reads_and_searches_task_output(tmp_path):
    store = _MemoryArtifacts()
    ref = store.refs["task-1"][0]
    capability = ArtifactCapability(store)

    listed = await capability.invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-1",
            call_id="list",
            arguments={"operation": "list"},
        )
    )
    assert listed.status is CapabilityResultStatus.OK
    assert json.loads(listed.output)["artifacts"][0]["uri"] == ref.uri

    read = await capability.invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-1",
            call_id="read",
            arguments={
                "operation": "slice",
                "artifact_uri": ref.uri,
                "offset": 6,
                "limit": 8,
            },
        )
    )
    assert json.loads(read.output)["content"] == "line\nimp"

    search = await capability.invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-1",
            call_id="search",
            arguments={
                "operation": "search",
                "artifact_uri": ref.uri,
                "query": "finding",
            },
        )
    )
    result = json.loads(search.output)
    assert result["matches"][0]["line"] == 2
    assert "important finding" in result["matches"][0]["text"]
    assert store.load_calls == 0
    assert store.stream_calls >= 2


@pytest.mark.asyncio
async def test_artifact_uri_does_not_bypass_task_ownership(tmp_path):
    store = _MemoryArtifacts()
    ref = store.refs["task-owner"][0]
    capability = ArtifactCapability(store)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-other",
            call_id="read",
            arguments={"operation": "read", "artifact_uri": ref.uri},
        )
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "not visible" in (result.error or "")


@pytest.mark.asyncio
async def test_extract_is_bounded_and_retains_source_provenance(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = await store.save(
        task_id="task-1",
        content="0123456789abcdef",
        mime_type="text/plain",
        producer="execute",
    )
    result = await ArtifactCapability(store).invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-1",
            call_id="extract",
            arguments={"operation": "extract", "artifact_uri": ref.uri, "max_bytes": 5},
        )
    )

    assert result.status is CapabilityResultStatus.OK
    extracted = json.loads(result.output)
    assert extracted["text"] == "01234"
    assert extracted["truncated"] is True
    assert extracted["metadata"]["source_uri"] == ref.uri
    assert extracted["metadata"]["source_hash"] == ref.hash
    assert extracted["derived_artifact"]["metadata"]["source_uri"] == ref.uri
    assert extracted["derived_artifact"]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_extract_json_is_deterministic(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = await store.save(
        task_id="task-json",
        content='{"z": 1, "a": [true, false]}',
        mime_type="application/json",
    )
    result = await ArtifactCapability(store).invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-json",
            call_id="extract-json",
            arguments={"operation": "extract", "artifact_uri": ref.uri},
        )
    )

    extracted = json.loads(result.output)
    assert extracted["text"] == '{"a":[true,false],"z":1}'
    assert extracted["metadata"]["parse_status"] == "parsed"
    assert extracted["derived_artifact"]["mime_type"] == "application/json"


@pytest.mark.asyncio
async def test_extract_binary_is_opaque_and_does_not_create_text_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = await store.save(task_id="task-bin", content=b"\x00\xff\x01", mime_type="image/png")
    result = await ArtifactCapability(store).invoke(
        CapabilityRequest(
            capability_id="artifacts",
            task_id="task-bin",
            call_id="extract-bin",
            arguments={"operation": "extract", "artifact_uri": ref.uri},
        )
    )

    extracted = json.loads(result.output)
    assert extracted["text"] == ""
    assert extracted["derived_artifact"] is None
    assert extracted["metadata"]["binary"] is True
    assert extracted["metadata"]["bytes"] == 3
