"""Read-only project Git intelligence.

Git is an observation surface here, not Athena's mutation authority. Every
command is executed without a shell from the routed workspace root, and only
the explicit read operations below are exposed to the model.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

_MAX_OUTPUT = 100_000
_REF = re.compile(r"^[A-Za-z0-9_./:@+\-~^]+$")


def _result(
    request: CapabilityRequest,
    *,
    ok: bool,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output[:_MAX_OUTPUT],
        error=error,
        metadata=dict(metadata or {}),
    )


class GitCapability:
    """Expose safe project history and working-tree observations."""

    descriptor = CapabilityDescriptor(
        id="git",
        description=(
            "Read-only Git project intelligence: status, diff, history, "
            "file attribution, branch, merge-base, and the current baseline. "
            "Git mutation commands are intentionally not exposed. Operations: "
            "status/diff/log/show/blame/branch/merge_base/baseline."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "diff",
                        "log",
                        "show",
                        "blame",
                        "branch",
                        "merge_base",
                        "baseline",
                    ],
                },
                "path": {"type": "string", "maxLength": 4096},
                "ref": {"type": "string", "maxLength": 256},
                "other_ref": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "start": {"type": "integer", "minimum": 1},
                "end": {"type": "integer", "minimum": 1},
                "cached": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request: CapabilityRequest, *, context=None, **_) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        workspace = getattr(context, "workspace", None)
        root = os.path.realpath(os.path.abspath(getattr(workspace, "root", "")))
        if not root or not os.path.isdir(root):
            return _result(request, ok=False, error="workspace root is unavailable")

        try:
            command = self._command(operation, args, root)
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))
        loop = asyncio.get_running_loop()
        rc, stdout, stderr = await loop.run_in_executor(None, lambda: _run_git(command, root))
        metadata = {
            "operation": operation,
            "returncode": rc,
            "workspace_root": root,
        }
        if rc != 0:
            return _result(
                request,
                ok=False,
                output=stdout,
                error=(stderr or stdout or "git command failed").strip(),
                metadata=metadata,
            )
        return _result(request, ok=True, output=stdout, metadata=metadata)

    @classmethod
    def _command(cls, operation: str, args: dict[str, Any], root: str) -> list[str]:
        path = args.get("path")
        if path is not None:
            path = cls._workspace_path(str(path), root)
            path_arg = os.path.relpath(path, root)
        else:
            path_arg = None

        if operation == "status":
            return ["git", "status", "--porcelain=v1", "--branch"]
        if operation == "diff":
            command = ["git", "diff"]
            if args.get("cached"):
                command.append("--cached")
            if path_arg is not None:
                command.extend(["--", path_arg])
            return command
        if operation == "log":
            command = [
                "git",
                "log",
                f"-n{int(args.get('limit') or 20)}",
                "--date=iso-strict",
                "--format=%H%n%aI%n%an%n%s",
            ]
            if path_arg is not None:
                command.extend(["--", path_arg])
            return command
        if operation == "show":
            ref = cls._ref(args.get("ref") or "HEAD")
            command = ["git", "show", "--stat", "--oneline", ref]
            if path_arg is not None:
                command.extend(["--", path_arg])
            return command
        if operation == "blame":
            if path_arg is None:
                raise ValueError("blame requires path")
            start = int(args.get("start") or 1)
            end = int(args.get("end") or start + 99)
            if end < start:
                raise ValueError("blame end must be greater than or equal to start")
            return ["git", "blame", "-L", f"{start},{end}", "--", path_arg]
        if operation == "branch":
            return ["git", "branch", "--show-current"]
        if operation == "merge_base":
            first = cls._ref(args.get("ref") or "HEAD")
            second = cls._ref(args.get("other_ref") or "HEAD~1")
            return ["git", "merge-base", first, second]
        if operation == "baseline":
            return [
                "git",
                "rev-parse",
                "--show-toplevel",
                "HEAD",
                "--abbrev-ref",
                "HEAD",
            ]
        raise ValueError(f"unknown git operation: {operation}")

    @staticmethod
    def _ref(value: Any) -> str:
        ref = str(value).strip()
        if not ref or not _REF.fullmatch(ref) or ref.startswith("-"):
            raise ValueError("invalid git ref")
        return ref

    @staticmethod
    def _workspace_path(value: str, root: str) -> str:
        candidate = os.path.realpath(
            os.path.abspath(value if os.path.isabs(value) else os.path.join(root, value))
        )
        if candidate != root and not candidate.startswith(root + os.sep):
            raise ValueError("git path is outside the workspace")
        return candidate


def _run_git(command: list[str], root: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out"
    except FileNotFoundError:
        return 127, "", "git executable not found"


__all__ = ["GitCapability"]
