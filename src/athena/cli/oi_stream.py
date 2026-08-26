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

from athena.cli.dual_pane import Mascot
from athena.protocol.events import Event

_CLEAR = "\x1b[2J\x1b[H"
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
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.max_lines = max_lines
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.mascot = Mascot()
        self.lines: list[tuple[str, bool]] = []  # (text, is_err)
        self._pending_approval: dict | None = None
        self._status = "starting"

    # -- ingestion ------------------------------------------------------
    async def handle_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        self.mascot.observe(etype)

        if etype == "ModelDelta":
            text = str(payload.get("text") or "")
            if text:
                self._append(text, err=False)
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
            if text and (not self.lines or self.lines[-1][0] != f"◆ {text}"):
                self._append(f"◆ {text}", err=False)
        elif etype == "CapabilityCompleted":
            out = str(payload.get("output") or "")
            if out.strip():
                self._append(out, err=False)
        elif etype == "StdoutChunk":
            self._append(str(payload.get("data") or ""), err=False)
        elif etype == "StderrChunk":
            self._append(str(payload.get("data") or ""), err=True)
        elif etype == "CapabilityRequested":
            args = payload.get("arguments") or {}
            code = args.get("code")
            if code:
                first = str(code).splitlines()[0][:100]
                lang = args.get("language", "?")
                self._append(f"$ [{lang}] {first}", err=False)
        elif etype == "ApprovalRequested":
            self._pending_approval = payload
            self._status = "WAITING FOR APPROVAL"
            await self._offer_approval(payload)
        elif etype == "ApprovalResolved":
            self._pending_approval = None
            self._status = "running"
        elif etype.startswith("Task"):
            self._status = etype.removeprefix("Task").lower() or self._status

    def _append(self, text: str, *, err: bool) -> None:
        for i, ln in enumerate(text.splitlines()):
            prefix = "[err] " if err else ""
            self.lines.append((f"{prefix}{ln}" if not err else f"[err] {ln}", err))
        if text.endswith("\n") is False and self.lines:
            pass  # partial tail kept as-is; next chunk continues naturally

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
        body = self.lines[-max(height - len(mascot_lines) - 2, 1):]
        for i in range(max(height - len(mascot_lines) - 2, 1)):
            text, is_err = body[i] if i < len(body) else ("", False)
            color = "\x1b[31m" if is_err else ""
            side = mascot_lines[i] if i < len(mascot_lines) else ""
            print(f"{color}{text:<58}{_RESET}{side}")
        for ln in mascot_lines[len(body):]:
            print(ln)
        tail = f"{_DIM}{self._status}{_RESET}"
        print(f"╰─ {tail} " + "─" * 30)


async def run_viewer(service: Any, task_id: str | None = None) -> int:
    viewer = OIStreamViewer(service=service, task_id=task_id)
    cursor = 0  # rowid for global tail, per-task sequence for single task
    print(_CLEAR)
    while True:
        events = []
        if viewer.task_id:
            async for ev in service.stream_events(viewer.task_id, after_sequence=0):
                seq = getattr(ev, "sequence", None) or 0
                if seq > (getattr(ev, "_last_seen", -1)):
                    pass
                events.append(ev)
            # stream_events already tails live; consume once then break out
            # of the batch and re-render; loop below re-polls.
            for ev in events[-50:]:
                await viewer.handle_event(ev)
            if not events:
                await asyncio.sleep(0.2)
            continue
        else:
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
