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
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


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
    status: str  # passed | failed | skipped
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
        return all(check.status != "failed" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "metadata": dict(self.metadata),
        }


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

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def validate(
        self,
        code: str,
        *,
        tier: ValidationTier | str = ValidationTier.TASK,
    ) -> SourceValidation:
        selected = tier if isinstance(tier, ValidationTier) else ValidationTier(tier)
        checks: list[ValidationCheck] = []
        source = str(code or "")

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
                        "failed" if "ruff" in self._REQUIRED_TOOLS[selected] else "skipped",
                        "ruff is not installed",
                        tool="ruff",
                    )
                )
                checks.append(
                    ValidationCheck(
                        "lint",
                        "failed" if "ruff" in self._REQUIRED_TOOLS[selected] else "skipped",
                        "ruff is not installed",
                        tool="ruff",
                    )
                )
            else:
                format_result = _run_tool(
                    [ruff, "format", path],
                    cwd=root,
                    timeout=self._timeout,
                )
                if format_result.returncode == 0:
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
                            "format", "failed", _tool_detail(format_result), tool="ruff"
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
                    timeout=self._timeout,
                )
                checks.append(
                    ValidationCheck(
                        "lint",
                        "passed" if lint_result.returncode == 0 else "failed",
                        _tool_detail(lint_result),
                        tool="ruff",
                    )
                )

            mypy = shutil.which("mypy")
            if mypy is None:
                checks.append(
                    ValidationCheck(
                        "typecheck",
                        "failed" if "mypy" in self._REQUIRED_TOOLS[selected] else "skipped",
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
                    timeout=self._timeout,
                )
                checks.append(
                    ValidationCheck(
                        "typecheck",
                        "passed" if type_result.returncode == 0 else "failed",
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

        return SourceValidation(
            selected,
            source,
            tuple(checks),
            metadata={
                "required_tools": list(self._REQUIRED_TOOLS[selected]),
                "available_tools": [tool for tool in ("ruff", "mypy") if shutil.which(tool)],
            },
        )


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


def _run_tool(command: list[str], *, cwd: str, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_tool_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, "", f"timed out after {exc.timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _tool_env() -> dict[str, str]:
    """Give static tools a deterministic, non-secret environment."""
    # Do not inherit PYTHONPATH: a generated file must not cause static tools
    # to import arbitrary host/project modules while being checked.
    allowed = ("PATH", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _tool_detail(result: subprocess.CompletedProcess[str]) -> str:
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
