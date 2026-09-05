"""MCP prompt parity tests (P1-18).

Prompts are the third MCP primitive (after tools and resources). The
contract under test:

* Discovery surfaces every connected server's prompts, deduplicated,
  tolerating dead servers.
* Materialization returns context blocks — never capabilities — carrying
  UNTRUSTED provenance (SourceType.MCP / TrustClass.UNTRUSTED), so the
  compiler may drop them under budget pressure like any other external
  content.
* The wrapper names the origin server and states the content is
  procedural context, not instruction — an assistant-authored prompt
  cannot impersonate a user instruction to the host.
"""

from __future__ import annotations

import pytest

from athena.mcp.client import MCPMessage, MCPPromptRef
from athena.mcp.prompts import MCPPromptProvider, mcp_prompt_provenance
from athena.protocol.messages import SourceType, TrustClass


class _FakeClient:
    """Stands in for MCPClient: only the provider's surface is needed."""

    def __init__(self, connection_id, prompts=None, messages=None, fail=False):
        self.connection_id = connection_id
        self._prompts = prompts or []
        self._messages = messages or []
        self._fail = fail
        self.requested: list[tuple[str, dict]] = []

    async def list_prompts(self):
        if self._fail:
            raise RuntimeError("server dead")
        return list(self._prompts)

    async def get_prompt(self, name, arguments):
        self.requested.append((name, dict(arguments or {})))
        if self._fail:
            raise RuntimeError("server dead")
        return list(self._messages)


def _ref(name, server, description="", arguments=()):
    return MCPPromptRef(
        name=name, description=description, arguments=tuple(arguments), server=server
    )


@pytest.mark.athena_evidence("test", "unit")
async def test_discovery_unions_servers_and_tolerates_dead_ones():
    dead = _FakeClient("dead", fail=True)
    live_a = _FakeClient("a", prompts=[_ref("review", "a")])
    live_b = _FakeClient("b", prompts=[_ref("review", "b"), _ref("plan", "b")])
    provider = MCPPromptProvider({"dead": dead, "a": live_a, "b": live_b})

    refs = await provider.available()

    names = sorted((r.server, r.name) for r in refs)
    assert names == [("a", "review"), ("b", "plan"), ("b", "review")]


@pytest.mark.athena_evidence("test", "unit")
async def test_materialization_returns_untrusted_procedural_blocks():
    client = _FakeClient(
        "docs",
        messages=[
            MCPMessage(role="user", text="Summarize the attached report."),
            MCPMessage(role="assistant", text="I will need the file path."),
        ],
    )
    provider = MCPPromptProvider({"docs": client})

    blocks = await provider.render_prompt_blocks("summarize", {"file": "r.txt"})

    assert len(blocks) == 2
    for block in blocks:
        assert block.provenance is not None
        assert block.provenance.source_type == SourceType.MCP
        assert block.provenance.trust == TrustClass.UNTRUSTED
        assert block.provenance.source_id == "mcp:docs:prompt:summarize"
    # arguments reach the server; roles are preserved in the wrapper text
    assert client.requested == [("summarize", {"file": "r.txt"})]
    assert blocks[0].text.startswith("[MCP prompt 'summarize' from server 'docs'")
    assert "not an instruction" in blocks[0].text
    assert "user: Summarize the attached report." in blocks[0].text
    assert "assistant:" in blocks[1].text


@pytest.mark.athena_evidence("test", "unit")
async def test_empty_messages_produce_no_blocks():
    client = _FakeClient("solo", messages=[MCPMessage(role="user", text="   ")])
    provider = MCPPromptProvider({"solo": client})

    assert await provider.render_prompt_blocks("blank") == []


@pytest.mark.athena_evidence("test", "unit")
async def test_ambiguous_name_requires_connection_id():
    a = _FakeClient("a", prompts=[_ref("dup", "a")])
    b = _FakeClient("b", prompts=[_ref("dup", "b")],
                    messages=[MCPMessage(role="user", text="b's version")])
    provider = MCPPromptProvider({"a": a, "b": b})

    with pytest.raises(LookupError):
        await provider.render_prompt_blocks("dup")

    blocks = await provider.render_prompt_blocks("dup", connection_id="b")
    assert blocks and "from server 'b'" in blocks[0].text


@pytest.mark.athena_evidence("test", "unit")
async def test_single_client_resolves_without_connection_id():
    client = _FakeClient("only", messages=[MCPMessage(role="user", text="go")])
    provider = MCPPromptProvider({"only": client})

    blocks = await provider.render_prompt_blocks("anything")
    assert len(blocks) == 1


@pytest.mark.athena_evidence("test", "unit")
def test_provenance_is_always_untrusted_mcp():
    prov = mcp_prompt_provenance("srv", "p")
    assert prov.source_type == SourceType.MCP
    assert prov.trust == TrustClass.UNTRUSTED
    assert prov.scope == "mcp:prompt"
    assert prov.source_id == "mcp:srv:prompt:p"
