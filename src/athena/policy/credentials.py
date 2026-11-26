"""Secret resolution and scoped credential leases.

Implements BUILDSPEC 100-103 and BEHAVIORSPEC 20:

* credential resolution happens only after policy checks (BHV-073); this module
  resolves a value only when an authorized, scoped lease is requested;
* materialization is scoped to a concrete task/backend with an expiry (BHV-074,
  101);
* children do NOT inherit parent credentials without an explicit delegation
  grant (BHV-007, 102);
* local -> remote privacy-boundary transitions require explicit policy
  authorization (BHV-008, 103).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable


class SecretError(KeyError):
    """Raised on a resolution or lease failure."""


class PrivacyBoundaryError(PermissionError):
    """Raised when a local -> remote transition lacks explicit authorization."""


class SecretSource:
    """A pluggable source that resolves a credential name to a value."""

    name: str = "base"

    def resolve(self, name: str) -> str | None:
        raise NotImplementedError


class EnvSource(SecretSource):
    """Resolve managed and explicitly named environment credentials.

    The managed ``ATHENA_SECRET_<NAME>`` form remains the namespaced default.
    The direct ``<NAME>`` fallback supports provider-standard names such as
    ``OPENROUTER_API_KEY`` without copying the secret into application config.
    """

    name = "env"

    def __init__(self, prefix: str = "ATHENA_SECRET_") -> None:
        self._prefix = prefix

    def resolve(self, name: str) -> str | None:
        key = self._prefix + name.upper().replace("/", "_").replace("-", "_")
        value = os.environ.get(key)
        if value is None and self._prefix == "ATHENA_SECRET_":
            value = os.environ.get(name)
        return value


class FileSource(SecretSource):
    """Resolve a credential by reading a configured base directory file."""

    name = "file"
    _NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    def __init__(self, base_dir: str | None = None, *, require_private: bool = False) -> None:
        self._base_dir = base_dir
        self._require_private = require_private

    def resolve(self, name: str) -> str | None:
        if not name or self._NAME.fullmatch(str(name)) is None:
            return None
        if self._base_dir:
            root = Path(self._base_dir).resolve(strict=False)
            candidate = (root / str(name)).resolve(strict=False)
            try:
                candidate.relative_to(root)
            except ValueError:
                return None
        else:
            candidate = Path(str(name))
            if candidate.is_absolute():
                return None
        try:
            if self._require_private and candidate.stat().st_mode & 0o077:
                return None
            with open(candidate, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None


class KeyringSource(SecretSource):
    """Optional OS keyring backend; unavailable keyring fails closed."""

    name = "keyring"

    def __init__(self, service: str = "athena") -> None:
        self._service = str(service)

    def resolve(self, name: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(self._service, str(name))
        except Exception:
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None


class _CommandSecretSource(SecretSource):
    """Base for optional owner-authenticated CLI secret stores.

    Commands receive a fixed argument vector and never a shell string. Their
    output is consumed in memory only; stderr is discarded so a credential
    value or CLI diagnostic cannot leak into Athena logs.
    """

    timeout_seconds = 5.0

    def _run(self, argv: list[str]) -> str | None:
        if shutil.which(argv[0]) is None:
            return None
        try:
            completed = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=owner-authenticated secret-store CLI; architecture-exception: secret-store-cli
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return value or None


class OnePasswordSource(_CommandSecretSource):
    """Resolve an item field through the operator's ``op`` CLI session."""

    name = "1password"

    def __init__(self, vault: str | None = None, field: str = "credential") -> None:
        self.vault = str(vault).strip() if vault else None
        self.field = str(field).strip() or "credential"

    def resolve(self, name: str) -> str | None:
        reference = str(name).strip()
        if not reference.startswith("op://"):
            if not self.vault:
                return None
            reference = f"op://{self.vault}/{reference}/{self.field}"
        return self._run(["op", "read", reference])


class BitwardenSource(_CommandSecretSource):
    """Resolve a login password through the operator's ``bw`` CLI session."""

    name = "bitwarden"

    def resolve(self, name: str) -> str | None:
        return self._run(["bw", "get", "password", str(name)])


class ResolverSecretSource(SecretSource):
    """Adapter for operator-installed vault/1Password/Bitwarden resolvers."""

    def __init__(self, name: str, resolver: Callable[[str], str | None]) -> None:
        self.name = str(name)
        self._resolver = resolver

    def resolve(self, name: str) -> str | None:
        try:
            value = self._resolver(str(name))
        except Exception:
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None


def user_secret_dir() -> Path:
    """Return the per-user XDG secret directory used by Athena."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "athena" / "secrets"


def write_user_secret(name: str, value: str) -> Path:
    """Atomically write one owner-only user secret and return its path."""
    if FileSource._NAME.fullmatch(str(name)) is None:
        raise ValueError("invalid secret name")
    if not value or "\n" in value or "\r" in value:
        raise ValueError("secret value must be non-empty and single-line")
    root = user_secret_dir()
    _reject_symlinked_path(root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    # ``mkdir`` follows a symlink if a path is swapped between the check and
    # creation. Re-check before opening the directory used for the commit.
    _reject_symlinked_path(root)
    root.chmod(0o700)
    directory_fd = os.open(
        str(root),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    fd = -1
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.",
            suffix=".tmp",
            dir=str(root),
        )
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        destination = root / str(name)
        os.replace(temporary, destination)
        os.fsync(directory_fd)
        return destination
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _reject_symlinked_path(path: Path) -> None:
    """Reject a secret-store path that redirects through a symlink."""
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"refusing to use symlinked secret directory: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def delete_user_secret(name: str) -> None:
    """Remove one user secret if present."""
    if FileSource._NAME.fullmatch(str(name)) is None:
        return
    try:
        (user_secret_dir() / str(name)).unlink()
    except FileNotFoundError:
        pass


@dataclass(frozen=True)
class CredentialLease:
    """A scoped, expiring authorization to use an actual secret value (101)."""

    credential_id: str
    task_id: str
    backend: str
    value: str
    granted_at: datetime
    expires_at: datetime
    lease_id: str = ""

    @property
    def remaining(self) -> timedelta:
        return self.expires_at - datetime.now()

    def is_valid(self, at: datetime | None = None) -> bool:
        return (at or datetime.now()) < self.expires_at


@dataclass(frozen=True)
class SecretDelegation:
    """Explicit child inheritance grant (B-007 / 102)."""

    parent_task: str
    child_task: str
    credential_id: str
    granted_by: str
    granted_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime | None = None

    def is_valid(self, at: datetime | None = None) -> bool:
        now = at or datetime.now()
        return self.expires_at is None or self.expires_at > now


class SecretManager:
    """Resolves named secrets and issues scoped, expiring leases.

    The manager does not decide authorization; callers must ensure policy
    allowed SECRET_READ before requesting a lease. This keeps raw values out of
    model context by default (B-072) and resolves strictly after policy
    checks (B-073).

    Runtime-supplied secrets (operator-answered ``request_input`` with
    ``expected="secret"``) are stored task-scoped and in-memory only: they
    never touch the durable input-request ``answer`` column or the model
    transcript, preserving Athena's secret-opacity contract.
    """

    def __init__(
        self,
        sources: Iterable[SecretSource] | None = None,
        *,
        on_lease: Callable[[CredentialLease], None] | None = None,
    ) -> None:
        default_sources: list[SecretSource] = [
            EnvSource(),
            FileSource(str(user_secret_dir()), require_private=True),
            FileSource("/etc/athena/secrets", require_private=True),
            KeyringSource(),
        ]
        self._sources: list[SecretSource] = (
            list(sources) if sources is not None else default_sources
        )
        self._leases: list[CredentialLease] = []
        self._delegations: list[SecretDelegation] = []
        self._on_lease = on_lease
        # Task-scoped runtime secrets (operator-supplied via request_input).
        # In-memory only; never persisted to the input_request answer column
        # or the model transcript.
        self._task_secrets: dict[str, dict[str, str]] = {}

    def register_source(self, source: SecretSource) -> None:
        self._sources.append(source)

    def available(self, name: str) -> bool:
        """Report availability without exposing the value (B-071, opacity)."""
        return any(source.resolve(name) is not None for source in self._sources)

    def describe(self, name: str) -> str:
        return (
            f"credential available: {name}"
            if self.available(name)
            else f"credential unknown: {name}"
        )

    def issue_lease(
        self,
        credential_id: str,
        *,
        task_id: str,
        backend: str = "local",
        ttl: timedelta = timedelta(minutes=15),
        owner_task: str | None = None,
        parent_task_id: str | None = None,
    ) -> CredentialLease:
        """Resolve and lease a credential to a task/backend.

        Authorization (B-007 / 101-102) is enforced BEFORE materializing the
        value: the task must own the credential or hold an explicit
        delegation, otherwise the lease is denied.
        """
        if not self.can_use(task_id, credential_id, parent_task_id, owner_task=owner_task):
            raise SecretError(f"task {task_id} not permitted to use credential {credential_id}")
        value = self._resolve(credential_id)
        if value is None:
            raise SecretError(f"cannot resolve credential: {credential_id}")
        now = datetime.now()
        from athena.protocol.ids import new_id

        lease = CredentialLease(
            credential_id=credential_id,
            task_id=task_id,
            backend=backend,
            value=value,
            granted_at=now,
            expires_at=now + ttl,
            lease_id=new_id("cred"),
        )
        self._leases.append(lease)
        if self._on_lease is not None:
            self._on_lease(lease)
        return lease

    def resolve(
        self,
        credential_id: str,
        *,
        owner_task: str = "system",
        backend: str = "local",
        ttl: timedelta = timedelta(minutes=15),
    ) -> str:
        """Resolve a secret value to its owner at the composition boundary.

        The service owns the secrets it bootstraps providers/MCP with; the
        caller is both the requesting task and the owner, satisfying policy
        (BHV-073) without leaking the value into any model-facing surface.
        Raises :class:`SecretError` when unresolvable or not permitted.
        """
        lease = self.issue_lease(
            credential_id,
            task_id=owner_task,
            owner_task=owner_task,
            backend=backend,
            ttl=ttl,
        )
        return lease.value

    def delegate(
        self,
        parent_task: str,
        child_task: str,
        credential_id: str,
        *,
        granted_by: str = "policy",
        expires_at: datetime | None = None,
    ) -> SecretDelegation:
        """Explicitly grant a child access to a parent's credential.

        Inheritance is never silent (B-007 / 102): a child must receive an
        explicit delegation before it may use a parent's secret.
        """
        grant = SecretDelegation(
            parent_task=parent_task,
            child_task=child_task,
            credential_id=credential_id,
            granted_by=granted_by,
            expires_at=expires_at,
        )
        self._delegations.append(grant)
        return grant

    def is_delegated(self, child_task: str, credential_id: str) -> bool:
        now = datetime.now()
        return any(
            d.child_task == child_task and d.credential_id == credential_id and d.is_valid(now)
            for d in self._delegations
        )

    def can_use(
        self,
        child_task: str,
        credential_id: str,
        parent_task_id: str | None = None,
        *,
        owner_task: str | None = None,
    ) -> bool:
        """A task may use a credential if it owns it or holds an explicit
        delegation. Children never inherit the parent's set implicitly, and
        omitting ownership/delegation context denies rather than permits."""
        if owner_task is not None and child_task == owner_task:
            return True
        if self.is_delegated(child_task, credential_id):
            return True
        return False

    def check_privacy_transition(
        self,
        *,
        current_backend: str,
        requested_backend: str,
        permitted: bool = False,
    ) -> None:
        """Gate local -> remote transitions (B-008 / 103).

        Remote providers only receive authorized, compiled context; switching an
        object's runtime to a remote backend without express permission raises a
        PrivacyBoundaryError.
        """
        _local = {"local", "native", "container"}
        local_before = current_backend in _local
        remote_after = requested_backend not in _local
        if local_before and remote_after and not permitted:
            raise PrivacyBoundaryError(
                f"privacy boundary: {current_backend} -> {requested_backend} "
                "requires explicit policy authorization"
            )

    def leases_for(self, task_id: str) -> list[CredentialLease]:
        now = datetime.now()
        return [lease for lease in self._leases if lease.task_id == task_id and lease.is_valid(now)]

    def prune_expired(self) -> None:
        now = datetime.now()
        self._leases = [lease for lease in self._leases if lease.is_valid(now)]

    # -- runtime-secret handling (operator-answered request_input) ---------- #
    def store_task_secret(
        self,
        task_id: str,
        *,
        name: str,
        value: str,
        context: str | None = None,
    ) -> str:
        """Store an operator-supplied secret for a task in memory only.

        Returns a stable ref (``runtime:<task>:<name>``) that can be used in
        input_requests.answer_ref so the durable column never holds the raw
        value.  The model sees only that a credential is available, never the
        value itself.
        """
        task_entry = self._task_secrets.setdefault(task_id, {})
        task_entry[name] = value
        ref = f"runtime:{task_id}:{name}"
        return ref

    def resolve_task_secret(self, task_id: str, name: str) -> str | None:
        """Resolve a runtime secret previously stored for a task."""
        return self._task_secrets.get(task_id, {}).get(name)

    def clear_task_secrets(self, task_id: str) -> None:
        """Discard all runtime secrets for a task (e.g. on completion/cancel)."""
        self._task_secrets.pop(task_id, None)

    def _resolve(self, name: str) -> str | None:
        for source in self._sources:
            value = source.resolve(name)
            if value is not None:
                return value
        return None


__all__ = [
    "SecretManager",
    "SecretSource",
    "EnvSource",
    "FileSource",
    "KeyringSource",
    "OnePasswordSource",
    "BitwardenSource",
    "ResolverSecretSource",
    "CredentialLease",
    "SecretDelegation",
    "SecretError",
    "PrivacyBoundaryError",
]
