"""Provision and supervise Athena's dedicated Hermes referee.

This module is the operator-facing boundary.  It deliberately invokes Hermes
with an explicit interpreter and source root, passes credentials over stdin,
and enables Athena's referee configuration only after the live capability and
structured-review probes succeed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from athena.hermes.agent_adapter import HermesAgentEvaluator
from athena.hermes.referee import HermesReferee, ReviewPacket
from athena.policy.credentials import SecretManager, write_user_secret
from athena.service.config import global_config_path, load_config, load_toml_file

DEFAULT_PROFILE = "athena-referee"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8643
DEFAULT_CREDENTIAL_ID = "HERMES_REFEREE_API_KEY"


class HermesRefereeManagerError(RuntimeError):
    """Raised when referee provisioning or proof cannot complete."""


@dataclass(frozen=True)
class HermesRefereeManager:
    """Idempotent manager for one dedicated local Hermes referee service."""

    config_path: Path | None = None
    runtime_root: Path | None = None
    profile: str = DEFAULT_PROFILE
    endpoint: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    credential_id: str = DEFAULT_CREDENTIAL_ID
    timeout_seconds: float = 60.0

    def _config_file(self) -> Path:
        return (self.config_path or global_config_path()).expanduser()

    def _settings(self) -> Any:
        return load_config(explicit_path=str(self._config_file()))

    def _endpoint(self) -> str:
        if self.endpoint.strip():
            return self.endpoint.strip().rstrip("/")
        return f"http://{self.host}:{self.port}"

    def _service_binding(self) -> tuple[str, int]:
        """Return the loopback bind selected by the managed endpoint."""
        parsed = urlsplit(self._endpoint())
        if parsed.scheme not in {"http", "https"} or parsed.hostname != self.host:
            raise HermesRefereeManagerError(
                "managed Hermes referee endpoint must bind to the configured loopback host"
            )
        port = parsed.port or self.port
        if not 1 <= port <= 65535:
            raise HermesRefereeManagerError("managed Hermes referee port is invalid")
        return parsed.hostname, port

    def _root(self) -> Path:
        configured = self.runtime_root
        if configured is None:
            settings = self._settings().hermes_referee
            configured = Path(settings.runtime_root).expanduser() if settings.runtime_root else None
        if configured is None:
            env_root = os.environ.get("ATHENA_HERMES_ROOT", "").strip()
            configured = Path(env_root).expanduser() if env_root else None
        if configured is None:
            raise HermesRefereeManagerError(
                "Hermes runtime root is required; pass --hermes-root or set "
                "ATHENA_HERMES_ROOT"
            )
        root = configured.resolve()
        required = (
            root / "hermes_cli" / "main.py",
            root / "gateway" / "platforms" / "api_server.py",
        )
        if not root.is_dir() or not all(path.is_file() for path in required):
            raise HermesRefereeManagerError(
                f"invalid Hermes runtime root: {root}"
            )
        api_server = required[1].read_text(encoding="utf-8", errors="replace")
        if '"tool_execution": "disabled"' not in api_server or "_referee_mode" not in api_server:
            raise HermesRefereeManagerError(
                f"Hermes runtime root does not contain the referee implementation: {root}"
            )
        return root

    def _python(self, root: Path) -> Path:
        for relative in (
            Path("venv/bin/python"),
            Path("venv/bin/python3"),
            Path(".venv/bin/python"),
            Path(".venv/bin/python3"),
        ):
            candidate = root / relative
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return Path(sys.executable).resolve()

    def _environment(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        # A parent Hermes profile must never win over the explicit profile.
        env.pop("HERMES_HOME", None)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(root) + (os.pathsep + existing if existing else "")
        env["HERMES_EXPECTED_RUNTIME_ROOT"] = str(root)
        return env

    async def _run_hermes(self, args: list[str], *, secret: str | None = None) -> None:
        root = self._root()
        argv = [str(self._python(root)), "-m", "hermes_cli.main", *args]
        stdin = asyncio.subprocess.PIPE if secret is not None else asyncio.subprocess.DEVNULL
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            env=self._environment(root),
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = (secret + "\n").encode("utf-8") if secret is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise HermesRefereeManagerError("Hermes provisioning timed out") from exc
        if process.returncode:
            # Never include child stdout: a future Hermes version must not be
            # able to make a provisioning failure echo the bearer credential.
            detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
            message = detail[-1] if detail else f"exit {process.returncode}"
            if secret:
                message = message.replace(secret, "[REDACTED]")
            raise HermesRefereeManagerError(f"Hermes provisioning failed: {message}")

    async def _probe(self, key: str, *, e2e: bool) -> Mapping[str, Any]:
        adapter = HermesAgentEvaluator(
            endpoint=self._endpoint(),
            profile=self.profile,
            timeout_seconds=self.timeout_seconds,
            api_key=key,
        )
        try:
            preflight = await adapter.preflight()
            verdict = None
            if e2e:
                packet = ReviewPacket(
                    kind="candidate",
                    mission={"purpose": "transport verification"},
                    verification_results=({"id": "setup", "passed": True},),
                    release_results={"review_eligible": True},
                )
                verdict = await HermesReferee(adapter).review(packet)
                if verdict.packet_hash != packet.digest():
                    raise HermesRefereeManagerError("Hermes setup probe returned the wrong packet hash")
                if verdict.rationale.startswith(
                    (
                        "Hermes preflight/evaluator failed:",
                        "Hermes returned an invalid verdict",
                    )
                ):
                    raise HermesRefereeManagerError(verdict.rationale)
            return {
                "preflight": preflight.to_record(),
                "e2e_decision": verdict.decision.value if verdict is not None else None,
            }
        except httpx.HTTPError as exc:
            raise HermesRefereeManagerError(f"Hermes referee endpoint unavailable: {exc}") from exc
        finally:
            await adapter.aclose()

    def _write_settings(self, *, enabled: bool, runtime_root: Path | None) -> None:
        path = self._config_file()
        data = load_toml_file(path)
        section = data.setdefault("hermes_referee", {})
        if not isinstance(section, dict):
            raise HermesRefereeManagerError(f"{path} has a non-table hermes_referee value")
        section.update(
            {
                "enabled": enabled,
                "managed": True,
                "endpoint": self._endpoint(),
                "profile": self.profile,
                "credential_id": self.credential_id,
                "required_for_self_host": True,
            }
        )
        if runtime_root is not None:
            section["runtime_root"] = str(runtime_root)
        import tomli_w

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as handle:
            tomli_w.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _existing_key(self, *credential_ids: str | None) -> str | None:
        manager = SecretManager()
        candidates = credential_ids or (self.credential_id, DEFAULT_CREDENTIAL_ID)
        for candidate in (*candidates, self.credential_id, DEFAULT_CREDENTIAL_ID):
            if not candidate:
                continue
            try:
                return manager.resolve(candidate, owner_task="system")
            except Exception:
                continue
        return None

    async def setup(self) -> Mapping[str, Any]:
        """Provision, prove, and then enable the managed referee."""
        root = self._root()
        key = self._existing_key() or secrets.token_urlsafe(48)
        await self._run_hermes(
            [
                "-p",
                self.profile,
                "referee",
                "provision",
                "--profile",
                self.profile,
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--key-stdin",
            ],
            secret=key,
        )
        report = await self._probe(key, e2e=True)
        write_user_secret(self.credential_id, key)
        self._write_settings(enabled=True, runtime_root=root)
        return report

    async def status(self) -> Mapping[str, Any]:
        settings = self._settings().hermes_referee
        result: dict[str, Any] = {
            "enabled": settings.enabled,
            "managed": settings.managed,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "runtime_root": settings.runtime_root,
        }
        if not settings.enabled:
            result["state"] = "disabled"
            return result
        key = self._existing_key(settings.credential_id)
        if not key:
            result.update(state="degraded", error="referee credential unavailable")
            return result
        try:
            report = await HermesRefereeManager(
                config_path=self._config_file(),
                runtime_root=Path(settings.runtime_root) if settings.runtime_root else self.runtime_root,
                profile=settings.profile,
                endpoint=settings.endpoint,
                credential_id=settings.credential_id or self.credential_id,
                timeout_seconds=settings.timeout_seconds,
            )._probe(key, e2e=False)
        except Exception as exc:
            result.update(state="unsafe", safety_verified=False, error=str(exc))
            return result
        result.update(state="safety_verified", safety_verified=True, **report)
        return result

    async def repair(self) -> Mapping[str, Any]:
        settings = self._settings().hermes_referee
        configured_root = Path(settings.runtime_root).expanduser() if settings.runtime_root else None
        configured_profile = settings.profile or self.profile
        configured_endpoint = settings.endpoint or self.endpoint
        configured_host, configured_port = HermesRefereeManager(
            endpoint=configured_endpoint,
            host=self.host,
            port=self.port,
        )._service_binding()
        manager = HermesRefereeManager(
            config_path=self._config_file(),
            runtime_root=configured_root or self.runtime_root,
            profile=configured_profile,
            endpoint=configured_endpoint,
            host=configured_host,
            port=configured_port,
            credential_id=settings.credential_id or self.credential_id,
            timeout_seconds=settings.timeout_seconds,
        )
        root = manager._root()
        key = manager._existing_key() or secrets.token_urlsafe(48)
        await manager._run_hermes(
            [
                "-p",
                manager.profile,
                "referee",
                "provision",
                "--profile",
                manager.profile,
                "--host",
                manager.host,
                "--port",
                str(manager.port),
                "--key-stdin",
            ],
            secret=key,
        )
        report = await manager._probe(key, e2e=True)
        write_user_secret(manager.credential_id, key)
        manager._write_settings(enabled=True, runtime_root=root)
        return report

    async def disable(self) -> None:
        settings = self._settings().hermes_referee
        root = Path(settings.runtime_root).expanduser() if settings.runtime_root else self.runtime_root
        manager = HermesRefereeManager(
            config_path=self._config_file(),
            runtime_root=root,
            profile=settings.profile or self.profile,
            endpoint=settings.endpoint or self.endpoint,
            credential_id=settings.credential_id or self.credential_id,
            timeout_seconds=settings.timeout_seconds,
        )
        if root is not None and root.exists():
            try:
                await manager._run_hermes(["-p", manager.profile, "gateway", "stop"])
            except HermesRefereeManagerError:
                # Disabling Athena is still safe if the already-stopped service
                # cannot be reached. The next setup/repair will reconcile it.
                pass
        manager._write_settings(enabled=False, runtime_root=root.resolve() if root else None)


def run_referee_action(
    action: str,
    *,
    config_path: str | None = None,
    runtime_root: str | None = None,
    profile: str = DEFAULT_PROFILE,
    endpoint: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    credential_id: str = DEFAULT_CREDENTIAL_ID,
) -> int:
    """Synchronous CLI bridge; child processes remain argv-only."""
    manager = HermesRefereeManager(
        config_path=Path(config_path).expanduser() if config_path else None,
        runtime_root=Path(runtime_root).expanduser() if runtime_root else None,
        profile=profile,
        endpoint=endpoint,
        host=host,
        port=port,
        credential_id=credential_id,
    )

    async def run() -> Mapping[str, Any] | None:
        if action == "setup":
            return await manager.setup()
        if action == "status":
            return await manager.status()
        if action == "repair":
            return await manager.repair()
        if action == "disable":
            await manager.disable()
            return None
        raise HermesRefereeManagerError(f"unknown referee action: {action}")

    try:
        result = asyncio.run(run())
    except Exception as exc:
        print(f"athena referee {action}: failed: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print(f"athena referee {action}: disabled")
        return 0
    state = result.get("state") if isinstance(result, Mapping) else None
    if action in {"setup", "repair"}:
        print(f"athena referee {action}: safety verified")
    else:
        print(f"athena referee status: {state or 'unknown'}")
    if isinstance(result, Mapping) and result.get("e2e_decision"):
        print(f"  e2e verdict: {result['e2e_decision']}")
    return 0 if state in {None, "safety_verified"} or action in {"setup", "repair"} else 1


__all__ = ["HermesRefereeManager", "HermesRefereeManagerError", "run_referee_action"]
