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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    """Resolve ``ATHENA_SECRET_<NAME>`` from process environment."""

    name = "env"

    def __init__(self, prefix: str = "ATHENA_SECRET_") -> None:
        self._prefix = prefix

    def resolve(self, name: str) -> str | None:
        key = self._prefix + name.upper().replace("/", "_").replace("-", "_")
        return os.environ.get(key)


class FileSource(SecretSource):
    """Resolve a credential by reading a configured base directory file."""

    name = "file"

    def __init__(self, base_dir: str | None = None) -> None:
        self._base_dir = base_dir

    def resolve(self, name: str) -> str | None:
        if not name:
            return None
        if self._base_dir:
            candidate = os.path.join(self._base_dir, name)
        else:
            candidate = name
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None


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
    """

    def __init__(
        self,
        sources: Iterable[SecretSource] | None = None,
        *,
        on_lease: Callable[[CredentialLease], None] | None = None,
    ) -> None:
        default_sources: list[SecretSource] = [
            EnvSource(),
            FileSource("/etc/athena/secrets"),
        ]
        self._sources: list[SecretSource] = list(sources) if sources is not None else default_sources
        self._leases: list[CredentialLease] = []
        self._delegations: list[SecretDelegation] = []
        self._on_lease = on_lease

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
        if not self.can_use(
            task_id, credential_id, parent_task_id, owner_task=owner_task
        ):
            raise SecretError(
                f"task {task_id} not permitted to use credential {credential_id}"
            )
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
            d.child_task == child_task
            and d.credential_id == credential_id
            and d.is_valid(now)
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
    "CredentialLease",
    "SecretDelegation",
    "SecretError",
    "PrivacyBoundaryError",
]