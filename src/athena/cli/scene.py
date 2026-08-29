"""OI scene graph built from the shared projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from athena.cli.activity import VisualActionKind
from athena.cli.code_view import CodeViewport, make_code_view
from athena.cli.layout import Rect
from athena.cli.projection import ProjectionState
from athena.cli.terminal import sanitize_terminal_text


@dataclass(frozen=True)
class TreeNode:
    """A node in a normalised scene tree (workspace or runtime)."""

    id: str
    kind: str
    label: str
    status: str = "idle"
    children: tuple[TreeNode, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneEntity:
    id: str
    kind: str
    label: str
    status: str = "idle"
    anchor: str = "center"
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# tree_rows
# ---------------------------------------------------------------------------


def tree_rows(nodes: tuple[TreeNode, ...], *, _indent: int = 0) -> tuple[tuple[str, TreeNode], ...]:
    """Return a tuple of ``(prefix, node)`` for every node in *nodes* depth-first."""
    out: list[tuple[str, TreeNode]] = []
    for node in nodes:
        prefix = "  " * _indent + ("\u251c\u2500 " if _indent else "")
        if _indent == 0:
            prefix = ""
        out.append((prefix + node.label, node))
        if node.children:
            out.extend(tree_rows(node.children, _indent=_indent + 1))
    return tuple(out)


# ---------------------------------------------------------------------------
# normalize_workspace_tree
# ---------------------------------------------------------------------------


def normalize_workspace_tree(
    entities: list[SceneEntity],
    *,
    max_depth: int = 8,
    max_nodes: int = 32,
) -> tuple[TreeNode, ...]:
    """Build a deterministic tree without losing resource identity.

    Producers may put the authoritative path in ``metadata.path`` (or
    ``uri``/``resource``); ``label`` remains the display fallback.  Path
    separators are canonicalised for both POSIX and Windows producers, while
    path case is deliberately preserved.  Opaque URIs remain atomic nodes.
    """

    def _truncation_marker() -> TreeNode:
        return TreeNode(
            id="tree:truncated",
            kind="marker",
            label="",
            status="truncated",
            metadata={"path_truncated": True},
        )

    def _value(ent: SceneEntity) -> str:
        for key in ("path", "uri", "resource"):
            value = ent.metadata.get(key)
            if value:
                return sanitize_terminal_text(value).strip()
        return sanitize_terminal_text(ent.label).strip()

    def _canonical(value: str) -> str:
        return value.replace("\\", "/")

    def _parts(value: str) -> tuple[str, ...]:
        value = _canonical(value)
        if not value:
            return ()
        absolute = value.startswith("/")
        drive = len(value) >= 2 and value[1] == ":"
        raw = [part for part in value.split("/") if part not in {"", "."}]
        out: list[str] = ["/"] if absolute else []
        if drive and raw and raw[0].casefold() == value[:2].casefold():
            raw[0] = value[:2]
        for part in raw:
            if part == ".." and out and out[-1] not in {"/", ".."}:
                out.pop()
            elif part != ".." or not out:
                out.append(part)
        return tuple(out)

    def _path(parts: tuple[str, ...]) -> str:
        if parts and parts[0] == "/":
            return "/" + "/".join(parts[1:])
        return "/".join(parts)

    opaque: list[tuple[str, SceneEntity]] = []
    entries: list[tuple[tuple[str, ...], SceneEntity, str]] = []
    seen_ids: set[str] = set()
    for ent in entities:
        if ent.kind not in {"resource", "artifact"} or ent.id in seen_ids:
            continue
        seen_ids.add(ent.id)
        value = _value(ent)
        if "://" in value:
            opaque.append((value, ent))
            continue
        parts = _parts(value)
        if parts:
            entries.append((parts, ent, value))

    files: dict[tuple[str, ...], dict[str, tuple[SceneEntity, str]]] = {}
    directories: set[tuple[str, ...]] = set()
    for parts, ent, value in entries:
        parent, leaf = parts[:-1], parts[-1]
        files.setdefault(parent, {}).setdefault(leaf, (ent, value))
        for index in range(1, len(parts)):
            directories.add(parts[:index])

    def _node_id(parts: tuple[str, ...]) -> str:
        return f"workspace:{_path(parts)}"

    def _build(prefix: tuple[str, ...]) -> tuple[TreeNode, ...]:
        children: list[TreeNode] = []
        for leaf, (ent, value) in sorted(files.get(prefix, {}).items()):
            parts = prefix + (leaf,)
            metadata = {**ent.metadata, "canonical_path": _path(parts)}
            children.append(
                TreeNode(_node_id(parts), ent.kind, ent.label, ent.status, metadata=metadata)
            )
        direct_dirs = sorted(directory for directory in directories if directory[:-1] == prefix)
        for directory in direct_dirs:
            children.append(
                TreeNode(
                    _node_id(directory),
                    "directory",
                    directory[-1],
                    "unknown",
                    children=_build(directory),
                    metadata={"canonical_path": _path(directory)},
                )
            )
        children.sort(key=lambda node: (node.kind == "directory", node.label, node.id))
        if max_nodes is not None and len(children) > max_nodes:
            children = children[:max_nodes] + [_truncation_marker()]
        return tuple(children)

    roots: list[TreeNode] = [
        TreeNode(f"workspace:{value}", ent.kind, ent.label, ent.status, metadata=dict(ent.metadata))
        for value, ent in sorted(opaque, key=lambda item: (item[0], item[1].id))
    ]
    top_parts = sorted(directory for directory in directories if len(directory) == 1)
    for directory in top_parts:
        roots.append(
            TreeNode(
                _node_id(directory),
                "directory",
                directory[-1],
                "unknown",
                children=_build(directory),
                metadata={"canonical_path": _path(directory)},
            )
        )
    for leaf, (ent, value) in sorted(files.get((), {}).items()):
        parts = (leaf,)
        roots.append(
            TreeNode(
                _node_id(parts),
                ent.kind,
                ent.label,
                ent.status,
                metadata={**ent.metadata, "canonical_path": _path(parts)},
            )
        )
    roots.sort(key=lambda node: (node.kind == "directory", node.label, node.id))

    # Apply depth cap — pure reconstruction (no frozen mutation).
    # The roots list is already flat (opaque URIs, top-level dirs,
    # root-level files are all siblings).  We cap depth by walking
    # each root tree and rebuilding nodes without mutating frozen
    # dataclass fields.
    #
    # depth=0 = roots.  When depth >= max_depth, children at this
    # depth are replaced by a single tree:truncated marker.  The
    # boundary parent (last retained node) gets path_truncated=True.
    # All nodes are deep-copied via TreeNode() constructor.
    def _cap(nodes: tuple[TreeNode, ...], depth: int) -> tuple[TreeNode, ...]:
        out: list[TreeNode] = []
        for node in nodes:
            if depth >= max_depth:
                # Replace remaining siblings with truncation marker
                if not out or out[-1].id != "tree:truncated":
                    out.append(_truncation_marker())
                return tuple(out)
            # Rebuild node with capped children
            capped_children = _cap(node.children, depth + 1)
            was_truncated = len(capped_children) > 0 and capped_children[-1].id == "tree:truncated"
            merged_meta = {**node.metadata}
            if was_truncated:
                merged_meta["path_truncated"] = True
            out.append(
                TreeNode(
                    id=node.id,
                    kind=node.kind,
                    label=node.label,
                    status=node.status,
                    children=capped_children,
                    metadata=merged_meta,
                )
            )
        return tuple(out)

    return _cap(tuple(roots), 0)


# ---------------------------------------------------------------------------
# normalize_runtime_tree
# ---------------------------------------------------------------------------


def normalize_runtime_tree(
    entities: list[SceneEntity],
    *,
    max_depth: int = 6,
    max_nodes: int = 24,
) -> tuple[TreeNode, ...]:
    """Build a runtime hierarchy from workflow/task entities."""

    def _truncation_marker() -> TreeNode:
        return TreeNode(
            id="tree:truncated",
            kind="marker",
            label="",
            status="truncated",
            metadata={"path_truncated": True},
        )

    by_id: dict[str, SceneEntity] = {e.id: e for e in entities}
    known = set(by_id.keys())

    # parent -> [child_ids]
    children_map: dict[str, list[str]] = {}
    parent_of: dict[str, str | None] = {}
    for ent in entities:
        pid = ent.metadata.get("parent_id")
        parent_of[ent.id] = pid
        if pid and pid in known:
            children_map.setdefault(pid, []).append(ent.id)

    # Detect cycles via DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {eid: WHITE for eid in by_id}
    in_cycle: set[str] = set()

    def _dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for cid in children_map.get(node, []):
            if cid not in color:
                continue
            if color[cid] == GRAY:
                idx = path.index(cid)
                in_cycle.update(path[idx:])
            elif color[cid] == WHITE:
                _dfs(cid, path)
        path.pop()
        color[node] = BLACK

    for eid in sorted(by_id):
        if color.get(eid, WHITE) == WHITE:
            _dfs(eid, [])

    # Orphans: entity whose parent_id points to a missing entity
    orphan_set: set[str] = set()
    for eid, pid in parent_of.items():
        if pid and pid not in known:
            orphan_set.add(eid)

    # Cycle members and their children all become flat roots.
    # Cycle edges are removed entirely; non-cycle children of cycle members
    # are detached and become roots with parent_cycle metadata.
    non_cycle_children: dict[str, list[str]] = {}
    for pid, cids in children_map.items():
        filtered = []
        for cid in cids:
            if pid in in_cycle and cid in in_cycle:
                continue
            filtered.append(cid)
        if filtered:
            non_cycle_children[pid] = filtered

    # Determine which entity IDs become roots:
    # - Cycle members always become roots (and lose all children)
    # - Entities not listed as children in non_cycle_children
    # - Orphans (their parent is missing, so they were never added)
    in_non_cycle_children: set[str] = set()
    for cids in non_cycle_children.values():
        in_non_cycle_children.update(cids)

    roots: list[str] = []
    detached_set: set[str] = set()
    for eid in sorted(by_id, key=lambda e: (by_id[e].kind, by_id[e].label, by_id[e].id)):
        if eid in in_cycle:
            # Cycle members become roots with no children.
            # Detach any non-cycle children — they become independent
            # roots with parent_cycle metadata.
            detached = non_cycle_children.pop(eid, [])
            roots.append(eid)
            for dc in detached:
                if dc not in in_cycle:
                    detached_set.add(dc)
                    roots.append(dc)
        elif eid not in in_non_cycle_children:
            roots.append(eid)
    # Re-sort to maintain deterministic order after detaching children.
    roots.sort(key=lambda e: (by_id[e].kind, by_id[e].label, by_id[e].id))

    def _build(node_id: str, depth: int) -> TreeNode:
        ent = by_id[node_id]
        child_ids = non_cycle_children.get(node_id, [])
        child_nodes: list[TreeNode] = []
        for cid in child_ids:
            if cid not in by_id:
                continue
            child_nodes.append(_build(cid, depth + 1))

        # Metadata
        metadata: dict[str, Any] = {}
        if node_id in orphan_set:
            metadata["orphan"] = True
        if node_id in in_cycle:
            metadata["cycle"] = True
        # parent_cycle: entity whose parent is a cycle member
        pid = parent_of.get(node_id)
        if pid and pid in in_cycle:
            metadata["parent_cycle"] = True

        # Node cap
        truncation = False
        if max_nodes is not None and len(child_nodes) > max_nodes:
            child_nodes = child_nodes[:max_nodes]
            truncation = True
        if truncation:
            child_nodes.append(_truncation_marker())

        # Depth cap: children at depth >= max_depth are replaced by a single marker.
        # "one" is at depth 0, "two" at depth 1, "three" at depth 2
        # With max_depth=2: retain depth 0 and 1; replace children at depth >= 2 with marker.
        if depth + 1 >= max_depth and child_nodes:
            child_nodes.clear()
            metadata["path_truncated"] = True
            child_nodes.append(_truncation_marker())

        # SceneEntity IDs are already namespaced for canonical task/resource
        # nodes. Preserve those IDs; legacy unqualified operation IDs receive
        # one namespace here so parent links and native consumers share one
        # stable identity space.
        tree_id = ent.id if ent.id.startswith(f"{ent.kind}:") else f"{ent.kind}:{ent.id}"
        return TreeNode(
            id=tree_id,
            kind=ent.kind,
            label=ent.label,
            status=ent.status,
            children=tuple(child_nodes),
            metadata=metadata,
        )

    return tuple(_build(r, 0) for r in roots)


@dataclass
class OIScene:
    viewport: Rect
    status: str
    title: str = "ATHENA OI // GLASS COMPUTE"
    character: str = "owl"
    entities: list[SceneEntity] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    stream: list[str] = field(default_factory=list)
    buddy_anchor: str = "center"
    mode: VisualActionKind = VisualActionKind.IDLE
    code_view: CodeViewport | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    instruments: tuple[dict[str, Any], ...] = ()
    verification_checks: tuple[dict[str, Any], ...] = ()
    progress: dict[str, Any] = field(default_factory=dict)
    workspace_tree: tuple[TreeNode, ...] = ()
    runtime_tree: tuple[TreeNode, ...] = ()
    trace: tuple[str, ...] = ()
    model_provider: str = ""
    model: str = ""
    model_role: str = ""
    model_request_id: str = ""
    model_request_status: str = "idle"
    model_request_label: str = ""

    @property
    def anchors(self) -> dict[str, tuple[float, float]]:
        return {
            "files": (0.18, 0.31),
            "graph": (0.78, 0.27),
            "center": (0.50, 0.54),
            "alert": (0.78, 0.72),
            "lower-left": (0.23, 0.78),
            "lower-right": (0.74, 0.82),
        }

    def anchor_position(self, name: str) -> tuple[int, int]:
        fx, fy = self.anchors.get(name, self.anchors["center"])
        return (
            self.viewport.x + int(max(self.viewport.width - 1, 0) * fx),
            self.viewport.y + int(max(self.viewport.height - 1, 0) * fy),
        )


def _buddy_anchor(status: str, mode: VisualActionKind = VisualActionKind.IDLE) -> str:
    if mode in {VisualActionKind.CODE, VisualActionKind.READ, VisualActionKind.SEARCH}:
        return "files"
    if mode in {VisualActionKind.TEST, VisualActionKind.VERIFY, VisualActionKind.EXECUTE}:
        return "graph"
    if mode is VisualActionKind.FAILURE:
        return "alert"
    if mode is VisualActionKind.APPROVAL:
        return "lower-right"
    return {
        "READING": "files",
        "SEARCHING": "files",
        "EXECUTING": "graph",
        "FAILURE": "alert",
        "BLOCKED": "alert",
        "APPROVAL": "lower-right",
        "DELEGATED": "center",
        "RECOVERING": "lower-left",
        "SUCCESS": "lower-right",
    }.get(status, "center")


def _label(value: object) -> str:
    return sanitize_terminal_text(value).strip()


def _event_entity(event_type: str, payload: Mapping[str, Any]) -> SceneEntity | None:
    """Turn observable event payloads into scene entities when they have data."""
    if event_type in {"FileRead", "InspectionStarted", "SearchStarted", "ResearchStarted"}:
        value = (
            payload.get("path")
            or payload.get("resource")
            or payload.get("query")
            or payload.get("uri")
        )
        label = _label(value)
        if label:
            return SceneEntity(
                f"resource:{label}",
                "research" if event_type in {"SearchStarted", "ResearchStarted"} else "resource",
                label,
                "active",
                "files",
                {"event_type": event_type},
            )
    if event_type == "ArtifactCreated":
        value = payload.get("uri") or payload.get("artifact_uri") or payload.get("name")
        label = _label(value)
        if label:
            return SceneEntity(
                f"artifact:{label}", "artifact", label, "complete", "lower-right", {}
            )
    # Child-task lifecycle is folded into ProjectionState.tasks.  Keeping a
    # second ``child:<id>`` entity here would create two competing runtime
    # hierarchies for the same task.
    if event_type.startswith(("Workflow", "Verification", "Acceptance", "Generated")):
        value = _label(
            payload.get("workflow_id")
            or payload.get("workflow")
            or payload.get("criterion")
            or payload.get("capability")
            or payload.get("name")
        )
        if value:
            if event_type.startswith("Generated"):
                kind = "generated_tool"
            else:
                kind = (
                    "workflow"
                    if event_type.startswith("Workflow")
                    else (
                        "verification"
                        if event_type.startswith("Verification")
                        else ("acceptance" if event_type.startswith("Acceptance") else "generated")
                    )
                )
            return SceneEntity(
                f"{kind}:{value}",
                kind,
                value,
                (str(payload.get("status")) or "active").lower(),
                "graph",
                {"event_type": event_type},
            )
    if event_type == "InstrumentProduced":
        instrument = payload.get("instrument")
        if isinstance(instrument, Mapping):
            value = _label(instrument.get("title") or instrument.get("kind") or "instrument")
            return SceneEntity(
                f"instrument:{_label(instrument.get('id') or value)}",
                "instrument",
                value,
                "complete",
                "lower-right",
                {"instrument": dict(instrument)},
            )
    return None


def build_oi_scene(
    state: ProjectionState,
    viewport: Rect,
    *,
    character: str = "owl",
) -> OIScene:
    """Build a bounded scene entirely from the canonical projection."""
    entities: list[SceneEntity] = []
    seen: set[str] = set()
    for task_id, task in sorted(state.tasks.items()):
        parent_id = task.get("parent_id")
        entities.append(
            SceneEntity(
                f"task:{task_id}",
                "task",
                str(task.get("label") or task_id),
                str(task.get("status") or "observed"),
                "graph",
                {
                    "parent_id": f"task:{parent_id}" if parent_id else None,
                    "task_id": task_id,
                },
            )
        )
        seen.add(f"task:{task_id}")
    for operation in list(state.operations.values())[-6:]:
        parent_id = operation.parent_id
        if parent_id and not parent_id.startswith("task:"):
            parent_id = f"task:{parent_id}"
        metadata = {
            "target": operation.target,
            "command": operation.command,
            "execution_id": operation.execution_id,
            "exit_code": operation.exit_code,
            "progress": operation.progress,
            "artifact": operation.artifact,
            "parent_id": parent_id or "",
        }
        entity = SceneEntity(
            operation.id, "operation", operation.label, operation.state, "graph", metadata
        )
        entities.append(entity)
        seen.add(entity.id)
        if operation.target:
            resource = SceneEntity(
                f"resource:{operation.id}",
                "resource",
                operation.target,
                operation.state,
                "files",
                {"operation_id": operation.id},
            )
            entities.append(resource)
            seen.add(resource.id)
        if operation.artifact:
            artifact = SceneEntity(
                f"artifact:{operation.id}",
                "artifact",
                operation.artifact,
                "complete",
                "lower-right",
                {"operation_id": operation.id},
            )
            entities.append(artifact)
            seen.add(artifact.id)

    for execution_id, execution in list(state.executions.items())[-12:]:
        execution_entity = SceneEntity(
            f"execution:{execution_id}",
            "execution",
            str(execution.get("label") or execution_id),
            str(execution.get("status") or "observed"),
            "graph",
            {
                "execution_id": execution_id,
                "parent_id": execution.get("parent_id"),
            },
        )
        entities.append(execution_entity)
        seen.add(execution_entity.id)

    for event_type, payload in state.raw_events:
        event_entity = _event_entity(event_type, payload)
        if event_entity is not None and event_entity.id not in seen:
            entities.append(event_entity)
            seen.add(event_entity.id)

    active = state.operations.get(state.active_operation_id or "")
    if active is None and state.last_operation_id:
        active = state.operations.get(state.last_operation_id)
    try:
        mode = VisualActionKind(
            state.semantic_state
            if state.semantic_state != VisualActionKind.IDLE.value
            else active.action_kind
            if active
            else VisualActionKind.IDLE.value
        )
    except ValueError:
        mode = VisualActionKind.IDLE
    if state.status in {"FAILURE", "WARNING", "BLOCKED"}:
        mode = VisualActionKind.FAILURE
    elif state.status == "APPROVAL":
        mode = VisualActionKind.APPROVAL
    elif state.status == "RECOVERING":
        mode = VisualActionKind.RECOVER
    buddy_anchor = _buddy_anchor(state.status, mode)
    if active and active.target and state.status in {"READING", "SEARCHING"}:
        buddy_anchor = "files"
    alerts = [text for glyph, text in list(state.recent)[-4:] if glyph in {"!", "?"}]
    code_view = None
    if active and (active.content_preview or active.diff_preview):
        code_view = make_code_view(
            path=active.target or active.label,
            text=active.content_preview or "\n".join(active.diff_preview),
            mutation_state=active.mutation_state,
            diff_hunks=active.diff_preview,
            preview_truncated=active.preview_truncated,
        )
    progress: dict[str, Any] = (
        {
            "value": active.progress_value,
            "determinate": active.progress_determinate,
            "label": active.progress,
        }
        if active
        else {}
    )
    if state.pending_approval:
        progress["approval"] = {
            key: state.pending_approval[key]
            for key in ("capability_id", "target", "path", "reason", "scopes")
            if state.pending_approval.get(key) is not None
        }

    # Normalised workspace tree — resources + artifacts only
    workspace_entities = [e for e in entities if e.kind in {"resource", "artifact"}]
    workspace_tree = normalize_workspace_tree(workspace_entities)

    # Normalised runtime tree — one task/workflow/operation/execution hierarchy
    runtime_entities = [
        e
        for e in entities
        if e.kind in {"workflow", "task", "child_task", "operation", "execution"}
    ]
    runtime_tree = normalize_runtime_tree(runtime_entities)

    # Trace from stream
    trace = tuple(state.stream)

    # Model request fields — latest observed from ProjectionState
    model_provider = state.active_provider or ""
    model_name = state.active_model or ""
    model_role = state.active_model_role or ""
    model_request_id = state.active_model_request_id or ""
    model_request_status = state.model_request_status or "idle"
    # Build model_request_label: em-dash unless both active_provider and active_model exist.
    if model_provider and model_name:
        model_request_label = f"{model_provider}/{model_name}"
    else:
        model_request_label = "\u2014"  # em-dash

    return OIScene(
        viewport=viewport,
        status=state.status,
        character=sanitize_terminal_text(character).strip().lower() or "owl",
        entities=entities,
        alerts=alerts,
        stream=list(state.stream)[-8:] + ([state.stream_partial] if state.stream_partial else []),
        buddy_anchor=buddy_anchor,
        mode=mode,
        code_view=code_view,
        diagnostics=tuple(active.diagnostics if active else state.diagnostics),
        verification_checks=tuple(state.verification_checks.values()),
        instruments=tuple(state.instruments),
        progress=progress,
        workspace_tree=workspace_tree,
        runtime_tree=runtime_tree,
        trace=trace,
        model_provider=model_provider,
        model=model_name,
        model_role=model_role,
        model_request_id=model_request_id,
        model_request_status=model_request_status,
        model_request_label=model_request_label,
    )


__all__ = [
    "OIScene",
    "SceneEntity",
    "TreeNode",
    "build_oi_scene",
    "normalize_runtime_tree",
    "normalize_workspace_tree",
    "tree_rows",
]
