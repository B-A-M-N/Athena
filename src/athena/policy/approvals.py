"""Approval lifecycle and grant management.

Implements BUILDSPEC 35-36 and BEHAVIORSPEC 14:

* request MUST resolve to APPROVED / DENIED / EXPIRED / CANCELLED (BHV-044);
* a call-scoped approval ID MUST NOT be replayable for a different call after
  use, i.e. single-use authorization (BHV-045);
* a broader scope MUST NOT be silently inferred from a narrower grant
  (BHV-046);
* every grant binds to enough context (principal, capability, effect, scope,
  resource) to prevent authorization confusion (BHV-047).

The manager is in-memory by default. It is deliberately independent of the
persistent SQLite ``ApprovalStore``; callers may wire the store in for durable
recording of requests/grants without coupling policy to I/O.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Mapping, Optional

from athena.protocol.policy import (
    ApprovalGrant,
    ApprovalScope,
    ApprovalState,
    PolicyRequest,
    Principal,
)


def args_digest(arguments: Mapping[str, Any]) -> str:
    """Canonical digest of a resolved argument map (TOCTOU guard, BHV-047).

    A generated grant binds to the digest of the exact arguments that were
    approved; a resumed call with substituted arguments fails to match and is
    re-prompted (or denied) instead of silently bypassing policy.

    ``call_id`` is EXCLUDED from the digest: it is transport bookkeeping, not
    semantics. The approval flow itself adds the original call's id when
    resuming a parked call; pinning the digest to it would make every exact
    CALL-scope resume fail its own grant.
    """
    cleaned = {k: v for k, v in dict(arguments or {}).items() if k != "call_id"}
    canonical = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalError(Exception):
    """Raised on invalid approval lifecycle transitions or misuse."""


class ApprovalManager:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def create_request(
        self,
        principal: Principal,
        scope: ApprovalScope | str,
        *,
        capability: Optional[str] = None,
        effect: Optional[str] = None,
        resource_pattern: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        approval_id: Optional[str] = None,
        args_digest: Optional[str] = None,
        call_id: Optional[str] = None,
    ) -> str:
        from athena.protocol.ids import new_id
        aid = approval_id or new_id("apr")
        sc = scope if isinstance(scope, ApprovalScope) else ApprovalScope(scope)
        self._records[aid] = {
            "_id": aid,
            "_principal": principal,
            "state": ApprovalState.REQUESTED,
            "scope": sc,
            "capability": capability,
            "effect": _effect_from(effect) if effect else None,
            "resource_pattern": resource_pattern,
            "task_id": task_id,
            "session_id": session_id,
            "expires_at": expires_at,
            "used": False,
            "resolver": None,
            "created_at": datetime.now(),
            "_args_digest": args_digest,
            "call_id": call_id,
        }
        return aid

    def grant(
        self,
        approval_id: str,
        *,
        resolver: Optional[str] = None,
    ) -> ApprovalGrant:
        rec = self._require(approval_id)
        if rec["state"] not in (ApprovalState.REQUESTED, ApprovalState.APPROVED):
            raise ApprovalError(
                f"approval {approval_id} cannot be granted from {rec['state'].value}"
            )
        rec["state"] = ApprovalState.APPROVED
        rec["resolver"] = resolver
        return self._to_grant(rec)

    def deny(
        self,
        approval_id: str,
        *,
        resolver: Optional[str] = None,
    ) -> None:
        rec = self._require(approval_id)
        if rec["state"] != ApprovalState.REQUESTED:
            raise ApprovalError(f"approval {approval_id} already {rec['state'].value}")
        rec["state"] = ApprovalState.DENIED
        rec["resolver"] = resolver

    def cancel(
        self,
        approval_id: str,
        *,
        resolver: Optional[str] = None,
    ) -> None:
        rec = self._require(approval_id)
        if rec["state"] != ApprovalState.REQUESTED:
            raise ApprovalError(f"approval {approval_id} already {rec['state'].value}")
        rec["state"] = ApprovalState.CANCELLED
        rec["resolver"] = resolver

    def state(self, approval_id: str) -> Optional[ApprovalState]:
        rec = self._records.get(approval_id)
        return rec["state"] if rec else None

    def get(self, approval_id: str) -> Optional[ApprovalGrant]:
        rec = self._records.get(approval_id)
        if rec is None or rec["state"] not in (
            ApprovalState.APPROVED, ApprovalState.REQUESTED,
        ):
            return None
        return self._to_grant(rec)

    # ------------------------------------------------------------------ matching

    def covers_request(self, request: PolicyRequest) -> Optional[ApprovalGrant]:
        """Return the single best active grant covering a policy request.

        BHV-047: a grant must match the caller's principal and applicable
        binding fields (capability / effect / resource / task).
        BHV-045: call-scoped grants are consumed on first use.
        BHV-046: a grant's scope is honoured exactly; no broader scope is
        inferred.
        """
        with self._lock:
            return self._covers_locked(request)

    def _covers_locked(self, request: PolicyRequest) -> Optional[ApprovalGrant]:
        now = datetime.now()
        best: Optional[dict[str, Any]] = None
        best_rank = -1
        best_id: Optional[str] = None
        for aid, rec in self._records.items():
            if rec["state"] != ApprovalState.APPROVED:
                continue
            if rec["expires_at"] is not None and rec["expires_at"] <= now:
                rec["state"] = ApprovalState.EXPIRED
                continue
            if rec["_principal"] != request.principal:
                continue
            if rec["scope"] == ApprovalScope.CALL and rec["used"]:
                continue
            rank = self._rank(rec, request)
            if rank < 0 or rank < best_rank:
                continue
            best_rank = rank
            best = rec
            best_id = aid
        if best is None:
            return None
        if best["scope"] == ApprovalScope.CALL:
            assert best_id is not None
            self._records[best_id]["used"] = True
        return self._to_grant(best)

    @staticmethod
    def _rank(rec: dict[str, Any], request: PolicyRequest) -> int:
        rank = 0
        digest = rec.get("_args_digest")
        if digest:
            if args_digest(request.arguments) != digest:
                return -1
            rank += 50
        cap = rec.get("capability")
        if cap is not None:
            if cap == "*" or _fnmatch(cap, request.capability_id):
                rank += 40
            else:
                return -1
        effect = rec.get("effect")
        if effect is not None:
            hit = effect in request.effects
            if not hit:
                return -1
            rank += 30
        resource = rec.get("resource_pattern")
        if resource is not None:
            arg = (
                request.arguments.get("path")
                or request.arguments.get("resource")
                or request.arguments.get("url")
                or request.arguments.get("uri")
                or ""
            )
            if arg and _fnmatch(resource, str(arg)):
                rank += 20
            else:
                return -1
        scope = rec.get("scope")
        if scope == ApprovalScope.CALL:
            grant_call_id = rec.get("call_id")
            grant_task_id = rec.get("task_id")
            if grant_call_id is not None and request.call_id is not None:
                if grant_call_id != request.call_id:
                    return -1
                rank += 15
            elif grant_call_id is not None or request.call_id is not None:
                return -1
            if grant_task_id is not None and request.task_id is not None:
                if grant_task_id != request.task_id:
                    return -1
                rank += 5
            elif grant_task_id is not None or request.task_id is not None:
                return -1
        elif scope in (ApprovalScope.TASK, ApprovalScope.SESSION):
            if scope == ApprovalScope.TASK and rec.get("task_id") != request.task_id:
                return -1
            if scope == ApprovalScope.SESSION and rec.get("session_id") != request.session_id:
                return -1
            rank += 10
        return rank

    def list_active(self) -> list[ApprovalGrant]:
        now = datetime.now()
        grants: list[ApprovalGrant] = []
        for rec in self._records.values():
            if rec["state"] != ApprovalState.APPROVED:
                continue
            if rec["expires_at"] is not None and rec["expires_at"] <= now:
                rec["state"] = ApprovalState.EXPIRED
                continue
            grants.append(self._to_grant(rec))
        return grants

    # ------------------------------------------------------------------ internal

    def _require(self, approval_id: str) -> dict[str, Any]:
        rec = self._records.get(approval_id)
        if rec is None:
            raise ApprovalError(f"unknown approval: {approval_id}")
        return rec

    @staticmethod
    def _to_grant(rec: dict[str, Any]) -> ApprovalGrant:
        return ApprovalGrant(
            id=rec["_id"],
            principal=rec["_principal"],
            scope=rec["scope"],
            capability=rec.get("capability"),
            resource_pattern=rec.get("resource_pattern"),
            effect=rec.get("effect"),
            task_id=rec.get("task_id"),
            session_id=rec.get("session_id"),
            expires_at=rec.get("expires_at"),
        )


def _effect_from(name: str):
    from athena.protocol.capabilities import EffectClass
    if name in EffectClass._value2member_map_:
        return EffectClass(name)
    try:
        return EffectClass[name]
    except (KeyError, ValueError, TypeError):
        return None


def _fnmatch(pattern: str, value: str) -> bool:
    import fnmatch
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return value == base or value.startswith(base + "/")
    return fnmatch.fnmatch(value, pattern)


__all__ = ["ApprovalManager", "ApprovalError", "ApprovalScope", "ApprovalGrant", "args_digest"]