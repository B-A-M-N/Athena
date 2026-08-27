from __future__ import annotations

import json

import pytest

from athena.affordances.scratch import ScratchManager
from athena.capabilities.scratch import ScratchCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.tasks import WorkspaceSpec
from athena.synthesis.engine import SynthesisEngine


@pytest.mark.asyncio
async def test_scratch_runs_once_without_registering_or_promoting():
    scratch = ScratchManager()
    capability = ScratchCapability(SynthesisEngine(), scratch)
    request = CapabilityRequest(
        capability_id="scratch",
        task_id="task-1",
        call_id="scratch-1",
        arguments={
            "operation": "run",
            "purpose": "normalize records",
            "code": "def run(args):\n    return {'total': len(args['items'])}\n",
            "args": {"items": [1, 2, 3]},
        },
    )

    result = await capability.invoke(request)

    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output) == {"total": 3}
    program_id = result.metadata["scratch_id"]
    assert [p.id for p in scratch.for_task("task-1")] == [program_id]
    assert scratch.results(program_id)[0]["ok"] is True


@pytest.mark.asyncio
async def test_scratch_rejects_host_escape_before_execution():
    scratch = ScratchManager()
    capability = ScratchCapability(SynthesisEngine(), scratch)
    result = await capability.invoke(CapabilityRequest(
        capability_id="scratch",
        task_id="task-2",
        call_id="scratch-2",
        arguments={
            "operation": "run",
            "code": "import subprocess\ndef run(args):\n return subprocess.run(args)\n",
            "args": {},
        },
    ))

    assert result.status is CapabilityResultStatus.FAILED
    assert (
        "security" in (result.error or "")
        or "host/process" in (result.error or "")
    )


@pytest.mark.asyncio
async def test_scratch_can_read_task_workspace_without_host_path_access(tmp_path):
    (tmp_path / "input.txt").write_text("alpha", encoding="utf-8")
    scratch = ScratchManager()
    capability = ScratchCapability(SynthesisEngine(), scratch)
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="scratch",
            task_id="task-3",
            call_id="scratch-3",
            arguments={
                "operation": "run",
                "code": (
                    "def run(args):\n"
                    "    with open(args['path']) as f:\n"
                    "        return {'text': f.read()}\n"
                ),
                "args": {"path": "/workspace/input.txt"},
            },
        ),
        context=type("Context", (), {
            "workspace": WorkspaceSpec(id="repo", root=str(tmp_path)),
        })(),
    )

    assert result.status is CapabilityResultStatus.OK
    assert json.loads(result.output) == {"text": "alpha"}
