"""`athena oi-stream` — the live OI window as a first-class pane.

A pure subscriber to the canonical event log (INV-007 safe: read-only).
Renders unbuffered model deltas + runtime output with the activity mascot,
and can resolve approvals inline when policy parks a task.

Usage:
    athena oi-stream [--task TASK_ID] [--db PATH] [--height N]
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections import deque
from collections.abc import Mapping
from typing import Any, Callable, TextIO

from athena.cli.dual_pane import (
    Mascot,
    _MASCOT_OFF,
    resolve_mascot_name,
)
from athena.cli.layout import Rect
from athena.cli.input import PromptController
from athena.cli.projection import ProjectionState
from athena.cli.render.ansi import CellGridDiffRenderer
from athena.cli.render.scene import render_scene_lines
from athena.cli.scene import build_oi_scene
from athena.cli.terminal import TerminalSession

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

class OIStreamViewer:
    """Full-pane OI window: raw stream + mascot header + inline approvals."""

    def __init__(
        self,
        *,
        service: Any = None,
        task_id: str | None = None,
        max_lines: int = 400,
        interactive: bool | None = None,
        output: TextIO | None = None,
        error: TextIO | None = None,
        input_fn: Callable[[str], str] | None = None,
        mascot: str | None = None,
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.max_lines = max_lines
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.output = output or sys.stdout
        self.error = error or sys.stderr
        self._input_fn = input_fn
        character = resolve_mascot_name(mascot)
        self.mascot_enabled = character not in _MASCOT_OFF
        self.mascot = Mascot(
            character=character if self.mascot_enabled else "owl"
        )
        self._handled_approvals: set[str] = set()
        self._last_policy_reason = ""
        self._last_target = ""
        self.projection = ProjectionState(stream=deque(maxlen=max_lines))
        self.scene = build_oi_scene(self.projection, Rect(0, 0, 80, 24))
        self.prompt = PromptController(input_fn=input_fn, output=self.output)
        self._renderer = CellGridDiffRenderer(self.output)
        self._terminal_session = TerminalSession(
            self.output,
            enabled=self.interactive,
        )

    def open(self) -> None:
        """Enter the OI viewer's screen only when attached to a real TTY."""
        self._terminal_session.open()

    def close(self) -> None:
        """Restore cursor/screen state after task completion or interruption."""
        self._terminal_session.close()

    @property
    def _pending_approval(self) -> dict | None:
        """Compatibility view; approval state belongs to the projection."""
        return self.projection.pending_approval

    @property
    def _status(self) -> str:
        """Compatibility label for callers of the older stream API."""
        if self.projection.status == "EXECUTING":
            return "running"
        if self.projection.status == "APPROVAL":
            return "WAITING FOR APPROVAL"
        return self.projection.status.lower()

    # -- ingestion ------------------------------------------------------
    async def handle_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        self.projection.reduce(etype, payload)
        self.mascot.observe(etype, payload)

        if etype == "CapabilityRequested":
            raw_args = payload.get("arguments")
            args = raw_args if isinstance(raw_args, Mapping) else {}
            if args:
                self._last_target = str(
                    payload.get("target")
                    or payload.get("resource")
                    or args.get("path")
                    or args.get("file")
                    or args.get("resource")
                    or ""
                )
        elif etype == "ApprovalRequested":
            approval_id = str(payload.get("approval_id") or "")
            # The kernel emits a count-only ApprovalRequested summary after
            # the dispatcher emits the actionable request. Preserve the
            # detailed request and never prompt twice for one approval.
            if approval_id and approval_id not in self._handled_approvals:
                await self._offer_approval(payload)
            elif not approval_id and self._handled_approvals:
                self.projection.ignore_approval_summary()
        elif etype == "PolicyDecisionMade":
            self._last_policy_reason = str(payload.get("reason") or "")

    # -- approvals -------------------------------------------------------
    async def _offer_approval(self, payload: dict) -> None:
        """Inline decision surface inside the OI pane."""
        scopes = [str(s) for s in payload.get("scopes") or ()] or ["call"]
        aid = payload.get("approval_id")
        cap = payload.get("capability_id") or "capability"
        target = payload.get("target") or payload.get("resource") or payload.get("path") or self._last_target
        reason = payload.get("reason") or payload.get("policy_reason") or self._last_policy_reason
        self._write(f"\n{_BOLD}APPROVAL REQUIRED{_RESET}  capability={cap}  id={aid}")
        if target:
            self._write(f"  target: {target}")
        if reason:
            self._write(f"  reason: {reason}")
        for i, s in enumerate(scopes, 1):
            self._write(f"  {i}) {s}")
        self._write("  d) deny  (the task is paused)")
        try:
            raw = self.prompt.read(f"approve [{1}-{len(scopes)}/d]> ")
        except (EOFError, KeyboardInterrupt):
            raw = "d"
        choice = raw.strip().lower()
        granted = choice not in {"d", "deny", "n", "no", ""}
        scope = None
        if granted and choice.isdigit() and 1 <= int(choice) <= len(scopes):
            scope = scopes[int(choice) - 1]
        elif granted:
            scope = scopes[0]
        if self.service is not None and aid:
            await self.service.approve(aid, granted=granted, scope=scope)
        if aid:
            self._handled_approvals.add(str(aid))
        self.projection.acknowledge_approval(granted=granted, scope=scope)

    # -- rendering --------------------------------------------------------
    def render(self, height: int | None = None) -> None:
        if height is None:
            _, height = shutil.get_terminal_size((80, 24))
        width, _ = shutil.get_terminal_size((80, max(height, 1)))
        width = max(width, 40)
        self.scene = build_oi_scene(self.projection, Rect(0, 0, width, max(height, 1)))
        m = self.mascot.render(max_width=30) if self.mascot_enabled else []
        state = self.projection.status
        lines = [f"{_BOLD}╭─ OI LIVE ─ state: {state} ─ {'─' * 20}{_RESET}"]
        body = render_scene_lines(
            self.projection,
            self.scene,
            width=width,
            height=max(height - 2, 1),
            buddy_lines=([f"BUDDY · {state}"] + m) if self.mascot_enabled else (),
            buddy_enabled=self.mascot_enabled,
        )
        lines.extend(body)
        display_status = self.projection.status.lower()
        tail = f"{_DIM}{display_status}{_RESET}"
        lines.append(f"╰─ {tail} " + "─" * max(width - len(display_status) - 8, 1))
        self._renderer.draw(lines, columns=width)

    def _write(self, text: str, *, end: str = "\n", stream: TextIO | None = None) -> None:
        target = stream or self.output
        target.write(text + end)
        target.flush()


async def run_viewer(
    service: Any,
    task_id: str | None = None,
    *,
    output: TextIO | None = None,
    error: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
    mascot: str | None = None,
) -> int:
    viewer = OIStreamViewer(
        service=service,
        task_id=task_id,
        output=output,
        error=error,
        input_fn=input_fn,
        mascot=mascot,
    )
    viewer.open()
    try:
        cursor = 0  # rowid for global tail
        if viewer.task_id:
            # Task-scoped: stream_events yields incrementally while the task runs;
            # handle+render each event as it arrives, track highest sequence seen,
            # and return once the generator ends at a terminal status.
            last_seq = -1
            async for ev in service.stream_events(viewer.task_id, after_sequence=0):
                seq = getattr(ev, "sequence", None)
                if isinstance(seq, int) and seq > last_seq:
                    last_seq = seq
                await viewer.handle_event(ev)
                viewer.render()
            # Terminal: one final render so nothing appended since the last event
            # frame is missed, then exit cleanly instead of looping forever.
            viewer.render()
            return 0
        # Global tail: cursor-poll list_recent forever.
        while True:
            items = await service._require_events().list_recent(after_rowid=cursor)
            for ev in items:
                rid = getattr(ev, "_rowid", None)
                if isinstance(rid, int):
                    cursor = max(cursor, rid)
                await viewer.handle_event(ev)
            if items:
                viewer.render()
            await asyncio.sleep(0.15)
    finally:
        viewer.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="athena oi-stream")
    parser.add_argument("--task", default=None)
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--db", dest="db_path", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--mascot", default=None)
    ns = parser.parse_args(argv)
    task_id = ns.task

    from athena.cli.app import build_config, build_service

    options = _opts(ns.db_path)
    options.config_path = ns.config_path
    options.workspace = ns.workspace
    options.mascot = ns.mascot
    config = build_config(options)
    service = build_service(config)

    from athena.cli.dual_pane import configure_mascots

    configure_mascots(getattr(config, "mascots", None))

    async def _runner():
        await service.start()
        try:
            return await run_viewer(
                service, task_id, mascot=getattr(config, "mascot", None)
            )
        finally:
            await service.stop()

    try:
        return asyncio.run(_runner())
    except KeyboardInterrupt:
        return 0


def _opts(db_path: str | None):
    from athena.cli.app import Options

    o = Options()
    o.db_path = db_path
    return o


if __name__ == "__main__":
    sys.exit(main())
