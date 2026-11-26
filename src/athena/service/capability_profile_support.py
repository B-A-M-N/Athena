"""Required capability-profile validation for service startup."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

_logger = logging.getLogger("athena.service.capability_profile_support")

__all__ = ["CapabilityProfileSupport"]


class CapabilityProfileSupport:
    """Validate configured native, MCP, skill, pack, and delegate requirements."""

    def __init__(
        self,
        *,
        config: Any,
        registry: Callable[[], Any],
        mcp_status: Callable[[], dict[str, dict[str, Any]]],
        skill_get: Callable[[str], Any],
        pack_inspect: Callable[[str], Any],
        delegate_preflight: Callable[[str], dict[str, Any]],
    ) -> None:
        self._config = config
        self._registry = registry
        self._mcp_status = mcp_status
        self._skill_get = skill_get
        self._pack_inspect = pack_inspect
        self._delegate_preflight = delegate_preflight
        self._status: dict[str, Any] = {}

    def live_status(
        self,
        *,
        mcp: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Refresh the synchronous profile projection without async probes."""
        configured = tuple(self._config.effective_required_capabilities)
        if not configured:
            self._status = {
                "profile": self._config.capability_profile,
                "required": [],
                "resolved": [],
                "missing": [],
                "status": "ok",
                "blocking": False,
            }
            return dict(self._status)
        previous = {
            str(item.get("id")): dict(item)
            for item in self._status.get("missing", ())
            if isinstance(item, Mapping)
        }
        resolved: list[str] = []
        missing: list[dict[str, Any]] = []
        aliases = {
            "terminal": "terminal_session",
            "external_delegate": "external_delegate",
            "external-delegate": "external_delegate",
        }
        from athena.protocol.capabilities import Availability

        registry = self._registry()
        mcp_status = dict(mcp or {})
        for configured_id in configured:
            requirement = str(configured_id).strip()
            kind, separator, identifier = requirement.partition(":")
            kind = kind.casefold() if separator else "capability"
            identifier = identifier.strip() if separator else requirement
            reason: str | None = None
            if kind == "mcp":
                state = mcp_status.get(identifier)
                if state is None:
                    reason = "MCP server is not configured"
                elif state.get("state") != "connected":
                    reason = str(state.get("last_error") or state.get("state") or "not connected")
            elif kind == "capability":
                if registry is None:
                    reason = "capability registry is not initialized"
                else:
                    try:
                        descriptor = registry.resolve(
                            aliases.get(identifier.casefold(), identifier)
                        )
                        if descriptor.availability is not Availability.AVAILABLE:
                            reason = f"capability is {descriptor.availability.value}"
                    except Exception:
                        reason = "capability is not registered"
            elif kind == "delegate":
                try:
                    preflight = self._delegate_preflight(identifier)
                    if not preflight.get("available"):
                        reason = str(preflight.get("reason") or "delegate is unavailable")
                except KeyError:
                    reason = "delegate is not configured"
            elif kind in {"skill", "pack"}:
                if requirement in previous:
                    reason = str(previous[requirement].get("reason") or "not ready")
                elif self._status.get("status") != "ok":
                    reason = "capability profile has not been verified"
            else:
                reason = f"unknown capability requirement kind: {kind}"
            if reason is None:
                resolved.append(requirement)
            else:
                missing.append({"id": requirement, "reason": reason})
        self._status = {
            "profile": self._config.capability_profile,
            "required": list(configured),
            "resolved": resolved,
            "missing": missing,
            "status": "ok" if not missing else "failed",
            "blocking": True,
        }
        return dict(self._status)

    async def validate(self) -> dict[str, Any]:
        configured = tuple(self._config.effective_required_capabilities)
        status: dict[str, Any] = {
            "profile": self._config.capability_profile,
            "required": list(configured),
            "resolved": [],
            "missing": [],
            "blocking": bool(configured),
        }
        if not configured:
            status["status"] = "ok"
            return status
        aliases = {
            "terminal": "terminal_session",
            "external_delegate": "external_delegate",
            "external-delegate": "external_delegate",
            "external delegates": "external_delegate",
        }
        from athena.protocol.capabilities import Availability

        registry = self._registry()
        mcp_status = self._mcp_status()
        for requested in configured:
            requirement = str(requested).strip()
            kind, separator, identifier = requirement.partition(":")
            kind = kind.casefold() if separator else "capability"
            identifier = identifier.strip() if separator else requirement
            reason: str | None = None
            if kind == "mcp":
                state = mcp_status.get(identifier)
                if state is None:
                    reason = "MCP server is not configured"
                elif state.get("state") != "connected":
                    reason = str(state.get("last_error") or state.get("state") or "not connected")
            elif kind == "skill":
                skill = await self._skill_get(identifier)
                if skill is None:
                    reason = "skill is not installed"
                elif not bool(getattr(skill, "enabled", False)):
                    reason = "skill is disabled"
            elif kind == "pack":
                detail = await self._pack_inspect(identifier)
                if detail is None:
                    reason = "pack is not installed"
                elif not bool(detail.get("enabled")):
                    reason = "pack is disabled"
                elif (detail.get("health_detail") or {}).get("status") != "healthy":
                    health = detail.get("health_detail") or {}
                    reason = str(health.get("reason") or health.get("status"))
            elif kind == "delegate":
                try:
                    preflight = self._delegate_preflight(identifier)
                    if not preflight.get("available"):
                        reason = str(preflight.get("reason") or "delegate is unavailable")
                except KeyError:
                    reason = "delegate is not configured"
            elif kind == "capability":
                resolved_name = aliases.get(identifier.casefold(), identifier)
                if registry is None:
                    reason = "capability registry is not initialized"
                else:
                    try:
                        descriptor = registry.resolve(resolved_name)
                    except Exception:
                        reason = "capability is not registered"
                    else:
                        if descriptor.availability is not Availability.AVAILABLE:
                            reason = f"capability is {descriptor.availability.value}"
            else:
                reason = f"unknown capability requirement kind: {kind}"
            if reason is None:
                status["resolved"].append(requirement)
            else:
                status["missing"].append({"id": requirement, "reason": reason})
        status["status"] = "ok" if not status["missing"] else "failed"
        self._status = dict(status)
        return status
