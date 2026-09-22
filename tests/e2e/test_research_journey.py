"""Real local discovery/capture/evidence journey for the research fabric."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from athena.artifacts.store import ArtifactStore
from athena.capabilities.research import HttpDiscoveryProvider, ResearchCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.tasks import NetworkPolicy
from athena.research.policy import SourcePolicy
from athena.research.store import ResearchStore
from athena.state.database import Database


class _ResearchFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = urlsplit(self.path).path
        if path == "/search":
            payload = {
                "results": [
                    {"uri": f"{base}/doc-a", "title": "Primary status"},
                ]
            }
            content_type = "application/json"
        elif path == "/doc-a":
            payload = "status=ready\nrelease=2026-09-07\n"
            content_type = "text/plain"
        elif path == "/doc-b":
            payload = "status=blocked\nrelease=2026-09-07\n"
            content_type = "text/plain"
        else:
            self.send_error(404)
            return
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.mark.athena_capability("TCP_LOOPBACK")
@pytest.mark.dsh_release
@pytest.mark.athena_evidence("e2e", "security")
@pytest.mark.asyncio
async def test_local_research_discover_capture_evidence_and_contradiction(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResearchFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = Database(":memory:")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = ResearchStore(db)

    def resolver(host, port, **kwargs):
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    policy = SourcePolicy(allowed_domains=("127.0.0.1",), allow_private_network=True)
    provider = HttpDiscoveryProvider(
        f"http://127.0.0.1:{server.server_port}/search",
        source_policy=policy,
        host_resolver=resolver,
    )
    capability = ResearchCapability(
        store,
        artifact_store=artifacts,
        source_policy=policy,
        host_resolver=resolver,
        discovery_providers=(provider,),
    )
    context = SimpleNamespace(
        workspace=SimpleNamespace(id="research-fixture", network_policy=NetworkPolicy.ALLOW)
    )

    async def invoke(call_id: str, arguments: dict):
        result = await capability.invoke(
            CapabilityRequest(
                capability_id="research",
                task_id="research-task",
                session_id="research-session",
                call_id=call_id,
                arguments=arguments,
            ),
            context=context,
        )
        assert result.status is CapabilityResultStatus.OK, result.error
        return json.loads(result.output)

    try:
        discovered = await invoke(
            "discover",
            {"operation": "discover", "query": "release status", "limit": 5},
        )
        assert discovered["candidates"], discovered
        assert discovered["candidates"][0]["origin"] == "http-index"
        assert discovered["network_used"] is True

        first = await invoke(
            "fetch-a",
            {"operation": "fetch", "uri": discovered["candidates"][0]["uri"]},
        )
        second = await invoke(
            "fetch-b",
            {
                "operation": "fetch",
                "uri": f"http://127.0.0.1:{server.server_port}/doc-b",
            },
        )
        first_source = first["source"]
        second_source = second["source"]
        assert first_source["artifact_uri"].startswith("artifact://")
        assert second_source["artifact_uri"].startswith("artifact://")

        first_evidence = await invoke(
            "evidence-a",
            {
                "operation": "record_evidence",
                "source_id": first_source["id"],
                "claim": "The release is ready.",
                "excerpt": "status=ready",
            },
        )
        assert (
            first_evidence["evidence"]["metadata"]["source_content_hash"]
            == first_source["content_hash"]
        )
        assert first_evidence["evidence"]["metadata"]["excerpt_hash"]
        second_evidence = await invoke(
            "evidence-b",
            {
                "operation": "record_evidence",
                "source_id": second_source["id"],
                "claim": "The release is blocked.",
                "excerpt": "status=blocked",
                "contradicts": [first_evidence["evidence"]["id"]],
            },
        )
        verified = await invoke(
            "verify-a",
            {"operation": "verify", "evidence_id": first_evidence["evidence"]["id"]},
        )
        assert verified["status"] == "verified"
        assert second_evidence["evidence"]["contradicts"] == [first_evidence["evidence"]["id"]]

        critique = await invoke(
            "critique",
            {"operation": "critique", "min_independent_groups": 2},
        )
        assert critique["contradiction_evidence_ids"] == [second_evidence["evidence"]["id"]]
        assert critique["single_group_warning"] is True
        assert critique["meets_independence_threshold"] is False

        bundle = await invoke("bundle", {"operation": "bundle", "limit": 10})
        assert {item["id"] for item in bundle["sources"]} == {
            first_source["id"],
            second_source["id"],
        }
        assert any(
            first_evidence["evidence"]["id"] in item["contradicts"] for item in bundle["evidence"]
        )
    finally:
        server.shutdown()
        server.server_close()
        await db.close()
