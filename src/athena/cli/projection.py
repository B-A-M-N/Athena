"""Canonical event-to-view projection shared by Athena surfaces.

The service/kernel remain authoritative.  ``ProjectionState`` is a read-only
presentation reducer: it keeps enough semantic context for the operator well,
OI scene, history, and stream to agree without any renderer owning execution
state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.cli.terminal import sanitize_terminal_text


@dataclass
class OperationNode:
    id: str
    label: str = "operation"
    state: str = "requested"
    target: str = ""
    command: str = ""
    detail: str = ""
    progress: str = ""
    execution_id: str | None = None
    exit_code: int | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    error: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    artifact: str = ""


@dataclass
class ProjectionState:
    """Pure-ish reducer state used by both the dual surface and ``oi-stream``."""

    operations: dict[str, OperationNode] = field(default_factory=dict)
    execution_to_operation: dict[str, str] = field(default_factory=dict)
    active_operation_id: str | None = None
    last_operation_id: str | None = None
    chat: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=160))
    recent: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=24))
    stream: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    stream_partial: str = ""
    pending_approval: dict[str, Any] | None = None
    status: str = "READY"
    status_message: str = "Type a request below."
    semantic_state: str = "idle"
    thinking: bool = False
    event_count: int = 0
    raw_events: deque[tuple[str, Mapping[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=128)
    )

    def add_recent(self, glyph: str, text: object) -> None:
        clean = sanitize_terminal_text(text).strip()
        if clean and (not self.recent or self.recent[-1] != (glyph, clean)):
            self.recent.append((glyph, clean))

    def add_chat(self, role: str, text: object) -> None:
        clean = sanitize_terminal_text(text).strip()
        if clean:
            self.chat.append({"role": role, "text": clean})

    def feed_stream(self, text: object, *, prefix: str = "") -> None:
        clean = sanitize_terminal_text(text)
        # A chunk may end in the middle of a line. Prefix only the beginning
        # of that logical line so split stderr chunks do not become
        # ``[err] first[err] second`` in the shared stream.
        if prefix and not self.stream_partial:
            clean = prefix + clean
        self.stream_partial += clean
        while "\n" in self.stream_partial:
            line, _, self.stream_partial = self.stream_partial.partition("\n")
            self.stream.append(line)

    def seal_stream(self) -> None:
        if self.stream_partial:
            self.stream.append(self.stream_partial)
            self.stream_partial = ""

    def acknowledge_approval(self, *, granted: bool, scope: str | None = None) -> None:
        """Project a local approval choice before its durable event arrives.

        Approval input is an interaction, not a second source of task state.
        Keeping the optimistic transition here lets every frontend repaint
        consistently while the service is resuming the task; the subsequent
        ``ApprovalResolved`` event remains authoritative and may refine it.
        """
        approval = self.pending_approval
        self.pending_approval = None
        if approval is not None and scope:
            approval = dict(approval)
            approval["selected_scope"] = scope
        if granted:
            self.status = "EXECUTING"
            self.status_message = "Approval accepted; resuming."
        else:
            self.status = "WARNING"
            self.status_message = "Approval denied."

    def ignore_approval_summary(self) -> None:
        """Discard a count-only approval summary after its request was handled."""
        self.pending_approval = None
        if self.status == "APPROVAL":
            self.status = "EXECUTING"
            self.status_message = "Approval accepted; resuming."

    def _operation(self, payload: Mapping[str, Any], *, create: bool = True) -> OperationNode | None:
        raw_id = payload.get("call_id") or payload.get("execution_id")
        op_id = str(raw_id or self.active_operation_id or self.last_operation_id or "operation")
        op_id = self.execution_to_operation.get(op_id, op_id)
        operation = self.operations.get(op_id)
        if operation is None and create:
            raw_args = payload.get("arguments")
            args = raw_args if isinstance(raw_args, Mapping) else {}
            command = sanitize_terminal_text(
                payload.get("command") or args.get("code") or args.get("command") or ""
            )
            target = sanitize_terminal_text(
                payload.get("target")
                or payload.get("resource")
                or payload.get("path")
                or args.get("path")
                or args.get("file")
                or args.get("resource")
                or args.get("url")
                or ""
            )
            operation = OperationNode(
                id=op_id,
                label=str(payload.get("capability_id") or payload.get("runtime") or "operation"),
                target=target,
                command=(command.splitlines() or [""])[0],
                detail=sanitize_terminal_text(args.get("operation") or args.get("action") or ""),
            )
            self.operations[op_id] = operation
            self.last_operation_id = op_id
            self.active_operation_id = op_id
            while len(self.operations) > 80:
                oldest = next(iter(self.operations))
                if oldest == self.active_operation_id:
                    break
                self.operations.pop(oldest, None)
        return operation

    def _close_active(self, state: str) -> None:
        operation = self.operations.get(self.active_operation_id or "")
        if operation is not None and operation.state in {
            "requested", "validated", "approval", "approved", "running",
        }:
            operation.state = state
        self.active_operation_id = None

    def reduce(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        """Apply one event. Raw payload is retained separately for audit/debug views."""
        payload = dict(payload or {})
        etype = str(event_type)
        self.event_count += 1
        self.raw_events.append((etype, payload))

        if etype in {"TaskCreated", "TaskQueued"}:
            self.status = "READY"
        elif etype == "TaskStarted":
            self.status, self.status_message, self.thinking = (
                "THINKING", "Athena is working through the request.", True
            )
        elif etype in {"ContextBuildStarted", "ContextBuilt", "ContextCompressed"}:
            self.status = "INSPECTING"
            self.add_recent("·", "Context compressed with provenance retained" if etype == "ContextCompressed" else "Context assembled")
        elif etype == "ModelRequestStarted":
            self.status, self.thinking = "THINKING", True
            provider = payload.get("provider") or "model"
            model = payload.get("model") or ""
            self.add_recent("·", f"Model request · {provider}/{model}".rstrip("/"))
        elif etype in {"ModelReasoningDelta", "TaskIterationStarted"}:
            self.status, self.thinking = "THINKING", True
        elif etype == "ModelDelta":
            self.status, self.thinking = "RESPONDING", False
            self.feed_stream(payload.get("text") or "")
        elif etype == "ModelResponseCompleted":
            self.thinking = False
            self.status = "RESPONDING"
            self.seal_stream()
        elif etype == "ModelRequestFailed":
            self.status, self.status_message, self.thinking = (
                "FAILURE", "The model request failed; inspect the error and retry.", False
            )
            self.add_recent("!", f"Model request failed · {payload.get('error') or payload.get('reason') or 'provider error'}")
        elif etype in {"SearchStarted", "ResearchStarted", "FileRead", "InspectionStarted"}:
            searching = etype in {"SearchStarted", "ResearchStarted"}
            self.status = "SEARCHING" if searching else "READING"
            label = payload.get("query") or payload.get("path") or payload.get("resource") or "workspace"
            self.add_recent("·", f"{'Search' if searching else 'Read'} · {label}")
        elif etype == "CapabilityRequested":
            operation = self._operation(payload)
            if operation:
                operation.state = "requested"
                raw_args = payload.get("arguments")
                args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
                code = sanitize_terminal_text(args.get("code") or args.get("command") or "")
                if code:
                    self.feed_stream(f"$ {code.splitlines()[0]}\n")
                self.status = "TOOLS"
                self.add_recent("·", f"{operation.label} requested")
        elif etype == "CapabilityValidated":
            operation = self._operation(payload, create=False)
            if operation:
                operation.state = "validated"
                self.add_recent("·", f"{operation.label} validated")
        elif etype == "PolicyDecisionMade":
            operation = self._operation(payload, create=False)
            decision = str(payload.get("decision") or "recorded").lower()
            if operation:
                operation.state = "approval" if decision in {"ask", "approval", "pending"} else decision
                operation.detail = sanitize_terminal_text(payload.get("reason") or operation.detail)
            if decision in {"deny", "denied"}:
                self.status, self.status_message = "WARNING", "Policy denied this operation."
            self.add_recent("!" if decision in {"deny", "denied"} else "·", f"Policy · {decision}")
        elif etype == "ApprovalRequested":
            operation = self._operation(payload)
            if operation:
                operation.state = "approval"
            if payload.get("approval_id") or self.pending_approval is None:
                self.pending_approval = payload
            self.status, self.status_message = "APPROVAL", "Paused until you choose a scope."
            self.add_recent("?", f"Approval required · {operation.label if operation else 'capability'}")
        elif etype == "ApprovalResolved":
            operation = self._operation(payload, create=False)
            decision = str(payload.get("decision") or payload.get("status") or "resolved").lower()
            denied = decision in {"deny", "denied", "rejected"}
            if operation:
                operation.state = "denied" if denied else "approved"
            self.pending_approval = None
            self.status = "WARNING" if denied else "TOOLS"
            self.add_recent("!" if denied else "✓", f"Approval {decision}")
        elif etype in {"CapabilityStarted", "CapabilityProgress", "CapabilityCompleted", "CapabilityFailed"}:
            operation = self._operation(payload)
            if operation:
                if etype == "CapabilityStarted":
                    operation.state, self.status = "running", "EXECUTING"
                elif etype == "CapabilityProgress":
                    operation.progress = sanitize_terminal_text(payload.get("message") or payload.get("progress") or "active")
                elif etype == "CapabilityCompleted":
                    operation.state = "complete"
                    output = sanitize_terminal_text(payload.get("output") or "")
                    if output:
                        operation.output.extend(output.splitlines() or [output])
                        self.feed_stream(output)
                    if self.active_operation_id == operation.id:
                        self.active_operation_id = None
                else:
                    operation.state = "failed"
                    operation.detail = sanitize_terminal_text(payload.get("reason") or payload.get("error") or "failed")
                    self.status, self.status_message = "FAILURE", f"{operation.label} failed; inspect the operation details."
                    self.add_recent("!", f"{operation.label} failed")
                    if self.active_operation_id == operation.id:
                        self.active_operation_id = None
        elif etype == "ExecutionStarted":
            operation = self._operation(payload)
            if operation:
                execution_id = str(payload.get("execution_id") or "")
                if execution_id:
                    operation.execution_id = execution_id
                    self.execution_to_operation[execution_id] = operation.id
                operation.state = "running"
                operation.command = operation.command or sanitize_terminal_text(payload.get("runtime") or "runtime")
            self.status = "EXECUTING"
            self.feed_stream(f"$ {payload.get('runtime') or 'runtime'}\n")
        elif etype == "RuntimeStateLost":
            self.status = "WARNING"
            self.status_message = "A runtime session was lost across restart; state was not guessed."
            self.add_recent(
                "!",
                f"Runtime state lost · {payload.get('runtime_session_id') or 'session'}",
            )
        elif etype in {"StdoutChunk", "StderrChunk"}:
            data = sanitize_terminal_text(payload.get("data") or "")
            operation = self._operation(payload, create=False)
            if operation and data:
                target = operation.error if etype == "StderrChunk" else operation.output
                target.extend(data.splitlines() or [data])
            if data:
                self.feed_stream(data, prefix="[err] " if etype == "StderrChunk" else "")
            self.status = "EXECUTING"
        elif etype in {"ExecutionExited", "ExecutionTimedOut", "ExecutionInterrupted"}:
            operation = self._operation(payload, create=False)
            if operation:
                operation.exit_code = payload.get("exit_code")
                operation.state = (
                    "timed out" if etype == "ExecutionTimedOut" else
                    "interrupted" if etype == "ExecutionInterrupted" else
                    "complete" if operation.exit_code in (None, 0) else "failed"
                )
                if self.active_operation_id == operation.id:
                    self.active_operation_id = None
                self.status = "SUCCESS" if operation.state == "complete" else "INTERRUPTED" if operation.state == "interrupted" else "FAILURE"
                if operation.state != "complete":
                    self.add_recent("!", f"{operation.label} {operation.state}")
        elif etype == "ArtifactCreated":
            operation = self._operation(payload, create=False)
            ref = payload.get("uri") or payload.get("artifact_uri") or payload.get("artifact_ref") or payload.get("name") or "artifact created"
            if operation:
                operation.artifact = sanitize_terminal_text(ref)
            else:
                self.add_recent("*", f"Artifact · {ref}")
        elif etype in {"ChildTaskCreated", "ChildTaskCompleted", "DelegationStarted", "BackgroundTaskStarted", "BackgroundTaskCompleted", "BackgroundTaskFailed"}:
            started = etype in {"ChildTaskCreated", "DelegationStarted", "BackgroundTaskStarted"}
            failed = etype == "BackgroundTaskFailed"
            self.status = "DELEGATED" if started else "FAILURE" if failed else self.status
            label = "Background work failed" if failed else "Delegated work started" if started else "Delegated work completed"
            self.add_recent("!" if failed else "↗" if started else "✓", label)
        elif etype in {"ToolRepaired", "MutationRecorded", "MutationRecordFailed", "MemoryCandidateCreated", "MemoryWritten", "SkillCandidateCreated", "SkillActivated", "InterpreterProposalDispatched", "ToolInputCorrectionExhausted", "RuntimeSessionCreated", "MutationRolledBack"}:
            labels = {
                "ToolRepaired": "Tool input repaired", "MutationRecorded": "Mutation recorded", "MutationRecordFailed": "Mutation record failed",
                "MemoryCandidateCreated": "Memory candidate captured", "MemoryWritten": "Knowledge saved", "SkillCandidateCreated": "Skill candidate captured",
                "SkillActivated": "Skill activated", "InterpreterProposalDispatched": "Computer proposal dispatched", "ToolInputCorrectionExhausted": "Tool repair budget exhausted",
                "RuntimeSessionCreated": "Runtime session created", "MutationRolledBack": "Mutation rolled back",
            }
            serious = etype in {"MutationRecordFailed", "ToolInputCorrectionExhausted"}
            if serious:
                self.status, self.status_message = "FAILURE", labels[etype]
            self.add_recent("!" if serious else "·", labels.get(etype, etype))
        elif etype == "TaskStateChanged":
            state = sanitize_terminal_text(payload.get("status") or payload.get("to") or "changed").upper()
            status_map = {
                "WAITING_APPROVAL": ("APPROVAL", "Paused for operator approval."), "WAITING_INPUT": ("WAITING", "Paused for operator input."),
                "BLOCKED": ("BLOCKED", "Task is blocked; inspect the reason."), "RECOVERY_REQUIRED": ("RECOVERING", "Task requires recovery."),
                "RUNNING": ("EXECUTING", "Task is running."),
            }
            self.status, self.status_message = status_map.get(state, (state, f"Task state: {state.lower()}."))
            self.thinking = state in {"RUNNING", "WAITING_INPUT"}
            self.add_recent("!" if state in {"BLOCKED", "RECOVERY_REQUIRED"} else "·", f"Task state · {state.lower()}")
        elif etype == "RecoveryStarted":
            self.status, self.status_message = "RECOVERING", "Recovering task state and retained evidence."
            self.add_recent("·", "Recovery started")
        elif etype == "RecoveryCompleted":
            self.status, self.status_message = "READY", "Recovery complete; awaiting task continuation."
            self.add_recent("·", "Recovery completed")
        elif etype == "TaskCompleted":
            self.status, self.status_message, self.thinking = "SUCCESS", "Task complete.", False
            self._close_active("complete")
            self.add_recent("✓", "Task completed")
        elif etype == "TaskPartial":
            self.status, self.status_message = "WARNING", "Task stopped with a partial result."
            self._close_active("partial")
            self.add_recent("!", "Task partial")
        elif etype in {"TaskFailed", "TaskBlocked"}:
            blocked = etype == "TaskBlocked"
            self.status = "BLOCKED" if blocked else "FAILURE"
            self.status_message = "Task is blocked; inspect the reason on the right." if blocked else "Task needs attention; inspect the activity on the right."
            self.thinking = False
            self._close_active("blocked" if blocked else "failed")
            self.add_recent("!", "Task blocked" if blocked else "Task failed")
        elif etype in {"TaskCancelled", "TaskInterrupted"}:
            self.status, self.status_message, self.thinking = "INTERRUPTED", "Task stopped safely.", False
            self._close_active("interrupted")
            self.add_recent("!", "Task cancelled" if etype == "TaskCancelled" else "Task interrupted")
        elif etype and payload.get("details"):
            self.add_recent("·", etype)


__all__ = ["OperationNode", "ProjectionState"]
