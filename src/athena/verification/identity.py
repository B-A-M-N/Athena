"""Stable identities and subsumption rules for verification probes."""

from __future__ import annotations

import re
import shlex

from athena.protocol.tasks import VerificationSpec, VerificationType

_SPACE_RE = re.compile(r"\s+")


def command_proof_id(command: str) -> str:
    """Map equivalent command implementations to one proof identity."""
    normalized = _SPACE_RE.sub(" ", str(command or "").strip().casefold())
    if (
        "tests/contract" in normalized
        and "tests/security" in normalized
        and "tests/crash" in normalized
    ):
        return "python.frozen_safety"
    if "uv sync --locked --offline" in normalized:
        return "python.dependency_environment"
    if "uv lock --check" in normalized:
        return "dependency.lock"
    if "architecture-lint" in normalized and "--root /workspace" in normalized:
        return "architecture.frozen"
    if "architecture-lint" in normalized:
        return "architecture"
    if "native-smoke" in normalized:
        return "native.smoke"
    if re.search(r"\bcargo\s+test\b", normalized):
        return "rust.tests"
    if re.search(r"\bcargo\s+check\b", normalized):
        return "rust.check"
    if re.search(r"\bruff\s+format\b", normalized):
        return "python.format"
    if re.search(r"\bruff\s+check\b", normalized):
        return "python.lint"
    if re.search(r"\bmypy\b", normalized):
        return "python.types"
    if re.search(r"\bpytest\b", normalized):
        if "tests/e2e" in normalized or "test_release_black_box" in normalized:
            if "--ignore=tests/e2e" not in normalized:
                return "python.e2e_tests"
        if (
            "tests/" in normalized or "::test_" in normalized
        ) and "--ignore=tests/e2e" not in normalized:
            try:
                tokens = shlex.split(normalized)
            except ValueError:
                tokens = normalized.split()
            paths = sorted(
                {
                    token
                    for token in tokens
                    if not token.startswith("-") and ("tests/" in token or "::test_" in token)
                }
            )
            return "python.affected_tests:" + ",".join(paths)
        return "python.full_tests"
    if re.search(r"\bpython(?:3)?\s+--version\b", normalized):
        return "python.runtime"
    return f"command:{normalized}"


def verification_proof_id(spec: VerificationSpec) -> str:
    """Return a semantic identity independent of criterion object identity."""
    if spec.type is VerificationType.COMMAND:
        return command_proof_id(spec.command or "")
    if spec.type is VerificationType.FILE:
        return f"file:{spec.path or ''}:{spec.predicate or ''}"
    if spec.type is VerificationType.ARTIFACT_PREDICATE:
        return f"artifact:{spec.path or ''}:{spec.predicate or ''}"
    if spec.type is VerificationType.CAPABILITY_CHECK:
        return f"capability:{spec.capability or ''}"
    if spec.type is VerificationType.MODEL_JUDGMENT:
        return f"model_judgment:{spec.predicate or spec.path or spec.command or ''}"
    if spec.type is VerificationType.MANUAL:
        return f"manual:{spec.predicate or spec.path or ''}"
    return f"verification:{spec.type.value}"


def proof_subsumes(left: str, right: str) -> bool:
    """Return whether one proof makes a second equivalent proof redundant."""
    if left == right:
        return True
    if left == "python.full_tests" and right.startswith("python.affected_tests:"):
        return True
    if left == "python.dependency_environment":
        return right in {
            "python.format",
            "python.lint",
            "python.types",
            "python.full_tests",
            "python.e2e_tests",
        } or right.startswith("python.affected_tests:")
    return False


__all__ = ["command_proof_id", "proof_subsumes", "verification_proof_id"]
