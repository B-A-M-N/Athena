"""Bounded, read-only project profiling and impact hints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".venv312",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "target",
    "dist",
    "build",
    ".tox",
}
_SOURCE_EXTENSIONS = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".sh": "Shell",
    ".ps1": "PowerShell",
}
_PROFILE_FILES = {
    "pyproject.toml": ("Python", "pyproject"),
    "setup.py": ("Python", "setuptools"),
    "requirements.txt": ("Python", "pip"),
    "package.json": ("JavaScript", "npm"),
    "pnpm-lock.yaml": ("JavaScript", "pnpm"),
    "yarn.lock": ("JavaScript", "yarn"),
    "package-lock.json": ("JavaScript", "npm"),
    "Cargo.toml": ("Rust", "cargo"),
    "Cargo.lock": ("Rust", "cargo"),
    "go.mod": ("Go", "go"),
    "go.sum": ("Go", "go"),
    "pom.xml": ("Java", "maven"),
    "build.gradle": ("Java", "gradle"),
    "Gemfile": ("Ruby", "bundler"),
    "composer.json": ("PHP", "composer"),
}
_IMPORT_PATTERNS = (
    re.compile(r"\bfrom\s+([A-Za-z0-9_./-]+)\s+import\b"),
    re.compile(r"\bimport\s+([A-Za-z0-9_./-]+)"),
    re.compile(r"""(?:require|include)\s*\(\s*["']([^"']+)["']\s*\)"""),
)


@dataclass(frozen=True)
class ProjectEnvironment:
    """Machine-dependent facts kept separate from project identity."""

    root: str
    commands: dict[str, tuple[str, ...]] | None = None
    toolchain: dict[str, str] | None = None
    git: dict[str, str | None] | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            encoded = json.dumps(
                self.to_dict(include_fingerprint=False),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            object.__setattr__(self, "fingerprint", hashlib.sha256(encoded).hexdigest())

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "root": self.root,
            "commands": {key: list(value) for key, value in (self.commands or {}).items()},
            "toolchain": dict(self.toolchain or {}),
            "git": dict(self.git or {}),
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result


@dataclass(frozen=True)
class ProjectProfile:
    root: str
    languages: tuple[str, ...] = ()
    package_systems: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    generated_dirs: tuple[str, ...] = ()
    lockfiles: tuple[str, ...] = ()
    entrypoints: tuple[str, ...] = ()
    commands: dict[str, tuple[str, ...]] | None = None
    toolchain: dict[str, str] | None = None
    git: dict[str, str | None] | None = None
    environment: ProjectEnvironment | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # PATH/toolchain discovery and live git placement are environment
            # facts. They must not alter the intrinsic project identity.
            intrinsic = self.to_dict(include_fingerprint=False)
            intrinsic.pop("commands", None)
            intrinsic.pop("toolchain", None)
            intrinsic.pop("git", None)
            intrinsic.pop("environment", None)
            encoded = json.dumps(intrinsic, sort_keys=True, separators=(",", ":")).encode()
            object.__setattr__(self, "fingerprint", hashlib.sha256(encoded).hexdigest())

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "root": self.root,
            "languages": list(self.languages),
            "package_systems": list(self.package_systems),
            "source_roots": list(self.source_roots),
            "test_roots": list(self.test_roots),
            "generated_dirs": list(self.generated_dirs),
            "lockfiles": list(self.lockfiles),
            "entrypoints": list(self.entrypoints),
            "commands": {key: list(value) for key, value in (self.commands or {}).items()},
            "toolchain": dict(self.toolchain or {}),
            "git": dict(self.git or {}),
        }
        if self.environment is not None:
            result["environment"] = self.environment.to_dict()
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result


class ProjectInspector:
    """Perform bounded project scans without executing project code.

    Pure filesystem/config parsing only — no subprocess.  Git and enriched
    toolchain facts are layered on by a coordinator that routes through
    Athena's existing governed surfaces.
    """

    def __init__(self, *, max_files: int = 10_000, max_file_bytes: int = 1_000_000) -> None:
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def inspect(
        self,
        root: str,
        *,
        inventory: Iterable[str | Path] | None = None,
    ) -> ProjectProfile:
        root_path = Path(os.path.realpath(os.path.abspath(root)))
        files = (
            [Path(path) for path in inventory]
            if inventory is not None
            else list(self._files(root_path))
        )
        languages = {
            _SOURCE_EXTENSIONS[path.suffix.lower()]
            for path in files
            if path.suffix.lower() in _SOURCE_EXTENSIONS
        }
        systems: set[str] = set()
        lockfiles: list[str] = []
        for path in files:
            marker = _PROFILE_FILES.get(path.name)
            if marker:
                languages.add(marker[0])
                systems.add(marker[1])
                if "lock" in path.name.casefold() or path.name in {
                    "requirements.txt",
                    "poetry.lock",
                    "Pipfile.lock",
                }:
                    lockfiles.append(self._relative(path, root_path))

        relative_files = [self._relative(path, root_path) for path in files]
        source_roots = self._roots(relative_files, {"src", "lib", "app", "source"})
        test_roots = self._roots(relative_files, {"test", "tests", "__tests__", "spec", "specs"})
        generated_dirs = self._roots(
            relative_files, {"generated", "gen", "build", "dist", "target"}
        )
        entrypoints = [
            relative
            for relative in relative_files
            if Path(relative).name
            in {
                "main.py",
                "__main__.py",
                "main.go",
                "main.rs",
                "index.js",
                "index.ts",
                "app.py",
                "manage.py",
            }
        ][:100]
        commands, toolchain = self._commands(languages, systems)
        git = self._git(root_path)
        environment = ProjectEnvironment(
            root=str(root_path),
            commands=commands,
            toolchain=toolchain,
            git=git,
        )
        return ProjectProfile(
            root=str(root_path),
            languages=tuple(sorted(languages)),
            package_systems=tuple(sorted(systems)),
            source_roots=tuple(source_roots),
            test_roots=tuple(test_roots),
            generated_dirs=tuple(generated_dirs),
            lockfiles=tuple(sorted(lockfiles)),
            entrypoints=tuple(sorted(entrypoints)),
            commands=commands,
            toolchain=toolchain,
            git=git,
            environment=environment,
        )

    def impact(self, root: str, paths: list[str]) -> dict[str, Any]:
        root_path = Path(os.path.realpath(os.path.abspath(root)))
        changed: list[str] = []
        targets: set[str] = set()
        for value in paths:
            path = self._resolve(Path(str(value)), root_path)
            if path is None:
                raise ValueError(f"impact path outside workspace: {value}")
            if not path.is_file():
                continue
            relative = self._relative(path, root_path)
            changed.append(relative)
            targets.update(
                {
                    path.name,
                    path.stem,
                    relative,
                    relative.rsplit(".", 1)[0],
                    relative.replace(os.sep, ".").rsplit(".", 1)[0],
                }
            )

        impacted: dict[str, dict[str, Any]] = {}
        for candidate in self._files(root_path):
            relative = self._relative(candidate, root_path)
            if relative in changed or candidate.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            try:
                if candidate.stat().st_size > self.max_file_bytes:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            reasons: set[str] = set()
            confidence = "low"
            for pattern in _IMPORT_PATTERNS:
                for match in pattern.finditer(text):
                    imported = match.group(1).replace("\\", "/")
                    if any(_token_matches(imported, target) for target in targets):
                        reasons.add(f"imports {imported}")
                        confidence = "high"
            if not reasons:
                for target in targets:
                    if len(target) >= 3 and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(Path(target).stem)}"
                        r"(?![A-Za-z0-9_])",
                        text,
                    ):
                        reasons.add(f"mentions {Path(target).stem}")
            if reasons:
                impacted[relative] = {
                    "path": relative,
                    "reasons": sorted(reasons),
                    "confidence": confidence,
                    "test": any(
                        part in {"test", "tests", "__tests__", "spec", "specs"}
                        for part in Path(relative).parts
                    ),
                }
        return {
            "changed": sorted(set(changed)),
            "impacted": [impacted[key] for key in sorted(impacted)],
            "method": "bounded lexical import/reference scan",
        }

    def _files(self, root: Path):
        count = 0
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not _is_environment_directory(Path(directory) / name, name)
            )
            for name in sorted(filenames):
                if count >= self.max_files:
                    return
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                count += 1
                yield path

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        return path.relative_to(root).as_posix()

    @staticmethod
    def _resolve(path: Path, root: Path) -> Path | None:
        candidate = Path(
            os.path.realpath(os.path.abspath(str(path) if path.is_absolute() else str(root / path)))
        )
        if candidate != root and not str(candidate).startswith(str(root) + os.sep):
            return None
        return candidate

    @staticmethod
    def _roots(files: list[str], names: set[str]) -> list[str]:
        found = {
            Path(relative).parts[0]
            for relative in files
            if Path(relative).parts and Path(relative).parts[0] in names
        }
        return sorted(found)

    @staticmethod
    def _commands(
        languages: set[str], systems: set[str]
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
        commands: dict[str, tuple[str, ...]] = {}
        toolchain: dict[str, str] = {}
        candidates = {
            "Python": ("python", "python -m pytest", "ruff check", "mypy"),
            "JavaScript": ("node", "npm test", "npm run build"),
            "TypeScript": ("node", "npm test", "npm run build", "tsc --noEmit"),
            "Go": ("go", "go test ./...", "go vet ./..."),
            "Rust": ("cargo", "cargo test", "cargo check"),
        }
        for language in sorted(languages):
            available = tuple(
                command
                for command in candidates.get(language, ())
                if shutil.which(command.split()[0])
            )
            if available:
                commands[language.casefold()] = available
            binary = candidates.get(language, ())[0] if candidates.get(language) else None
            if binary and shutil.which(binary):
                toolchain[language.casefold()] = str(shutil.which(binary))
        if "npm" in systems and shutil.which("npm"):
            toolchain["npm"] = str(shutil.which("npm"))
        return commands, toolchain

    @staticmethod
    def _git(root: Path) -> dict[str, str | None]:
        """Read git facts from the filesystem directly — no subprocess."""
        result: dict[str, str | None] = {
            "repository": "yes" if (root / ".git").exists() else "no",
            "baseline": None,
            "branch": None,
        }
        if result["repository"] != "yes":
            return result
        git_head = root / ".git" / "HEAD"
        try:
            head_text = git_head.read_text(encoding="utf-8").strip()
        except OSError:
            return result
        if head_text.startswith("ref: refs/heads/"):
            result["branch"] = head_text[len("ref: refs/heads/") :]
            ref_path = root / ".git" / head_text[5:]
        else:
            # Detached HEAD: the file content is the commit sha.
            result["baseline"] = head_text
            return result
        try:
            result["baseline"] = ref_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            # Ref may be packed; the branch name is still valid.
            pass
        return result


def _is_environment_directory(path: Path, name: str) -> bool:
    """Skip generated environments without enumerating project packages."""
    if name in _IGNORED_DIRS or name == "site-packages":
        return True
    try:
        return (path / "pyvenv.cfg").is_file()
    except OSError:
        return False


def _token_matches(imported: str, target: str) -> bool:
    imported = imported.removesuffix(".py").removesuffix(".js").removesuffix(".ts")
    target = target.removesuffix(".py").removesuffix(".js").removesuffix(".ts")
    return imported == target or imported.endswith("." + target) or imported.endswith("/" + target)


__all__ = ["ProjectEnvironment", "ProjectInspector", "ProjectProfile"]
