"""`athena oi-stream` — the live OI window as a first-class pane.

A pure subscriber to the canonical event log (INV-007 safe: read-only).
Renders unbuffered model deltas + runtime output with the activity mascot,
and can resolve approvals inline when policy parks a task.

Usage:
    athena oi-stream [--task TASK_ID] [--db PATH] [--height N]
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from athena.cli.dual_pane import Mascot, _OIWindow
from athena.protocol.events import Event

_CLEAR = "\x1b[2J\x1b[H"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

_ERR_PREFIX = "[err] "


class OIStreamViewer:
    """Full-pane OI window: raw stream + mascot header + inline approvals."""

    def __init__(
        self,
        *,
        service: Any = None,
        task_id: str | None = None,
        max_lines: int = 400,
        interactive: bool | None = None,
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.max_lines = max_lines
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.mascot = Mascot()
        self.window = _OIWindow(max_lines=max_lines)
        self._pending_approval: dict | None = None
        self._status = "starting"
        self._err_partial = ""  # unterminated stderr fragment across chunks

    # -- ingestion ------------------------------------------------------
    async def handle_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        self.mascot.observe(etype)

        if etype == "ModelDelta":
            text = str(payload.get("text") or "")
            if text:
                self.window.feed_delta(text)
        elif etype == "ModelResponseCompleted":
            # Non-streaming providers: payload carries only provider/model,
            # so pull the final answer from the task's durable result.
            text = str(payload.get("text") or payload.get("summary") or "")
            if not text and self.service is not None:
                try:
                    task_id = getattr(event, "task_id", None)
                    if task_id:
                        result = await self.service.get_result(task_id)
                        text = str(getattr(result, "summary", "") or "")
                except Exception:
                    pass
            if text:
                final = f"◆ {text}"
                tail = (self.window._partial or "").strip()
                committed = list(self.window.lines)[-1:] if self.window.lines else []
                last = tail or (committed[-1] if committed else "")
                if last.strip() != final.strip():
                    self.window.seal_partial()
                    self.window.feed(final + "\n")
        elif etype == "CapabilityCompleted":
            out = str(payload.get("output") or "")
            if out.strip():
                self.window.seal_partial()
                self.window.feed(out if out.endswith("\n") else out + "\n")
        elif etype == "StdoutChunk":
            self.window.feed(str(payload.get("data") or ""))
        elif etype == "StderrChunk":
            data = str(payload.get("data") or "")
            if data:
                # Rejoin a fragment split across chunks: drop the held
                # delta-view partial, prepend it, and re-feed as one stream.
                held = f"{_ERR_PREFIX}{self._err_partial}" if self._err_partial else ""
                if held and self.window._partial == held:
                    self.window._partial = ""
                data = self._err_partial + data
                self._err_partial = ""
                lines = data.split("\n")
                tail = lines.pop()  # unterminated partial after last newline
                for ln in lines:
                    self.window.feed(f"{_ERR_PREFIX}{ln}\n")
                if tail:
                    # Partial line: hold as delta so the next chunk continues it.
                    self._err_partial = tail
                    self.window.feed_delta(f"{_ERR_PREFIX}{tail}")
        elif etype == "CapabilityRequested":
            args = payload.get("arguments") or {}
            code = args.get("code")
            if code:
                first = str(code).splitlines()[0][:100]
                lang = args.get("language", "?")
                self.window.seal_partial()
                self.window.feed(f"$ [{lang}] {first}\n")
        elif etype == "ApprovalRequested":
            self._pending_approval = payload
            self._status = "WAITING FOR APPROVAL"
            await self._offer_approval(payload)
        elif etype == "ApprovalResolved":
            self._pending_approval = None
            self._status = "running"
        elif etype.startswith("Task"):
            self._status = etype.removeprefix("Task").lower() or self._status

    # -- approvals -------------------------------------------------------
    async def _offer_approval(self, payload: dict) -> None:
        """Inline decision surface inside the OI pane."""
        scopes = [str(s) for s in payload.get("scopes") or ()] or ["call"]
        aid = payload.get("approval_id")
        cap = payload.get("capability_id") or "capability"
        print(f"\n{_BOLD}APPROVAL REQUIRED{_RESET}  capability={cap}  id={aid}")
        for i, s in enumerate(scopes, 1):
            print(f"  {i}) {s}")
        print("  d) deny")
        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                None, input, f"approve [{1}-{len(scopes)}/d]> "
            )
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
        self._pending_approval = None
        self._status = "running"

    # -- rendering --------------------------------------------------------
    def render(self, height: int = 24) -> None:
        m = self.mascot.render(max_width=30)
        state = self.mascot.state.upper()
        print(_CLEAR)
        print(f"{_BOLD}╭─ OI LIVE ─ state: {state} ─ {'─' * 20}{_RESET}")
        mascot_lines = m[:3]
        body_height = max(height - len(mascot_lines) - 2, 1)
        body = self.window.snapshot(body_height, width=58)
        for i in range(body_height):
            text = body[i] if i < len(body) else ""
            is_err = text.startswith(_ERR_PREFIX) or text.startswith("[err]")
            color = "\x1b[31m" if is_err else ""
            side = mascot_lines[i] if i < len(mascot_lines) else ""
            print(f"{color}{text:<58}{_RESET}{side}")
        for ln in mascot_lines[len(body):]:
            print(ln)
        tail = f"{_DIM}{self._status}{_RESET}"
        print(f"╰─ {tail} " + "─" * 30)


async def run_viewer(service: Any, task_id: str | None = None) -> int:
    viewer = OIStreamViewer(service=service, task_id=task_id)
    cursor = 0  # rowid for global tail
    print(_CLEAR)
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    task_id = None
    if "--task" in argv:
        i = argv.index("--task")
        task_id = argv[i + 1] if i + 1 < len(argv) else None
    db_path = os.environ.get("ATHENA_DB")
    from athena.cli.app import build_config, build_service

    config = build_config(_opts(db_path))
    service = build_service(config)

    async def _runner():
        await service.start()
        try:
            return await run_viewer(service, task_id)
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
