"""Generated child-process runtime mechanism (P1-10 extraction).

Spawn, communicate, and persistent-session machinery for synthesized
capability children, moved verbatim from :mod:`athena.synthesis.engine`.
Subordinate to SynthesisEngine: this module holds no authority of its own —
effect scoping, admission, and proof decisions stay on the engine.

Patch seams: tests patch ``athena.synthesis.engine.<name>`` (e.g.
``sandbox_argv``), so process-tree helpers resolve through the engine
module at call time via :func:`_mod`; the engine keeps re-exports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from athena.execution.process_tree import (
    kill_tree,
    kill_tree_async,
    sandbox_argv,
    spawn_owned,
)
from athena.protocol.resources import TaskResourceCloseResult
from athena.synthesis.runtime import GeneratedToolHost, PersistentGeneratedSession

if TYPE_CHECKING:
    from athena.synthesis.models import SyntheticCapability as SyntheticCapabilityT
    from athena.synthesis.engine import SynthesisEngine

_logger = logging.getLogger(__name__)

__all__ = ["ChildRuntime", "_namespace_python_paths"]


class ChildRuntime:
    """Spawn/communicate/persistent-session machinery for generated children.

    Verbatim extraction from ``SynthesisEngine``: engine-owned state and the
    patch-sensitive engine bindings resolve through ``self._e`` at call time.
    """

    def __init__(self, engine: SynthesisEngine) -> None:
        self._e = engine

    def _child_env(self, python_paths: Sequence[str] = ()) -> dict:
        if not self._e._restricted_env:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        else:
            allowed = ("PATH", "PYTHONIOENCODING", "LANG", "LC_ALL", "TMPDIR")
            env = {k: os.environ[k] for k in allowed if k in os.environ}
            env["PYTHONIOENCODING"] = "utf-8"
        if python_paths:
            env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return env

    def _run_child(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None = None,
        effects: set[str] | None = None,
        python_paths: Sequence[str] = (),
    ) -> tuple[str, str, int]:
        """Run generated code inside the same namespace boundary as runtimes.

        A subprocess with a sanitized environment is not a sandbox: Python
        can still open arbitrary host paths.  Bubblewrap is therefore required
        here as well.  Read-only synthetic capabilities receive a read-only
        workspace; write/delete effects explicitly receive writable scope.
        """
        owned_root = workspace_root is None
        root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-")
        values = effects or set()
        writable = bool({"WRITE_LOCAL", "DELETE"} & values)
        network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
        proc = None
        try:
            proc = spawn_owned(
                [sys.executable, "-c", child],
                env=self._e._child_env(python_paths),
                sandbox_root=root,
                network_policy=network,
                sandbox_writable=writable,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(input=payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                stdout, stderr = proc.communicate()
                return stdout, stderr or "synthetic execution timed out", 124
            return stdout, stderr, proc.returncode
        finally:
            if owned_root:
                shutil.rmtree(root, ignore_errors=True)

    async def _run_child_async(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None = None,
        effects: set[str] | None = None,
        python_paths: Sequence[str] = (),
        host: GeneratedToolHost | None = None,
    ) -> tuple[str, str, int]:
        """Async equivalent of :meth:`_run_child` for live validation/invocation.

        Validation and generated capability calls are part of the async agent
        path.  Using ``asyncio.create_subprocess_exec`` keeps the event loop
        responsive and avoids relying on thread-pool subprocess semantics.
        The command line is built by the same fail-closed Bubblewrap policy as
        the synchronous compatibility path.
        """
        owned_root = workspace_root is None
        root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-")
        values = effects or set()
        writable = bool({"WRITE_LOCAL", "DELETE"} & values)
        network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
        proc = None
        try:
            argv = sandbox_argv(
                [sys.executable, "-c", child],
                root=root,
                network_policy=network,
                writable=writable,
            )
            proc = await asyncio.create_subprocess_exec(  # architecture-lint: allow subprocess-outside-approved-backends reason=generated validation worker; architecture-exception: synthesis-validation-worker
                *argv,
                env=self._e._child_env(_namespace_python_paths(python_paths, root)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                if host is None:
                    # ``athena.call`` is still injected so source validation
                    # and execution use one stable contract.  Without a
                    # parent host, answer the first mediated request with a
                    # deterministic failure instead of leaving the child
                    # blocked on stdin until the execution timeout.
                    unavailable = json.dumps(
                        {
                            "ok": False,
                            "error": "generated host is unavailable in this context",
                        }
                    )
                    # write/readline/read_all avoids a rare pipe/register
                    # race in ``communicate()`` observed on constrained
                    # hosts where an exited child can leave the write-end
                    # registered and block EOF delivery until timeout.
                    assert proc.stdin is not None
                    assert proc.stdout is not None
                    assert proc.stderr is not None
                    proc.stdin.write((payload + "\n" + unavailable + "\n").encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                    stdout_task = asyncio.create_task(proc.stdout.read())
                    stderr_task = asyncio.create_task(proc.stderr.read())
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task),
                        timeout,
                    )
                else:
                    stdout, stderr = await asyncio.wait_for(
                        self._communicate_with_host(proc, payload, host),
                        timeout=timeout,
                    )
            except TimeoutError:
                await kill_tree_async(proc)
                stdout, stderr = await proc.communicate()
                return (
                    stdout.decode("utf-8", errors="replace"),
                    stderr.decode("utf-8", errors="replace") or "synthetic execution timed out",
                    124,
                )
            decoded = stdout.decode("utf-8", errors="replace")
            if decoded.startswith("__RESULT__"):
                returncode = 0
            else:
                returncode = proc.returncode if proc.returncode is not None else 1
            return (
                decoded,
                stderr.decode("utf-8", errors="replace"),
                returncode,
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await asyncio.shield(kill_tree_async(proc))
                await asyncio.shield(proc.communicate())
            raise
        finally:
            if owned_root:
                shutil.rmtree(root, ignore_errors=True)

    async def _run_persistent_child_async(
        self,
        child: str,
        payload: str,
        *,
        timeout: float,
        workspace_root: str | None,
        effects: set[str] | None,
        python_paths: Sequence[str],
        host: GeneratedToolHost | None,
        session_key: tuple[str, str, str],
    ) -> tuple[str, str, int]:
        """Run one call on the task/workspace-scoped generated process."""
        handle = self._e._persistent_sessions.get(session_key)
        if handle is None or handle[0].closed:
            if handle is not None:
                await handle[0].close()
                if handle[2]:
                    shutil.rmtree(handle[1], ignore_errors=True)
            owned_root = workspace_root is None
            root = workspace_root or tempfile.mkdtemp(prefix="athena-synth-persistent-")
            values = effects or set()
            writable = bool({"WRITE_LOCAL", "DELETE"} & values)
            network = "allow" if {"NETWORK_READ", "NETWORK_WRITE"} & values else "deny"
            try:
                session = PersistentGeneratedSession(
                    sandbox_argv(
                        [sys.executable, "-c", child],
                        root=root,
                        network_policy=network,
                        writable=writable,
                    ),
                    env=self._e._child_env(_namespace_python_paths(python_paths, root)),
                )
                await session.start()
            except BaseException:
                if owned_root:
                    shutil.rmtree(root, ignore_errors=True)
                raise
            handle = (session, root, owned_root)
            self._e._persistent_sessions[session_key] = handle
        result = await handle[0].invoke(payload, host, timeout=timeout)
        if handle[0].closed:
            self._e._persistent_sessions.pop(session_key, None)
            if handle[2]:
                shutil.rmtree(handle[1], ignore_errors=True)
        return result

    async def close_persistent_sessions(self) -> TaskResourceCloseResult:
        """Stop all generated persistent runtimes during service shutdown."""
        handles = tuple(self._e._persistent_sessions.items())
        return await self._e._close_persistent_handles(handles)

    async def close_persistent_sessions_for_task(self, task_id: str) -> TaskResourceCloseResult:
        """Stop task-owned generated state when the task reaches a terminal state."""
        selected = tuple(
            (key, self._e._persistent_sessions[key])
            for key in tuple(self._e._persistent_sessions)
            if key[1] == task_id
        )
        return await self._e._close_persistent_handles(selected)

    async def _close_persistent_handles(self, handles) -> TaskResourceCloseResult:
        resource_ids = tuple("/".join(str(part) for part in key) for key, _ in handles)
        closed_ids: list[str] = []
        unproven: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        task_ids = {str(key[1]) for key, _ in handles}
        task_id = next(iter(task_ids), "") if len(task_ids) == 1 else "service"
        for key, (session, root, owned_root) in handles:
            resource_id = "/".join(str(part) for part in key)
            try:
                outcome = await session.close()
                if not getattr(outcome, "proven_dead", False):
                    unproven.append(
                        {
                            "session_id": resource_id,
                            "survivors": list(getattr(outcome, "survivors", ()) or ()),
                        }
                    )
                    continue
                self._e._persistent_sessions.pop(key, None)
                closed_ids.append(resource_id)
                if owned_root:
                    shutil.rmtree(root, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 - preserve each resource obligation
                errors.append({"session_id": resource_id, "error": str(exc)})
                _logger.warning("persistent generated runtime close failed: %s", exc)
        return TaskResourceCloseResult(
            task_id=task_id,
            resource_type="generated_runtime",
            resource_ids=resource_ids,
            closed_ids=tuple(closed_ids),
            unproven=tuple(unproven),
            errors=tuple(errors),
        )

    async def _run_generated_child(
        self,
        *,
        cap: SyntheticCapabilityT,
        child: str,
        payload: str,
        timeout: float,
        workspace_root: str | None,
        effects: set[str],
        python_paths: Sequence[str],
        context,
        request,
    ) -> tuple[str, str, int]:
        host = (
            GeneratedToolHost(
                dispatcher=self._e._dispatcher,
                workspace=context.workspace,
                task_id=request.task_id,
                session_id=getattr(request, "session_id", None),
                profile=getattr(context, "autonomy", None),
                task_policy=getattr(context, "capability_policy", None),
                task_budget=getattr(context, "resource_budget", None),
                call_depth=getattr(context, "generated_call_depth", 0),
                call_chain=tuple(getattr(context, "generated_call_chain", ())) + (cap.id,),
                allowed_capabilities=frozenset(cap.required_capabilities),
                inherited_effects=frozenset(
                    getattr(
                        getattr(context, "directives", None),
                        "inherited_effects",
                        (),
                    )
                ),
                inherited_capability_id=getattr(
                    getattr(context, "directives", None),
                    "inherited_capability_id",
                    None,
                ),
            )
            if self._e._dispatcher is not None and context is not None
            else None
        )
        if cap.runtime != "python_persistent" or request.task_id is None:
            result = await self._e._run_child_async(
                child,
                payload,
                timeout=timeout,
                workspace_root=workspace_root,
                effects=effects,
                python_paths=python_paths,
                host=host,
            )
            # A child may lose its entire timeout budget to sandbox/process
            # startup when the host is heavily contended.  Read-only children
            # are idempotent and safe to retry once; write/delete/network-write
            # children deliberately do not retry so an unknown timed-out effect
            # is never duplicated.
            retryable_effects = {"WRITE_LOCAL", "DELETE", "NETWORK_WRITE"}
            if (
                result[2] == 124
                and result[1].strip() == "synthetic execution timed out"
                and not (set(effects or ()) & retryable_effects)
            ):
                # Startup contention can consume the first invocation budget.
                # A fresh retry doubles the wall-clock allowance for this
                # idempotent read/execute path without reducing its isolation.
                timeout *= 2
                result = await self._e._run_child_async(
                    child,
                    payload,
                    timeout=timeout,
                    workspace_root=workspace_root,
                    effects=effects,
                    python_paths=python_paths,
                    host=host,
                )
            return result
        root_key = os.path.realpath(os.path.abspath(workspace_root or f"<task:{request.task_id}>"))
        return await self._e._run_persistent_child_async(
            child,
            payload,
            timeout=timeout,
            workspace_root=workspace_root,
            effects=effects,
            python_paths=python_paths,
            host=host,
            session_key=(cap.id, str(request.task_id), root_key),
        )

    @staticmethod
    async def _communicate_with_host(proc, payload: str, host: GeneratedToolHost):
        """Serve framed ``__HOST__`` requests until the child returns."""
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write((payload + "\n").encode())
        await proc.stdin.drain()
        stderr_task = asyncio.create_task(proc.stderr.read())
        output: list[bytes] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if line.startswith(b"__HOST__"):
                try:
                    request = json.loads(line[len(b"__HOST__") :])
                    value = await host.call(request["capability_id"], request["arguments"])
                    response = {"ok": True, "value": value}
                except Exception as exc:  # noqa: BLE001 - return failure to child
                    response = {"ok": False, "error": str(exc)}
                proc.stdin.write((json.dumps(response) + "\n").encode())
                await proc.stdin.drain()
                continue
            output.append(line)
            if line.startswith(b"__RESULT__"):
                break
        if not proc.stdin.is_closing():
            proc.stdin.close()
        # The result marker is authoritative: child output proves completion.
        # Avoid waiting on the host process transition because bwrap teardown
        # can transiently retain pipe registration on constrained hosts.
        return b"".join(output), await stderr_task


def _namespace_python_paths(paths: Sequence[str], root: str) -> tuple[str, ...]:
    """Map host workspace paths to the sandbox's ``/workspace`` mount."""
    root_abs = os.path.realpath(os.path.abspath(root))
    mapped: list[str] = []
    for path in paths:
        path_abs = os.path.realpath(os.path.abspath(path))
        if path_abs == root_abs or path_abs.startswith(root_abs + os.sep):
            mapped.append("/workspace" + path_abs[len(root_abs) :])
        else:
            raise ValueError("dependency import path escaped workspace")
    return tuple(mapped)
