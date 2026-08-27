"""Golden scenario: a full scripted task through the dual-pane surface.

One deterministic event script exercises conversation, execution, streaming,
progress, stderr, an approval, an artifact, and completion.  The machine
frame and calm-pane output are asserted semantically (not pixel-exact) so the
test survives intentional styling changes while still proving the integrated
behaviour (UI mission §32/§34).
"""

from __future__ import annotations

import asyncio
from io import StringIO

from athena.cli.dual_pane import DualPaneSurface
from athena.protocol.events import make_event

SCRIPT = [
    make_event("TaskStarted", {}),
    make_event("ModelDelta", {"text": "I'll check the repo and write a summary."}),
    make_event("ModelResponseCompleted", {}),
    make_event("CapabilityRequested",
               {"capability_id": "execute", "call_id": "c1",
                "arguments": {"language": "shell", "code": "git status"}}),
    make_event("CapabilityStarted", {"capability_id": "execute", "call_id": "c1"}),
    make_event("StdoutChunk", {"call_id": "c1", "data": "on branch main\n"}),
    make_event("StdoutChunk", {"call_id": "c1", "data": "scan 10%\rscan 88%\rscan 100%\n"}),
    make_event("StderrChunk", {"call_id": "c1", "data": "warn: large tree\n"}),
    make_event("CapabilityCompleted", {"capability_id": "execute", "call_id": "c1"}),
    make_event("CapabilityRequested",
               {"capability_id": "fs.write", "call_id": "c2",
                "arguments": {"operation": "write", "path": "summary.md"}}),
    make_event("ApprovalRequested",
               {"approval_id": "a1", "capability_id": "fs.write",
                "scopes": ["call", "task"], "reason": "write requires grant"}),
    make_event("ApprovalResolved", {"approval_id": "a1", "decision": "approved"}),
    make_event("CapabilityCompleted", {"capability_id": "fs.write", "call_id": "c2"}),
    make_event("ArtifactCreated", {"uri": "file:///ws/summary.md", "name": "summary.md"}),
    make_event("ModelDelta", {"text": "Done — summary written."}),
    make_event("ModelResponseCompleted", {}),
    make_event("TaskCompleted", {}),
]


def test_golden_full_task_scenario():
    out, err = StringIO(), StringIO()
    s = DualPaneSurface(output=out, error=err, interactive=False)

    async def go():
        for e in SCRIPT:
            await s.render_event(e)
        s.finish()

    asyncio.run(go())

    # ---- left pane: calm conversation ----------------------------------
    left = out.getvalue()
    assert "I'll check the repo and write a summary." in left
    assert "Done — summary written." in left
    assert "[task complete]" in left
    # conversation is not a firehose: raw scan progress never appears inline
    assert "scan 10%" not in left
    assert "scan 88%" not in left

    # ---- right pane: structured machine state --------------------------
    assert s.activity.active is None                    # all ops retired
    assert len(s.activity.recent) == 2                  # execute + fs.write
    assert s.activity.approval is None                  # approval cleaned up
    assert s.activity.artifacts[0].name == "summary.md"
    assert s.buddy.state in {"success", "idle"}

    frame = "\n".join(s.chassis_text())
    assert "ATHENA MACHINE" in frame
    assert "summary.md" in frame                        # artifact discoverable
    assert "git status" in frame                        # recent op visible
    assert "APPROVAL REQUIRED" not in frame             # no stuck approval

    # ---- stream viewport: progress collapsed, stderr flagged ------------
    snap = [ln for ln in s.stream.snapshot(10, 80) if ln.text]
    texts = [ln.text for ln in snap]
    assert "scan 100%" in texts
    assert "scan 10%" not in texts and "scan 88%" not in texts
    assert any(ln.err and "warn: large tree" in ln.text for ln in snap)
