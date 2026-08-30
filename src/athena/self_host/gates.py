"""Service-owned proof requirements for self-hosted changes.

The caller may add acceptance criteria, but it can never replace these
minimum gates.  Keeping the list in a host-owned module also gives the future
frozen-base verifier one canonical definition to capture.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from pathlib import Path
import subprocess
from dataclasses import dataclass

from athena.release.gates import candidate_commands


class SelfHostGatePolicy:
    """Build the mandatory candidate proof set for Athena source changes."""

    REQUIRED_COMMANDS = candidate_commands()
    DEPENDENCY_MANIFESTS = frozenset(
        {
            "pyproject.toml",
            "uv.lock",
            "requirements.txt",
            "requirements-dev.txt",
            "setup.py",
            "setup.cfg",
        }
    )

    DESIGN_CONTRACTS = (
        "SPEC.md",
        "BUILDSPEC.md",
        "BEHAVIORSPEC.md",
        "SECURITY.md",
        "docs/ARCHITECTURE.md",
        "SHELL_HARDENING.md",
        "SELF_HOSTING.md",
        "src/athena/release/gates.py",
        "src/athena/self_host/gates.py",
        "src/athena/self_host/controller.py",
        "src/athena/self_host/reviewer.py",
        "src/athena/self_host/risk.py",
        "src/athena/service/service.py",
        "src/athena/reality/coordinator.py",
        "src/athena/shadow/engine.py",
        "src/athena/kernel/verifiers.py",
        "src/athena/execution/environment.py",
        "scripts/architecture-lint",
    )

    @classmethod
    def frozen_safety_commands(cls, root: str) -> tuple[str, ...]:
        """Return base-owned safety checks pointed at the candidate source."""
        base = str(Path(root).resolve())
        return (
            "uv run --frozen --no-sync pytest -p no:cacheprovider -q "
            f"{base}/tests/contract {base}/tests/security {base}/tests/crash",
            f"uv run --frozen --no-sync python {base}/scripts/architecture-lint --root /workspace",
        )

    @classmethod
    def required_criteria(
        cls,
        additional: Iterable[str] = (),
        *,
        frozen_safety: Iterable[str] = (),
    ) -> tuple[str, ...]:
        """Return mandatory gates followed by unique caller-supplied gates."""
        commands = (*cls.REQUIRED_COMMANDS, *tuple(frozen_safety))
        criteria = [f"command:{command}" for command in commands]
        seen = set(criteria)
        for item in additional:
            value = str(item or "").strip()
            if value:
                normalized = value if value.lower().startswith("command:") else value
                if normalized not in seen:
                    criteria.append(normalized)
                    seen.add(normalized)
        return tuple(criteria)

    @classmethod
    def dependency_sync_command(cls, task_id: str) -> str:
        """Return an isolated, offline dependency proof for one candidate."""
        proof_id = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:16]
        return (
            f"UV_PROJECT_ENVIRONMENT=/tmp/athena-self-proof-{proof_id} "
            "uv sync --locked --offline --extra dev"
        )

    @classmethod
    def requires_dependency_proof(cls, changed_resources: Iterable[str]) -> bool:
        """Identify dependency material from candidate-relative paths."""
        for raw in changed_resources:
            path = str(raw).replace("\\", "/").lstrip("./")
            if path in cls.DEPENDENCY_MANIFESTS or path.rsplit("/", 1)[-1] in {
                "pyproject.toml",
                "uv.lock",
            }:
                return True
        return False


@dataclass(frozen=True)
class SelfHostGateBundle:
    """Immutable base-owned inputs used to certify one self-host candidate."""

    source_revision: str
    project_root: str
    design_files: tuple[dict[str, str], ...]
    safety_files: tuple[dict[str, str], ...]
    required_commands: tuple[str, ...]
    design_bundle_hash: str
    gate_bundle_hash: str

    @classmethod
    def capture(cls, root: str, *, allow_dirty: bool = False) -> "SelfHostGateBundle":
        project_root = Path(root).resolve()
        source_revision = _git_output(["git", "-C", str(project_root), "rev-parse", "HEAD"])
        status = _git_output(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        )
        if status and not allow_dirty:
            raise ValueError("athena self requires a clean source checkout")
        design_files = tuple(
            _file_digest(project_root, relative) for relative in SelfHostGatePolicy.DESIGN_CONTRACTS
        )
        safety_files = tuple(
            _file_digest(project_root, relative)
            for directory in ("tests/contract", "tests/security", "tests/crash")
            for relative in _python_files(project_root / directory)
        )
        required_commands = SelfHostGatePolicy.frozen_safety_commands(str(project_root))
        design_hash = _digest({"source_revision": source_revision, "files": design_files})
        gate_hash = _digest(
            {
                "source_revision": source_revision,
                "design_bundle_hash": design_hash,
                "safety_files": safety_files,
                "required_commands": required_commands,
            }
        )
        return cls(
            source_revision=source_revision,
            project_root=str(project_root),
            design_files=design_files,
            safety_files=safety_files,
            required_commands=required_commands,
            design_bundle_hash=design_hash,
            gate_bundle_hash=gate_hash,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "source_revision": self.source_revision,
            "project_root": self.project_root,
            "design_files": [dict(item) for item in self.design_files],
            "safety_files": [dict(item) for item in self.safety_files],
            "required_commands": list(self.required_commands),
            "design_bundle_hash": self.design_bundle_hash,
            "gate_bundle_hash": self.gate_bundle_hash,
        }

    def design_context(self, *, max_bytes: int = 24_000) -> str:
        """Return bounded contract text after rechecking captured hashes.

        This reads only the host-captured base checkout.  It never reads the
        candidate tree, and a changed authority file invalidates the context.
        """
        chunks: list[str] = []
        consumed = 0
        core = {
            "SECURITY.md",
            "SELF_HOSTING.md",
            "docs/ARCHITECTURE.md",
            "SHELL_HARDENING.md",
        }
        root = Path(self.project_root)
        # Put the compact, high-value contracts first.  With a bounded prompt
        # budget, iterating the manifest order could exhaust the budget on
        # SPEC/BUILD excerpts before SELF_HOSTING.md was ever presented.
        ordered_entries = sorted(
            self.design_files,
            key=lambda entry: (
                0 if str(entry.get("path") or "") in core else 1,
                self.design_files.index(entry),
            ),
        )
        for entry in ordered_entries:
            relative = str(entry.get("path") or "")
            path = root / relative
            if hashlib.sha256(path.read_bytes()).hexdigest() != str(entry.get("sha256") or ""):
                raise ValueError(f"frozen design authority changed: {relative}")
            text = path.read_text(encoding="utf-8", errors="replace")
            budget = 4_500 if relative in core else 1_200
            excerpt = text[:budget]
            block = f"\n--- {relative} ---\n{excerpt}\n"
            if consumed + len(block) > max_bytes:
                break
            chunks.append(block)
            consumed += len(block)
        return "".join(chunks)


def _git_output(command: list[str]) -> str:
    try:
        result = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=read-only self-host base identity
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("could not inspect the Athena source revision") from exc
    return result.stdout.strip()


def _python_files(root: Path) -> tuple[str, ...]:
    if not root.is_dir():
        raise ValueError(f"self-host safety directory is missing: {root}")
    return tuple(
        path.relative_to(root.parent.parent).as_posix()
        for path in sorted(root.rglob("*.py"))
        if path.is_file()
    )


def _file_digest(root: Path, relative: str) -> dict[str, str]:
    path = root / relative
    if not path.is_file():
        raise ValueError(f"self-host authority file is missing: {relative}")
    return {
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["SelfHostGateBundle", "SelfHostGatePolicy"]
