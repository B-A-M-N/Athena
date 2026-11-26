"""Read-only task, session, result, and event observation for the service facade."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from athena.protocol.tasks import TaskStatus

__all__ = ["TaskObservationService"]


class TaskObservationService:
    """Project canonical task/session stores without owning task state."""

    def __init__(
        self,
        *,
        task_store: Any,
        task_api: Any,
        schedule_store: Any,
        workflow_store: Any,
        default_workspace: Any,
        cache_namespace: str,
        sessions: Any,
        browser: Any,
        event_store: Any,
    ) -> None:
        self._task_store = task_store
        self._task_api = task_api
        self._schedule_store = schedule_store
        self._workflow_store = workflow_store
        self._default_workspace = default_workspace
        self._cache_namespace = cache_namespace
        self._sessions = sessions
        self._browser = browser
        self._event_store = event_store

    @staticmethod
    def _resolve(value: Any) -> Any:
        return value() if callable(value) else value

    async def get_task(self, task_id: str):
        return await self._task_api.get_task(task_id)

    async def list_tasks(self, status: TaskStatus | None = None) -> list[dict]:
        task_store = self._resolve(self._task_store)
        if task_store is None:
            return []
        if status is not None:
            return await task_store.list_by_status(status)
        rows: list[dict] = []
        for task_status in TaskStatus:
            rows.extend(await task_store.list_by_status(task_status))
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""))

    async def get_result(self, task_id: str):
        return await self._task_api.get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0) -> AsyncIterator[Any]:
        async for event in self._task_api.stream_events(task_id, after_sequence=after_sequence):
            yield event

    async def stream_all(self, after_rowid: int = 0, limit: int = 200) -> AsyncIterator[Any]:
        async for event in self._task_api.stream_all(after_rowid=after_rowid, limit=limit):
            yield event

    async def get_task_status(self, task_id: str) -> str | None:
        return await self._task_api.get_task_status(task_id)

    async def list_jobs(self, *, enabled_only: bool = False) -> list[dict]:
        schedule_store = self._resolve(self._schedule_store)
        if schedule_store is None:
            return []
        jobs = await schedule_store.list_jobs(enabled_only=enabled_only)
        for job in jobs:
            job["last_run_receipt"] = await schedule_store.last_run(job["id"])
        return jobs

    async def list_workflows(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        workflow_store = self._resolve(self._workflow_store)
        if workflow_store is None:
            return []
        workflows = await workflow_store.list(
            task_id=task_id,
            project_id=getattr(self._default_workspace, "id", None),
            user_id=self._cache_namespace,
        )
        return [workflow.to_record() for workflow in workflows]

    async def inspect_workflow(self, workflow_id: str, *, task_id: str | None = None):
        workflow_store = self._resolve(self._workflow_store)
        if workflow_store is None:
            return None
        workflow = await workflow_store.get(
            workflow_id,
            task_id=task_id,
            project_id=getattr(self._default_workspace, "id", None),
            user_id=self._cache_namespace,
        )
        return workflow.to_record() if workflow is not None else None

    async def list_sessions(self) -> list[dict]:
        sessions = self._resolve(self._sessions)
        if sessions is None:
            return []
        return await sessions.list_all()

    async def close_session(self, session_id: str) -> bool:
        sessions = self._resolve(self._sessions)
        if sessions is None:
            raise RuntimeError("AthenaService not started")
        if await sessions.get(session_id) is None:
            return False
        browser = self._resolve(self._browser)
        if browser is not None:
            close_session = getattr(browser, "close_session", None)
            if callable(close_session):
                await close_session(session_id)
        closed = await sessions.close(session_id)
        event_store = self._resolve(self._event_store)
        if closed and event_store is not None:
            await event_store.append_event(
                "SessionClosed", {"session_id": session_id}, session_id=session_id
            )
        return closed
