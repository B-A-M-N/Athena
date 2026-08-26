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
from athena.policy.profiles import profile_ruleset
from athena.policy.rules import RuleSet
from athena.protocol.capabilities import EffectClass
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
    ) -> None:
        self.profile: AutonomyLevel = _to_level(profile)
        self.approvals: ApprovalManager = approvals or ApprovalManager()

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
        """
        level = _to_level(autonomy or self.profile)
        rules = profile_ruleset(level)

        hit = self.approvals.covers_request(request)
        if hit is not None:
            return _decision(
                "allow", f"approved grant {hit.id} covers request",
                f"approval:{hit.id}", request,
            )

        if _has(EffectClass.WRITE_LOCAL, request.effects) or self._is_files_op(request, _WRITE_OPS):
            # Process-spawning capabilities carry WRITE_LOCAL as a secondary
            # effect but must be evaluated as EXECUTE (they run code, not
            # write files). Only route to _eval_write when there is no
            # execute/spawn effect present.
            if not (_has(EffectClass.EXECUTE, request.effects)
                    or _has(EffectClass.SPAWN_PROCESS, request.effects)):
                out = self._eval_write(request, rules)
            else:
                out = self._eval_execute(request, rules, level)
        elif _has(EffectClass.DELETE, request.effects) or self._is_files_op(request, _DELETE_OPS):
            out = self._eval_delete(request, rules)
        elif (_has(EffectClass.EXECUTE, request.effects)
              or _has(EffectClass.SPAWN_PROCESS, request.effects)
              or self._is_exec(request)):
            out = self._eval_execute(request, rules, level)
        elif _has(EffectClass.READ_LOCAL, request.effects) or self._is_files_op(request, _READ_OPS):
            out = self._eval_read(request, rules)
        else:
            out = self._eval_rule(request, rules, _primary(request.effects), request.capability_id)
        return out

    # ------------------------------------------------------------- workspace
    def _eval_write(self, req, rules):
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return _deny("write call missing resolved path", "files.path")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, writable_only=True):
            return _deny(f"write outside writable scope: {path}")
        return self._eval_rule(req, rules, EffectClass.WRITE_LOCAL, "files.write")

    def _eval_delete(self, req, rules):
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return _deny("delete call missing resolved path")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, writable_only=True):
            return _deny(f"delete outside writable scope: {path}")
        return self._eval_rule(req, rules, EffectClass.DELETE, "files.delete")

    def _eval_read(self, req, rules):
        path = req.arguments.get("path") or req.arguments.get("resource")
        if not path:
            return self._eval_rule(req, rules, EffectClass.READ_LOCAL, "files.read")
        target = self._abs(path, req.workspace)
        if not self._within(target, req.workspace, writable_only=False):
            return _deny(f"read outside readable scope: {path}")
        return self._eval_rule(req, rules, EffectClass.READ_LOCAL, "files.read")

    def _eval_execute(self, req, rules, level):
        # HIGH-6: a network-policy DENY workspace must not reach the network.
        # Denying unrestricted execute is the conservative v1 guard: shell/python
        # execution can trivially reach the network, so treat DENY as denying
        # execute entirely. Documented limitation: this is a coarse guard; a
        # true network sandbox (no_new_privileges / seccomp / egress rules) is
        # deferred to the execution runtimes.
        if req.workspace is not None and req.workspace.network_policy == NetworkPolicy.DENY:
            return _deny("execute denied: workspace network_policy is DENY")
        if self._out_of_workspace(req) and not _execute_granted(level, req):
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

    def _within(self, target, ws: WorkspaceSpec, *, writable_only: bool) -> bool:
        root = os.path.realpath(os.path.abspath(ws.root))
        if target != root and not target.startswith(root + _SEP):
            return False
        rules = ws.writable if writable_only else (ws.readable if ws.readable else ws.writable)
        if not rules:
            return True
        matched = False
        for rule in rules:
            if _path_match(target, rule.path):
                if not rule.allow:
                    return False
                matched = True
        return matched

    def _out_of_workspace(self, req) -> bool:
        cwd = req.arguments.get("cwd") or req.arguments.get("workdir") or req.arguments.get("path")
        if not cwd or not os.path.isabs(str(cwd)):
            return False
        target = self._abs(str(cwd), req.workspace)
        return not self._within(target, req.workspace, writable_only=True)

    def _is_files_op(self, req, ops) -> bool:
        if req.capability_id not in ("files", "fs"):
            return False
        return str(req.arguments.get("operation", "")).lower() in ops

    def _is_exec(self, req) -> bool:
        return req.capability_id in ("execute", "shell", "process", "bash")


def _to_level(value) -> AutonomyLevel:
    return value if isinstance(value, AutonomyLevel) else AutonomyLevel(value)


def _decision(verdict: str, reason: str, matched: Optional[str], req: PolicyRequest) -> PolicyDecision:
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


def _has(cls, effects) -> bool:
    return cls in effects


def _primary(effects) -> Optional[EffectClass]:
    for eff in (
        EffectClass.PRIVILEGED, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
        EffectClass.FINANCIAL, EffectClass.SECRET_READ, EffectClass.NETWORK_WRITE,
        EffectClass.NETWORK_READ, EffectClass.EXTERNAL_PUBLISH,
        EffectClass.EXTERNAL_MESSAGE, EffectClass.DELETE, EffectClass.WRITE_LOCAL,
        EffectClass.READ_LOCAL, EffectClass.COMPUTER_INPUT,
    ):
        if eff in effects:
            return eff
    return None


def _path_match(target: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return target == base or target.startswith(base + "/")
    return fnmatch.fnmatch(target, pattern)


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