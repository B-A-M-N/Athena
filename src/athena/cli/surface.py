"""Stable, OI-inspired operator surface for the Athena CLI.

This module is intentionally a renderer and interaction adapter only.  It
does not execute code, approve capabilities, or own task state.  The service
and kernel remain authoritative; the surface projects their durable events
into a calm, human-paced terminal view.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Callable, TextIO


@dataclass(frozen=True)
class ApprovalChoice:
    """A human decision returned by the approval selector."""

    granted: bool
    scope: str | None = None


class OperatorSurface:
    """Render task activity and collect approval decisions.

    The default view deliberately coalesces noisy model/runtime events into
    meaningful cards.  ``details=True`` exposes individual model deltas while
    retaining the same controls and service boundary.
    """

    _CODE_CAPABILITIES = frozenset({"execute", "tools.execute", "computer.execute"})
    _OUTPUT_FLUSH_CHARS = 160

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        error: TextIO | None = None,
        interactive: bool | None = None,
        details: bool = False,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.output = output or sys.stdout
        self.error = error or sys.stderr
        self.interactive = (
            bool(sys.stdin.isatty()) if interactive is None else interactive
        )
        self.details = details
        self._input_fn = input_fn or input
        self._model_text = ""
        self._stdout = ""
        self._stderr = ""
        self._handled_approvals: set[str] = set()
        self._last_capability: str | None = None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    async def render_event(self, event: Any) -> None:
        """Render one canonical event without changing application state."""
        event_type = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})

        if event_type == "ModelDelta":
            text = str(payload.get("text") or "")
            if self.details:
                self._write(text, end="")
            else:
                self._model_text += text
            return

        if event_type == "ModelResponseCompleted":
            self._flush_model()
            provider = payload.get("provider")
            model = payload.get("model")
            if self.details and (provider or model):
                self._write(f"  model: {provider or 'unknown'}/{model or 'unknown'}")
            return

        if event_type == "CapabilityRequested":
            self._flush_model()
            self._render_capability_request(payload)
            return

        if event_type == "CapabilityStarted":
            capability = payload.get("capability_id") or self._last_capability or "capability"
            self._write(f"  · {capability} started")
            return

        if event_type in {"CapabilityCompleted", "CapabilityFailed"}:
            self._flush_runtime_output()
            capability = payload.get("capability_id") or self._last_capability or "capability"
            if event_type == "CapabilityCompleted":
                self._write(f"  ✓ {capability} completed")
            else:
                reason = payload.get("reason") or payload.get("error") or "failed"
                self._write(f"  ✗ {capability} failed: {reason}", stream=self.error)
            return

        if event_type == "ExecutionStarted":
            runtime = payload.get("runtime") or "runtime"
            self._write(f"  · {runtime} started")
            return

        if event_type in {"ExecutionExited", "ExecutionTimedOut", "ExecutionInterrupted"}:
            self._flush_runtime_output()
            runtime = payload.get("runtime") or "runtime"
            exit_status = payload.get("exit_status") or event_type.removeprefix("Execution")
            exit_code = payload.get("exit_code")
            if event_type == "ExecutionExited" and exit_code in (0, None):
                self._write(f"  · {runtime} exited ({str(exit_status).lower()})")
            else:
                suffix = f" exit={exit_code}" if exit_code is not None else ""
                self._write(
                    f"  ! {runtime} {str(exit_status).lower()}{suffix}",
                    stream=self.error,
                )
            return

        if event_type in {"StdoutChunk", "StderrChunk"}:
            data = str(payload.get("data") or "")
            if event_type == "StdoutChunk":
                self._stdout += data
                if self.details or "\n" in self._stdout or len(self._stdout) >= self._OUTPUT_FLUSH_CHARS:
                    self._flush_stream("stdout")
            else:
                self._stderr += data
                if self.details or "\n" in self._stderr or len(self._stderr) >= self._OUTPUT_FLUSH_CHARS:
                    self._flush_stream("stderr")
            return

        if event_type == "ApprovalRequested":
            # The dispatcher event contains the actionable approval id.  The
            # kernel also emits a summary event without one; render that
            # summary but never prompt twice for the same approval.
            approval_id = payload.get("approval_id")
            if approval_id and approval_id not in self._handled_approvals:
                self._render_approval(payload)
            return

        if event_type == "ApprovalResolved":
            decision = payload.get("decision") or payload.get("status") or "resolved"
            self._write(f"  approval {decision}")
            return

        if event_type == "ArtifactCreated":
            ref = payload.get("uri") or payload.get("artifact_uri") or payload.get("artifact_ref")
            name = payload.get("name") or payload.get("filename")
            label = f"{name}: " if name else ""
            self._write(f"  artifact · {label}{ref or 'created'}")
            return

        if event_type == "TaskStarted":
            self._write("[task started]")
        elif event_type == "TaskCompleted":
            self._flush_all()
            self._write("[task complete]")
        elif event_type == "TaskPartial":
            self._flush_all()
            self._write("[task partial]")
        elif event_type == "TaskFailed":
            self._flush_all()
            self._write("[task failed]", stream=self.error)
        elif event_type == "TaskCancelled":
            self._flush_all()
            self._write("[task cancelled]")
        elif event_type == "TaskInterrupted":
            self._flush_all()
            self._write("[task interrupted]")
        elif event_type == "TaskStateChanged" and self.details:
            status = payload.get("status") or payload.get("to") or "changed"
            self._write(f"  state: {status}")
        elif event_type == "TaskIterationStarted" and self.details:
            self._write(f"  iteration {payload.get('iteration', '?')}")

    def _render_capability_request(self, payload: dict[str, Any]) -> None:
        capability = str(payload.get("capability_id") or "capability")
        self._last_capability = capability
        arguments = payload.get("arguments") or {}
        self._write("")
        self._write(f"┌─ {capability} ─────────────────────────")
        if capability in self._CODE_CAPABILITIES:
            language = arguments.get("language") or "shell"
            code = arguments.get("code") or ""
            self._write(f"│ language: {language}")
            self._write("│ code:")
            self._write_block(str(code), prefix="│   ")
        elif arguments:
            operation = arguments.get("operation") or arguments.get("action")
            if operation:
                self._write(f"│ operation: {operation}")
            for key, value in arguments.items():
                if key in {"operation", "action"}:
                    continue
                self._write(f"│ {key}: {value}")
        self._write("└──────────────────────────────────────")

    def render_direct_execution(
        self,
        source: str,
        result: dict[str, Any],
        *,
        inject_into_context: bool,
    ) -> None:
        """Render a CLI ``!``/``!!`` execution using the same execution card.

        Direct shell escapes intentionally bypass model inference, but they
        should not create a second visual language for code and output.  This
        method only projects the already-returned service result; it does not
        execute, persist, or inject anything itself.
        """
        self._flush_all()
        self._render_capability_request(
            {
                "capability_id": "execute",
                "arguments": {"language": "shell", "code": source},
            }
        )
        mode = "recorded in session context" if inject_into_context else "displayed only"
        self._write(f"  direct command · {mode}")

        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        if stdout:
            self._write_block(stdout, prefix="│ ")
        if stderr:
            self._write_block(stderr, prefix="│! ", stream=self.error)

        exit_code = result.get("exit_code")
        status = str(result.get("status") or "").lower()
        failed = status in {"failed", "timed_out", "interrupted"} or exit_code not in (0, None)
        if failed:
            detail = f"exit {exit_code}" if exit_code not in (None, 0) else (status or "failed")
            self._write(f"  ✗ execute failed: {detail}", stream=self.error)
        else:
            self._write("  ✓ execute completed")

    def _render_approval(self, payload: dict[str, Any]) -> None:
        capability = payload.get("capability_id") or self._last_capability or "capability"
        scopes = [str(s) for s in payload.get("scopes") or () if s]
        if not scopes:
            scopes = ["call"]
        self._write("")
        self._write("┌─ approval required ───────────────────")
        self._write(f"│ capability: {capability}")
        self._write("│ choose authorization scope:")
        for index, scope in enumerate(scopes, start=1):
            self._write(f"│   {index}) {scope}")
        self._write("│   d) deny")
        self._write("└──────────────────────────────────────")

    async def choose_approval(self, event: Any) -> ApprovalChoice:
        """Show a scope selector and return a conservative human decision."""
        payload = dict(getattr(event, "payload", {}) or {})
        scopes = [str(s) for s in payload.get("scopes") or () if s]
        if not scopes:
            scopes = ["call"]
        if not self.interactive:
            self._write("approval unavailable without an interactive terminal; denied", stream=self.error)
            return ApprovalChoice(False)

        while True:
            try:
                raw = await self._read_line(f"approval [1-{len(scopes)} / d] ")
            except (EOFError, KeyboardInterrupt):
                self._write("no approval received; denied", stream=self.error)
                return ApprovalChoice(False)
            choice = raw.strip().lower()
            if choice in {"d", "deny", "n", "no"}:
                return ApprovalChoice(False)
            if choice in {"", "y", "yes"}:
                return ApprovalChoice(True, scopes[0])
            if choice.isdigit() and 1 <= int(choice) <= len(scopes):
                return ApprovalChoice(True, scopes[int(choice) - 1])
            if choice in scopes:
                return ApprovalChoice(True, choice)
            self._write("choose a listed scope or d to deny", stream=self.error)

    async def _read_line(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._input_fn, prompt)

    # ------------------------------------------------------------------
    # Buffers
    # ------------------------------------------------------------------
    def _flush_model(self) -> None:
        if not self._model_text:
            return
        self._write_block(self._model_text, prefix="assistant> ")
        self._model_text = ""

    def _flush_stream(self, name: str) -> None:
        text = self._stdout if name == "stdout" else self._stderr
        if not text:
            return
        prefix = "│ " if name == "stdout" else "│! "
        self._write_block(text, prefix=prefix, stream=self.error if name == "stderr" else self.output)
        if name == "stdout":
            self._stdout = ""
        else:
            self._stderr = ""

    def _flush_runtime_output(self) -> None:
        self._flush_stream("stdout")
        self._flush_stream("stderr")

    def _flush_all(self) -> None:
        self._flush_model()
        self._flush_runtime_output()

    def finish(self) -> None:
        """Flush pending coalesced output at the end of a stream."""
        self._flush_all()

    def mark_approval_handled(self, approval_id: str) -> None:
        self._handled_approvals.add(approval_id)

    def approval_was_handled(self, approval_id: str) -> bool:
        """Return whether this surface already resolved an approval event."""
        return approval_id in self._handled_approvals

    def _write_block(self, text: str, *, prefix: str = "", stream: TextIO | None = None) -> None:
        target = stream or self.output
        lines = text.splitlines() or [""]
        for line in lines:
            self._write(f"{prefix}{line}", stream=target)

    def _write(self, text: str, *, end: str = "\n", stream: TextIO | None = None) -> None:
        target = stream or self.output
        target.write(text + end)
        target.flush()


__all__ = ["ApprovalChoice", "OperatorSurface"]
