"""Autonomy profiles.

Each baseline profile (BUILDSPEC 37, BEHAVIORSPEC 15) is a function returning a
RuleSet expressed in terms of resolved effect classes (BHV-041) plus
capability/resource matchers. Map ``AutonomyLevel`` to the builder; the
PolicyEngine resolves the concrete request and delegates verdicts to the
profile's RuleSet before applying approval override and workspace/path
constraints.
"""

from __future__ import annotations

from athena.protocol.capabilities import EffectClass
from athena.protocol.tasks import AutonomyLevel
from athena.policy.rules import Rule, RuleSet

ALLOW = "allow"
ASK = "ask"
DENY = "deny"


def supervised() -> RuleSet:
    return RuleSet(
        rules=(
            _privileged(DENY),
            _secret_read(ASK),
            _external_publish(ASK),
            _execute(ASK),
            _write(ASK),
            _delete(ASK),
            _computer(ASK),
            _financial(ASK),
            _read(ALLOW),
            _net_read(ALLOW),
            _net_write(ASK),
        ),
        default=ASK,
    )


def coding() -> RuleSet:
    return RuleSet(
        rules=(
            _privileged(DENY),
            _secret_read(ASK),
            _external_publish(ASK),
            _package(ASK),
            _delete(ASK),
            _computer(ASK),
            _financial(ASK),
            _execute(ALLOW, 80),
            _write(ALLOW, 60),
            _net_read(ALLOW, 55),
            _net_write(ASK),
        ),
        default=ASK,
    )


def autonomous() -> RuleSet:
    return RuleSet(
        rules=(
            _privileged(DENY),
            _secret_read(ASK),
            _external_publish(ASK),
            _computer(ASK),
            _financial(ASK),
            _write(ALLOW, 90),
            _execute(ALLOW, 85),
            _net_read(ALLOW, 70),
            _net_write(ALLOW, 60),
        ),
        default=ASK,
    )


def offline() -> RuleSet:
    return RuleSet(
        rules=(
            _remote_inference(DENY),
            _remote_mcp(DENY),
            _telemetry(DENY),
            _secret_read(DENY),
            _package(DENY),
            _privileged(DENY),
            _write(ASK),
            _delete(ASK),
            _local_inference(ALLOW, 80),
            _execute(ALLOW, 70),
            _read(ALLOW, 60),
        ),
        default=DENY,
    )


_BUILDERS = {
    AutonomyLevel.SUPERVISED: supervised,
    AutonomyLevel.CODING: coding,
    AutonomyLevel.AUTONOMOUS: autonomous,
    AutonomyLevel.OFFLINE: offline,
}


def profile_ruleset(level: AutonomyLevel | str) -> RuleSet:
    key = level if isinstance(level, AutonomyLevel) else AutonomyLevel(level)
    builder = _BUILDERS.get(key)
    if builder is None:
        raise KeyError(f"unknown autonomy profile: {key}")
    return builder()


def available_profiles() -> tuple[AutonomyLevel, ...]:
    return tuple(AutonomyLevel)


# --- shared rule constructors ---------------------------------------------------

def _read(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.READ_LOCAL, priority=priority, reason="local read")


def _write(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.WRITE_LOCAL, priority=priority, reason="local write")


def _net_read(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.NETWORK_READ, priority=priority, reason="network read")


def _net_write(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.NETWORK_WRITE, priority=priority, reason="network write")


def _delete(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.DELETE, priority=priority, reason="delete")


def _execute(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, effect=EffectClass.EXECUTE, priority=priority, reason="execute")


def _secret_read(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.SECRET_READ, priority=90, reason="secret read")


def _external_publish(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.EXTERNAL_PUBLISH, priority=90, reason="external publish")


def _privileged(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.PRIVILEGED, priority=110, reason="privileged")


def _package(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.PRIVILEGED, priority=85, reason="package install")


def _financial(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.FINANCIAL, priority=100, reason="financial")


def _computer(verdict: str) -> Rule:
    return Rule(verdict, effect=EffectClass.COMPUTER_INPUT, priority=95, reason="computer control")


def _remote_inference(verdict: str) -> Rule:
    return Rule(verdict, resource="remote", capability_id="model", priority=120, reason="remote inference")


def _local_inference(verdict: str, priority: int = 50) -> Rule:
    return Rule(verdict, resource="local", capability_id="model", priority=priority, reason="local inference")


def _remote_mcp(verdict: str) -> Rule:
    return Rule(verdict, resource="remote", capability_id="mcp", priority=120, reason="remote mcp")


def _telemetry(verdict: str) -> Rule:
    return Rule(verdict, capability_id="telemetry", priority=110, reason="telemetry")


__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "profile_ruleset",
    "available_profiles",
]