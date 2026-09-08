"""Context retrieval mechanism (P1-10).

Extracted from ContextCompiler. Mechanism, not a second authority: every
store (transcript, memory, skills, research, context blocks, capability
registry, workspace reader) resolves through the owning compiler instance
(``self._c``), and the degradation ledger stays on the owner so
``athena inspect`` sees one truthful degradation view. The compiler alone
decides WHAT enters the compiled context; this module holds HOW each
bounded source is loaded and scoped.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from athena.context.instructions import (
    provider_role_for_source,
    render_instruction,
    source_for_context,
)
from athena.context.provenance import prov
from athena.context.selection import estimate_tokens
from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.errors import CapabilityReadinessError
from athena.protocol.memory import MemoryScope
from athena.protocol.messages import Message, Role, SourceType, TrustClass
from athena.protocol.tasks import capability_id_permitted
from athena.protocol.tasks import TaskSpec
from athena.skills.selector import SkillSelector
from athena.strategy import StrategyAffordance, is_explicit_response_turn

if TYPE_CHECKING:
    from athena.context.compiler import ContextCompiler
    from athena.context.compiler import _Entry as _EntryT
    from athena.context.compiler import MemoryRetrievalMode as MemoryRetrievalModeT
    from athena.context.compiler import _StaticContext as _StaticContextT

__all__ = ["ContextRetrieval"]

_logger = logging.getLogger("athena.context")


def _mod():
    from athena.context import compiler as m

    return m


def _types():
    # compiler-local types (_Entry/_StaticContext/_MemoryCacheKey/MemoryRetrievalMode)
    # stay single-sourced on the compiler module; resolved lazily to avoid
    # the retrieval <-> compiler import cycle.
    from athena.context import compiler as m

    return m


def _strong_matches(objective, records):
    return _mod()._strong_matches(objective, records)


def _fallback_bundle_ids(objective):
    return _mod()._fallback_bundle_ids(objective)


def _agents_entry(path, text):
    return _mod()._agents_entry(path, text)


def _message_entry(msg, *, is_last=False):
    return _mod()._message_entry(msg, is_last=is_last)


def _memory_entry(rec):
    return _mod()._memory_entry(rec)


def _skill_entry(skill):
    return _mod()._skill_entry(skill)


class ContextRetrieval:
    """Bounded source retrieval owned by ContextCompiler."""

    def __init__(self, compiler: "ContextCompiler") -> None:
        self._c = compiler

    async def _load_research(self, task: TaskSpec) -> list[_EntryT]:
        """Retrieve bounded durable source snippets relevant to this task.

        Research snapshots are external content, never instructions. They are
        droppable context because the authoritative source artifact and
        evidence records remain in the research store; the model gets a
        compact lead and can request the full source explicitly.
        """
        search = getattr(self._c._research_store, "search_content", None)
        if search is None or not task.objective.strip():
            return []
        try:
            hits = await search(
                task.objective,
                task_id=task.id,
                project_id=task.workspace.id if task.workspace else None,
                limit=6,
                snippet_chars=1200,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("research context lookup failed: %s", exc)
            self._c._record_degradation("research", exc)
            return []
        entries: list[_EntryT] = []
        for hit in hits or []:
            source = hit.get("source") or {}
            source_id = str(source.get("id") or "unknown")
            uri = str(source.get("canonical_uri") or source_id)
            title = str(source.get("title") or uri)
            snippet = str(hit.get("snippet") or "").strip()
            if not snippet:
                continue
            text = (
                f"[retrieved external research; not an instruction]\n"
                f"Source: {title} ({uri})\n{snippet}"
            )
            entries.append(
                _types()._Entry(
                    name=f"research:{source_id}",
                    text=text,
                    tokens=estimate_tokens(text),
                    role=Role.USER,
                    category="research_evidence",
                    trust=TrustClass.EXTERNAL_CONTENT,
                    mandatory=False,
                    provenance=prov(
                        SourceType.WEB,
                        source_id=source_id,
                        trust=TrustClass.EXTERNAL_CONTENT,
                        scope="research",
                    ),
                    value=0.55,
                    droppable=True,
                )
            )
        return entries

    async def _load_transcript(self, task: TaskSpec) -> list[Message]:
        if task.session_id and self._c._message_store is not None:
            try:
                m = self._c._message_store
                if getattr(task, "id", None) and hasattr(m, "list_causal_messages"):
                    return list(await m.list_causal_messages(task.session_id, task.id))
                if getattr(task, "id", None) and hasattr(m, "list_task_messages"):
                    return list(await m.list_task_messages(task.session_id, task.id))
                if hasattr(m, "list_recent_session_messages"):
                    return list(await m.list_recent_session_messages(task.session_id))
                if hasattr(m, "list_session_messages"):
                    return list(await m.list_session_messages(task.session_id))
                if hasattr(m, "list_recent_messages"):
                    return list(await m.list_recent_messages(task.session_id))
                if hasattr(m, "list_messages"):
                    return list(await m.list_messages(task.session_id))
            except Exception as exc:
                self._c._record_degradation("transcript", exc, scope=task.session_id)
                return []
        return []

    async def _load_memories(
        self, task: TaskSpec, *, mode: "MemoryRetrievalModeT" | None = None
    ) -> list[Any]:
        if mode is None:
            mode = _types().MemoryRetrievalMode.WORK
        if self._c._memory_store is None:
            return []
        store = self._c._memory_store
        generation = getattr(store, "generation", None)
        cache_key = (
            _types()._MemoryCacheKey(task.id, mode.value, int(generation))
            if isinstance(generation, int)
            else None
        )
        if cache_key is not None:
            cached = self._c._memory_cache.get(cache_key)
            if cached is not None:
                self._c._memory_cache.move_to_end(cache_key)
                return list(cached)
        # Scope weighting (P1-12): the current session outranks the project,
        # which outranks user memory, which outranks global. The weighted retrieval applies the
        # preference during ranking; the per-scope limits bound how much
        # each scope may contribute before merge.
        combined = getattr(store, "search_scopes", None)
        if callable(combined):
            scopes: list[tuple[MemoryScope, str | None]] = []
            if task.session_id:
                scopes.append((MemoryScope.SESSION, task.session_id))
            scopes.append((MemoryScope.PROJECT, task.workspace.id if task.workspace else None))
            scopes.append((MemoryScope.USER, self._c._principal_id))
            scopes.append((MemoryScope.GLOBAL, None))
            try:
                retrieve_scopes = getattr(store, "retrieve_scopes_weighted", None)
                if callable(retrieve_scopes):
                    result = list(
                        await retrieve_scopes(
                            task.objective,
                            scopes,
                            limit=24,
                            mode="relevance",
                            weights=_mod()._MEMORY_SCOPE_WEIGHTS,
                        )
                    )
                    if mode is _types().MemoryRetrievalMode.WORK:
                        result = _strong_matches(task.objective, result)
                else:
                    result = list(
                        await combined(
                            task.objective,
                            scopes,
                            limit=24,
                        )
                    )
                    if mode is _types().MemoryRetrievalMode.WORK:
                        result = _strong_matches(task.objective, result)
                if cache_key is not None:
                    self._c._memory_cache[cache_key] = tuple(result)
                    self._c._memory_cache.move_to_end(cache_key)
                    while len(self._c._memory_cache) > 256:
                        self._c._memory_cache.popitem(last=False)
                return result
            except Exception as exc:
                self._c._record_degradation("memory", exc, scope="scopes")
        out: list[Any] = []
        try:
            if task.session_id:
                out.extend(
                    await store.search(
                        task.objective,
                        scope=MemoryScope.SESSION,
                        scope_id=task.session_id,
                    )
                )
        except Exception as exc:
            self._c._record_degradation("memory", exc, scope="session")
        try:
            out.extend(
                await store.search(
                    task.objective,
                    scope=MemoryScope.PROJECT,
                    scope_id=task.workspace.id if task.workspace else None,
                )
            )
        except Exception as exc:
            self._c._record_degradation("memory", exc, scope="project")
        try:
            out.extend(
                await store.search(
                    task.objective,
                    scope=MemoryScope.USER,
                    scope_id=self._c._principal_id,
                )
            )
        except Exception as exc:
            self._c._record_degradation("memory", exc, scope="user")
        try:
            out.extend(await store.search(task.objective, scope=MemoryScope.GLOBAL))
        except Exception as exc:
            self._c._record_degradation("memory", exc, scope="global")
        if mode is _types().MemoryRetrievalMode.WORK:
            out = _strong_matches(task.objective, out)
        if cache_key is not None:
            self._c._memory_cache[cache_key] = tuple(out)
            self._c._memory_cache.move_to_end(cache_key)
            while len(self._c._memory_cache) > 256:
                self._c._memory_cache.popitem(last=False)
        return out

    async def _load_skills(self, task: TaskSpec) -> list[Any]:
        if self._c._skill_loader is None:
            return []
        try:
            available = list(await self._c._skill_loader.load_active())
        except Exception as exc:
            self._c._record_degradation("skills", exc)
            return []
        if not available:
            return []
        selected = await SkillSelector(min_score=0.01).select(
            task_objective=task.objective,
            available=available,
            limit=self._c.skill_limit,
        )
        return selected

    async def _load_context_blocks(self, task: TaskSpec) -> list[_EntryT]:
        store = self._c._context_block_store
        if store is None:
            return []
        scopes: list[tuple[str, str]] = [("task", task.id)]
        if task.session_id:
            scopes.append(("session", task.session_id))
        if task.workspace is not None:
            scopes.append(("project", task.workspace.id))
        scopes.extend([("user", self._c._principal_id), ("global", "global")])
        try:
            blocks = await store.list(scopes=scopes, attached_only=True, limit=64)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("attached context lookup failed: %s", exc)
            self._c._record_degradation("context_blocks", exc)
            return []
        entries: list[_EntryT] = []
        for block in blocks or ():
            content = block.bounded_content()
            text = (
                f"[attached context: {block.label}; version {block.version}; "
                f"scope={block.scope}]\n{content}"
            )
            trust = block.trust
            source = source_for_context(block.scope, trust)
            rendered_text = render_instruction(text, source)
            entries.append(
                _types()._Entry(
                    name=f"context_block:{block.id}:v{block.version}",
                    text=rendered_text,
                    tokens=estimate_tokens(rendered_text),
                    role=provider_role_for_source(source, scope=block.scope, trust=trust),
                    category="task_state",
                    trust=trust,
                    mandatory=True,
                    provenance=block.effective_provenance,
                    created_at=block.updated_at or block.created_at,
                    cache_zone=(
                        "stable" if block.scope in {"project", "user", "global"} else "dynamic"
                    ),
                )
            )
        return entries

    async def _load_capabilities(
        self, *, task: TaskSpec | None = None, require_tools: bool = False
    ) -> tuple[tuple[CapabilityDescriptor, ...], tuple[StrategyAffordance, ...], str]:
        reg = self._c._capability_registry
        if reg is None:
            return (), (), "degraded"
        for name in ("list_descriptors", "list_available", "list_capabilities"):
            method = getattr(reg, name, None)
            if method is None:
                continue
            try:
                result = method(
                    task_id=task.id if task is not None else None,
                    project_id=(task.workspace.id if task and task.workspace else None),
                    user_id=self._c._principal_id,
                )
            except TypeError:
                # Small test doubles and legacy registries do not accept the
                # overlay selectors; they still expose the global surface.
                result = method()
            if inspect.isawaitable(result):
                result = await result
            policy = task.capability_policy if task is not None else None
            descriptors = [
                d
                for d in (result or ())
                if isinstance(d, CapabilityDescriptor) and capability_id_permitted(d.id, policy)
            ]
            descriptors, records, discovery_state = await self._c._select_relevant_capabilities(
                reg,
                descriptors,
                task,
            )
            if descriptors:
                return (
                    tuple(descriptors),
                    tuple(
                        _mod()._strategy_affordance(descriptor, records.get(descriptor.id))
                        for descriptor in descriptors
                    ),
                    discovery_state,
                )
            if require_tools:
                objective = task.objective if task is not None else ""
                raise CapabilityReadinessError(
                    "tool-required task has no policy-permitted capability surface "
                    f"(registry={name}, objective={objective!r})"
                )
            return (), (), discovery_state
        if require_tools:
            raise CapabilityReadinessError(
                "tool-required task cannot discover a policy-permitted capability surface: "
                "registry exposes no descriptor-list method"
            )
        return (), (), "degraded"

    async def _select_relevant_capabilities(
        self,
        registry: Any,
        descriptors: list[CapabilityDescriptor],
        task: TaskSpec | None,
    ) -> tuple[list[CapabilityDescriptor], dict[str, Mapping[str, Any]], str]:
        """Progressively disclose relevant affordances when the fabric supports search.

        A search miss on an action-shaped request keeps a *minimal foundational
        fallback bundle* visible — reflection plus the smallest need-compatible
        primitives — so ordinary work does not require a discovery round-trip
        merely because lexical search failed to recognize the wording. The
        fallback never expands into the complete capability fabric.
        Legacy registries without ``search`` continue to expose their normal
        descriptor list.
        """
        search = getattr(registry, "search", None)
        policy = task.capability_policy if task is not None else None
        descriptors = [
            descriptor
            for descriptor in descriptors
            if capability_id_permitted(descriptor.id, policy)
        ]
        if search is None or task is None or not task.objective:
            return descriptors, {}, "resolved" if descriptors else "degraded"
        try:
            result = search(
                task.objective,
                task_id=task.id,
                project_id=task.workspace.id if task.workspace else None,
                user_id=self._c._principal_id,
                limit=self._c.capability_limit,
                workspace=task.workspace,
            )
            if inspect.isawaitable(result):
                result = await result
            records = {
                str(item.get("id")): item
                for item in (result or ())
                if isinstance(item, Mapping)
                and item.get("id")
                and capability_id_permitted(str(item.get("id")), policy)
            }
        except Exception as exc:
            self._c._record_degradation("capabilities", exc)
            if not is_explicit_response_turn(task.objective):
                return (
                    self._c._fallback_bundle(descriptors, task),
                    {},
                    "degraded",
                )
            return [], {}, "degraded"
        ids = set(records)
        if not ids:
            if not is_explicit_response_turn(task.objective):
                # Keep the bounded fallback bundle visible when lexical search
                # misses a tool-eligible request: the reflection affordance
                # plus the smallest effect-compatible primitives, never the
                # entire registry. Only turns that are *definitely* response-
                # only compile without a working surface.
                return (
                    self._c._fallback_bundle(descriptors, task),
                    {},
                    "miss",
                )
            return [], {}, "miss"
        selected = [descriptor for descriptor in descriptors if descriptor.id in ids]
        if not selected and not is_explicit_response_turn(task.objective):
            return (
                self._c._fallback_bundle(descriptors, task),
                {},
                "miss",
            )
        if selected and not is_explicit_response_turn(task.objective):
            selected = self._c._ensure_reflection_visible(selected, descriptors)
        return selected, records, "resolved" if selected else "miss"

    def _ensure_reflection_visible(
        self,
        selected: list[CapabilityDescriptor],
        descriptors: list[CapabilityDescriptor],
    ) -> list[CapabilityDescriptor]:
        """Keep the ``capabilities`` reflection affordance on every
        work-bearing surface.

        Hierarchical disclosure contract: the compiled working set is small,
        but reflection must always remain available on turns that can work,
        so the model can expand the surface without a discovery round-trip
        failing closed. Precomputation may shrink what is shown by default;
        it may NOT decide what Athena is allowed to look for.
        """
        if any(descriptor.id == "capabilities" for descriptor in selected):
            return selected
        reflection = next((item for item in descriptors if item.id == "capabilities"), None)
        if reflection is None:
            return selected
        return [*selected, reflection]

    def _fallback_bundle(
        self,
        descriptors: list[CapabilityDescriptor],
        task: TaskSpec,
    ) -> list[CapabilityDescriptor]:
        """Assemble the smallest need-compatible fallback surface for a miss.

        The bundle is always led by the ``capabilities`` reflection affordance
        (so the model can still search explicitly) and adds at most the
        primitives that match the turn's apparent need. Total disclosure stays
        far below the full registry.
        """
        available = {descriptor.id for descriptor in descriptors}
        needed = _fallback_bundle_ids(task.objective)
        bundle: list[CapabilityDescriptor] = []
        for capability_id in needed:
            if capability_id in available:
                descriptor = next((item for item in descriptors if item.id == capability_id), None)
                if descriptor is not None:
                    bundle.append(descriptor)
        # If the need-scoped bundle matched nothing this workspace exposes
        # (e.g. a research ask in a repo with no research capability), fall
        # back to the default workspace-observation bundle rather than
        # shipping reflection alone — a bare affordance is not a way to look.
        primitives = [item for item in bundle if item.id != "capabilities"]
        if not primitives:
            for capability_id in _mod()._DEFAULT_FALLBACK_BUNDLE:
                if capability_id in available:
                    descriptor = next(
                        (item for item in descriptors if item.id == capability_id),
                        None,
                    )
                    if descriptor is not None and all(item.id != descriptor.id for item in bundle):
                        bundle.append(descriptor)
        if not any(descriptor.id == "capabilities" for descriptor in bundle):
            reflection = next((item for item in descriptors if item.id == "capabilities"), None)
            if reflection is not None:
                bundle.append(reflection)
        return bundle

    async def _collect_entries(
        self,
        task: TaskSpec,
        recent: Sequence[Any] | None,
        static: "_StaticContextT",
    ) -> list[_EntryT]:
        out: list[_EntryT] = []
        out.extend(await self._load_context_digests(task))
        transcript = list(recent) if recent else await self._c._load_transcript(task)
        # ``!!`` direct escapes are durable audit records, but explicitly opt
        # out of the next model context. Keeping this at the compiler boundary
        # preserves one transcript while honoring the OI-style display-only
        # escape semantics.
        transcript = [
            m
            for m in transcript
            if not (
                getattr(m, "metadata", None)
                and m.metadata.get("direct_execution")
                and m.metadata.get("inject_into_context") is False
            )
        ]
        for i, m in enumerate(transcript):
            out.append(_message_entry(m, is_last=(i == len(transcript) - 1)))
        for rec in static.memories:
            out.append(_memory_entry(rec))
        for s in static.skills:
            out.append(_skill_entry(s))
        out.extend(static.research)
        return out

    async def _load_context_digests(self, task: TaskSpec) -> list[_EntryT]:
        store = self._c._context_digest_store
        if store is None or not task.session_id:
            return []
        try:
            digest = await store.latest_for_session(task.session_id, self._c._principal_id)
        except Exception as exc:
            self._c._record_degradation("context_digest", exc, scope=task.session_id)
            return []
        entries: list[_EntryT] = []
        if digest is None:
            return entries
        fields = digest.normalized_fields()
        rendered = "\n".join(
            f"{name}: {json.dumps(value, sort_keys=True, default=str)}"
            for name, value in fields.items()
            if value
        )
        if not rendered:
            return entries
        text = (
            f"[durable context digest level {digest.level}; "
            f"task={digest.task_id}; anchors={','.join(digest.transcript_anchors[:8])}]\n"
            f"{rendered}\nRecovery queries: " + ", ".join(digest.recovery_queries[:8])
        )[:8_000]
        entries.append(
            _types()._Entry(
                name=f"digest:{digest.id}",
                text=text,
                tokens=estimate_tokens(text),
                role=Role.USER,
                category="task_state",
                trust=TrustClass.AGENT_CURATED,
                mandatory=True,
                provenance=prov(
                    SourceType.RUNTIME,
                    source_id=digest.id,
                    trust=TrustClass.AGENT_CURATED,
                    scope="context_digest",
                ),
                cache_zone="dynamic",
            )
        )
        return entries

    def _project_entries(self, workspace: str | None) -> list[_EntryT]:
        reader = self._c._workspace_reader
        if reader is None or not hasattr(reader, "list_agents_md"):
            return []
        snapshot = getattr(reader, "snapshot", None)
        files: list[tuple[str, str]]
        revision: str
        try:
            if callable(snapshot):
                raw_revision, raw_files = snapshot()
                revision = str(raw_revision)
                files = list(raw_files or ())
            else:
                files = list(reader.list_agents_md())
                revision = hashlib.sha256(
                    json.dumps(files, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
        except Exception as exc:
            self._c._record_degradation("workspace", exc)
            return []
        key = str(workspace or "")
        cached = self._c._project_cache.get(key)
        if cached is not None and cached[0] == revision:
            return list(cached[1])
        entries = tuple(_agents_entry(path, text) for path, text in files)
        self._c._project_cache[key] = (revision, entries)
        return list(entries)
