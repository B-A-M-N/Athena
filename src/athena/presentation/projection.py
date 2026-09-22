"""Canonical event-to-view projection shared by Athena surfaces.

The service/kernel remain authoritative.  ``ProjectionState`` is a read-only
presentation reducer: it keeps enough semantic context for the operator well,
OI scene, history, and stream to agree without any renderer owning execution
state.
"""

from __future__ import annotations

import base64
import binascii
import difflib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.presentation.code_view import MAX_CODE_PREVIEW, bounded_preview
from athena.presentation.execution_projection import reduce_execution_stream
from athena.presentation.semantics import (
    VisualActionKind,
    classify_event,
    classify_visual_action,
    language_for_path,
)
from athena.presentation.text import sanitize_terminal_text
from athena.protocol.failure import FailureInfo

_CONTENT_KEYS = (
    "content",
    "new_content",
    "content_base64",
    "new_content_base64",
)
_MAX_TASKS = 128
_MAX_EXECUTIONS = 256
_MAX_VERIFICATION_CHECKS = 64
_MAX_PARTIAL_DISPLAY = 32 * 1024
_PARTIAL_TRUNCATION_MARKER = "[… truncated …]"
_SELF_HOST_PHASES = frozenset({"PLAN", "PATCH", "PROVE", "REVIEW", "REFEREE", "PROMOTION"})


def _bounded_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep presentation-side raw events bounded without mutating the event."""
    result = dict(payload)
    for key in _CONTENT_KEYS:
        if key in result:
            result[key] = _content_preview(result[key])[0]
    raw_args = result.get("arguments")
    if isinstance(raw_args, Mapping):
        args = dict(raw_args)
        for key in _CONTENT_KEYS:
            if key in args:
                args[key] = _content_preview(args[key])[0]
        result["arguments"] = args
    return result


def _content_preview(value: object) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return "", False
    text = str(value)
    if len(text) > MAX_CODE_PREVIEW * 2:
        text = text[: MAX_CODE_PREVIEW * 2]
    return bounded_preview(text)


def _content_from_args(args: Mapping[str, Any]) -> tuple[str, bool]:
    for key in ("content", "new_content"):
        if args.get(key) is not None:
            return _content_preview(args[key])
    for key in ("content_base64", "new_content_base64"):
        value = args.get(key)
        if value is None:
            continue
        try:
            raw = base64.b64decode(str(value)[: MAX_CODE_PREVIEW * 2], validate=True)
            return bounded_preview(raw.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return "", False
    return "", False


def _diff_preview(payload: Mapping[str, Any], args: Mapping[str, Any]) -> tuple[str, ...]:
    supplied = payload.get("diff") or payload.get("diff_preview") or args.get("diff")
    if isinstance(supplied, str):
        text, _ = bounded_preview(supplied)
        return tuple(text.splitlines())
    if isinstance(supplied, (list, tuple)):
        lines: list[str] = []
        for item in supplied:
            line, truncated = bounded_preview(item, limit=4096)
            lines.extend(line.splitlines() or [""])
            if truncated:
                break
        return tuple(lines[:256])
    before = args.get("before_content") or args.get("old_content")
    after = args.get("new_content") or args.get("content")
    if before is None or after is None:
        return ()
    before_text, _ = bounded_preview(before)
    after_text, _ = bounded_preview(after)
    return tuple(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )[:256]


def _progress_label(payload: Mapping[str, Any]) -> tuple[str, float | None, bool]:
    # CapabilityDispatcher's canonical determinate-progress contract calls
    # the numerator ``value``.  ``current`` is retained as a compatibility
    # alias for older producers and external event replays.
    current = payload.get("value", payload.get("current"))
    total = payload.get("total")
    determinate = bool(payload.get("determinate"))
    if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return f"{current:g}/{total:g}", float(current) / float(total), True
    message = payload.get("message") or payload.get("progress") or "active"
    return sanitize_terminal_text(message), None, determinate


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
    action_kind: str = VisualActionKind.IDLE.value
    language: str = ""
    content_preview: str = ""
    preview_truncated: bool = False
    diff_preview: tuple[str, ...] = ()
    mutation_state: str = ""
    progress_value: float | None = None
    progress_determinate: bool = False
    diagnostics: tuple[dict[str, Any], ...] = ()
    parent_id: str | None = None


@dataclass
class ProjectionState:
    """Pure-ish reducer state used by both the dual surface and ``oi-stream``."""

    operations: dict[str, OperationNode] = field(default_factory=dict)
    # Task lifecycle facts are retained independently of operation cards so a
    # scene can render the canonical task hierarchy even when no capability
    # has emitted an operation yet.
    tasks: dict[str, dict[str, str | None]] = field(default_factory=dict)
    # Execution nodes are separate from operation nodes so the runtime tree
    # can show task -> operation -> execution without making a renderer infer
    # process identity from an operation's mutable fields.
    executions: dict[str, dict[str, str | None]] = field(default_factory=dict)
    execution_to_operation: dict[str, str] = field(default_factory=dict)
    active_operation_id: str | None = None
    last_operation_id: str | None = None
    chat: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=160))
    recent: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=24))
    # Learning/maintenance is deliberately separate from task activity. It is
    # available to details/history views without displacing operational facts.
    maintenance: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=24))
    stream: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    stream_partial: str = ""
    # Approval requests are a durable ordered set, not a single mutable
    # banner.  A projection must never overwrite request A when request B
    # arrives, nor clear unrelated requests when one resolves.
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_state_lost: bool = False
    runtime_recovery: dict[str, Any] = field(default_factory=dict)
    status: str = "READY"
    status_message: str = "Type a request below."
    attention: bool = False
    semantic_state: str = "idle"
    self_host_phase: str = ""
    thinking: bool = False
    event_count: int = 0
    projection_revision: int = 0
    diagnostics: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=80))
    instruments: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=48))
    verification_checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    verification_status: str = ""
    failure_reason: str = ""
    failure_stage: str = ""
    failure_info: FailureInfo = field(default_factory=FailureInfo)
    # These fields are the latest observed model request, not configuration.
    # They intentionally remain populated after completion/failure so a
    # fallback or failed attempt cannot make an unobserved model look active.
    active_provider: str | None = None
    active_model: str | None = None
    active_model_role: str | None = None
    active_model_request_id: str | None = None
    model_request_status: str = "idle"
    raw_events: deque[tuple[str, Mapping[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    _task_order: deque[str] = field(default_factory=deque, repr=False)
    _execution_order: deque[str] = field(default_factory=deque, repr=False)
    _approval_order: deque[str] = field(default_factory=deque, repr=False)
    _stream_partial_truncated: bool = field(default=False, repr=False)
    _native_scene_cache: tuple[tuple[int, int, int, str], Any] | None = field(
        default=None, repr=False
    )

    def mark_mutated(self) -> None:
        """Advance the presentation revision for non-event UI mutations."""
        self.projection_revision += 1

    def set_status(self, status: str, message: str | None = None) -> None:
        self.status = str(status)
        if message is not None:
            self.status_message = message
        self.mark_mutated()

    def set_status_message(self, message: str) -> None:
        self.status_message = str(message)
        self.mark_mutated()

    @property
    def pending_approval(self) -> dict[str, Any] | None:
        """Compatibility view of the oldest pending approval.

        New consumers should use :attr:`pending_approvals` or
        :meth:`ordered_pending_approvals`; keeping this view avoids making
        older hosted widgets a second approval authority during migration.
        """
        for approval_id in self._approval_order:
            approval = self.pending_approvals.get(approval_id)
            if approval is not None:
                return approval
        return next(iter(self.pending_approvals.values()), None)

    @pending_approval.setter
    def pending_approval(self, value: dict[str, Any] | None) -> None:
        # Compatibility setter for older presentation code.  Assignment of
        # None means "clear the displayed compatibility item"; lifecycle
        # reducers use the ID-keyed helpers below and never clear the set.
        if value is None:
            self.pending_approvals.clear()
            self._approval_order.clear()
            self.mark_mutated()
            return
        approval_id = self._approval_id(value)
        if approval_id:
            self.pending_approvals[approval_id] = dict(value)
            self._touch(self._approval_order, approval_id)
            self.mark_mutated()

    @staticmethod
    def _approval_id(payload: Mapping[str, Any]) -> str | None:
        value = payload.get("approval_id") or payload.get("id")
        clean = sanitize_terminal_text(value).strip() if value is not None else ""
        return clean or None

    def ordered_pending_approvals(self) -> list[dict[str, Any]]:
        """Return pending approvals in creation/event order."""
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for approval_id in self._approval_order:
            approval = self.pending_approvals.get(approval_id)
            if approval is not None:
                result.append(approval)
                seen.add(approval_id)
        result.extend(
            approval
            for approval_id, approval in self.pending_approvals.items()
            if approval_id not in seen
        )
        return result

    def _remove_pending_approval(self, approval_id: str) -> None:
        self.pending_approvals.pop(approval_id, None)
        try:
            self._approval_order.remove(approval_id)
        except ValueError:
            pass

    def add_recent(self, glyph: str, text: object) -> None:
        clean = sanitize_terminal_text(text).strip()
        if clean and (not self.recent or self.recent[-1] != (glyph, clean)):
            self.recent.append((glyph, clean))
            self.mark_mutated()

    def add_maintenance(self, glyph: str, text: object) -> None:
        clean = sanitize_terminal_text(text).strip()
        if clean and (not self.maintenance or self.maintenance[-1] != (glyph, clean)):
            self.maintenance.append((glyph, clean))
            self.mark_mutated()

    def add_chat(self, role: str, text: object) -> None:
        clean = sanitize_terminal_text(text).strip()
        if clean:
            self.chat.append({"role": role, "text": clean})
            self.mark_mutated()

    def feed_stream(self, text: object, *, prefix: str = "") -> None:
        clean = sanitize_terminal_text(text)
        if clean:
            self.mark_mutated()
        # A chunk may end in the middle of a line. Prefix only the beginning
        # of that logical line so split stderr chunks do not become
        # ``[err] first[err] second`` in the shared stream.
        if prefix and not self.stream_partial:
            clean = prefix + clean
        self.stream_partial += clean
        while "\n" in self.stream_partial:
            line, _, self.stream_partial = self.stream_partial.partition("\n")
            self.stream.append(_cap_display(line))
            self._stream_partial_truncated = False
        if len(self.stream_partial) > _MAX_PARTIAL_DISPLAY:
            self.stream_partial = _cap_display(self.stream_partial)
            self._stream_partial_truncated = True

    def seal_stream(self) -> None:
        if self.stream_partial:
            self.stream.append(_cap_display(self.stream_partial))
            self.stream_partial = ""
            self._stream_partial_truncated = False
            self.mark_mutated()

    def acknowledge_approval(
        self,
        *,
        granted: bool,
        scope: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        """Project a local approval choice before its durable event arrives.

        Approval input is an interaction, not a second source of task state.
        Keeping the optimistic transition here lets every frontend repaint
        consistently while the service is resuming the task; the subsequent
        ``ApprovalResolved`` event remains authoritative and may refine it.
        """
        approval = self.pending_approvals.get(approval_id) if approval_id else self.pending_approval
        if approval_id:
            self._remove_pending_approval(approval_id)
        elif approval is not None:
            oldest_id = self._approval_id(approval)
            if oldest_id:
                self._remove_pending_approval(oldest_id)
        if approval is not None and scope:
            approval = dict(approval)
            approval["selected_scope"] = scope
        if granted:
            self.set_status("EXECUTING", "Approval accepted; resuming.")
        else:
            self.set_status("WARNING", "Approval denied.")

    def ignore_approval_summary(self) -> None:
        """Discard a count-only approval summary after its request was handled."""
        if self.status == "APPROVAL" and not self.pending_approvals:
            self.set_status("EXECUTING", "Approval accepted; resuming.")

    def _operation(
        self, payload: Mapping[str, Any], *, create: bool = True
    ) -> OperationNode | None:
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
            action = classify_visual_action(
                payload.get("capability_id") or payload.get("runtime"),
                operation=args.get("operation") or args.get("action"),
                arguments=args,
                effects=payload.get("effects"),
            )
            operation = OperationNode(
                id=op_id,
                label=str(payload.get("capability_id") or payload.get("runtime") or "operation"),
                target=target,
                command=(command.splitlines() or [""])[0],
                detail=sanitize_terminal_text(args.get("operation") or args.get("action") or ""),
                action_kind=action.value,
                language=sanitize_terminal_text(args.get("language") or language_for_path(target)),
                parent_id=sanitize_terminal_text(
                    payload.get("parent_id")
                    or payload.get("parent_task_id")
                    or payload.get("_event_task_id")
                    or ""
                )
                or None,
            )
            self.operations[op_id] = operation
            self.last_operation_id = op_id
            self.active_operation_id = op_id
            while len(self.operations) > 80:
                oldest = next(iter(self.operations))
                if oldest == self.active_operation_id:
                    break
                self.operations.pop(oldest, None)
        if operation is not None:
            self._enrich_operation(operation, payload)
        return operation

    @staticmethod
    def _enrich_operation(operation: OperationNode, payload: Mapping[str, Any]) -> None:
        raw_args = payload.get("arguments")
        args = raw_args if isinstance(raw_args, Mapping) else {}
        action = classify_visual_action(
            payload.get("capability_id") or operation.label,
            operation=args.get("operation") or args.get("action") or operation.detail,
            arguments=args,
            effects=payload.get("effects"),
        )
        if action is not VisualActionKind.IDLE:
            operation.action_kind = action.value
        preview, truncated = _content_from_args(args)
        if preview:
            operation.content_preview = preview
            operation.preview_truncated = operation.preview_truncated or truncated
            operation.mutation_state = operation.mutation_state or "proposed"
            operation.language = operation.language or language_for_path(operation.target)
        diff = _diff_preview(payload, args)
        if diff:
            operation.diff_preview = diff
            operation.mutation_state = operation.mutation_state or "prepared"
        if payload.get("language"):
            operation.language = sanitize_terminal_text(payload["language"])
        parent_id = sanitize_terminal_text(
            payload.get("parent_id")
            or payload.get("parent_task_id")
            or payload.get("_event_task_id")
            or ""
        )
        if parent_id:
            operation.parent_id = parent_id

    @staticmethod
    def _task_key(value: object) -> str:
        value = sanitize_terminal_text(value).strip()
        return value.removeprefix("task:") if value.startswith("task:") else value

    def _reduce_task_lifecycle(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Reset per-turn transient state when a new task lifecycle begins."""
        if etype not in {"TaskCreated", "TaskQueued", "TaskStarted"}:
            return
        # A new task begins with no live operation. Completed operation
        # history remains available, but must never reappear as live.
        self.active_operation_id = None
        self.diagnostics.clear()
        self.verification_checks.clear()
        self.verification_status = ""
        self.stream.clear()
        self.stream_partial = ""
        self._stream_partial_truncated = False
        self.failure_reason = ""
        self.failure_stage = ""
        self.failure_info = FailureInfo()
        self.attention = False
        self.mark_mutated()

    def _reduce_task(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Project task lifecycle facts into one stable runtime namespace."""
        raw_id = (
            payload.get("child_task_id") or payload.get("task_id") or payload.get("_event_task_id")
        )
        task_id = self._task_key(raw_id)
        if not task_id:
            return
        parent = self._task_key(payload.get("parent_task_id") or payload.get("parent_id"))
        existing = self.tasks.get(task_id, {})
        status_by_event = {
            "TaskCreated": "created",
            "TaskQueued": "queued",
            "TaskStarted": "running",
            "TaskCompleted": "complete",
            "TaskPartial": "partial",
            "TaskFailed": "failed",
            "TaskBlocked": "blocked",
            "TaskCancelled": "cancelled",
            "TaskInterrupted": "interrupted",
            "TaskRecoveryRequired": "recovery_required",
            "ChildTaskCreated": "created",
            "ChildTaskCompleted": "complete",
        }
        status = status_by_event.get(event_type)
        if event_type == "TaskStateChanged":
            status = sanitize_terminal_text(payload.get("status") or payload.get("to") or "changed")
        if event_type in {"DelegationStarted", "BackgroundTaskStarted"}:
            status = "running"
        if event_type in {"BackgroundTaskCompleted"}:
            status = "complete"
        if event_type in {"BackgroundTaskFailed"}:
            status = "failed"
        self.tasks[task_id] = {
            "label": sanitize_terminal_text(
                payload.get("objective") or payload.get("name") or existing.get("label") or task_id
            ),
            "status": status or existing.get("status") or "observed",
            "parent_id": parent or existing.get("parent_id"),
        }
        self._touch(self._task_order, task_id)
        self._prune_tasks()

    def _reduce_execution(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Project execution lifecycle into the operation's runtime child."""
        execution_id = sanitize_terminal_text(payload.get("execution_id") or "").strip()
        if not execution_id:
            return
        existing = self.executions.get(execution_id, {})
        status = {
            "ExecutionStarted": "running",
            "ExecutionExited": "complete" if payload.get("exit_code") in (None, 0) else "failed",
            "ExecutionTimedOut": "timed out",
            "ExecutionInterrupted": "interrupted",
        }.get(event_type)
        call_id = sanitize_terminal_text(payload.get("call_id") or "").strip()
        task_id = self._task_key(payload.get("_event_task_id"))
        self.executions[execution_id] = {
            "label": sanitize_terminal_text(
                payload.get("runtime") or existing.get("label") or execution_id
            ),
            "status": status or existing.get("status") or "observed",
            # A call id is the canonical operation identity in the event
            # stream. Fall back to the task only for externally replayed
            # execution events that omit it.
            "parent_id": call_id
            or existing.get("parent_id")
            or (f"task:{task_id}" if task_id else None),
        }
        self._touch(self._execution_order, execution_id)
        self._prune_executions()

    def _close_active(self, state: str) -> None:
        operation = self.operations.get(self.active_operation_id or "")
        if operation is not None and operation.state in {
            "requested",
            "validated",
            "approval",
            "approved",
            "running",
        }:
            operation.state = state
        self.active_operation_id = None

    @staticmethod
    def _touch(order: deque[str], key: str) -> None:
        try:
            order.remove(key)
        except ValueError:
            pass
        order.append(key)

    def _prune_tasks(self) -> None:
        active = {
            key
            for key, value in self.tasks.items()
            if value.get("status") in {"created", "queued", "running", "approval", "approved"}
        }
        keep = active | set(list(self._task_order)[-_MAX_TASKS:])
        for key in tuple(self.tasks):
            if key not in keep:
                self.tasks.pop(key, None)
        self._task_order = deque(key for key in self._task_order if key in self.tasks)

    def _prune_executions(self) -> None:
        active = {
            key
            for key, value in self.executions.items()
            if value.get("status") in {"running", "observed"}
        }
        keep = active | set(list(self._execution_order)[-_MAX_EXECUTIONS:])
        for key in tuple(self.executions):
            if key not in keep:
                self.executions.pop(key, None)
        self._execution_order = deque(
            key for key in self._execution_order if key in self.executions
        )
        for execution_id in tuple(self.execution_to_operation):
            if execution_id not in self.executions:
                self.execution_to_operation.pop(execution_id, None)

    def _model_request_identity(self, payload: Mapping[str, Any], *, reset: bool = False) -> None:
        """Record provider facts emitted by the actual request lifecycle."""
        provider = (
            sanitize_terminal_text(
                payload.get("provider") or payload.get("provider_profile_id") or ""
            )
            or None
        )
        model = (
            sanitize_terminal_text(payload.get("model") or payload.get("model_id") or "") or None
        )
        role = sanitize_terminal_text(payload.get("role") or "") or None
        request_id = sanitize_terminal_text(payload.get("request_id") or "") or None
        if reset:
            self.active_provider = provider
            self.active_model = model
            self.active_model_role = role
            self.active_model_request_id = request_id
            return
        if provider:
            self.active_provider = provider
        if model:
            self.active_model = model
        if role:
            self.active_model_role = role
        if request_id:
            self.active_model_request_id = request_id

    def reduce(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> None:
        """Apply one event. Raw payload is retained separately for audit/debug views."""
        payload = dict(payload or {})
        etype = str(event_type)
        self.event_count += 1
        self.projection_revision += 1
        self.raw_events.append((etype, _bounded_event_payload(payload)))
        if task_id:
            payload.setdefault("_event_task_id", task_id)
        raw_phase = payload.get("self_host_phase") or payload.get("phase")
        phase = sanitize_terminal_text(raw_phase).upper()
        if phase == "PROMOTE":
            phase = "PROMOTION"
        if phase in _SELF_HOST_PHASES:
            self.self_host_phase = phase
            self.add_recent("·", f"Self-host · {phase}")
        self._reduce_task(etype, payload)
        self._reduce_execution(etype, payload)
        action = classify_event(etype, payload)
        if action is not VisualActionKind.IDLE:
            if etype in {
                "CapabilityStarted",
                "CapabilityProgress",
                "CapabilityCompleted",
            }:
                active = self.operations.get(self.active_operation_id or "")
                if active is not None and active.action_kind != VisualActionKind.IDLE.value:
                    action = VisualActionKind(active.action_kind)
            self.semantic_state = action.value

        self._reduce_task_lifecycle(etype, payload)
        self._reduce_model_status(etype, payload)
        self._reduce_capability_lifecycle(etype, payload)
        self._reduce_execution_stream(etype, payload)
        self._reduce_maintenance(etype, payload)
        self._reduce_terminal_states(etype, payload)
        self._reduce_unrecognized(etype, payload)

    def _reduce_model_status(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Model-request, reasoning, response, and inspection status chain."""
        if etype in {"TaskCreated", "TaskQueued"}:
            self.status = "READY"
        elif etype == "TaskStarted":
            self.status, self.status_message, self.thinking = (
                "THINKING",
                "Athena is working through the request.",
                True,
            )
        elif etype in {"ContextBuildStarted", "ContextBuilt", "ContextCompressed"}:
            self.status = "INSPECTING"
            self.add_recent(
                "·",
                "Context compressed with provenance retained"
                if etype == "ContextCompressed"
                else "Context assembled",
            )
        elif etype == "ModelRequestStarted":
            self.status, self.thinking = "THINKING", True
            self.attention = False
            self._model_request_identity(payload, reset=True)
            self.model_request_status = "active"
            model_label = (
                f"{self.active_provider or '—'}/{self.active_model or '—'}"
                if self.active_provider or self.active_model
                else "—"
            )
            self.add_recent("·", f"Model request · {model_label}")
        elif etype in {"ModelReasoningDelta", "TaskIterationStarted"}:
            self.status, self.thinking = "THINKING", True
        elif etype == "ModelDelta":
            self.status, self.thinking = "RESPONDING", False
            self.feed_stream(payload.get("text") or "")
        elif etype == "ModelResponseCompleted":
            self._model_request_identity(payload)
            self.thinking = False
            self.status = "RESPONDING"
            self.model_request_status = "completed"
            self.seal_stream()
        elif etype == "ModelRequestFailed":
            self._model_request_identity(payload)
            self.thinking = False
            self.model_request_status = "failed"
            self.status_message = "The model request failed; inspect the error and retry."
            self.attention = True
            self.add_recent(
                "!",
                f"Model request failed · {payload.get('error') or payload.get('reason') or 'provider error'}",
            )
        elif etype in {"SearchStarted", "ResearchStarted", "FileRead", "InspectionStarted"}:
            searching = etype in {"SearchStarted", "ResearchStarted"}
            self.status = "SEARCHING" if searching else "READING"
            label = (
                payload.get("query")
                or payload.get("path")
                or payload.get("resource")
                or "workspace"
            )
            self.add_recent("·", f"{'Search' if searching else 'Read'} · {label}")

    def _reduce_terminal_states(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Task terminal and recovery state transitions."""
        if etype == "TaskStateChanged":
            state = sanitize_terminal_text(
                payload.get("status") or payload.get("to") or "changed"
            ).upper()
            status_map = {
                "WAITING_APPROVAL": ("APPROVAL", "Paused for operator approval."),
                "WAITING_INPUT": ("WAITING", "Paused for operator input."),
                "BLOCKED": ("BLOCKED", "Task is blocked; inspect the reason."),
                "RECOVERY_REQUIRED": ("RECOVERING", "Task requires recovery."),
                "RUNNING": ("EXECUTING", "Task is running."),
            }
            self.status, self.status_message = status_map.get(
                state, (state, f"Task state: {state.lower()}.")
            )
            self.thinking = state in {"RUNNING", "WAITING_INPUT"}
            self.add_recent(
                "!" if state in {"BLOCKED", "RECOVERY_REQUIRED"} else "·",
                f"Task state · {state.lower()}",
            )
        elif etype == "TaskRecoveryRequired":
            self.status, self.status_message = (
                "RECOVERING",
                sanitize_terminal_text(payload.get("reason") or "Task requires recovery."),
            )
            self.thinking = False
            self.add_recent("!", "Provider outcome requires reconciliation")
        elif etype == "RecoveryStarted":
            self.status, self.status_message = (
                "RECOVERING",
                "Recovering task state and retained evidence.",
            )
            self.add_recent("·", "Recovery started")
        elif etype == "RecoveryCompleted":
            self.status, self.status_message = (
                "READY",
                "Recovery complete; awaiting task continuation.",
            )
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
            failure = FailureInfo.from_payload(
                payload,
                default_kind="task",
                default_stage="task",
            )
            self.failure_info = failure
            self.failure_reason = sanitize_terminal_text(failure.message)
            self.failure_stage = sanitize_terminal_text(failure.stage)
            self.status = "BLOCKED" if blocked else "FAILURE"
            self.status_message = self.failure_reason or (
                "Task is blocked." if blocked else "Task failed."
            )
            self.thinking = False
            self._close_active("blocked" if blocked else "failed")
            self.add_recent("!", "Task blocked" if blocked else "Task failed")
        elif etype in {"TaskCancelled", "TaskInterrupted"}:
            self.status, self.status_message, self.thinking = (
                "INTERRUPTED",
                "Task stopped safely.",
                False,
            )
            self._close_active("interrupted")
            self.add_recent(
                "!", "Task cancelled" if etype == "TaskCancelled" else "Task interrupted"
            )

    def _reduce_capability_lifecycle(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Capability admission, approval, and policy event families."""
        if etype == "CapabilityRequested":
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
                operation.state = (
                    "approval" if decision in {"ask", "approval", "pending"} else decision
                )
                operation.detail = sanitize_terminal_text(payload.get("reason") or operation.detail)
            if decision in {"deny", "denied"}:
                self.status, self.status_message = "WARNING", "Policy denied this operation."
            self.add_recent("!" if decision in {"deny", "denied"} else "·", f"Policy · {decision}")
        elif etype == "ApprovalRequested":
            operation = self._operation(payload)
            if operation:
                operation.state = "approval"
            approval_id = self._approval_id(payload)
            if approval_id:
                self.pending_approvals[approval_id] = dict(payload)
                self._touch(self._approval_order, approval_id)
            self.status, self.status_message = "APPROVAL", "Paused until you choose a scope."
            self.add_recent(
                "?", f"Approval required · {operation.label if operation else 'capability'}"
            )
        elif etype == "ApprovalResolved":
            operation = self._operation(payload, create=False)
            decision = str(payload.get("decision") or payload.get("status") or "resolved").lower()
            denied = decision in {"deny", "denied", "rejected"}
            if operation:
                operation.state = "denied" if denied else "approved"
            approval_id = self._approval_id(payload)
            if approval_id:
                self._remove_pending_approval(approval_id)
            self.status = "WARNING" if denied else "TOOLS"
            self.add_recent("!" if denied else "✓", f"Approval {decision}")
        elif etype in {
            "CapabilityStarted",
            "CapabilityProgress",
            "CapabilityCompleted",
            "CapabilityFailed",
        }:
            operation = self._operation(payload)
            if operation:
                if etype == "CapabilityStarted":
                    operation.state, self.status = "running", "EXECUTING"
                    self.attention = False
                    if operation.content_preview or operation.diff_preview:
                        operation.mutation_state = "applying"
                elif etype == "CapabilityProgress":
                    (
                        operation.progress,
                        operation.progress_value,
                        operation.progress_determinate,
                    ) = _progress_label(payload)
                elif etype == "CapabilityCompleted":
                    operation.state = "complete"
                    self.attention = False
                    if operation.content_preview or operation.diff_preview:
                        operation.mutation_state = "applied"
                    output = sanitize_terminal_text(payload.get("output") or "")
                    if output:
                        operation.output.extend(output.splitlines() or [output])
                        self.feed_stream(output)
                    if self.active_operation_id == operation.id:
                        self.active_operation_id = None
                else:
                    operation.state = "failed"
                    if operation.content_preview or operation.diff_preview:
                        operation.mutation_state = "failed"
                    operation.detail = sanitize_terminal_text(
                        payload.get("reason") or payload.get("error") or "failed"
                    )
                    self.status_message = (
                        f"{operation.label} failed; inspect the operation details."
                    )
                    self.attention = True
                    self.add_recent("!", f"{operation.label} failed")
                    if self.active_operation_id == operation.id:
                        self.active_operation_id = None

    def _reduce_execution_stream(self, etype: str, payload: Mapping[str, Any]) -> None:
        reduce_execution_stream(
            self,
            etype,
            payload,
            bounded_event_payload=_bounded_event_payload,
        )

    def _reduce_maintenance(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Memory, skill, tool-repair, and interpreter maintenance families."""
        if etype in {
            "MemoryCandidatesRecorded",
            "MemoryCandidateCreated",
            "MemoryWritten",
            "SkillCandidateCreated",
            "SkillActivated",
        }:
            labels = {
                "MemoryCandidatesRecorded": "Memory candidates recorded",
                "MemoryCandidateCreated": "Memory candidate recorded",
                "MemoryWritten": "Memory written",
                "SkillCandidateCreated": "Skill candidate recorded",
                "SkillActivated": "Skill activated",
            }
            self.add_maintenance("·", labels[etype])
        elif etype in {
            "ToolRepaired",
            # MutationRecorded/MutationRolledBack are handled by the
            # execution-stream family above; the original single elif chain
            # never reached here for them (dead labels preserved as comments).
            "MutationRecordFailed",
            "InterpreterProposalDispatched",
            "ToolInputCorrectionExhausted",
            "RuntimeSessionCreated",
        }:
            if etype == "RuntimeSessionCreated":
                self.runtime_state_lost = False
                self.runtime_recovery = {}
            labels = {
                "ToolRepaired": "Tool input repaired",
                "MutationRecorded": "Mutation recorded",
                "MutationRecordFailed": "Mutation record failed",
                "InterpreterProposalDispatched": "Computer proposal dispatched",
                "ToolInputCorrectionExhausted": "Tool repair budget exhausted",
                "RuntimeSessionCreated": "Runtime session created",
                "MutationRolledBack": "Mutation rolled back",
            }
            serious = etype in {"MutationRecordFailed", "ToolInputCorrectionExhausted"}
            if serious:
                self.status_message = labels[etype]
                self.attention = True
            self.add_recent("!" if serious else "·", labels.get(etype, etype))

    def _reduce_unrecognized(self, etype: str, payload: Mapping[str, Any]) -> None:
        """Fallback handler for unrecognized-but-informative events."""
        if etype and payload.get("details"):
            self.add_recent("·", etype)


def _cap_display(value: object) -> str:
    text = str(value)
    if len(text) <= _MAX_PARTIAL_DISPLAY:
        return text
    tail_length = _MAX_PARTIAL_DISPLAY - len(_PARTIAL_TRUNCATION_MARKER)
    return _PARTIAL_TRUNCATION_MARKER + text[-max(tail_length, 0) :]


__all__ = ["OperationNode", "ProjectionState"]
