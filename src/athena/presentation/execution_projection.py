"""Execution-side event reduction for the canonical presentation projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from athena.presentation.semantics import VisualActionKind
from athena.protocol.failure import FailureInfo
from athena.presentation.text import sanitize_terminal_text

__all__ = ["reduce_execution_stream"]


def reduce_execution_stream(
    state: Any,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    bounded_event_payload: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> None:
    if event_type == "ExecutionStarted":
        operation = state._operation(payload)
        if operation:
            execution_id = str(payload.get("execution_id") or "")
            if execution_id:
                operation.execution_id = execution_id
                state.execution_to_operation[execution_id] = operation.id
            operation.state = "running"
            operation.command = operation.command or sanitize_terminal_text(
                payload.get("runtime") or "runtime"
            )
        state.status = "EXECUTING"
        state.feed_stream(f"$ {payload.get('runtime') or 'runtime'}\n")
    elif event_type == "RuntimeStateLost":
        state.runtime_state_lost = True
        state.runtime_recovery = {
            "runtime_session_id": payload.get("runtime_session_id"),
            "task_id": payload.get("_event_task_id"),
            "backend": payload.get("backend"),
            "route": payload.get("recovery_route") or "execute",
            "action": payload.get("recovery_action") or "reestablish_runtime",
            "reason": payload.get("reason") or "runtime process was not reattachable",
        }
        state.status = "WARNING"
        state.status_message = (
            "RUNTIME STATE LOST · invoke execute with a fresh runtime; state was not guessed."
        )
        state.add_recent(
            "!",
            f"RUNTIME STATE LOST · {payload.get('runtime_session_id') or 'session'}",
        )
    elif event_type in {"StdoutChunk", "StderrChunk"}:
        data = sanitize_terminal_text(payload.get("data") or "")
        operation = state._operation(payload, create=False)
        if operation and data:
            target = operation.error if event_type == "StderrChunk" else operation.output
            target.extend(data.splitlines() or [data])
        if data:
            state.feed_stream(data, prefix="[err] " if event_type == "StderrChunk" else "")
        state.status = "EXECUTING"
    elif event_type in {"ExecutionExited", "ExecutionTimedOut", "ExecutionInterrupted"}:
        operation = state._operation(payload, create=False)
        if operation:
            operation.exit_code = payload.get("exit_code")
            operation.state = (
                "timed out"
                if event_type == "ExecutionTimedOut"
                else "interrupted"
                if event_type == "ExecutionInterrupted"
                else "complete"
                if operation.exit_code in (None, 0)
                else "failed"
            )
            if state.active_operation_id == operation.id:
                state.active_operation_id = None
            if operation.state != "complete":
                state.attention = True
                state.add_recent("!", f"{operation.label} {operation.state}")
    elif event_type == "MutationPrepared":
        operation = state._operation(payload)
        if operation:
            operation.mutation_state = "prepared"
            state.semantic_state = VisualActionKind.CODE.value
    elif event_type == "MutationRecorded":
        operation = state._operation(payload, create=False)
        if operation:
            operation.mutation_state = "applied"
        state.add_recent("✓", "Mutation applied")
    elif event_type == "MutationRolledBack":
        operation = state._operation(payload, create=False)
        if operation:
            operation.mutation_state = "rolled_back"
        state.semantic_state = VisualActionKind.RECOVER.value
        state.add_recent("!", "Mutation rolled back")
    elif event_type == "DiagnosticsProduced":
        operation = state._operation(payload, create=False)
        raw_diagnostics = payload.get("diagnostics") or ()
        diagnostics = tuple(
            bounded_event_payload(item)
            if isinstance(item, Mapping)
            else {"message": sanitize_terminal_text(item)}
            for item in raw_diagnostics
        )
        state.diagnostics.extend(diagnostics)
        if operation:
            operation.diagnostics = diagnostics
        fatal = payload.get("fatal") is True or any(
            isinstance(item, Mapping)
            and (
                item.get("fatal") is True
                or str(item.get("severity") or "").casefold() in {"fatal", "critical"}
            )
            for item in diagnostics
        )
        if fatal:
            state.status = "FAILURE"
            state.semantic_state = VisualActionKind.FAILURE.value
            state.failure_info = FailureInfo(
                message="fatal diagnostics produced",
                code="diagnostics_fatal",
                stage="diagnostics",
                kind="diagnostics",
                fatal=True,
            )
            state.failure_reason = state.failure_info.message
            state.failure_stage = state.failure_info.stage
            state.add_recent("!", f"Diagnostics · {len(diagnostics)} issue(s)")
    elif event_type == "InstrumentProduced":
        instrument = payload.get("instrument")
        if isinstance(instrument, Mapping):
            item = bounded_event_payload(instrument)
            state.instruments.append(item)
            operation = state._operation(payload, create=False)
            if operation:
                operation.detail = sanitize_terminal_text(
                    instrument.get("title") or instrument.get("kind") or "instrument"
                )
            state.add_recent(
                "·",
                f"Instrument · {instrument.get('title') or instrument.get('kind') or 'view'}",
            )
    elif event_type == "VerificationStarted":
        state.status, state.semantic_state = "VERIFYING", VisualActionKind.VERIFY.value
        operation = state._operation(payload, create=False)
        if operation:
            operation.mutation_state = "verifying"
    elif event_type == "VerificationCheckCompleted":
        check_id = str(
            payload.get("criterion") or payload.get("check_id") or len(state.verification_checks)
        )
        state.verification_checks[check_id] = bounded_event_payload(payload)
        while len(state.verification_checks) > 64:
            state.verification_checks.pop(next(iter(state.verification_checks)))
        state.verification_status = sanitize_terminal_text(payload.get("status") or "running")
    elif event_type == "VerificationCompleted":
        state.verification_status = sanitize_terminal_text(payload.get("status") or "completed")
        operation = state._operation(payload, create=False)
        if operation and state.verification_status.casefold() in {
            "passed",
            "complete",
            "completed",
        }:
            operation.mutation_state = "verified"
        if state.verification_status.casefold() in {"failed", "failure", "error"}:
            state.status_message = "Verification failed; the candidate was not accepted."
            state.attention = True
            if payload.get("fatal") is True or payload.get("terminal") is True:
                state.status = "FAILURE"
                state.failure_info = FailureInfo(
                    message=state.status_message,
                    code="verification_failed",
                    stage="verification",
                    kind="verification",
                    fatal=True,
                )
                state.failure_reason = state.failure_info.message
                state.failure_stage = state.failure_info.stage
        state.semantic_state = VisualActionKind.VERIFY.value
    elif event_type == "ArtifactCreated":
        operation = state._operation(payload, create=False)
        ref = (
            payload.get("uri")
            or payload.get("artifact_uri")
            or payload.get("artifact_ref")
            or payload.get("name")
            or "artifact created"
        )
        if operation:
            operation.artifact = sanitize_terminal_text(ref)
        else:
            state.add_recent("*", f"Artifact · {ref}")
    elif event_type in {
        "ChildTaskCreated",
        "ChildTaskCompleted",
        "DelegationStarted",
        "BackgroundTaskStarted",
        "BackgroundTaskCompleted",
        "BackgroundTaskFailed",
    }:
        started = event_type in {"ChildTaskCreated", "DelegationStarted", "BackgroundTaskStarted"}
        failed = event_type == "BackgroundTaskFailed"
        if failed:
            state.status_message = "Background work failed."
            state.attention = True
        elif started:
            state.status = "DELEGATED"
        label = (
            "Background work failed"
            if failed
            else "Delegated work started"
            if started
            else "Delegated work completed"
        )
        state.add_recent("!" if failed else "↗" if started else "✓", label)
