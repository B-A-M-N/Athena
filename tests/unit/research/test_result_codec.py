from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.research.models import EvidenceObject, SourceRecord
from athena.research.result_codec import (
    artifact_visible,
    decode_object,
    evidence_visible,
    result,
    source_visible,
    strings,
    unique_candidates,
    unique_strings,
)


class _Artifacts:
    def __init__(self, uris):
        self.uris = uris

    async def list(self, *, task_id, limit):
        del limit
        from types import SimpleNamespace

        return [SimpleNamespace(uri=uri) for uri in self.uris]


async def test_codec_result_preserves_failure_truth():
    request = CapabilityRequest(capability_id="research", task_id="t", call_id="c", arguments={})
    assert result(request, ok=False, error="bad").status is CapabilityResultStatus.FAILED
    assert decode_object("{broken") == {}
    assert decode_object('[{"x":1}]') == {}
    assert strings([" a ", "", 3], limit=2) == ["a"]
    assert unique_strings(["a", " a ", "b"]) == ["a", "b"]
    assert unique_candidates(
        [
            {"source": {"id": "s"}, "rank": 2},
            {"source": {"id": "s"}, "rank": 1},
            {"rank": 3},
        ]
    ) == [{"source": {"id": "s"}, "rank": 2}, {"rank": 3}]


async def test_codec_visibility_is_task_or_project_scoped():
    request = CapabilityRequest(
        capability_id="research", task_id="task-a", call_id="c", arguments={}
    )
    context = type("Context", (), {"workspace": type("Workspace", (), {"id": "proj"})()})()
    task_source = SourceRecord.for_uri("https://example.test/a", content_hash="a", task_id="task-a")
    project_source = SourceRecord.for_uri(
        "https://example.test/b", content_hash="b", project_id="proj"
    )
    foreign_source = SourceRecord.for_uri(
        "https://example.test/c", content_hash="c", task_id="task-b"
    )
    assert source_visible(task_source, request, context)
    assert source_visible(project_source, request, context)
    assert not source_visible(foreign_source, request, context)

    async def lookup(source_id):
        return {"s-a": task_source, "s-b": project_source}.get(source_id)

    evidence = EvidenceObject.for_content(
        source_id="s-a",
        extracted_claim="claim",
        exact_supporting_excerpt="claim",
        task_id="task-a",
    )
    assert await evidence_visible(evidence, request, context, lookup)
    assert await artifact_visible(_Artifacts(["artifact://x"]), "artifact://x", "task-a")
    assert not await artifact_visible(_Artifacts(["artifact://y"]), "artifact://x", "task-a")
