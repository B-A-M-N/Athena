"""Typed research domain commands (review item 31)."""

from __future__ import annotations

import json

import pytest

from athena.research.commands import ResearchCommand, ResearchResult


def test_research_result_factories():
    ok = ResearchResult.success("sources", {"sources": []})
    assert ok.ok is True
    assert ok.operation == "sources"
    assert ok.payload == {"sources": []}
    assert ok.error is None

    failed = ResearchResult.failure("fetch", "store unavailable")
    assert failed.ok is False
    assert failed.error == "store unavailable"
    assert failed.payload == {}


@pytest.mark.asyncio
async def test_run_command_storeless_service_fails_cleanly():
    """store=None produces an explicit typed failure, never a crash."""
    from athena.research.service import ResearchService

    service = ResearchService(store=None)
    for op in ("sources", "definitely_not_real", "search"):
        result = await service.run_command(ResearchCommand(operation=op))
        assert result.ok is False, f"{op}: expected typed failure"
        assert result.error is not None
        assert result.operation == op


@pytest.mark.asyncio
async def test_run_command_json_payload_decoded():
    """A JSON output string from a handler becomes a structured payload."""
    from athena.research.commands import ResearchResult

    raw = json.dumps({"count": 3})
    result = ResearchResult.success("gaps", json.loads(raw))
    assert result.payload["count"] == 3
