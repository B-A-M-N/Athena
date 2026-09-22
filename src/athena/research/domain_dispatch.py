"""Research command dispatch mechanism subordinate to ResearchService."""

from __future__ import annotations

import json
import sqlite3

from athena.research.commands import ResearchCommand, ResearchResult
from typing import TYPE_CHECKING, Any

from athena.research.internal import InternalContext, InternalRequest
from athena.research.result_codec import result as _result
from athena.research.policy import SourcePolicyError


if TYPE_CHECKING:
    pass


class RunCommandMixin:
    """Own ResearchCommand -> ResearchResult dispatch without transport shape."""

    _store: Any
    _fetch: Any
    _discover: Any
    _record_source: Any
    _sources: Any
    _search: Any
    _record_evidence: Any
    _evidence: Any
    _close_gap: Any
    _verify: Any
    _plan: Any
    _assess: Any
    _critique: Any
    _bundle: Any
    _run: Any
    _record_gap: Any
    _gaps: Any

    async def run_command(self, command: ResearchCommand) -> ResearchResult:
        """Domain entry point: ResearchCommand in, ResearchResult out."""
        request = InternalRequest(
            arguments=dict(command.arguments),
            task_id=command.task_id,
            session_id=command.session_id,
            call_id=command.call_id or command.operation,
        )
        context = InternalContext(project_id=command.project_id)
        args = dict(command.arguments)
        operation = str(command.operation)
        if self._store is None:
            return ResearchResult.failure(operation, "research store not available")
        handlers = {
            "fetch": self._fetch,
            "discover": self._discover,
            "record_source": self._record_source,
            "sources": self._sources,
            "search": self._search,
            "record_evidence": self._record_evidence,
            "evidence": self._evidence,
            "close_gap": self._close_gap,
            "verify": self._verify,
            "plan": self._plan,
            "assess": self._assess,
            "critique": self._critique,
            "bundle": self._bundle,
            "run": self._run,
        }
        handler = handlers.get(operation)
        try:
            if handler is not None:
                result = await handler(request, args, context)
            elif operation == "record_gap":
                result = await self._record_gap(request, args)
            elif operation == "gaps":
                result = await self._gaps(request, args)
            else:
                result = _result(request, ok=False, error=f"unknown operation: {operation}")
        except (KeyError, SourcePolicyError, ValueError) as exc:
            return ResearchResult.failure(operation, str(exc))
        except (OSError, RuntimeError, TypeError, sqlite3.Error) as exc:
            return ResearchResult.failure(operation, f"research operation failed: {exc}")
        if result.status.value != "ok":
            return ResearchResult.failure(operation, str(result.error or "failed"))
        try:
            payload = json.loads(result.output) if result.output else {}
        except (TypeError, ValueError):
            payload = {"raw": result.output}
        payload["_raw_output"] = result.output
        return ResearchResult.success(operation, payload)
