"""Workspace capability family: status/snapshot/restore/diff/changed_files
over the task's workspace root (P1-10, checkpoint-manager backed)."""

from athena.capabilities import environment as _facade
from athena.capabilities.operations import native_descriptor
from athena.protocol.capabilities import CapabilityOrigin
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.capabilities import CapabilityResult
from athena.protocol.capabilities import EffectClass
from typing import Any
import json
import os

from athena.capabilities.environment_common import _result
from athena.concurrency import run_blocking


class WorkspaceCapability:
    """Status / snapshot / diff / changed-files for the task workspace."""

    def __init__(
        self,
        checkpoint_manager=None,
        *,
        mutation_store=None,
        mutation_observer=None,
        project_index_store=None,
        project_index_coordinator=None,
    ) -> None:
        from athena.project import ProjectInspector
        from athena.project.index import ProjectIndexBuilder

        self._checkpoints = checkpoint_manager
        self._mutations = mutation_store
        self._mutation_observer = mutation_observer
        self._project_inspector = ProjectInspector()
        self._project_index_builder = ProjectIndexBuilder(
            inspector=self._project_inspector,
        )
        self._project_index_store = project_index_store
        self._project_index_coordinator = project_index_coordinator

    descriptor = native_descriptor(
        id="workspace",
        description=(
            "Workspace lifecycle: status summary, snapshot (via checkpoint "
            "manager), restore, changed-file listing (git when available, "
            "mtime scan otherwise), project profile, and bounded impact hints. "
            "Operations: status/changed_files/profile/index/impact/snapshot/restore."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "changed_files",
                        "profile",
                        "index",
                        "impact",
                        "snapshot",
                        "restore",
                    ],
                },
                "label": {"type": "string"},
                "checkpoint_id": {"type": "string"},
                "expected_fingerprint": {"type": "string", "minLength": 1},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4096},
                },
                "path": {"type": "string", "maxLength": 4096},
                "refresh": {"type": "boolean"},
            },
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
    )

    def _bind_context(self, context) -> str | None:
        return context.workspace.root if context else None

    async def invoke(self, request: CapabilityRequest, context=None, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        root = self._bind_context(context)
        refresh_index = bool(args.get("refresh", False))
        if root is None:
            return _result(request, ok=False, error="no workspace bound to this call")

        if op == "status":

            def _st():
                files = sum(len(f) for _, _, f in os.walk(root))
                size = sum(
                    os.path.getsize(os.path.join(d, f))
                    for d, _, fs in os.walk(root)
                    for f in fs
                    if os.path.exists(os.path.join(d, f))
                )
                git = "yes" if os.path.isdir(os.path.join(root, ".git")) else "no"
                return f"root={root}\nfiles={files}\nbytes={size}\ngit={git}"

            return _result(request, output=await run_blocking(_st))

        if op == "changed_files":

            def _changed():
                rc, out, _ = _facade._run(["git", "-C", root, "status", "--porcelain"])
                if rc == 0 and out.strip():
                    return out
                # fallback: newest-modified files
                entries = []
                for d, _, fs in os.walk(root):
                    if ".git" in d:
                        continue
                    for f in fs:
                        p = os.path.join(d, f)
                        try:
                            entries.append((os.path.getmtime(p), os.path.relpath(p, root)))
                        except OSError:
                            pass
                entries.sort(reverse=True)
                return "\n".join(f"{e[1]}" for e in entries[:25])

            return _result(request, output=await run_blocking(_changed))

        if op == "profile":
            index = await self._build_index(root, refresh=refresh_index)
            profile_data = dict(index.profile)
            profile_data["environment"] = dict(index.environment)
            profile_data["index_revision"] = index.index_revision
            return _result(
                request,
                output=json.dumps(profile_data, sort_keys=True),
                meta={"profile": profile_data, "index_revision": index.index_revision},
            )

        if op == "index":
            index = await self._build_index(root, refresh=refresh_index)
            record = index.to_record()
            return _result(
                request,
                output=json.dumps(record, sort_keys=True),
                meta={"index_revision": index.index_revision},
            )

        if op == "impact":
            raw_paths = args.get("paths")
            if raw_paths is None and args.get("path") is not None:
                raw_paths = [args["path"]]
            if not isinstance(raw_paths, list) or not raw_paths:
                return _result(
                    request,
                    ok=False,
                    error="impact requires a non-empty paths list",
                )
            root_real = os.path.realpath(os.path.abspath(root))
            for raw_path in raw_paths:
                candidate = os.path.realpath(
                    os.path.abspath(
                        str(raw_path)
                        if os.path.isabs(str(raw_path))
                        else os.path.join(root, str(raw_path))
                    )
                )
                if candidate != root_real and not candidate.startswith(root_real + os.sep):
                    return _result(
                        request,
                        ok=False,
                        error=f"impact path outside workspace: {raw_path}",
                    )
            try:
                index = await self._build_index(root, refresh=refresh_index)
                impact = index.impact([str(path) for path in raw_paths])
            except ValueError as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(
                request,
                output=json.dumps(impact, sort_keys=True),
                meta={"impact": impact},
            )

        if op == "snapshot":
            mgr = self._checkpoints
            label = str(args.get("label") or "workspace-snapshot")
            manifest = await mgr.capture(
                task_id=request.task_id or "unknown", workspace_root=root, label=label
            )
            cid = manifest.get("checkpoint_id") or manifest.get("id")
            return _result(
                request,
                output=f"snapshot {cid} ({manifest.get('file_count')} files)",
                meta={"checkpoint_id": cid},
            )

        if op == "restore":
            cid = str(args.get("checkpoint_id") or "")
            if not cid or self._checkpoints is None:
                return _result(request, ok=False, error="checkpoint_id required")
            mutation_id = None
            before_checkpoint = None
            task_id = request.task_id or "unknown"
            if self._mutations is not None:
                # A restore is a real workspace mutation. Capture the
                # before-state and write the intent before touching the
                # target, so a partial restore is recoverable and auditable.
                before_checkpoint = await self._checkpoints.capture(
                    task_id=task_id,
                    workspace_root=root,
                    label=f"before-restore-{cid}",
                )
                expected = await self._checkpoints.fingerprint(root)
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    root,
                    "workspace.restore",
                    before_ref=f"checkpoint://{before_checkpoint['id']}",
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                    metadata={"checkpoint_id": cid, "expected_fingerprint": expected},
                )
                await self._mutations.mark_started(mutation_id)
            try:
                outcome = await self._checkpoints.restore(
                    checkpoint_id=cid,
                    workspace_root=root,
                    expected_fingerprint=(
                        str(args.get("expected_fingerprint") or expected)
                        if self._mutations is not None
                        else args.get("expected_fingerprint")
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if mutation_id is not None:
                    await self._mutations.mark_recovery_required(mutation_id)
                return _result(request, ok=False, error=str(exc))
            if mutation_id is not None:
                if before_checkpoint is None:
                    raise RuntimeError("restore mutation has no before checkpoint")
                await self._mutations.complete(
                    mutation_id,
                    after_hash=outcome.get("workspace_fingerprint"),
                    reversible=True,
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                )
            mutation = None
            if mutation_id is not None:
                mutation = {
                    "mutation_id": mutation_id,
                    "resource": root,
                    "operation": "workspace.restore",
                    "after_hash": outcome.get("workspace_fingerprint"),
                    "before_ref": (
                        f"checkpoint://{before_checkpoint['id']}"
                        if before_checkpoint is not None
                        else None
                    ),
                    "reversible": True,
                    "inverse": {"checkpoint_id": before_checkpoint["id"]}
                    if before_checkpoint is not None
                    else None,
                }
            return _result(
                request,
                output=json.dumps(outcome)[:1000],
                meta={"mutation": mutation} if mutation is not None else None,
            )

        return _result(request, ok=False, error=f"unknown operation: {op}")

    async def _build_index(
        self,
        root: str,
        *,
        refresh: bool = False,
    ) -> Any:
        """Return the central index, rebuilding only when requested/stale."""
        if self._project_index_coordinator is not None:
            return await self._project_index_coordinator.current(root, refresh=refresh)
        index = await run_blocking(self._project_index_builder.build, root)
        if self._project_index_store is not None:
            await self._project_index_store.save(index)
        return index
