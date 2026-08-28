"""Shared semantic classification for operator-surface activity.

This module is presentation policy only. It does not infer task state; it
maps already-emitted capability/event facts to a stable visual vocabulary so
the projection, mascot, Glass renderer, and native bridge cannot disagree
about whether Athena is reading, coding, testing, or verifying.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping


class VisualActionKind(str, enum.Enum):
    IDLE = "idle"
    THINK = "think"
    RESPOND = "respond"
    INSPECT = "inspect"
    READ = "read"
    SEARCH = "search"
    CODE = "code"
    EXECUTE = "execute"
    TEST = "test"
    VERIFY = "verify"
    GENERATE = "generate"
    APPROVAL = "approval"
    RECOVER = "recover"
    FAILURE = "failure"


_TEST_WORDS = re.compile(r"(?:pytest|test|cargo\s+test|go\s+test|npm\s+test|jest)", re.I)
_VERIFY_WORDS = re.compile(r"(?:check|lint|mypy|typecheck|tsc|vet|validate)", re.I)
_WRITE_OPS = frozenset(
    {
        "write",
        "patch",
        "mkdir",
        "copy",
        "move",
        "create",
        "update",
        "append",
        "delete",
        "remove",
        "rename",
    }
)
_READ_OPS = frozenset({"read", "list", "stat", "exists", "inspect", "tree"})


def classify_visual_action(
    capability_id: object = "",
    *,
    operation: object = "",
    arguments: Mapping[str, object] | None = None,
    effects: object = None,
    event_type: object = "",
    status: object = "",
) -> VisualActionKind:
    """Classify one observable action using capability, operation, and data."""
    capability = str(capability_id or "").casefold()
    op = str(operation or "").casefold()
    event = str(event_type or "").casefold()
    state = str(status or "").casefold()
    args = arguments if isinstance(arguments, Mapping) else {}
    command = str(args.get("command") or args.get("code") or "")
    combined = " ".join((capability, op, command)).casefold()

    if state in {"failed", "failure", "error", "rejected"} or event.endswith("failed"):
        return VisualActionKind.FAILURE
    if "approval" in event or capability in {"approval", "approvals"}:
        return VisualActionKind.APPROVAL
    if event.startswith(("recovery", "rollback")) or "recover" in combined:
        return VisualActionKind.RECOVER
    if event.startswith(("modelrequest", "contextbuild", "taskiteration")):
        return VisualActionKind.THINK
    if event in {"modeldelta", "modelresponsecompleted"}:
        return VisualActionKind.RESPOND
    if event.startswith(("verification", "acceptance")):
        return VisualActionKind.VERIFY
    if event.startswith(("search", "research")) or "search" in combined:
        return VisualActionKind.SEARCH
    if event in {"fileread", "inspectionstarted"}:
        return VisualActionKind.READ if event == "fileread" else VisualActionKind.INSPECT
    if capability in {"synthesis", "scratch"} or "generated" in capability:
        return VisualActionKind.GENERATE
    if op in _WRITE_OPS or any(word in capability for word in ("write", "patch", "refactor")):
        return VisualActionKind.CODE
    if _TEST_WORDS.search(combined):
        return VisualActionKind.TEST
    if _VERIFY_WORDS.search(combined):
        return VisualActionKind.VERIFY
    if op in _READ_OPS or capability in {"fs.read", "files.read", "read"}:
        return VisualActionKind.READ
    if capability in {"workspace", "workspace.impact", "git.diff", "inspect"} or op in {
        "impact",
        "diff",
        "overview",
    }:
        return VisualActionKind.INSPECT
    if capability in {"execute", "shell", "process", "terminal_session", "debugger"}:
        return VisualActionKind.EXECUTE
    if isinstance(effects, (list, tuple, set, frozenset)) and any(
        "write" in str(effect).casefold() for effect in effects
    ):
        return VisualActionKind.CODE
    return VisualActionKind.IDLE


def classify_event(
    event_type: object, payload: Mapping[str, object] | None = None
) -> VisualActionKind:
    """Classify one canonical event for all presentation consumers."""
    data = payload if isinstance(payload, Mapping) else {}
    raw_args = data.get("arguments")
    args: Mapping[str, object] = raw_args if isinstance(raw_args, Mapping) else {}
    return classify_visual_action(
        data.get("capability_id") or data.get("capability") or data.get("runtime"),
        operation=data.get("operation")
        or data.get("action")
        or args.get("operation")
        or args.get("action"),
        arguments=args,
        effects=data.get("effects"),
        event_type=event_type,
        status=data.get("status") or data.get("decision") or data.get("state"),
    )


def language_for_path(path: object) -> str:
    """Return a small deterministic language label for code previews."""
    value = str(path or "").casefold()
    suffix = value.rsplit(".", 1)[-1] if "." in value else ""
    return {
        "py": "python",
        "rs": "rust",
        "go": "go",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "java": "java",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "cc": "cpp",
        "json": "json",
        "toml": "toml",
        "yaml": "yaml",
        "yml": "yaml",
        "md": "markdown",
        "sh": "shell",
        "bash": "shell",
    }.get(suffix, "text")


__all__ = [
    "VisualActionKind",
    "classify_event",
    "classify_visual_action",
    "language_for_path",
]
