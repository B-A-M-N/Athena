"""Activation and rollback coordinator for declarative pack contributions."""

from __future__ import annotations

from dataclasses import replace

from athena.packs.models import PackState
from athena.packs.ports import PackActivationPorts


class PackActivator:
    """Own activation ordering, rehydration, and rollback semantics."""

    def __init__(self, ports: PackActivationPorts) -> None:
        """Receive exactly the activation capabilities this mechanism needs."""
        self.ports = ports

    async def activate(self, state: PackState) -> None:
        existing = await self.ports.contributions(state.id)
        if existing:
            await self.reactivate_existing(state, existing)
            return
        contributions: list[tuple[str, str]] = []
        try:
            for activator in (
                self.ports.activate_skills,
                self.ports.activate_workflows,
                self.ports.activate_capabilities,
                self.ports.activate_instruments,
                self.ports.activate_mcp_servers,
                self.ports.activate_hooks,
            ):
                created = await activator(state)
                contributions.extend(created)
                for kind, contribution_id in created:
                    await self.ports.save_contribution(state.id, kind, contribution_id)
        except Exception:  # noqa: BLE001 - partial activation must roll back
            await self.deactivate(state, remove=True)
            raise

    async def reactivate_existing(
        self,
        state: PackState,
        contributions: list[dict[str, str]],
    ) -> None:
        for item in contributions:
            kind = item["kind"]
            contribution_id = item["contribution_id"]
            if kind == "workflow" and self.ports.workflow_store is not None:
                workflow = await self.ports.workflow_store.get(contribution_id)
                if workflow is not None and (
                    not workflow.enabled or workflow.lifecycle_state != "ACTIVE"
                ):
                    await self.ports.workflow_store.save(
                        replace(workflow, enabled=True, lifecycle_state="ACTIVE")
                    )
            elif kind == "skill" and self.ports.skill_lifecycle is not None:
                await self.ports.skill_lifecycle.enable(contribution_id)
        if any(item["kind"] == "capability" for item in contributions):
            await self.ports.activate_capabilities(state)
        if any(item["kind"] == "instrument" for item in contributions):
            await self.ports.activate_instruments(state)
        if any(item["kind"] == "mcp" for item in contributions):
            await self.ports.activate_mcp_servers(state)
        if any(item["kind"] == "hook" for item in contributions):
            await self.ports.activate_hooks(state)

    async def deactivate(self, state: PackState, *, remove: bool) -> None:
        for item in await self.ports.contributions(state.id):
            kind = item["kind"]
            contribution_id = item["contribution_id"]
            if kind == "workflow" and self.ports.workflow_store is not None:
                if remove:
                    await self.ports.workflow_store.delete(contribution_id)
                else:
                    workflow = await self.ports.workflow_store.get(contribution_id)
                    if workflow is not None:
                        await self.ports.workflow_store.save(
                            replace(workflow, enabled=False, lifecycle_state="DISABLED")
                        )
            elif kind == "skill" and self.ports.skill_lifecycle is not None:
                if remove:
                    await self.ports.skill_lifecycle.archive(contribution_id)
                else:
                    await self.ports.skill_lifecycle.disable(contribution_id)
            elif kind in {"capability", "instrument"} and self.ports.fabric is not None:
                registry = self.ports.fabric.global_registry
                try:
                    descriptor = registry.resolve(contribution_id)
                except Exception:  # noqa: BLE001 - optional contribution may be gone
                    descriptor = None
                if descriptor is not None and descriptor.origin.value == "plugin":
                    registry.unregister(contribution_id)
            elif kind == "mcp" and self.ports.mcp_adapter is not None:
                self.ports.mcp_adapter.unregister_connection(contribution_id)
                client = self.ports.mutable.mcp_clients.pop(contribution_id, None)
                if client is not None:
                    await client.close()
            elif kind == "hook":
                for hook_id, callback in self.ports.mutable.hook_callbacks.get(state.id, ()):
                    if hook_id == contribution_id and self.ports.event_store is not None:
                        self.ports.event_store.unsubscribe(callback)
                        self.ports.mutable.hook_contracts.pop(hook_id, None)
        if remove:
            await self.ports.delete_contributions(state.id)
            self.ports.mutable.hook_callbacks.pop(state.id, None)


__all__ = ["PackActivator"]
