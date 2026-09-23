"""Deterministic validation for generated executable affordances.

Tool-input repair and generated-code validation meet at the contract
boundary, but they are deliberately different operations:

* repair normalizes one model-produced argument candidate;
* this module decides whether generated source is admissible machinery.

The validator is intentionally independent of the model and of the generated
runtime.  Static tools inspect a temporary source file only; execution of the
result still belongs to ``SynthesisEngine`` and its restricted backend.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping


class ValidationTier(str, Enum):
    """Required rigor for the intended lifetime of generated machinery."""

    SCRATCH = "scratch"
    TASK = "task"
    CANDIDATE = "candidate"
    PROJECT = "project"
    USER = "user"


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: str  # passed | failed | skipped | timed_out | unavailable | tool_error
    detail: str = ""
    tool: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.tool:
            value["tool"] = self.tool
        return value


@dataclass(frozen=True)
class SourceValidation:
    """Static source result, including any formatter-normalized source."""

    tier: ValidationTier
    code: str
    checks: tuple[ValidationCheck, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # A timeout or infrastructure failure is not evidence that source is
        # invalid.  It is still a non-passing validation result, so callers
        # cannot accidentally promote unvalidated code.
        required = {"parse", "interface", "security"}
        names = {check.name for check in self.checks}
        return bool(self.checks) and required.issubset(names) and all(
            check.status in {"passed", "skipped"} for check in self.checks
        )

    @property
    def outcome(self) -> str:
        """Classify why validation did or did not produce admissible proof."""
        statuses = {check.status for check in self.checks}
        if "timed_out" in statuses:
            return "timed_out"
        if "unavailable" in statuses:
            return "environment_unavailable"
        if "tool_error" in statuses:
            return "tool_failed"
        if "failed" in statuses:
            return "invalid_source"
        return "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "metadata": {"outcome": self.outcome, **dict(self.metadata)},
        }


@dataclass(frozen=True)
class _ToolResult:
    """Small subprocess result with an explicit infrastructure outcome."""

    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    status: str = "failed"


class GeneratedSourceValidator:
    """Run source checks without executing generated source in the host.

    Ruff is used for formatting and linting when installed.  Candidate and
    project-level artifacts require Ruff; project/user artifacts also require
    Mypy.  Task-local artifacts remain usable in minimal installations but
    retain explicit ``skipped`` records when optional tools are absent.
    """

    _REQUIRED_TOOLS: ClassVar[dict[ValidationTier, tuple[str, ...]]] = {
        ValidationTier.SCRATCH: (),
        ValidationTier.TASK: (),
        ValidationTier.CANDIDATE: ("ruff",),
        ValidationTier.PROJECT: ("ruff", "mypy"),
        ValidationTier.USER: ("ruff", "mypy"),
    }

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        tool_timeouts: Mapping[str, float] | None = None,
        cache_size: int = 128,
    ) -> None:
        if timeout <= 0:
            raise ValueError("validation timeout must be positive")
        self._timeout = float(timeout)
        self._tool_timeouts = {
            str(tool): float(value) for tool, value in (tool_timeouts or {}).items()
        }
        if any(value <= 0 for value in self._tool_timeouts.values()):
            raise ValueError("validation tool timeouts must be positive")
        self._cache: dict[str, SourceValidation] = {}
        self._cache_size = max(0, int(cache_size))

    def validate(
        self,
        code: str,
        *,
        tier: ValidationTier | str = ValidationTier.TASK,
    ) -> SourceValidation:
        selected = tier if isinstance(tier, ValidationTier) else ValidationTier(tier)
        checks: list[ValidationCheck] = []
        source = str(code or "")
        cache_key = self._cache_key(source, selected)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            tree = ast.parse(source, filename="generated_capability.py")
            checks.append(ValidationCheck("parse", "passed"))
        except SyntaxError as exc:
            checks.append(ValidationCheck("parse", "failed", str(exc)))
            return SourceValidation(selected, source, tuple(checks))

        checks.extend(_contract_checks(tree))
        checks.extend(_security_checks(tree))
        if any(check.status == "failed" for check in checks):
            return SourceValidation(selected, source, tuple(checks))

        with tempfile.TemporaryDirectory(prefix="athena-generated-validate-") as root:
            path = os.path.join(root, "generated_capability.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            ruff = shutil.which("ruff")
            if ruff is None:
                checks.append(
                    ValidationCheck(
                        "format",
                        "unavailable" if "ruff" in self._REQUIRED_TOOLS[selected] else "skipped",
                        "ruff is not installed",
                        tool="ruff",
                    )
                )
                checks.append(
                    ValidationCheck(
                        "lint",
                        "unavailable" if "ruff" in self._REQUIRED_TOOLS[selected] else "skipped",
                        "ruff is not installed",
                        tool="ruff",
                    )
                )
            else:
                format_result = _run_tool(
                    [ruff, "format", path],
                    cwd=root,
                    timeout=self._timeout_for("ruff"),
                )
                if format_result.status == "passed":
                    # Formatter output is canonical input to subsequent checks
                    # and to the eventual code hash.
                    with open(path, encoding="utf-8") as handle:
                        source = handle.read()
                    checks.append(
                        ValidationCheck(
                            "format", "passed", format_result.stdout.strip(), tool="ruff"
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            "format",
                            format_result.status,
                            _tool_detail(format_result),
                            tool="ruff",
                        )
                    )

                # ``athena`` is injected by the generated runtime, so it is
                # intentionally absent from the submitted source. Give Ruff
                # and Mypy a local type/name stub without changing the
                # canonical source that will be executed or hashed.
                if _uses_generated_host(source):
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(_GENERATED_HOST_STUB + source)

                lint_result = _run_tool(
                    [ruff, "check", "--select", "E4,E7,E9,F,B,I,UP", path],
                    cwd=root,
                    timeout=self._timeout_for("ruff"),
                )
                checks.append(
                    ValidationCheck(
                        "lint",
                        lint_result.status,
                        _tool_detail(lint_result),
                        tool="ruff",
                    )
                )

            mypy = shutil.which("mypy")
            if mypy is None:
                checks.append(
                    ValidationCheck(
                        "typecheck",
                        "unavailable" if "mypy" in self._REQUIRED_TOOLS[selected] else "skipped",
                        "mypy is not installed",
                        tool="mypy",
                    )
                )
            elif selected in {
                ValidationTier.CANDIDATE,
                ValidationTier.PROJECT,
                ValidationTier.USER,
            }:
                type_result = _run_tool(
                    [mypy, "--ignore-missing-imports", "--follow-imports=skip", path],
                    cwd=root,
                    timeout=self._timeout_for("mypy"),
                )
                checks.append(
                    ValidationCheck(
                        "typecheck",
                        type_result.status,
                        _tool_detail(type_result),
                        tool="mypy",
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        "typecheck", "skipped", "not required for task-local machinery", tool="mypy"
                    )
                )

        result = SourceValidation(
            selected,
            source,
            tuple(checks),
            metadata={
                "required_tools": list(self._REQUIRED_TOOLS[selected]),
                "available_tools": [tool for tool in ("ruff", "mypy") if shutil.which(tool)],
                "timeouts": {tool: self._timeout_for(tool) for tool in ("ruff", "mypy")},
                "cache_size": self._cache_size,
            },
        )
        # Tool failures and timeouts are intentionally not cached: a transient
        # CI/process failure must be retryable and must not become durable
        # negative proof. Valid and source-invalid outcomes are deterministic
        # for the keyed source/tool environment and safely avoid repeated
        # expensive Mypy runs during promotion/revalidation.
        if self._cache_size and result.outcome in {"valid", "invalid_source"}:
            if len(self._cache) >= self._cache_size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = result
        return result

    def _timeout_for(self, tool: str) -> float:
        return self._tool_timeouts.get(tool, self._timeout)

    def _cache_key(self, source: str, tier: ValidationTier) -> str:
        tools = tuple((tool, shutil.which(tool) or "") for tool in ("ruff", "mypy"))
        material = repr(
            (
                source,
                tier.value,
                tools,
                self._timeout_for("ruff"),
                self._timeout_for("mypy"),
            )
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


def _contract_checks(tree: ast.AST) -> list[ValidationCheck]:
    if not isinstance(tree, ast.Module):
        return [ValidationCheck("interface", "failed", "source must be a module")]
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
    ]
    if len(definitions) != 1:
        return [
            ValidationCheck(
                "interface", "failed", "source must define exactly one run(args) function"
            )
        ]
    function = definitions[0]
    if isinstance(function, ast.AsyncFunctionDef):
        return [ValidationCheck("interface", "failed", "run must be a synchronous function")]
    if len(function.args.posonlyargs) + len(function.args.args) != 1:
        return [
            ValidationCheck("interface", "failed", "run must accept exactly one args parameter")
        ]
    return [ValidationCheck("interface", "passed")]


def _security_checks(tree: ast.AST) -> list[ValidationCheck]:
    """Reject unambiguous host-escape primitives before sandbox execution."""
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "CDLL",
    }
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden_calls:
                findings.append(f"{node.func.id}()")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            is_process_run = (
                node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"os", "subprocess"}
            )
            is_generated_host_call = (
                node.func.attr == "call"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "athena"
            )
            if (node.func.attr in forbidden_calls and not is_generated_host_call) or is_process_run:
                findings.append(f".{node.func.attr}()")
    if findings:
        return [
            ValidationCheck(
                "security",
                "failed",
                "host/process escape primitive is not allowed: " + ", ".join(sorted(set(findings))),
            )
        ]
    return [ValidationCheck("security", "passed")]


def _run_tool(command: list[str], *, cwd: str, timeout: float) -> _ToolResult:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_tool_env(),
            check=False,
        )
        return _ToolResult(
            command,
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            (
                "passed"
                if result.returncode == 0
                # Ruff and Mypy use exit 1 for ordinary source diagnostics.
                # Other non-zero codes are tool/ invocation failures and must
                # not be misreported as evidence that the source is invalid.
                else "failed"
                if result.returncode == 1
                else "tool_error"
            ),
        )
    except subprocess.TimeoutExpired as exc:
        return _ToolResult(
            command,
            124,
            "",
            f"timed out after {exc.timeout}s",
            "timed_out",
        )
    except OSError as exc:
        return _ToolResult(command, 127, "", str(exc), "unavailable")


def _tool_env() -> dict[str, str]:
    """Give static tools a deterministic, non-secret environment."""
    # Do not inherit PYTHONPATH: a generated file must not cause static tools
    # to import arbitrary host/project modules while being checked.
    allowed = ("PATH", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _tool_detail(result: _ToolResult | subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout or "") + (result.stderr or "")
    return output.strip()[-2000:]


_GENERATED_HOST_STUB = (
    "class _GeneratedHost:\n"
    "    def call(self, capability_id, arguments): ...\n"
    "\n"
    "athena = _GeneratedHost()\n\n"
)


def _uses_generated_host(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == "athena" and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


__all__ = [
    "GeneratedSourceValidator",
    "SourceValidation",
    "ValidationCheck",
    "ValidationTier",
]
