"""Python service session hosted inside the native Athena terminal PTY."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import termios
from io import StringIO
from typing import Any

from athena.cli.app import Options, _autonomy, _model_policy, build_config, workspace_spec
from athena.cli.native_bridge import write_native_projection
from athena.cli.projection import ProjectionState
from athena.protocol.tasks import AgentRequest

_FORCE_PROJECTION_EVENTS = frozenset(
    {
        "ModelRequestStarted",
        "ApprovalRequested",
        "TaskCompleted",
        "TaskFailed",
        "TaskCancelled",
        "RecoveryRequired",
    }
)


class NativeSession:
    """One interactive service session with a projection socket publisher."""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.service: Any = None
        self.projection = ProjectionState()
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._foreground_task: asyncio.Task[Any] | None = None
        self._foreground_task_id: str | None = None
        self._projection_task: asyncio.Task[Any] | None = None
        self._projection_lock = asyncio.Lock()
        self._projection_dirty = False
        self._projection_interval = 0.05

    async def start(self) -> None:
        socket_path = os.environ.get("ATHENA_NATIVE_BRIDGE_SOCKET")
        if not socket_path:
            raise RuntimeError("ATHENA_NATIVE_BRIDGE_SOCKET is not set")
        self._writer = await self._connect(socket_path)
        from athena.service.service import AthenaService

        self.service = AthenaService(config=build_config(self.options))
        await self.service.start()
        events = getattr(self.service, "_store_events", None)
        if events is None:
            raise RuntimeError("AthenaService did not expose its event store")
        events.subscribe(self._on_event)
        self._projection_dirty = True
        await self._flush_projection()

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._projection_task is not None:
            self._projection_task.cancel()
            await asyncio.gather(self._projection_task, return_exceptions=True)
            self._projection_task = None
        self._foreground_task = None
        self._foreground_task_id = None
        if self.service is not None:
            try:
                await self.service.stop()
            except Exception:
                pass
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()

    async def _connect(self, path: str) -> asyncio.StreamWriter:
        last_error: Exception | None = None
        for _ in range(100):
            try:
                _reader, writer = await asyncio.open_unix_connection(path)
                return writer
            except OSError as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise RuntimeError(f"could not connect native projection bridge: {last_error}")

    async def _on_event(self, event: Any) -> None:
        self.projection.reduce(event.type, event.payload, task_id=event.task_id)
        self._projection_dirty = True
        if self._projection_task is None or self._projection_task.done():
            self._projection_task = asyncio.create_task(self._debounced_projection())
        if event.type in _FORCE_PROJECTION_EVENTS:
            await self._flush_projection()

    async def _debounced_projection(self) -> None:
        """Coalesce event bursts; idle sessions send no bridge traffic."""
        while True:
            await asyncio.sleep(self._projection_interval)
            await self._flush_projection()
            if self._projection_dirty:
                continue
            # Clear the task marker before returning. An event arriving in
            # this handoff window can then schedule a new worker instead of
            # stranding dirty state behind a task that is about to finish.
            if self._projection_task is asyncio.current_task():
                self._projection_task = None
            return

    async def _flush_projection(self) -> None:
        if not self._projection_dirty:
            return
        async with self._projection_lock:
            if not self._projection_dirty:
                return
            self._projection_dirty = False
            try:
                await self._send_projection()
            except Exception:
                self._projection_dirty = True
                raise

    async def _send_projection(self) -> None:
        if self._writer is None:
            return
        output = StringIO()
        write_native_projection(
            output,
            self.projection,
            character=self.options.mascot or "owl",
        )
        self._writer.write(output.getvalue().encode("utf-8"))
        await self._writer.drain()

    async def run(self) -> int:
        input_attrs = self._disable_input_echo()
        try:
            await self.start()
            print("ATHENA // NATIVE TERMINAL")
            print("Type a request. /help for commands; /exit to close.")
            while True:
                try:
                    line = await self._readline()
                except KeyboardInterrupt:
                    await self._cancel_foreground()
                    continue
                if not line:
                    return 0
                line = line.strip()
                if not line:
                    continue
                if line in {"/exit", "/quit"}:
                    return 0
                if line == "/help":
                    print("/approve ID  /cancel ID  /exit")
                    continue
                if line.startswith("/approve "):
                    await self._approve(line.removeprefix("/approve ").strip())
                    continue
                if line.startswith("/cancel "):
                    await self._cancel(line.removeprefix("/cancel ").strip())
                    continue
                task = asyncio.create_task(self._submit(line))
                self._tasks.add(task)
                self._foreground_task = task
                task.add_done_callback(self._task_finished)
                task.add_done_callback(self._tasks.discard)
        finally:
            self._restore_input_echo(input_attrs)
            await self.close()

    async def _readline(self) -> str:
        """Read one completed native-editor line without worker-thread leaks."""
        if not sys.stdin.isatty():
            return await asyncio.to_thread(sys.stdin.readline)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        fd = sys.stdin.fileno()

        def ready() -> None:
            if future.done():
                return
            try:
                future.set_result(sys.stdin.readline())
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                loop.remove_reader(fd)

        loop.add_reader(fd, ready)
        try:
            return await future
        finally:
            loop.remove_reader(fd)

    @staticmethod
    def _disable_input_echo() -> list[Any] | None:
        """Keep line echo in the native prompt instead of the PTY grid."""
        if not sys.stdin.isatty():
            return None
        try:
            attrs = termios.tcgetattr(sys.stdin.fileno())
            updated = attrs.copy()
            updated[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, updated)
            return attrs
        except (OSError, termios.error):
            return None

    @staticmethod
    def _restore_input_echo(attrs: list[Any] | None) -> None:
        if attrs is None or not sys.stdin.isatty():
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
        except (OSError, termios.error):
            pass

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        if self._foreground_task is task:
            self._foreground_task = None
            self._foreground_task_id = None

    async def _cancel_foreground(self) -> None:
        """Ctrl-C cancels the foreground task, never the whole session."""
        task = self._foreground_task
        task_id = self._foreground_task_id
        if task_id and self.service is not None:
            try:
                await self.service.cancel(task_id)
            except Exception:
                pass
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._foreground_task is task:
            self._foreground_task = None
            self._foreground_task_id = None

    async def _submit(self, objective: str) -> None:
        print(f"\nYOU\n{objective}")
        request = AgentRequest(
            prompt=objective,
            autonomy=_autonomy(self.options.autonomy),
            workspace=workspace_spec(self.options.workspace),
            model_policy=_model_policy(self.options.model),
            metadata={
                "acceptance_criteria": [
                    item.strip()
                    for item in (self.options.criteria or "").split(";")
                    if item.strip()
                ],
            }
            if self.options.criteria
            else {},
        )
        task = await self.service.submit(request, wait=False)
        task_id = getattr(task, "id", task)
        self._foreground_task_id = str(task_id)
        print(f"ATHENA · task {task_id}")
        async for event in self.service.stream_events(task_id, after_sequence=0):
            if event.type == "ApprovalRequested":
                approval_id = (event.payload or {}).get("approval_id", "?")
                print(f"APPROVAL REQUIRED · /approve {approval_id}")
        result = await self.service.get_result(task_id)
        if result is None:
            print(f"ATHENA · task {task_id} has no final result")
            return
        status = getattr(result.status, "value", result.status)
        print(f"ATHENA [{status.upper()}]\n{getattr(result, 'summary', '') or ''}")

    async def _approve(self, approval_id: str) -> None:
        if not approval_id:
            print("approval id required")
            return
        await self.service.approve(approval_id, granted=True)
        print(f"approval {approval_id}: granted")

    async def _cancel(self, task_id: str) -> None:
        if not task_id:
            print("task id required")
            return
        await self.service.cancel(task_id)
        print(f"cancel requested for {task_id}")


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(prog="athena-native-session")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--db", dest="db_path")
    parser.add_argument("--workspace")
    parser.add_argument("--autonomy")
    parser.add_argument("--model")
    parser.add_argument("--criteria")
    parser.add_argument("--mascot")
    parser.add_argument("--no-animations", action="store_true")
    parser.add_argument("--reduced-motion", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return Options(
        command="native",
        config_path=args.config_path,
        db_path=args.db_path,
        workspace=args.workspace,
        autonomy=args.autonomy,
        model=args.model,
        criteria=args.criteria,
        mascot=args.mascot,
        animations=False if args.no_animations else None,
        reduced_motion=args.reduced_motion,
        verbose=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(NativeSession(parse_args(argv)).run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"athena native session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
