"""``athena inspect`` — deep task observability (BUILDSPEC §99).

Renders every facet of a task that survives in the system so a human can
reconstruct *why Athena did this*: status, parent/children, budget/usage, the
result object, and a chronological event timeline.

This is a pure read-only renderer. All data comes from the documented
``AthenaService`` accessors (get_task / get_result / inspect / stream_events) —
the inspection never mutates anything.
"""

from __future__ import annotations

import sys
from typing import Any

from athena.cli import chat
from athena.protocol.events import Event

_SECTIONS = "—" * 60


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def _num(n: Any) -> str:
    return "" if n is None else str(n)


async def _get_task(service: Any, task_id: Any):
    getter = getattr(service, "get_task", None)
    if getter is None:
        return None
    try:
        return await getter(task_id)
    except (NotImplementedError, AttributeError):
        return None


async def _get_events(service: Any, task_id: Any) -> list[Event]:
    stream = getattr(service, "stream_events", None)
    if stream is None:
        return []
    events: list[Event] = []
    try:
        async for ev in stream(task_id):
            if ev is not None:
                events.append(ev)
    except (NotImplementedError, AttributeError):
        return []
    except Exception:  # pragma: no cover
        return events
    return events


async def _render_budget(task: Any, result: Any = None) -> None:
    budget = getattr(task, "resource_budget", None) or getattr(task, "budget", None)
    usage = None
    if result is not None:
        usage = getattr(result, "usage", None)
    if budget is None and usage is None:
        return
    print()
    print(chat.bold("Budget & Usage"))
    print(_SECTIONS)
    if budget is not None:
        for attr, label in (
            ("max_agent_iterations", "max_iterations"),
            ("max_input_tokens", "max_input_tokens"),
            ("max_output_tokens", "max_output_tokens"),
            ("max_cost_usd", "max_cost_usd"),
            ("max_wall_time", "max_wall_time"),
        ):
            val = getattr(budget, attr, None)
            if val is not None:
                print(f"  {label:<15}: {_fmt(val)}")
    if usage is not None:
        for attr, label in (
            ("input_tokens", "in_tokens"),
            ("output_tokens", "out_tokens"),
            ("model_calls", "model_calls"),
            ("cost_usd", "cost_usd"),
            ("duration_ms", "duration_ms"),
            ("executions", "executions"),
            ("mutations", "mutations"),
        ):
            val = getattr(usage, attr, None)
            print(f"  {label:<15}: {_fmt(val)}")
    else:
        print("  usage: <not available>")


def _render_result(result: Any, task_id: str) -> None:
    print()
    print(chat.bold("Result"))
    print(_SECTIONS)
    if result is None:
        print("  <no result>")
        return
    status = getattr(result, "status", None)
    print(f"  status    : {_fmt(status)}")
    summary = getattr(result, "summary", "") or ""
    if summary:
        print(f"  summary   : {summary}")
    for art in getattr(result, "artifacts", ()) or ():
        print(f"  artifact  : {_fmt(getattr(art, 'uri', art))}")
    for mut in getattr(result, "mutations", ()) or ():
        print(f"  mutation  : {_fmt(getattr(mut, 'resource', mut))} ({_fmt(getattr(mut, 'operation', ''))})")
    for un in getattr(result, "unresolved", ()) or ():
        print(f"  unresolved: {un}")
    evidence = getattr(result, "evidence", ()) or ()
    if evidence:
        print(f"  evidence  : {len(evidence)} item(s)")
    # render_result_summary summary line
    more = chat.render_summary(result)
    if more:
        print()
        for line in more.splitlines():
            print(f"  {line}")


def _render_events(events: list[Event]) -> None:
    print()
    print(chat.bold("Timeline"))
    print(_SECTIONS)
    if not events:
        print("  <no events>")
        return
    for ev in events:
        ts = getattr(ev, "timestamp", None)
        etype = getattr(ev, "type", "?")
        payload = getattr(ev, "payload", None) or {}
        time_str = ts.isoformat() if ts is not None else "?"
        line = f"  [{time_str}] {etype}"
        extra = ""
        if etype == "ModelDelta":
            extra = (payload.get("text", "") or "").replace("\n", " ")
            extra = extra[:100]
        elif etype == "CapabilityCall":
            cap = payload.get("capability_id", payload.get("capability", ""))
            extra = f"{cap}"
        elif etype == "CapabilityResult":
            cap = payload.get("capability_id", payload.get("capability", ""))
            ok = payload.get("ok", True)
            extra = f"{cap} ok={ok}"
        elif etype.startswith("TaskState") or etype == "TaskStateChanged":
            extra = f"{payload.get('status', '')}"
        if extra:
            line += f"  {chat.dim(extra)}"
        print(line)


async def run_inspect(service: Any, task_id: str, *, verbose: bool = False) -> int:
    """Render a full inspection of ``task_id``. Returns process exit code."""
    task = await _get_task(service, task_id)
    events = await _get_events(service, task_id)

    status = None
    if task is None:
        # Try a structured inspect() or a get() fallback.
        inspect_fn = getattr(service, "inspect", None)
        if inspect_fn is not None:
            try:
                task = await inspect_fn(task_id)
            except Exception:  # pragma: no cover
                task = None
    if task is None:
        print(f"task {task_id}: not found", file=sys.stderr)
        return 1

    print(chat.bold("Task"))
    print(_SECTIONS)
    print(f"  id        : {task_id}")
    objective = (
        getattr(task, "objective", None)
        or (getattr(task, "result", None) and getattr(task.result, "summary", None))
        or ""
    )
    status = getattr(task, "status", None) or getattr(getattr(task, "result", None), "status", None)
    if isinstance(status, str):
        print(f"  status    : {status}")
    elif status is not None:
        print(f"  status    : {_fmt(status)}")
    if objective:
        print(f"  objective : {objective}")
    for attr, label in (
        ("created_at", "created_at"),
        ("deadline", "deadline"),
        ("parent_task_id", "parent"),
        ("session_id", "session"),
    ):
        val = getattr(task, attr, None)
        if val is not None and val != "":
            print(f"  {label:<10}: {_fmt(val)}")
    children = getattr(task, "child_task_ids", None) or getattr(task, "children", None) or []
    if children:
        print(f"  {'children':<10}: {', '.join(str(c) for c in children)}")

    result = getattr(task, "result", None)
    if result is None:
        getter = getattr(service, "get_result", None)
        if getter is not None:
            try:
                result = await getter(task_id)
            except Exception:  # pragma: no cover
                result = None

    await _render_budget(task, result)
    _render_result(result, task_id)

    _render_events(events)

    return 0