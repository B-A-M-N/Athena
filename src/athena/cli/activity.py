"""OI activity model — structured projection of operational events.

The right pane must answer two questions instantly (UI mission §19):

* **What is Athena doing now?**   → the ACTIVE OPERATION section
* **What just happened?**         → the RECENT ACTIVITY section

This module derives that view from the canonical event stream.  It owns no
execution state: every record here is a projection of authoritative kernel
events, keyed by the identifiers those events already carry.

Key invariants:

* ONE logical operation is updated in place across its lifecycle
  (requested → started → progress → completed/failed) — never appended once
  per lifecycle event (mission §16/§25);
* lifecycle chatter (TaskStateChanged etc.) updates status, never adds rows;
* output volume is bounded for *presentation* only — the durable event log
  is untouched;
* approvals, artifacts, and background/child tasks are first-class regions,
  not log lines.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ActivityModel",
    "Operation",
    "OpState",
    "PendingApproval",
    "ArtifactNote",
    "BackgroundTask",
]


class OpState:
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"      # paused on approval
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = frozenset({OpState.DONE, OpState.FAILED, OpState.CANCELLED})

# How many completed operations stay visible in RECENT ACTIVITY.
_RECENT_LIMIT = 8
# How many output lines an operation keeps for presentation.
_OUTPUT_TAIL = 6


@dataclass
class Operation:
    """One logical capability/execution, updated in place."""

    key: str
    capability: str
    summary: str = ""          # first line of code / command / description
    language: str = ""
    state: str = OpState.PENDING
    detail: str = ""           # error reason, exit status, progress note
    output_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_OUTPUT_TAIL))
    output_dropped: int = 0    # lines elided by the tail bound
    has_artifact: bool = False

    def feed_output(self, text: str, *, err: bool = False) -> None:
        for line in text.split("\n"):
            if line == "":
                continue
            if len(self.output_tail) == self.output_tail.maxlen:
                self.output_dropped += 1
            self.output_tail.append(("! " if err else "") + line)

    @property
    def glyph(self) -> str:
        return {
            OpState.PENDING: "·",
            OpState.RUNNING: "▸",
            OpState.WAITING: "⏸",
            OpState.DONE: "✓",
            OpState.FAILED: "✗",
            OpState.CANCELLED: "⊘",
        }[self.state]

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL


@dataclass
class PendingApproval:
    approval_id: str
    capability: str
    scopes: list[str]
    reason: str = ""


@dataclass
class ArtifactNote:
    ref: str
    name: str = ""
    operation: str = ""        # capability that produced it


@dataclass
class BackgroundTask:
    task_id: str
    label: str
    state: str = "running"     # running | done | failed
    needs_attention: bool = False


class ActivityModel:
    """Derived OI view-model fed by the canonical event stream."""

    def __init__(self) -> None:
        self.active: Operation | None = None
        self.recent: deque[Operation] = deque(maxlen=_RECENT_LIMIT)
        self.approval: PendingApproval | None = None
        self.artifacts: deque[ArtifactNote] = deque(maxlen=4)
        self.background: dict[str, BackgroundTask] = {}
        self.task_status: str = "idle"
        self._ops_by_key: dict[str, Operation] = {}
        self._last_capability: str = "capability"

    # ------------------------------------------------------------------
    # Operation lookup / creation
    # ------------------------------------------------------------------
    def _op_key(self, payload: Mapping[str, Any]) -> str:
        for k in ("call_id", "capability_call_id", "execution_id", "runtime_session_id"):
            v = payload.get(k)
            if v:
                return str(v)
        return "current"

    def _op_for(self, payload: Mapping[str, Any], *, create: bool) -> Operation | None:
        key = self._op_key(payload)
        op = self._ops_by_key.get(key)
        if op is None and create:
            cap = str(payload.get("capability_id") or self._last_capability)
            op = Operation(key=key, capability=cap)
            self._ops_by_key[key] = op
            self._promote(op)
        return op

    def _promote(self, op: Operation) -> None:
        """Make ``op`` the active operation; archive the previous one."""
        if self.active is op:
            return
        if self.active is not None and not self.active.terminal:
            # Superseded without a terminal event (shouldn't normally
            # happen): close it honestly rather than dropping it.
            self.active.state = OpState.CANCELLED
            self.active.detail = "superseded"
        if self.active is not None:
            self.recent.appendleft(self.active)
            self._ops_by_key.pop(self.active.key, None)
        self.active = op

    def _retire(self, op: Operation) -> None:
        """Move a finished active operation into RECENT ACTIVITY."""
        if self.active is op:
            self.active = None
            self.recent.appendleft(op)
            self._ops_by_key.pop(op.key, None)
        elif op.terminal and op not in self.recent:
            self.recent.appendleft(op)

    # ------------------------------------------------------------------
    # Event ingestion — the single routing table for the OI pane
    # ------------------------------------------------------------------
    def observe(self, event_type: str, payload: Mapping[str, Any]) -> None:
        payload = payload or {}

        if event_type == "CapabilityRequested":
            cap = str(payload.get("capability_id") or "capability")
            self._last_capability = cap
            op = self._op_for(payload, create=True)
            assert op is not None
            op.capability = cap
            op.state = OpState.PENDING
            args = payload.get("arguments") or {}
            code = str(args.get("code") or "")
            if code:
                op.language = str(args.get("language") or "")
                op.summary = code.splitlines()[0][:120] if code.splitlines() else ""
            elif args:
                action = args.get("operation") or args.get("action")
                op.summary = str(action or next(iter(args.values()), ""))[:120]

        elif event_type == "CapabilityValidated":
            op = self._op_for(payload, create=False)
            if op:
                op.detail = "validated"

        elif event_type == "PolicyDecisionMade":
            op = self._op_for(payload, create=False)
            if op:
                decision = payload.get("decision") or payload.get("verdict")
                if decision:
                    op.detail = f"policy: {decision}"

        elif event_type == "CapabilityStarted":
            op = self._op_for(payload, create=True)
            assert op is not None
            op.state = OpState.RUNNING
            cap = payload.get("capability_id")
            if cap:
                op.capability = str(cap)
                self._last_capability = str(cap)

        elif event_type == "CapabilityProgress":
            op = self._op_for(payload, create=False)
            if op:
                note = payload.get("message") or payload.get("note") or payload.get("status")
                if note:
                    op.detail = str(note)[:80]

        elif event_type in {"CapabilityCompleted", "CapabilityFailed"}:
            op = self._op_for(payload, create=True)
            assert op is not None
            if event_type == "CapabilityCompleted":
                op.state = OpState.DONE
                out = str(payload.get("output") or "")
                if out.strip():
                    op.feed_output(out)
            else:
                op.state = OpState.FAILED
                reason = payload.get("reason") or payload.get("error") or "failed"
                op.detail = str(reason)[:120]
            self._retire(op)

        elif event_type in {"ExecutionStarted", "RuntimeSessionCreated"}:
            op = self._op_for(payload, create=True)
            assert op is not None
            if op.state == OpState.PENDING:
                op.state = OpState.RUNNING
            if not op.summary:
                op.summary = f"{payload.get('runtime') or 'runtime'} session"

        elif event_type == "StdoutChunk":
            op = self._op_for(payload, create=True)
            assert op is not None
            op.state = OpState.RUNNING
            op.feed_output(str(payload.get("data") or ""))

        elif event_type == "StderrChunk":
            op = self._op_for(payload, create=True)
            assert op is not None
            op.state = OpState.RUNNING
            op.feed_output(str(payload.get("data") or ""), err=True)

        elif event_type in {"ExecutionExited", "ExecutionTimedOut",
                            "ExecutionInterrupted"}:
            op = self._op_for(payload, create=False)
            if op is not None and not op.terminal:
                if event_type == "ExecutionExited":
                    code = payload.get("exit_code")
                    if code in (0, None):
                        op.detail = str(payload.get("exit_status") or "exited").lower()
                    else:
                        op.state = OpState.FAILED
                        op.detail = f"exit {code}"
                elif event_type == "ExecutionTimedOut":
                    op.state = OpState.FAILED
                    op.detail = "timed out"
                else:
                    op.state = OpState.CANCELLED
                    op.detail = "interrupted"
                if op.terminal:
                    self._retire(op)

        elif event_type == "ApprovalRequested":
            aid = payload.get("approval_id")
            if aid:
                self.approval = PendingApproval(
                    approval_id=str(aid),
                    capability=str(payload.get("capability_id") or self._last_capability),
                    scopes=[str(s) for s in payload.get("scopes") or () if s] or ["call"],
                    reason=str(payload.get("reason") or payload.get("policy") or ""),
                )
                op = self._op_for(payload, create=False) or self.active
                if op is not None and not op.terminal:
                    op.state = OpState.WAITING

        elif event_type == "ApprovalResolved":
            decision = payload.get("decision") or payload.get("status") or "resolved"
            op = self.active
            if op is not None and op.state == OpState.WAITING:
                granted = str(decision).lower() in {"approved", "granted", "allow", "allowed", "true"}
                op.state = OpState.RUNNING if granted else OpState.CANCELLED
                if not granted:
                    op.detail = "denied"
                    self._retire(op)
            self.approval = None

        elif event_type == "ArtifactCreated":
            ref = payload.get("uri") or payload.get("artifact_uri") or payload.get("artifact_ref")
            name = payload.get("name") or payload.get("filename") or ""
            self.artifacts.appendleft(ArtifactNote(
                ref=str(ref or "created"),
                name=str(name),
                operation=self._last_capability,
            ))
            if self.active is not None:
                self.active.has_artifact = True

        elif event_type == "ChildTaskCreated":
            tid = str(payload.get("child_task_id") or payload.get("task_id") or "?")
            label = str(payload.get("objective") or payload.get("prompt") or "delegated task")[:60]
            self.background[tid] = BackgroundTask(task_id=tid, label=label)

        elif event_type == "ChildTaskCompleted":
            tid = str(payload.get("child_task_id") or payload.get("task_id") or "?")
            bg = self.background.get(tid)
            if bg:
                ok = payload.get("status") in (None, "COMPLETED", "completed", "success")
                bg.state = "done" if ok else "failed"
                bg.needs_attention = not ok

        elif event_type == "TaskBlocked":
            op = self.active
            if op is not None and not op.terminal:
                op.state = OpState.WAITING
                op.detail = str(payload.get("reason") or "blocked")[:80]

        elif event_type == "TaskStateChanged":
            status = str(payload.get("status") or payload.get("to") or "").upper()
            if status == "WAITING_APPROVAL" and self.active is not None:
                if not self.active.terminal:
                    self.active.state = OpState.WAITING
            elif status == "RUNNING" and self.active is not None:
                if self.active.state == OpState.WAITING and self.approval is None:
                    self.active.state = OpState.RUNNING

        elif event_type in {"TaskCompleted", "TaskPartial", "TaskFailed",
                            "TaskCancelled", "TaskInterrupted"}:
            self.task_status = {
                "TaskCompleted": "completed",
                "TaskPartial": "partial",
                "TaskFailed": "failed",
                "TaskCancelled": "cancelled",
                "TaskInterrupted": "interrupted",
            }[event_type]
            op = self.active
            if op is not None and not op.terminal:
                op.state = (
                    OpState.DONE if event_type in {"TaskCompleted", "TaskPartial"}
                    else OpState.CANCELLED if event_type in {"TaskCancelled", "TaskInterrupted"}
                    else OpState.FAILED
                )
                self._retire(op)

        elif event_type == "TaskStarted":
            self.task_status = "running"

    # ------------------------------------------------------------------
    # Queries for the renderer
    # ------------------------------------------------------------------
    def current_label(self) -> str:
        """One-line answer to 'what is Athena doing now?'."""
        if self.approval is not None:
            return f"awaiting approval · {self.approval.capability}"
        op = self.active
        if op is None:
            if self.task_status == "running":
                return "thinking"
            return self.task_status
        if op.state == OpState.WAITING:
            return f"paused · {op.capability}"
        if op.state == OpState.RUNNING:
            return f"{op.capability} · {op.summary or 'running'}"
        return f"{op.capability} · {op.state}"

    def reset_for_new_task(self) -> None:
        """Deterministic cleanup between tasks (no stale approvals/ops)."""
        if self.active is not None and not self.active.terminal:
            self.active.state = OpState.CANCELLED
            self.active.detail = "superseded"
            self._retire(self.active)
        self.active = None
        self.approval = None
        self.background.clear()
        self.task_status = "idle"
        self._ops_by_key.clear()
