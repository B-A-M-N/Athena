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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from athena.execution.process_tree import kill_tree_async
from athena.hermes.agent_adapter import HermesAgentEvaluator, HermesRefereeSafetyError
from athena.hermes.referee import HermesReferee, ReviewPacket
from athena.policy.credentials import (
    FileSource,
    SecretManager,
    delete_user_secret,
    user_secret_dir,
    write_user_secret,
)
from athena.hermes.config import (
    HermesSupervisionMode,
    global_config_path,
    load_settings,
    load_toml_file,
    write_toml_atomic_private,
)

DEFAULT_PROFILE = "athena-referee"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8643
DEFAULT_CREDENTIAL_ID = "HERMES_REFEREE_API_KEY"


class HermesRefereeManagerError(RuntimeError):
    """Raised when referee provisioning or proof cannot complete."""


class HermesRefereeDisconnectedError(HermesRefereeManagerError):
    """Raised when the configured Hermes endpoint cannot be reached."""


@dataclass(frozen=True)
class _HermesTransactionSnapshot:
    """Durable state captured before a managed Hermes lifecycle mutation."""

    config_bytes: bytes | None
    credential_value: str | None
    credential_existed: bool
    previous_enabled: bool
    previous_supervision: str


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
    self_host_supervision: str | None = None

    def _config_file(self) -> Path:
        return (self.config_path or global_config_path()).expanduser()

    def _settings(self) -> Any:
        return load_settings(self._config_file())

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
                "Hermes runtime root is required; pass --hermes-root or set ATHENA_HERMES_ROOT"
            )
        root = configured.resolve()
        required = (
            root / "hermes_cli" / "main.py",
            root / "gateway" / "platforms" / "api_server.py",
        )
        if not root.is_dir() or not all(path.is_file() for path in required):
            raise HermesRefereeManagerError(f"invalid Hermes runtime root: {root}")
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
        process = await asyncio.create_subprocess_exec(  # architecture-lint: allow subprocess-outside-approved-backends reason=owned Hermes referee service; architecture-exception: hermes-referee
            *argv,
            cwd=str(root),
            env=self._environment(root),
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        payload = (secret + "\n").encode("utf-8") if secret is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            await kill_tree_async(process, timeout=1.0)
            await process.communicate()
            raise HermesRefereeManagerError("Hermes provisioning timed out") from exc
        except asyncio.CancelledError:
            await kill_tree_async(process, timeout=1.0)
            await process.communicate()
            raise
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
                    raise HermesRefereeManagerError(
                        "Hermes setup probe returned the wrong packet hash"
                    )
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
            raise HermesRefereeDisconnectedError(
                f"Hermes referee endpoint unavailable: {exc}"
            ) from exc
        finally:
            await adapter.aclose()

    def _write_settings(
        self,
        *,
        enabled: bool,
        runtime_root: Path | None,
        self_host_supervision: str | HermesSupervisionMode,
    ) -> None:
        path = self._config_file()
        data = load_toml_file(path)
        section = data.setdefault("hermes_referee", {})
        if not isinstance(section, dict):
            raise HermesRefereeManagerError(f"{path} has a non-table hermes_referee value")
        try:
            mode = HermesSupervisionMode(str(self_host_supervision).strip().lower())
        except ValueError as exc:
            valid = ", ".join(item.value for item in HermesSupervisionMode)
            raise HermesRefereeManagerError(
                f"self-host supervision must be one of: {valid}"
            ) from exc
        section.pop("required_for_self_host", None)
        section.update(
            {
                "enabled": enabled,
                "managed": True,
                "endpoint": self._endpoint(),
                "profile": self.profile,
                "credential_id": self.credential_id,
                "self_host_supervision": mode.value,
            }
        )
        if runtime_root is not None:
            section["runtime_root"] = str(runtime_root)
        try:
            write_toml_atomic_private(path, data)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HermesRefereeManagerError(f"could not write {path}: {exc}") from exc

    def _existing_key(self, *credential_ids: str | None) -> str | None:
        manager = SecretManager()
        # An explicitly selected credential is an exact binding.  Falling
        # through to another bearer key could authenticate the wrong Hermes
        # service and hide a broken operator configuration.
        candidates = tuple(candidate for candidate in credential_ids if candidate)
        if not candidates:
            candidates = (self.credential_id or DEFAULT_CREDENTIAL_ID,)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return manager.resolve(candidate, owner_task="system")
            except Exception:
                continue
        return None

    def _transaction_snapshot(self) -> _HermesTransactionSnapshot:
        """Capture the state needed to compensate a managed lifecycle edit."""
        config_path = self._config_file()
        if config_path.is_symlink():
            raise HermesRefereeManagerError(
                f"refusing to snapshot symlinked config path: {config_path}"
            )
        config_bytes = config_path.read_bytes() if config_path.exists() else None
        settings = self._settings().hermes_referee
        credential_existed = self._existing_key() is not None
        credential_value: str | None = None
        if FileSource._NAME.fullmatch(str(self.credential_id)) is not None:
            credential_path = user_secret_dir() / self.credential_id
            if credential_path.is_file() and not credential_path.is_symlink():
                credential_value = credential_path.read_text(encoding="utf-8")
        return _HermesTransactionSnapshot(
            config_bytes=config_bytes,
            credential_value=credential_value,
            credential_existed=credential_existed,
            previous_enabled=settings.enabled,
            previous_supervision=settings.supervision_mode.value,
        )

    async def _stop_managed_service(self) -> str:
        """Stop the managed service and classify the operator-visible result."""
        try:
            await self._run_hermes(["-p", self.profile, "gateway", "stop"])
        except HermesRefereeManagerError as exc:
            message = str(exc).lower()
            if any(
                marker in message
                for marker in ("already stopped", "not running", "no process", "no such process")
            ):
                return "already_stopped"
            return "stop_unconfirmed"
        return "stopped"

    def _restore_config(self, content: bytes | None) -> None:
        """Restore the exact pre-transaction config bytes atomically."""
        path = self._config_file()
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        if path.is_symlink():
            raise HermesRefereeManagerError(f"refusing to restore symlinked config path: {path}")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(
                str(path.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd != -1:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _restore_credential(self, snapshot: _HermesTransactionSnapshot) -> None:
        """Restore or remove the Athena-owned credential material."""
        if snapshot.credential_value is None:
            delete_user_secret(self.credential_id)
            return
        write_user_secret(self.credential_id, snapshot.credential_value)

    async def _recover_transaction(
        self,
        snapshot: _HermesTransactionSnapshot,
        *,
        phase: str,
        cause: Exception,
    ) -> Mapping[str, Any] | None:
        """Compensate external state and restore durable state after failure."""
        managed_service_state = await self._stop_managed_service()
        recovery_errors: list[str] = []
        try:
            self._restore_credential(snapshot)
        except Exception as exc:  # pragma: no cover - platform/filesystem specific
            recovery_errors.append(f"credential restore failed: {exc}")
        try:
            self._restore_config(snapshot.config_bytes)
        except Exception as exc:  # pragma: no cover - platform/filesystem specific
            recovery_errors.append(f"configuration restore failed: {exc}")

        # If the previous state was enabled, stopping the newly provisioned
        # service cannot prove that the old live state was restored. Report
        # that boundary explicitly even when the stop command succeeded.
        recovery_required = bool(
            managed_service_state == "stop_unconfirmed"
            or snapshot.previous_enabled
            or recovery_errors
        )
        if recovery_required:
            return {
                "configuration_commit": "failed" if phase == "configuration_commit" else phase,
                "managed_service_state": managed_service_state,
                "recovery_required": True,
                "recommended_action": "athena referee repair",
                "error": str(cause),
                "previous_enabled": snapshot.previous_enabled,
                "previous_supervision": snapshot.previous_supervision,
                "credential_existed": snapshot.credential_existed,
                "operation_started": True,
                "service_reconfigured": snapshot.previous_enabled,
                "recovery_errors": recovery_errors,
            }
        raise HermesRefereeManagerError(
            f"Hermes {phase} failed after provisioning ({cause}); "
            f"managed service compensation: {managed_service_state}"
        ) from cause

    async def _setup_transaction(self) -> Mapping[str, Any]:
        """Provision, prove, and commit managed state as one bounded saga."""
        root = self._root()
        snapshot = self._transaction_snapshot()
        key = self._existing_key() or secrets.token_urlsafe(48)
        provisioned = False
        phase = "provision"
        try:
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
            provisioned = True
            phase = "probe"
            report = await self._probe(key, e2e=True)
            phase = "credential_commit"
            write_user_secret(self.credential_id, key)
            phase = "configuration_commit"
            self._write_settings(
                enabled=True,
                runtime_root=root,
                self_host_supervision=(
                    self.self_host_supervision or HermesSupervisionMode.REQUIRED.value
                ),
            )
            return report
        except Exception as exc:
            if not provisioned:
                raise
            recovery = await self._recover_transaction(snapshot, phase=phase, cause=exc)
            if recovery is not None:
                return recovery
            raise AssertionError("transaction recovery did not return or raise")

    async def setup(self) -> Mapping[str, Any]:
        """Provision, prove, and then enable the managed referee."""
        return await self._setup_transaction()

    async def status(self) -> Mapping[str, Any]:
        settings = self._settings().hermes_referee
        result: dict[str, Any] = {
            "enabled": settings.enabled,
            "managed": settings.managed,
            "self_host_supervision": settings.supervision_mode.value,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "runtime_root": settings.runtime_root,
        }
        if not settings.transport_enabled:
            result["state"] = "disabled"
            return result
        credential_id = settings.credential_id or self.credential_id
        key = self._existing_key(credential_id)
        if not key:
            result.update(
                state="error",
                safety_verified=False,
                error=f"referee credential {credential_id} unavailable",
            )
            return result
        try:
            report = await HermesRefereeManager(
                config_path=self._config_file(),
                runtime_root=Path(settings.runtime_root)
                if settings.runtime_root
                else self.runtime_root,
                profile=settings.profile,
                endpoint=settings.endpoint,
                credential_id=credential_id,
                timeout_seconds=settings.timeout_seconds,
                self_host_supervision=(
                    self.self_host_supervision or settings.supervision_mode.value
                ),
            )._probe(key, e2e=False)
        except HermesRefereeDisconnectedError as exc:
            result.update(state="disconnected", safety_verified=False, error=str(exc))
            return result
        except HermesRefereeSafetyError as exc:
            result.update(state="unsafe", safety_verified=False, error=str(exc))
            return result
        except Exception as exc:
            result.update(state="error", safety_verified=False, error=str(exc))
            return result
        result.update(state="safety_verified", safety_verified=True, **report)
        return result

    async def repair(self) -> Mapping[str, Any]:
        settings = self._settings().hermes_referee
        configured_root = (
            Path(settings.runtime_root).expanduser() if settings.runtime_root else None
        )
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
            self_host_supervision=(self.self_host_supervision or settings.supervision_mode.value),
        )
        return await manager._setup_transaction()

    async def disable(self) -> Mapping[str, Any]:
        """Disable Athena locally, then report whether Hermes stopped."""
        settings = self._settings().hermes_referee
        root = (
            Path(settings.runtime_root).expanduser() if settings.runtime_root else self.runtime_root
        )
        manager = HermesRefereeManager(
            config_path=self._config_file(),
            runtime_root=root,
            profile=settings.profile or self.profile,
            endpoint=settings.endpoint or self.endpoint,
            credential_id=settings.credential_id or self.credential_id,
            timeout_seconds=settings.timeout_seconds,
            self_host_supervision=settings.supervision_mode.value,
        )
        manager._write_settings(
            enabled=False,
            runtime_root=root.resolve() if root else None,
            self_host_supervision=settings.supervision_mode.value,
        )
        if root is None or not root.exists():
            managed_service = "already_stopped"
        else:
            managed_service = await manager._stop_managed_service()
        result: dict[str, Any] = {
            "enabled": False,
            "integration_state": "disabled",
            "managed_service": managed_service,
        }
        if managed_service == "stop_unconfirmed":
            result["warning"] = (
                "Athena is disabled locally, but Hermes shutdown is unconfirmed; "
                "run `athena referee repair` or stop Hermes manually."
            )
        return result


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
    self_host_supervision: str | None = None,
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
        self_host_supervision=self_host_supervision,
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
    if result.get("recovery_required"):
        print(f"athena referee {action}: recovery required", file=sys.stderr)
        for key in (
            "configuration_commit",
            "managed_service_state",
            "recommended_action",
            "error",
        ):
            if result.get(key):
                print(f"  {key}: {result[key]}", file=sys.stderr)
        return 1
    if action == "disable":
        print(
            f"athena referee disable: disabled (Hermes {result.get('managed_service', 'unknown')})"
        )
        if result.get("warning"):
            print(f"  warning: {result['warning']}", file=sys.stderr)
        return 0
    state = result.get("state") if isinstance(result, Mapping) else None
    if action in {"setup", "repair"}:
        print(f"athena referee {action}: safety verified")
    else:
        print(f"athena referee status: {state or 'unknown'}")
    if isinstance(result, Mapping) and result.get("e2e_decision"):
        print(f"  e2e verdict: {result['e2e_decision']}")
    return 0 if state in {None, "safety_verified"} or action in {"setup", "repair"} else 1


__all__ = [
    "HermesRefereeManager",
    "HermesRefereeManagerError",
    "HermesRefereeDisconnectedError",
    "run_referee_action",
]
