"""Staged memory-retrieval gating (P1-12).

Explicit referential language retrieves broadly; ordinary work turns are
held to a strong-match floor; self-contained response turns skip the
store. Scope ranking weights session > project > global.
"""

from __future__ import annotations

from athena.context.compiler import (
    MemoryRetrievalMode,
    _memory_context_mode,
    _strong_matches,
)
from athena.memory.retrieval import MemoryRetriever
from athena.protocol.memory import MemoryRecord, MemoryScope


class TestStaging:
    def test_explicit_referential_language(self):
        assert (
            _memory_context_mode("use my usual format for the changelog")
            is MemoryRetrievalMode.EXPLICIT
        )
        assert (
            _memory_context_mode("remember that we prefer uv over pip")
            is MemoryRetrievalMode.EXPLICIT
        )
        assert (
            _memory_context_mode("how did we decide to handle retries?")
            is MemoryRetrievalMode.EXPLICIT
        )

    def test_ordinary_work_is_work_mode(self):
        assert (
            _memory_context_mode("fix the failing import in src/app.py") is MemoryRetrievalMode.WORK
        )

    def test_definite_response_turn_skips(self):
        # Only a DEFINITELY-response-only turn skips; ambiguous short turns
        # ("will do") stay WORK — the classifier's own authority boundary.
        assert _memory_context_mode("thanks!") is MemoryRetrievalMode.SKIP
        assert _memory_context_mode("will do") is MemoryRetrievalMode.WORK

    def test_needed_aggregate_matches_mode(self):
        from athena.context.compiler import _memory_context_needed

        assert _memory_context_needed("fix the import") is True
        assert _memory_context_needed("thanks!") is False


class TestWorkFloor:
    def _rec(self, text):
        return {"content": text, "summary": ""}

    def test_strong_match_kept(self):
        objective = "fix the failing import in src/app.py"
        kept = _strong_matches(objective, [self._rec("how to fix the import in app.py")])
        assert len(kept) == 1

    def test_weak_match_dropped(self):
        objective = "fix the failing import in src/app.py"
        kept = _strong_matches(
            objective,
            [self._rec("unrelated note about deploy conventions")],
        )
        assert kept == []

    def test_empty_objective_keeps_nothing(self):
        assert _strong_matches("", [self._rec("anything")]) == []


class TestScopeWeighting:
    @staticmethod
    def _rec(scope: MemoryScope, content: str) -> MemoryRecord:
        from athena.protocol.memory import MemoryKind

        return MemoryRecord(
            id=f"m-{scope.value}-{content[:6]}",
            scope=scope,
            kind=MemoryKind.SEMANTIC,
            content=content,
            summary="",
            metadata={},
        )

    async def test_session_outranks_global_on_equal_overlap(self):
        """Same text overlap: the session-local record ranks first."""

        class _Store:
            async def retrieve_by_fts_scopes(self, query, scopes, limit, tags=None):
                # Order deliberately inverted: global first proves the
                # weight, not SQL order, drives the result.
                return [
                    self._global,
                    self._session,
                ]

        store = _Store()
        store._global = self._rec(MemoryScope.GLOBAL, "deploy with the release script")
        store._session = self._rec(MemoryScope.SESSION, "deploy with the release script")
        weights = {"SESSION": 1.0, "PROJECT": 0.6, "GLOBAL": 0.3}
        out = await MemoryRetriever(store).retrieve_scopes_weighted(
            query="deploy with the release script",
            scopes=[
                (MemoryScope.SESSION, "s-1"),
                (MemoryScope.PROJECT, "p-1"),
                (MemoryScope.GLOBAL, None),
            ],
            mode="relevance",
            limit=5,
            weights=weights,
        )
        assert [r.scope for r in out][0] is MemoryScope.SESSION
        assert len(out) == 2

    async def test_zero_overlap_never_ranks(self):
        """A scope weight cannot rescue a non-matching record."""

        class _Store:
            async def retrieve_by_fts_scopes(self, query, scopes, limit, tags=None):
                return [self._rec]

        store = _Store()
        store._rec = self._rec(MemoryScope.SESSION, "totally unrelated words here")
        out = await MemoryRetriever(store).retrieve_scopes_weighted(
            query="fix the import",
            scopes=[(MemoryScope.SESSION, "s-1")],
            mode="relevance",
            limit=5,
            weights={"SESSION": 1.0},
        )
        assert out == []

    async def test_unweighted_scope_is_excluded(self):
        """A scope absent from the weight map contributes nothing."""

        class _Store:
            async def retrieve_by_fts_scopes(self, query, scopes, limit, tags=None):
                return [self._global]

        store = _Store()
        store._global = self._rec(MemoryScope.GLOBAL, "deploy with the release script")
        out = await MemoryRetriever(store).retrieve_scopes_weighted(
            query="deploy with the release script",
            scopes=[(MemoryScope.SESSION, "s-1"), (MemoryScope.GLOBAL, None)],
            mode="relevance",
            limit=5,
            weights={"SESSION": 1.0},  # GLOBAL unweighted
        )
        assert out == []
