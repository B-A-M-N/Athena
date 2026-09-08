"""Historical session search: FTS-backed, closed-scope, provenance-anchored."""

from __future__ import annotations

import json

import pytest

from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.ids import new_id
from athena.protocol.messages import Message, Provenance, Role, SourceType, TextBlock, utcnow
from athena.state.database import Database
from athena.state.messages import MessageStore, sanitize_fts_query
from athena.state.sessions import SessionRepository
from athena.capabilities.session_search import SessionSearchCapability


async def _store_with_history() -> tuple[MessageStore, str]:
    db = Database(":memory:")
    store = MessageStore(db)
    from athena.state.sessions import SessionRepository

    sessions = SessionRepository(db)
    session = new_id("session")
    await sessions.create(session)
    for role, text in (
        (Role.USER, "we chose the rabbitmq approach for the ingest queue"),
        (Role.ASSISTANT, "confirmed: rabbitmq for ingest, postgres for state"),
        (Role.USER, "now deploy the ingest service"),
    ):
        await store.append_to_session(
            session,
            Message(
                id=new_id("msg"),
                role=role,
                blocks=(TextBlock(text=text),),
                created_at=utcnow(),
                provenance=Provenance(source_type=SourceType.USER),
                metadata={"task_id": "task-1"},
            ),
        )
    return store, session


@pytest.mark.asyncio
async def test_search_finds_historical_turn_by_content():
    store, session = await _store_with_history()

    hits = await store.search("rabbitmq", session_ids=(session,))

    assert len(hits) == 2
    assert all(hit["session_id"] == session for hit in hits)
    assert any("rabbitmq approach" in hit["text"] for hit in hits)
    assert all(hit["task_id"] == "task-1" for hit in hits)
    assert all(hit["created_at"] for hit in hits)


@pytest.mark.asyncio
async def test_search_is_scoped_to_named_sessions():
    store, session = await _store_with_history()
    other = new_id("session")
    from athena.state.sessions import SessionRepository

    await SessionRepository(store._db).create(other)  # noqa: SLF001 - fixture setup
    await store.append_to_session(
        other,
        Message(
            id=new_id("msg"),
            role=Role.USER,
            blocks=(TextBlock(text="rabbitmq here too"),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.USER),
            metadata={},
        ),
    )

    hits = await store.search("rabbitmq", session_ids=(session,))

    assert {hit["session_id"] for hit in hits} == {session}


@pytest.mark.asyncio
async def test_search_never_matches_unscoped():
    store, _session = await _store_with_history()

    assert await store.search("rabbitmq") == []
    assert await store.search("rabbitmq", session_ids=()) == []
    assert await store.search("", session_ids=("s",)) == []


def test_sanitize_fts_query_never_produces_syntax():
    malicious = 'cache" OR 1=1 -- * NEAR( Postgres'
    sanitized = sanitize_fts_query(malicious)

    assert '"' in sanitized
    # Only quoted barewords joined by OR survive.
    for token in sanitized.replace(" OR ", " ").split(" "):
        assert token == "" or (token.startswith('"') and token.endswith('"'))
    assert "*" not in sanitized
    assert "(" not in sanitized


@pytest.mark.asyncio
async def test_search_context_window_returns_surrounding_messages():
    store, session = await _store_with_history()

    hits = await store.search("postgres", session_ids=(session,), context_window=2)

    assert hits, "postgres hit expected"
    context = hits[0]["context"]
    assert isinstance(context, list)
    assert any("rabbitmq approach" in item["text"] for item in context)


@pytest.mark.asyncio
async def test_session_search_capability_scopes_and_formats():
    store, session = await _store_with_history()
    capability = SessionSearchCapability(store)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="session_search",
            call_id=new_id("call"),
            task_id="task-1",
            session_id=session,
            arguments={"query": "ingest queue"},
        )
    )

    from athena.protocol.capabilities import CapabilityResultStatus

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    assert payload["count"] >= 1
    assert payload["scope"] == [session]
    assert any("rabbitmq" in match["text"] for match in payload["matches"])


@pytest.mark.asyncio
async def test_session_search_capability_refuses_model_scope_widening():
    store, session = await _store_with_history()
    capability = SessionSearchCapability(store)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="session_search",
            call_id=new_id("call"),
            task_id="task-1",
            session_id=session,
            arguments={"query": "rabbitmq", "session_ids": ["session-elsewhere"]},
        )
    )

    from athena.protocol.capabilities import CapabilityResultStatus

    assert result.status is CapabilityResultStatus.OK
    payload = json.loads(result.output)
    # The unprivileged request stays scoped to its own session.
    assert payload["scope"] == [session]


@pytest.mark.asyncio
async def test_principal_history_search_and_anchor_read_are_host_scoped():
    db = Database(":memory:")
    sessions = SessionRepository(db)
    store = MessageStore(db)
    alice = new_id("session")
    bob = new_id("session")
    await sessions.create(alice, principal_id="alice", project_id="project-a")
    await sessions.create(bob, principal_id="bob", project_id="project-b")
    anchor = new_id("msg")
    await store.append_to_session(
        alice,
        Message(
            id=anchor,
            role=Role.ASSISTANT,
            blocks=(TextBlock(text="the durable decision is use postgres"),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.SESSION),
            metadata={},
        ),
    )
    await store.append_to_session(
        bob,
        Message(
            id=new_id("msg"),
            role=Role.ASSISTANT,
            blocks=(TextBlock(text="the durable decision is use sqlite"),),
            created_at=utcnow(),
            provenance=Provenance(source_type=SourceType.SESSION),
            metadata={},
        ),
    )

    hits = await store.search("postgres", principal_id="alice")
    assert [hit["session_id"] for hit in hits] == [alice]
    assert await store.search("sqlite", principal_id="alice") == []
    assert await store.read_context(anchor, principal_id="bob") is None
    detail = await store.read_context(anchor, principal_id="alice", before=0, after=0)
    assert detail is not None
    assert detail["session_id"] == alice
    assert [item["message_id"] for item in detail["anchor"]["context"]] == [anchor]
