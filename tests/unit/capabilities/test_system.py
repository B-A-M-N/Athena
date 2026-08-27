"""Tests for machine introspection and process control."""

from __future__ import annotations

import pytest
import os


from athena.capabilities.system import MachineCapability, ProcessCapability
from athena.protocol.capabilities import CapabilityRequest


def _req(cap, op, **args):
    return CapabilityRequest(
        capability_id=cap,
        arguments={"operation": op, **args},
        task_id="t1",
    )


@pytest.mark.athena_scenario("BODY-006")
async def test_machine_overview():
    cap = MachineCapability()
    r = await cap.invoke(_req("machine", "overview"))
    assert "cpus" in (r.output or "")
    assert "mem total" in (r.output or "")


async def test_machine_toolchain_and_env():
    cap = MachineCapability()
    r = await cap.invoke(_req("machine", "toolchain"))
    assert "python3" in (r.output or "")
    r = await cap.invoke(_req("machine", "env", name="HOME"))
    assert "$HOME" in (r.output or "") or "/" in (r.output or "")


@pytest.mark.athena_scenario("BODY-006")
async def test_machine_env_redacts_secrets():
    cap = MachineCapability()
    os.environ["ATHENA_TEST_SECRET_KEY_XYZ"] = "supersecret"
    try:
        r = await cap.invoke(_req("machine", "env",
                                  name="ATHENA_TEST_SECRET_KEY_XYZ"))
        assert "redacted" in (r.output or "") and "supersecret" not in (r.output or "")
    finally:
        del os.environ["ATHENA_TEST_SECRET_KEY_XYZ"]


async def test_process_tree_and_usage():
    cap = ProcessCapability()
    r = await cap.invoke(_req("process", "tree", pid=1))
    assert "no such" not in (r.error or "")
    r = await cap.invoke(_req("process", "usage", pid=os.getpid()))
    assert str(os.getpid()) in (r.output or "")


async def test_process_signal_missing_pid():
    cap = ProcessCapability()
    r = await cap.invoke(_req("process", "signal", pid=999999999, signal="TERM"))
    assert "no such pid" in (r.error or "")
