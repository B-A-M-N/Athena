"""Tests for the computational-body capabilities (P0)."""

from __future__ import annotations

import pytest

from athena.capabilities.terminal_session import TerminalSessionCapability
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.tasks import WorkspaceSpec


def _req(op: str, task_id=None, **args):
    return CapabilityRequest(
        capability_id="terminal_session",
        arguments={"operation": op, **args},
        task_id=task_id,
    )


def test_key_aliases_cover_navigation_and_function_keys():
    capability = TerminalSessionCapability()

    assert capability._escape_keys("up") == "\x1b[A"
    assert capability._escape_keys("PageDown") == "\x1b[6~"
    assert capability._escape_keys("F12") == "\x1b[24~"
    assert capability._escape_keys("C-c") == "\x03"


@pytest.fixture
def term():
    cap = TerminalSessionCapability()
    yield cap
    cap.close_all()


@pytest.mark.athena_scenario("BODY-001")
async def test_create_send_screen_kill(term, tmp_path):
    context = type("Context", (), {
        "workspace": WorkspaceSpec(id="w", root=str(tmp_path)),
    })()
    r = await term.invoke(
        _req("create", task_id="t1", command="bash --norc"), context=context)
    assert "created" in (r.output or "")
    sid = next(p for p in (r.output or "").split() if p.startswith("tty_"))

    r = await term.invoke(_req("send", task_id="t1", session=sid,
                               text="echo marker-$((21*2))"), context=context)
    assert "marker-42" in (r.output or "")

    # wait_for on fresh output
    await term.invoke(_req("send", task_id="t1", session=sid,
                           text="echo done-tag-xyz"), context=context)
    r = await term.invoke(_req("wait_for", task_id="t1", session=sid,
                               pattern="done-tag-xyz", timeout=5), context=context)
    assert r.metadata.get("matched") is True, (r.output or "")[-200:]

    r = await term.invoke(_req("kill", task_id="t1", session=sid), context=context)
    assert "terminated" in (r.output or "")


@pytest.mark.athena_scenario("BODY-001")
async def test_task_ownership_enforced(term, tmp_path):
    context = type("Context", (), {
        "workspace": WorkspaceSpec(id="w", root=str(tmp_path)),
    })()
    await term.invoke(_req("create", task_id="t1", command="bash --norc"),
                      context=context)
    sid = next(iter(term._sessions))
    # A different task must not touch the session.
    r = await term.invoke(_req("screen", task_id="t2", session=sid))
    assert "unowned" in (r.error or "")
