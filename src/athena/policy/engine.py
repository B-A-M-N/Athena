"""PolicyEngine - decides allow / ask / deny for concrete capability requests.

Every capability call passes through PolicyEngine.evaluate() after schema
validation and BEFORE execution (INV-004). Evaluation is concrete, not
name-only (BUILDSPEC 33; BHV-041): it considers resolved arguments, resolved
effect classes, workspace scopes, backend, and the active autonomy profile.

Rules
-----
The active profile (supervised / coding / autonomous / offline) supplies a
prioritized RuleSet (``profiles``). Path enforcement constrains WRITE_LOCAL,
DELETE and EXECUTE against the workspace writable set (BHV-053, INV-008): a
filesystem write or a shell/execute outside the workspace is a hard deny unless
the autonomy profile grants broader scope.

Observability (BHV-042): every decision carries decision, reason, matched rule,
and available approval scopes. Denial is a hard no (BHV-043) - the caller MUST
NOT execute.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Optional

from athena.policy.approvals import ApprovalManager
from athena.policy.rules import RuleSet
from athena.policy.snapshot import PolicySnapshot, get_snapshot
from athena.protocol.capabilities import EffectClass, ResourceClass
from athena.protocol.policy import ApprovalScope, PolicyDecision, PolicyRequest, PolicyVerdict
from athena.protocol.tasks import AutonomyLevel, NetworkPolicy, WorkspaceSpec

_WRITE_OPS = frozenset({"write", "patch", "mkdir", "copy", "move", "create", "update"})
_DELETE_OPS = frozenset({"delete", "remove", "rmtree", "unlink"})
_READ_OPS = frozenset({"read", "list", "stat", "read_text", "get", "exists", "open"})
_BUILD_CMDS = frozenset({"build", "test", "pytest", "make", "go", "cargo", "npm"})
_SEP = os.sep


class PolicyEngine:
    def __init__(
        self,
        profile: AutonomyLevel | str = AutonomyLevel.SUPERVISED,
        approvals: Optional[ApprovalManager] = None,
        policy_revision: str = "1",
    ) -> None:
        self.profile: AutonomyLevel = _to_level(profile)
        self.approvals: ApprovalManager = approvals or ApprovalManager()
        # Bumped by an authority holder when rule SOURCE changes out-of-band
        # (external rule sources); profile builders are static per process.
        self._policy_revision = str(policy_revision)

    # --------------------------------------------------------------- entry
    def evaluate(
        self,
        request: PolicyRequest,
        *,
        autonomy: AutonomyLevel | str | None = None,
    ) -> PolicyDecision:
        """Evaluate a fully resolved capability request.

        ``request.effects`` holds the resolved effect classes (computed after
        argument resolution; BHV-041). ``autonomy`` overrides the engine
        default profile for this call.

        Evaluation is COMPOSITIONAL and MONOTONIC over the resolved effect
        set. Every effect is evaluated independently against the profile
        rule set and the per-effect resource constraints; the verdicts are
        then combined with DENY > ASK > ALLOW. Adding an effect can never
        make a request easier to authorize.

        Ordering (P0 authority boundary):

        1. hard immutable constraints — workspace/path/backend/network
           containment, privilege floors. A structural DENY is final: an
           approval grant can never convert it into ALLOW.
        2. profile policy over each resolved effect.
        3. approval — an active grant converts ASK into ALLOW only.
        """
        level = _to_level(autonomy or self.profile)
        # Compiled accelerator (P1): the snapshot carries the ordered ruleset
        # and canonical workspace paths. It is validated against live
        # revision guards on every use and holds no verdicts of its own —
        # all authority stays in this method's composition.
        snapshot = get_snapshot(
            level=level,
            workspace=request.workspace,
            task_policy=None,
            policy_revision=self._policy_revision,
        )
        rules = snapshot.rules

        # ---- 1. hard containment (structural, approval-proof) ------------ #
        structural = self._eval_containment(request, level, snapshot)
        if structural is not None:
            return structural

        # ---- 2. per-effect profile verdicts, combined monotonically ------ #
        combined, reasons = self._eval_effects(request, rules)
        if combined is PolicyVerdict.DENY:
            return PolicyDecision(PolicyVerdict.DENY, "; ".join(reasons), None, ())

        # ---- 3. approval converts ASK -> ALLOW only ---------------------- #
        if combined is PolicyVerdict.ASK:
            hit = self.approvals.covers_request(request)
            if hit is not None:
                return _decision(
                    "allow",
                    f"approved grant {hit.id} covers request",
                    f"approval:{hit.id}",
                    request,
                )
        return _decision(combined.value, "; ".join(reasons), None, request)

    def _eval_containment(
        self, request: PolicyRequest, level: AutonomyLevel, snapshot: PolicySnapshot
    ) -> PolicyDecision | None:
        """Hard, approval-immutable constraints evaluated before policy.

        Returns a DENY decision when the request structurally escapes the
        workspace/authority envelope, or None when containment holds and
        evaluation may proceed. These checks are deliberately insensitive to
        the autonomy profile where the boundary is absolute (workspace
        containment for writes/deletes, network hard-deny), and profile-aware
        only where the profile itself defines the grant (out-of-workspace
        execute).
        """
        # Structural checks are profile-independent except the explicit
        # out-of-workspace execute grant the AUTONOMOUS profile carries.
        if request.workspace is None:
            return None
        effects = set(request.effects)

        # ---- universal network boundary (structural, approval-proof) ---- #
        # Workspace network_policy=DENY is a hard containment invariant: no
        # capability may read or write the network regardless of what its
        # descriptor declares. Individual capability backends enforce this as
        # defense in depth, but PolicyEngine is the authority — a third-party
        # native/generated/MCP capability must not need to rediscover the
        # workspace's hard network boundary on its own.
        if request.workspace.network_policy == NetworkPolicy.DENY and effects & {
            EffectClass.NETWORK_READ,
            EffectClass.NETWORK_WRITE,
        }:
            return _deny("network denied: workspace network_policy is DENY")

        execute_bearing = EffectClass.EXECUTE in effects or EffectClass.SPAWN_PROCESS in effects

        # WRITE_LOCAL / DELETE target concrete filesystem paths unless the
        # capability operates on Athena state rather than files (declared by
        # the pathless-write classification). EXECUTE-bearing calls resolve
        # their cwd/path through the execute containment check instead.
        if (
            EffectClass.WRITE_LOCAL in effects or EffectClass.DELETE in effects
        ) and not execute_bearing:
            if request.capability_id == "database":
                if not self._database_within(request, snapshot):
                    return _deny(
                        f"database outside writable scope: {request.arguments.get('path')}"
                    )
            elif request.arguments.get("path") or request.arguments.get("resource"):
                out = (
                    self._eval_write(request, snapshot)
                    if EffectClass.WRITE_LOCAL in effects
                    else self._eval_delete(request, snapshot)
                )
                if out.decision is PolicyVerdict.DENY:
                    return out
            elif request.resources and ResourceClass.FILESYSTEM in request.resources:
                # A filesystem write without a resolved path is structurally
                # uncontainable (P1-23: typed resources replace the
                # capability-name exception list).
                return _deny("write call missing resolved path", "files.path")
            elif not request.resources:
                # Unresolved resources (direct engine callers, legacy tests):
                # fall back to the descriptor inference default — a write
                # bearing capability is assumed FILESYSTEM until proven
                # otherwise, so the fail direction never loosens.
                return _deny("write call missing resolved path", "files.path")

        if execute_bearing:
            out = self._eval_execute_containment(request, level, snapshot)
            if out is not None:
                return out
        elif EffectClass.READ_LOCAL in effects and (
            request.arguments.get("path") or request.arguments.get("resource")
        ):
            out = self._eval_read(request, snapshot)
            if out.decision is PolicyVerdict.DENY:
                return out
        return None

    def _eval_effects(
        self, request: PolicyRequest, rules: RuleSet
    ) -> tuple[PolicyVerdict, list[str]]:
        """Evaluate every resolved effect independently and combine strictly.

        Each effect is evaluated against the rule set as a SINGLETON effect
        set — that is the compositional semantics
        ``verdict(E) = max(verdict({e}) for e in E)`` — which is what makes
        the result monotonic: adding an effect adds a term to the max and can
        never soften a stricter verdict. DENY > ASK > ALLOW. An effect with no
        matching rule falls to the profile default.
        """
        effects = tuple(request.effects)
        verdicts: list[tuple[PolicyVerdict, str]] = []
        for effect in effects:
            singleton = frozenset({effect})
            hit = rules.evaluate(request.capability_id, singleton, dict(request.arguments))
            if hit is None:
                raw_verdict, matched = (
                    rules.default,
                    f"{request.capability_id}.{effect.value}",
                )
                reason = f"no rule matched {effect.value}; profile default {rules.default}"
            else:
                raw_verdict, matched = hit
                reason = f"rule {matched}"
            verdicts.append((_verdict(raw_verdict), reason))
        if not verdicts:
            hit = rules.evaluate(request.capability_id, frozenset(), dict(request.arguments))
            if hit is None:
                verdicts = [(_verdict(rules.default), "no resolved effects; profile default")]
            else:
                verdicts = [(_verdict(hit[0]), f"rule {hit[1]}")]
        combined = max((v for v, _ in verdicts), key=_STRICTNESS_RANK.__getitem__)
        reasons = [reason for v, reason in verdicts if v is combined]
        return combined, reasons

    # ------------------------------------------------------------- workspace
    def _eval_write(self, req, snapshot: PolicySnapshot, rules=None):
        """Structural containment for a filesystem write target."""
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return _deny("write call missing resolved path", "files.path")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, snapshot=snapshot, writable_only=True):
            return _deny(f"write outside writable scope: {path}")
        return _allow("write within writable scope")

    def _database_within(self, req, snapshot: PolicySnapshot) -> bool:
        """Database write containment (BHV-041).

        A database file is a legitimate mutation target even outside the
        workspace when the caller was granted it; the policy question here
        is workspace containment. /tmp databases are scratch and stay
        allowed under profile rules.
        """
        path = str(req.arguments.get("path") or "")
        if self._out_of_workspace(req, snapshot) and os.path.realpath(
            os.path.abspath(path)
        ).startswith("/tmp/"):
            return True
        return self._within(
            self._abs(path, req.workspace), req.workspace, snapshot=snapshot, writable_only=True
        )

    def _eval_delete(self, req, snapshot: PolicySnapshot, rules=None):
        """Structural containment for a delete target."""
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return _deny("delete call missing resolved path")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, snapshot=snapshot, writable_only=True):
            return _deny(f"delete outside writable scope: {path}")
        return _allow("delete within writable scope")

    def _eval_read(self, req, snapshot: PolicySnapshot, rules=None):
        """Structural containment for a read target."""
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return _allow("read: no path argument")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, snapshot=snapshot, writable_only=False):
            return _deny(f"read outside readable scope: {path}")
        return _allow("read within readable scope")

    def _eval_execute_containment(
        self, req, level=None, snapshot: PolicySnapshot | None = None
    ) -> PolicyDecision | None:
        """Structural execute checks: a DENY decision, or None to continue.

        Out-of-workspace execute is granted only when the active profile
        explicitly carries that grant (AUTONOMOUS build/test commands).
        """
        # A normal local backend remains conservative: it cannot prove that
        # arbitrary code is network-confined.  The shadow backend is allowed
        # through only because its runtime contract invokes the fail-closed
        # namespace sandbox with a private network namespace.
        if req.workspace.network_policy == NetworkPolicy.DENY and req.execution_backend not in {
            "shadow",
            "sandbox",
            "sandboxed-local",
        }:
            return _deny("execute denied: workspace network_policy is DENY")
        if self._out_of_workspace(req, snapshot) and not (
            level is not None and _execute_granted(level, req)
        ):
            return _deny("execute outside workspace requires profile grant (INV-008)")
        return None

    def _eval_execute(self, req, rules, level):
        """Legacy isolated-entry execute evaluation: containment then rule.

        Kept for callers/tests that exercise the execute path directly.
        ``level`` gates the out-of-workspace grant the profile may carry.
        """
        if (
            req.workspace is not None
            and req.workspace.network_policy == NetworkPolicy.DENY
            and req.execution_backend not in {"shadow", "sandbox", "sandboxed-local"}
        ):
            return _deny("execute denied: workspace network_policy is DENY")
        if self._out_of_workspace(req, None) and not _execute_granted(level, req):
            return _deny(
                "execute outside workspace requires profile grant (INV-008)",
            )
        return self._eval_rule(req, rules, EffectClass.EXECUTE, "execute")

    # ------------------------------------------------------------- rule apply
    def _eval_rule(self, req, rules: RuleSet, effect, fallback) -> PolicyDecision:
        hit = rules.evaluate(req.capability_id, req.effects, dict(req.arguments))
        if hit is None:
            verdict = rules.default
            matched = fallback or f"{req.capability_id}.{effect.value if effect else 'any'}"
            reason = f"no rule matched; profile default {verdict}"
        else:
            verdict, matched = hit
            reason = f"rule {matched}"
        return _decision(verdict, reason, matched, req)

    # ------------------------------------------------------------- path scope
    def _abs(self, path, ws: WorkspaceSpec) -> str:
        if os.path.isabs(path):
            return os.path.realpath(os.path.abspath(path))
        return os.path.realpath(os.path.abspath(os.path.join(ws.root, path)))

    def _within(
        self,
        target,
        ws: WorkspaceSpec,
        *,
        snapshot: PolicySnapshot | None = None,
        writable_only: bool,
    ) -> bool:
        """Path-scope containment against precompiled workspace identity.

        The canonical root and canonical rule paths come from the snapshot
        the caller already resolved (P1-26: the containment path no longer
        re-calls get_snapshot); only the request's target is canonicalized
        per call. A None snapshot falls back to resolving one — for the
        legacy isolated-entry helpers only.
        """
        target = os.path.realpath(os.path.abspath(target))
        if snapshot is None:
            snapshot = get_snapshot(
                level=self.profile,
                workspace=ws,
                task_policy=None,
                policy_revision=self._policy_revision,
            )
        root = snapshot.workspace_root
        if target != root and not target.startswith(root + _SEP):
            return False
        rules = ws.writable if writable_only else (ws.readable if ws.readable else ws.writable)
        if not rules:
            return True
        canonical_rules = snapshot.writable_rules if writable_only else snapshot.readable_rules
        matched = False
        for rule, pattern_real in zip(rules, canonical_rules, strict=True):
            if _path_match_canonical(target, pattern_real):
                if not rule.allow:
                    return False
                matched = True
        return matched

    def _out_of_workspace(self, req, snapshot: PolicySnapshot | None = None) -> bool:
        cwd = req.arguments.get("cwd") or req.arguments.get("workdir") or req.arguments.get("path")
        if not cwd or not os.path.isabs(str(cwd)):
            return False
        target = self._abs(str(cwd), req.workspace)
        return not self._within(target, req.workspace, snapshot=snapshot, writable_only=True)

    def _is_files_op(self, req, ops) -> bool:
        if req.capability_id not in ("files", "fs"):
            return False
        return str(req.arguments.get("operation", "")).lower() in ops

    def _is_exec(self, req) -> bool:
        return req.capability_id in ("execute", "shell", "process", "bash")


def _to_level(value) -> AutonomyLevel:
    return value if isinstance(value, AutonomyLevel) else AutonomyLevel(value)


def _decision(
    verdict: str, reason: str, matched: Optional[str], req: PolicyRequest
) -> PolicyDecision:
    if verdict in (PolicyVerdict.DENY.value, "deny"):
        return PolicyDecision(PolicyVerdict.DENY, reason, matched, ())
    if verdict in (PolicyVerdict.ASK.value, "ask"):
        return PolicyDecision(
            PolicyVerdict.ASK,
            reason,
            matched,
            (ApprovalScope.CALL, ApprovalScope.TASK, ApprovalScope.SESSION),
        )
    return PolicyDecision(PolicyVerdict.ALLOW, reason, matched, ())


def _deny(reason: str, matched: Optional[str] = None) -> PolicyDecision:
    return PolicyDecision(PolicyVerdict.DENY, reason, matched, ())


def _allow(reason: str, matched: Optional[str] = None) -> PolicyDecision:
    return PolicyDecision(PolicyVerdict.ALLOW, reason, matched, ())


# STRICTNESS ranking for monotonic combination: ALLOW < ASK < DENY.
_STRICTNESS_RANK = {
    PolicyVerdict.ALLOW: 0,
    PolicyVerdict.ASK: 1,
    PolicyVerdict.DENY: 2,
}


def _verdict(value) -> PolicyVerdict:
    if isinstance(value, PolicyVerdict):
        return value
    v = str(value or "").lower()
    if v == "allow":
        return PolicyVerdict.ALLOW
    if v == "deny":
        return PolicyVerdict.DENY
    return PolicyVerdict.ASK


def _has(cls, effects) -> bool:
    return cls in effects


def _primary(effects) -> Optional[EffectClass]:
    for eff in (
        EffectClass.PRIVILEGED,
        EffectClass.EXECUTE,
        EffectClass.SPAWN_PROCESS,
        EffectClass.FINANCIAL,
        EffectClass.SECRET_READ,
        EffectClass.NETWORK_WRITE,
        EffectClass.NETWORK_READ,
        EffectClass.EXTERNAL_PUBLISH,
        EffectClass.EXTERNAL_MESSAGE,
        EffectClass.DELETE,
        EffectClass.WRITE_LOCAL,
        EffectClass.READ_LOCAL,
        EffectClass.COMPUTER_INPUT,
    ):
        if eff in effects:
            return eff
    return None


def _canonical_rule_path(pattern: str) -> str:
    """Canonicalize a rule path, keeping glob metacharacters intact.

    Globs are canonicalized on their literal prefix so ``/tmp/ws/*.log``
    resolves ``/tmp/ws`` but preserves the ``*`` for fnmatch at compare time.
    """
    text = os.path.expanduser(str(pattern))
    if "*" in text or "?" in text or "[" in text:
        # Canonicalize the longest glob-free prefix; fnmatch handles the rest.
        for i, ch in enumerate(text):
            if ch in "*?[":
                prefix = os.path.realpath(os.path.abspath(text[:i].rstrip("/\\") or "/"))
                return prefix + text[i:]
        return text
    return os.path.realpath(os.path.abspath(text))


def _path_match(target: str, pattern: str) -> bool:
    """Match a canonical target against a workspace path rule.

    A rule naming a directory grants that directory's descendants, just like
    the filesystem capability's writable/readable scope check.  Policy sees
    canonical targets, so canonicalize the rule before comparing it; this also
    makes relative and ``~``-prefixed rules behave consistently at both
    authority boundaries.
    """
    return _path_match_canonical(
        os.path.realpath(os.path.abspath(target)), _canonical_rule_path(pattern)
    )


def _path_match_canonical(target: str, pattern_real: str) -> bool:
    if "*" in pattern_real or "?" in pattern_real or "[" in pattern_real:
        return fnmatch.fnmatch(target, pattern_real)
    return target == pattern_real or target.startswith(pattern_real + _SEP)


def _execute_granted(level: AutonomyLevel, req) -> bool:
    """INV-008: whether the active profile permits out-of-workspace execute.

    Only the autonomous profile, and only for build/test-style commands, is
    permitted; anything else outside the workspace writable set is a hard deny.
    The shell ``code`` is parsed for a build/test first token; anything
    ambiguous or missing defaults to deny.
    """
    if level != AutonomyLevel.AUTONOMOUS:
        return False
    code = str(req.arguments.get("code") or "")
    if not code:
        return False
    tokens = code.split()
    if not tokens:
        return False
    base = os.path.basename(tokens[0])
    return base in _BUILD_CMDS


__all__ = ["PolicyEngine", "PolicyDecision", "PolicyVerdict", "AutonomyLevel"]
