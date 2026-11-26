"""Optional Hermes referee transport lifecycle owned by the service runtime."""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any

import httpx

from athena.hermes import HermesAgentEvaluator, HermesReferee, ReviewPacket
from athena.service.config import HermesSupervisionMode

__all__ = ["HermesRuntime", "HermesRuntimePorts"]


class HermesRuntimePorts:
    """Explicit configuration, secret, and health resources for Hermes."""

    _RESOURCE_NAMES = {
        "config": "config",
        "secrets": "_secrets",
        "startup_health": "_startup_health",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCE_NAMES.get(name)
        if target is None:
            raise AttributeError(f"Hermes runtime port is not allowed: {name}")
        return getattr(self._owner, target, None)


class HermesRuntime:
    """Own the optional evaluator/referee transport and its safety evidence."""

    def __init__(self, *, ports: HermesRuntimePorts, referee: HermesReferee | None = None) -> None:
        self._ports = ports
        self._referee = referee
        self._adapter: HermesAgentEvaluator | None = None
        self._referee_owned = False
        self._status_error: str | None = None

    @property
    def referee(self) -> HermesReferee | None:
        return self._referee

    @referee.setter
    def referee(self, value: HermesReferee | None) -> None:
        self._referee = value

    @property
    def adapter(self) -> HermesAgentEvaluator | None:
        return self._adapter

    @adapter.setter
    def adapter(self, value: HermesAgentEvaluator | None) -> None:
        self._adapter = value

    @property
    def referee_owned(self) -> bool:
        return self._referee_owned

    @referee_owned.setter
    def referee_owned(self, value: bool) -> None:
        self._referee_owned = bool(value)

    @property
    def status_error(self) -> str | None:
        return self._status_error

    @status_error.setter
    def status_error(self, value: str | None) -> None:
        self._status_error = value

    @property
    def supervision_mode(self) -> HermesSupervisionMode:
        return self._ports.config.hermes_referee.supervision_mode

    @property
    def supervision_active(self) -> bool:
        return (
            self._ports.config.hermes_referee.transport_enabled
            and self.supervision_mode is not HermesSupervisionMode.OFF
            and self._referee is not None
        )

    async def status(self) -> dict[str, Any]:
        """Return operator-safe configuration and connectivity status."""
        settings = self._ports.config.hermes_referee
        if not settings.transport_enabled:
            return {
                "enabled": False,
                "self_host_supervision": settings.supervision_mode.value,
                "state": "disabled",
                "profile": settings.profile,
                "endpoint": settings.endpoint,
            }
        if self._adapter is None:
            return {
                "enabled": settings.transport_enabled,
                "self_host_supervision": settings.supervision_mode.value,
                "state": (
                    "configured_unverified"
                    if self._referee is not None and self._status_error is None
                    else "error"
                ),
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "error": self._status_error,
            }
        try:
            preflight = await self._adapter.preflight()
        except httpx.HTTPError as exc:
            return self._status_error_record(settings, "disconnected", exc)
        except Exception as exc:
            from athena.hermes.agent_adapter import HermesRefereeSafetyError

            state = "unsafe" if isinstance(exc, HermesRefereeSafetyError) else "error"
            return self._status_error_record(settings, state, exc)
        return {
            "enabled": settings.transport_enabled,
            "self_host_supervision": settings.supervision_mode.value,
            "state": "safety_verified",
            "safety_verified": True,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "policy_fingerprint": preflight.policy_fingerprint,
            "runtime": dict(preflight.capabilities.get("runtime") or {}),
            "referee": dict(preflight.capabilities.get("referee") or {}),
            "build": dict(preflight.capabilities.get("build") or {}),
        }

    @staticmethod
    def _status_error_record(settings: Any, state: str, error: Exception) -> dict[str, Any]:
        return {
            "enabled": settings.transport_enabled,
            "self_host_supervision": settings.supervision_mode.value,
            "state": state,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "safety_verified": False,
            "error": str(error),
        }

    async def preflight(self) -> None:
        """Run the optional safety probe without blocking normal startup."""
        settings = self._ports.config.hermes_referee
        if not settings.transport_enabled:
            return
        evaluator: HermesAgentEvaluator | HermesReferee | None = self._adapter or self._referee
        probe = getattr(evaluator, "preflight", None)
        if not callable(probe):
            reason = "Hermes referee does not expose a safety preflight"
            self._status_error = reason
            self._set_health(settings, "degraded", state="injected", error=reason)
            return
        try:
            result = probe()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            from athena.hermes.agent_adapter import HermesRefereeSafetyError

            reason = str(exc)
            self._status_error = reason
            state = (
                "unsafe"
                if isinstance(exc, HermesRefereeSafetyError)
                else ("disconnected" if isinstance(exc, httpx.HTTPError) else "error")
            )
            self._set_health(settings, "degraded", state=state, error=reason)
            return
        record = result.to_record() if hasattr(result, "to_record") else {}
        self._set_health(
            settings,
            "ok",
            state="safety_verified",
            runtime=record.get("runtime", {}),
            referee=record.get("referee", {}),
            build=record.get("build", {}),
        )
        self._status_error = None

    def _set_health(self, settings: Any, status: str, **extra: Any) -> None:
        self._ports.startup_health["checks"]["hermes_referee"] = {
            "status": status,
            "blocking": False,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            **extra,
        }

    async def require_verified(self) -> None:
        """Refuse self-host work only when required Hermes supervision is active."""
        settings = self._ports.config.hermes_referee
        if self.supervision_mode is not HermesSupervisionMode.REQUIRED:
            return
        if self._adapter is None:
            reason = self._status_error or "Hermes referee is not configured"
            raise RuntimeError(
                "Self-hosting refused: Hermes referee is configured as required "
                f"but has not proven its read-only runtime contract ({reason}). "
                "Run `athena referee repair`."
            )
        try:
            preflight = await self._adapter.preflight()
        except Exception as exc:
            self._status_error = str(exc)
            raise RuntimeError(
                "Self-hosting refused: Hermes referee safety verification failed "
                f"({exc}). Run `athena referee repair`."
            ) from exc
        self._set_health(
            settings,
            "ok",
            state="safety_verified",
            runtime=dict(preflight.capabilities.get("runtime") or {}),
            referee=dict(preflight.capabilities.get("referee") or {}),
            build=dict(preflight.capabilities.get("build") or {}),
        )

    def configure(self) -> None:
        """Build the optional transport after the secret boundary exists."""
        settings = self._ports.config.hermes_referee
        if self._referee is not None and not self._referee_owned:
            self._set_health(settings, "ok", state="configured_unverified")
            return
        if not settings.transport_enabled:
            self._set_health(settings, "ok", state="disabled")
            return
        api_key = ""
        self._status_error = None
        if settings.credential_id:
            try:
                if self._ports.secrets is None:
                    raise RuntimeError("SecretManager is not ready")
                api_key = self._ports.secrets.resolve(settings.credential_id)
            except Exception as exc:
                reason = f"Hermes credential unavailable: {exc}"
                self._status_error = reason

                async def unavailable(_packet: ReviewPacket) -> Mapping[str, Any]:
                    raise RuntimeError(reason)

                self._referee = HermesReferee(unavailable)
                self._referee_owned = True
                self._set_health(settings, "degraded", state="error", error=reason)
                return
        try:
            adapter = HermesAgentEvaluator(
                endpoint=settings.endpoint,
                profile=settings.profile,
                timeout_seconds=settings.timeout_seconds,
                api_key=api_key,
                allow_remote=settings.allow_remote,
                allow_insecure_remote=settings.allow_insecure_remote,
            )
        except (TypeError, ValueError) as exc:
            reason = f"Hermes configuration invalid: {exc}"
            self._status_error = reason

            async def unavailable(_packet: ReviewPacket) -> Mapping[str, Any]:
                raise RuntimeError(reason)

            self._referee = HermesReferee(unavailable)
            self._referee_owned = True
            self._set_health(settings, "degraded", state="error", error=reason)
            return
        self._adapter = adapter
        self._referee = HermesReferee(adapter)
        self._referee_owned = True
        self._set_health(settings, "ok", state="configured")

    async def close(self) -> None:
        if self._adapter is not None:
            try:
                await self._adapter.aclose()
            finally:
                self._adapter = None
        if self._referee_owned:
            self._referee = None
            self._referee_owned = False
