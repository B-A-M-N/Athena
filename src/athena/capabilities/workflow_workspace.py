"""Disposable workspace staging for workflow trials and replay validation."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace

from athena.protocol.tasks import MutationMode, NetworkPolicy
from athena.workspace_manifest import copy_workspace_tree_async, rmtree_async


async def stage_disposable_workspace(
    workspace,
    *,
    identifier: str,
    prefix: str,
    max_files: int,
    max_bytes: int,
):
    """Copy a workspace off-loop and clean up if staging is interrupted."""
    root = tempfile.mkdtemp(prefix=prefix)
    try:
        await copy_workspace_tree_async(
            workspace.root,
            root,
            dirs_exist_ok=True,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except BaseException:
        await cancel_safe_cleanup(root)
        raise
    return replace(
        workspace,
        id=identifier,
        root=root,
        mutation_mode=MutationMode.DIRECT,
        network_policy=NetworkPolicy.DENY,
    ), root


async def cancel_safe_cleanup(path: str) -> None:
    """Finish disposable-workspace cleanup even when the caller is cancelled."""
    cleanup = asyncio.create_task(rmtree_async(path, ignore_errors=True))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(cleanup)
        finally:
            raise


__all__ = ["cancel_safe_cleanup", "stage_disposable_workspace"]
