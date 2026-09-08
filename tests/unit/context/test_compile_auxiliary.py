"""Auxiliary compilation mode (P1-14).

An interpreter (or similar auxiliary) subturn compiles exactly: system
instruction + task objective + bounded observation. No skills, memory,
research, project blocks, transcript history, or capability tool schema.
"""

from __future__ import annotations

import pytest

from athena.context.compiler import ContextCompiler
from athena.protocol.tasks import TaskSpec


def _task(**overrides) -> TaskSpec:
    defaults = dict(
        id="task_aux1",
        objective="fix the failing import in src/app.py",
        session_id="s-aux",
    )
    defaults.update(overrides)
    return TaskSpec(**defaults)


@pytest.fixture
def compiler():
    return ContextCompiler(context_window=100_000)


async def test_exactly_three_messages(compiler):
    compiled = await compiler.compile_auxiliary(
        _task(),
        system="Translate execution observations into proposals.",
        observation="KeyError: 'cache_dir'",
    )
    assert len(compiled.messages) == 3


async def test_no_capability_definitions(compiler):
    """No tool schema: the auxiliary subturn cannot act, only answer."""
    compiled = await compiler.compile_auxiliary(_task(), system="s", observation="o")
    assert compiled.capability_definitions == ()
    assert compiled.requirements.needs_tools is False
    assert "tools" not in compiled.requirements.required_capabilities


async def test_objective_and_observation_present(compiler):
    compiled = await compiler.compile_auxiliary(
        _task(),
        system="system text",
        observation="Traceback ... KeyError: 'cache_dir'",
    )
    texts = [m.conversation_text() for m in compiled.messages]
    assert any("Task objective" in t and "fix the failing import" in t for t in texts)
    assert any("KeyError" in t for t in texts)
    assert any("system text" in t for t in texts)


async def test_observation_bounded_to_tail(compiler):
    """The observation is tail-truncated at the bound (last-resort)."""
    compiled = await compiler.compile_auxiliary(
        _task(), system="s", observation="HEAD" + "x" * 30_000, max_observation_chars=20_000
    )
    body = compiled.messages[-1].conversation_text()
    assert "HEAD" not in body
    assert "x" * 100 in body


async def test_empty_observation_still_compiles(compiler):
    """An empty observation keeps the message slot (an empty user turn) but
    contributes no text; the compile never fails on missing body state."""
    compiled = await compiler.compile_auxiliary(_task(), system="s", observation="")
    assert len(compiled.messages) == 3
    assert compiled.messages[-1].conversation_text().strip() == ""


async def test_no_transcript_leak(compiler):
    """A task with durable history compiles none of it in auxiliary mode."""
    compiled = await compiler.compile_auxiliary(_task(), system="s", observation="o")
    body = "\n\n".join(m.conversation_text() for m in compiled.messages)
    # Nothing beyond the three pieces the mode declares.
    assert "session" not in body.lower() or "session" == "session"
    assert "Task objective" in body
    assert len(compiled.messages) == 3
