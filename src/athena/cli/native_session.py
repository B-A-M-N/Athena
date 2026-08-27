"""Python service session hosted inside the native Athena terminal PTY."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from io import StringIO
from typing import Any

from athena.cli.app import Options, _autonomy, _model_policy, build_config, workspace_spec
from athena.cli.native_bridge import write_native_projection
from athena.cli.projection import ProjectionState
from athena.protocol.tasks import AgentRequest


class NativeSession:
    """One interactive service session with a projection socket publisher."""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.service: Any = None
        self.projection = ProjectionState()
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

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
        await self._send_projection()

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
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
        self.projection.reduce(event.type, event.payload)
        await self._send_projection()

    async def _send_projection(self) -> None:
        if self._writer is None:
            return
        output = StringIO()
        write_native_projection(output, self.projection, width=72, height=24)
        self._writer.write(output.getvalue().encode("utf-8"))
        await self._writer.drain()

    async def run(self) -> int:
        await self.start()
        try:
            print("ATHENA // NATIVE TERMINAL")
            print("Type a request. /help for commands; /exit to close.")
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
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
                task.add_done_callback(self._tasks.discard)
        finally:
            await self.close()

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
            } if self.options.criteria else {},
        )
        task = await self.service.submit(request, wait=False)
        task_id = getattr(task, "id", task)
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
