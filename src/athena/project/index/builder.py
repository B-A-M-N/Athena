"""Bounded lexical project-index builder with optional semantic upgrades."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from athena.project.index.models import ProjectIndex
from athena.project.index.lexical import extract_imports, extract_symbols
from athena.project.index.semantic import SemanticProjectAnalyzer
from athena.project.profile import (
    _PROFILE_FILES,
    _SOURCE_EXTENSIONS,
    _is_environment_directory,
    ProjectInspector,
)
from athena.protocol.messages import utcnow

_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
    re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)",
        re.MULTILINE,
    ),
    re.compile(r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE),
)
_IMPORT_PATTERNS = (
    re.compile(r"\bfrom\s+([A-Za-z0-9_./-]+)\s+import\b"),
    re.compile(r"\bimport\s+([A-Za-z0-9_./-]+)"),
    re.compile(r"(?:require|include)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
)


class ProjectIndexBuilder:
    """Build a bounded index without executing project code or tools."""

    def __init__(
        self,
        *,
        inspector: ProjectInspector | None = None,
        max_files: int = 10_000,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._inspector = inspector or ProjectInspector(
            max_files=max_files,
            max_file_bytes=max_file_bytes,
        )
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def build(self, root: str) -> ProjectIndex:
        root_path = Path(os.path.realpath(os.path.abspath(root)))
        files, truncated = self._files(root_path)
        profile = self._inspector.inspect(str(root_path), inventory=files)
        profile_record = profile.to_dict()
        environment = dict(profile_record.pop("environment", {}) or {})

        records: list[dict[str, Any]] = []
        contents: dict[str, str] = {}
        imports: dict[str, tuple[str, ...]] = {}
        symbols: dict[str, tuple[str, ...]] = {}
        semantic_files: dict[str, dict[str, Any]] = {}
        analyzer = SemanticProjectAnalyzer()
        for path in files:
            relative = path.relative_to(root_path).as_posix()
            record: dict[str, Any] = {
                "path": relative,
                "language": _SOURCE_EXTENSIONS.get(path.suffix.lower()),
                "size": 0,
                "sha256": None,
            }
            try:
                stat = path.stat()
                record["size"] = stat.st_size
                record["mtime_ns"] = stat.st_mtime_ns
                if stat.st_size <= self.max_file_bytes:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    contents[relative] = content
                    record["sha256"] = hashlib.sha256(
                        content.encode("utf-8", errors="replace")
                    ).hexdigest()
            except OSError:
                records.append(record)
                continue
            records.append(record)
            language = record.get("language")
            if not isinstance(language, str):
                continue
            imported = extract_imports(contents.get(relative, ""))
            imports[relative] = tuple(imported)
            names = extract_symbols(contents.get(relative, ""))
            symbols[relative] = tuple(names)
            semantic = analyzer.analyze(
                path=relative,
                content=contents.get(relative, ""),
                language=language,
            )
            semantic_files[relative] = semantic
            imports[relative] = tuple(
                sorted(
                    set(imports[relative]) | {str(value) for value in semantic.get("imports") or ()}
                )
            )
            semantic_names = {
                str(item.get("name"))
                for item in semantic.get("definitions") or ()
                if isinstance(item, dict) and item.get("name")
            }
            symbols[relative] = tuple(sorted(set(symbols[relative]) | semantic_names))

        return self._assemble_index(
            root_path=root_path,
            profile_record=profile_record,
            environment=environment,
            records=records,
            contents=contents,
            imports=imports,
            symbols=symbols,
            semantic_files=semantic_files,
            truncated=truncated,
        )

    def incremental(
        self,
        root: str,
        previous: ProjectIndex,
        changed_paths: list[str] | tuple[str, ...] = (),
    ) -> ProjectIndex:
        """Update one index while re-parsing only changed source files."""
        root_path = Path(os.path.realpath(os.path.abspath(root)))
        files, truncated = self._files(root_path)
        profile = self._inspector.inspect(str(root_path), inventory=files)
        profile_record = profile.to_dict()
        environment = dict(profile_record.pop("environment", {}) or {})
        requested = {
            _relative_change(value, root_path)
            for value in changed_paths
            if _relative_change(value, root_path)
        }
        previous_records = {
            str(item.get("path")): dict(item) for item in previous.files if item.get("path")
        }
        imports = {str(key): tuple(value) for key, value in previous.imports.items()}
        symbols = {str(key): tuple(value) for key, value in previous.symbols.items()}
        semantic_files = {
            str(key): dict(value)
            for key, value in (previous.semantic.get("files") or {}).items()
            if isinstance(value, Mapping)
        }
        records: list[dict[str, Any]] = []
        contents: dict[str, str] = {}
        analyzer = SemanticProjectAnalyzer()
        current_paths: set[str] = set()
        for path in files:
            relative = path.relative_to(root_path).as_posix()
            current_paths.add(relative)
            old = previous_records.get(relative)
            try:
                stat = path.stat()
            except OSError:
                stat = None
            changed = (
                relative in requested
                or old is None
                or stat is None
                or old.get("mtime_ns") != stat.st_mtime_ns
                or old.get("size") != stat.st_size
            )
            record = (
                dict(old)
                if old is not None and not changed
                else {
                    "path": relative,
                    "language": _SOURCE_EXTENSIONS.get(path.suffix.lower()),
                    "size": 0,
                    "sha256": None,
                }
            )
            record.update(
                {
                    "path": relative,
                    "language": _SOURCE_EXTENSIONS.get(path.suffix.lower()),
                    "size": int(stat.st_size) if stat is not None else 0,
                    "mtime_ns": int(stat.st_mtime_ns) if stat is not None else 0,
                }
            )
            records.append(record)
            if not changed:
                continue
            content = ""
            if stat is not None and stat.st_size <= self.max_file_bytes:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = ""
                contents[relative] = content
                record["sha256"] = hashlib.sha256(
                    content.encode("utf-8", errors="replace")
                ).hexdigest()
            imports.pop(relative, None)
            symbols.pop(relative, None)
            semantic_files.pop(relative, None)
            language = record.get("language")
            if not isinstance(language, str):
                continue
            imported = extract_imports(content)
            imports[relative] = tuple(imported)
            names = extract_symbols(content)
            symbols[relative] = tuple(names)
            semantic = analyzer.analyze(
                path=relative,
                content=content,
                language=language,
            )
            semantic_files[relative] = semantic
            imports[relative] = tuple(
                sorted(
                    set(imports[relative]) | {str(value) for value in semantic.get("imports") or ()}
                )
            )
            semantic_names = {
                str(item.get("name"))
                for item in semantic.get("definitions") or ()
                if isinstance(item, dict) and item.get("name")
            }
            symbols[relative] = tuple(sorted(set(symbols[relative]) | semantic_names))

        for relative in set(previous_records) - current_paths:
            imports.pop(relative, None)
            symbols.pop(relative, None)
            semantic_files.pop(relative, None)

        # Association fallback needs test text, but never performs a nested
        # source-by-test scan.  Unchanged source facts stay in the previous
        # snapshot; only the test corpus is read once to build the inverted map.
        for record in records:
            relative = str(record["path"])
            if _is_test(relative) and relative not in contents:
                path = root_path / relative
                try:
                    if path.stat().st_size <= self.max_file_bytes:
                        contents[relative] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass

        return self._assemble_index(
            root_path=root_path,
            profile_record=profile_record,
            environment=environment,
            records=records,
            contents=contents,
            imports=imports,
            symbols=symbols,
            semantic_files=semantic_files,
            truncated=truncated,
        )

    def _assemble_index(
        self,
        *,
        root_path: Path,
        profile_record: dict[str, Any],
        environment: dict[str, Any],
        records: list[dict[str, Any]],
        contents: dict[str, str],
        imports: dict[str, tuple[str, ...]],
        symbols: dict[str, tuple[str, ...]],
        semantic_files: dict[str, dict[str, Any]],
        truncated: bool,
    ) -> ProjectIndex:
        references: dict[str, set[str]] = {}
        for source, names in symbols.items():
            for name in names:
                references.setdefault(str(name), set()).add(source)
            semantic = semantic_files.get(source) or {}
            for name in semantic.get("references") or ():
                references.setdefault(str(name), set()).add(source)

        path_by_variant = self._path_variants(records)
        edges: list[dict[str, str]] = []
        for source, imported_values in imports.items():
            for imported_name in imported_values:
                target = self._resolve_import(imported_name, path_by_variant)
                if target is not None and target != source:
                    edges.append(
                        {
                            "source": source,
                            "target": target,
                            "kind": "import",
                            "name": imported_name,
                        }
                    )

        definitions: dict[str, set[str]] = {}
        for target, semantic in semantic_files.items():
            for definition in semantic.get("definitions") or ():
                if isinstance(definition, dict) and definition.get("name"):
                    definitions.setdefault(str(definition["name"]), set()).add(target)
        for source, semantic in semantic_files.items():
            referenced = {str(name) for name in semantic.get("references") or () if name}
            for call in semantic.get("calls") or ():
                if isinstance(call, dict) and call.get("target"):
                    referenced.add(str(call["target"]).rsplit(".", 1)[-1])
            for type_reference in semantic.get("types") or ():
                if isinstance(type_reference, dict) and type_reference.get("target"):
                    referenced.add(str(type_reference["target"]).rsplit(".", 1)[-1])
            for name in sorted(referenced):
                targets = definitions.get(name, set())
                if len(targets) != 1:
                    continue
                target = next(iter(targets))
                if target != source:
                    edges.append(
                        {
                            "source": source,
                            "target": target,
                            "kind": "semantic",
                            "name": name,
                            "confidence": "high",
                        }
                    )

        test_associations: dict[str, set[str]] = {}
        for edge in edges:
            if _is_test(edge["source"]):
                test_associations.setdefault(edge["target"], set()).add(edge["source"])
        test_mentions: dict[str, set[str]] = {}
        for test, test_content in contents.items():
            if _is_test(test):
                for token in _identifier_tokens(test_content):
                    test_mentions.setdefault(token, set()).add(test)
        for record in records:
            source = str(record["path"])
            if _is_test(source) or source in test_associations:
                continue
            source_tokens = {Path(source).stem.casefold(), Path(source).name.casefold()}
            source_tokens.update(part.casefold() for part in Path(source).parts)
            associated = set().union(*(test_mentions.get(token, set()) for token in source_tokens))
            if associated:
                test_associations[source] = associated

        generated_dirs = set(profile_record.get("generated_dirs") or ())
        generated_files = tuple(
            sorted(
                record["path"]
                for record in records
                if any(part in generated_dirs for part in Path(record["path"]).parts)
            )
        )
        configs = tuple(
            sorted(
                record["path"] for record in records if Path(record["path"]).name in _PROFILE_FILES
            )
        )
        revision_payload = "\n".join(
            f"{record['path']}\x1f{record.get('sha256')}\x1f{record.get('size')}"
            for record in sorted(records, key=lambda item: item["path"])
        )
        revision = hashlib.sha256(
            (str(profile_record.get("fingerprint") or "") + "\x1f" + revision_payload).encode(
                "utf-8"
            )
        ).hexdigest()
        source_revision = _source_revision(records, truncated)
        return ProjectIndex(
            root=str(root_path),
            profile=profile_record,
            environment=environment,
            files=tuple(records),
            imports={key: tuple(value) for key, value in sorted(imports.items())},
            symbols={key: tuple(value) for key, value in sorted(symbols.items())},
            references={key: tuple(sorted(value)) for key, value in sorted(references.items())},
            generated_files=generated_files,
            test_associations={
                key: tuple(sorted(value)) for key, value in sorted(test_associations.items())
            },
            configs=configs,
            dependency_edges=tuple(
                sorted(edges, key=lambda edge: (edge["source"], edge["target"], edge["name"]))
            ),
            semantic={
                **SemanticProjectAnalyzer().status(),
                "complete": not truncated,
                "files": semantic_files,
            },
            complete=not truncated,
            truncated=truncated,
            truncation_reason="max_files" if truncated else None,
            index_revision=revision,
            source_revision=source_revision,
            built_at=utcnow().isoformat(),
        )

    def source_revision(self, root: str) -> str:
        """Return the bounded source revision without building semantic facts."""
        root_path = Path(os.path.realpath(os.path.abspath(root)))
        files, truncated = self._files(root_path)
        records: list[dict[str, Any]] = []
        for path in files:
            relative = path.relative_to(root_path).as_posix()
            record: dict[str, Any] = {
                "path": relative,
                "size": 0,
                "sha256": None,
            }
            try:
                stat = path.stat()
                record["size"] = stat.st_size
                if stat.st_size <= self.max_file_bytes:
                    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
            records.append(record)
        return _source_revision(records, truncated)

    def _files(self, root: Path) -> tuple[list[Path], bool]:
        if (root / ".git").exists():
            inventory = _git_inventory(root)
            if inventory is not None:
                return inventory[: self.max_files], len(inventory) > self.max_files
        count = 0
        output: list[Path] = []
        truncated = False
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not _is_environment_directory(Path(directory) / name, name)
            )
            for name in sorted(filenames):
                if count >= self.max_files:
                    truncated = True
                    return output, truncated
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                count += 1
                output.append(path)
        return output, truncated

    @staticmethod
    def _imports(content: str) -> list[str]:
        values: set[str] = set()
        for pattern in _IMPORT_PATTERNS:
            values.update(match.group(1).replace("\\", "/") for match in pattern.finditer(content))
        return sorted(values)

    @staticmethod
    def _symbols(content: str) -> list[str]:
        values: set[str] = set()
        for pattern in _SYMBOL_PATTERNS:
            values.update(match.group(1) for match in pattern.finditer(content))
        return sorted(values)

    @staticmethod
    def _path_variants(records: list[dict[str, Any]]) -> dict[str, str]:
        variants: dict[str, str] = {}
        for record in records:
            relative = str(record["path"])
            path = Path(relative)
            if path.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            without_suffix = relative[: -len(path.suffix)]
            dotted = without_suffix.replace("/", ".")
            dotted_parts = dotted.split(".")
            aliases = [".".join(dotted_parts[start:]) for start in range(len(dotted_parts))]
            slash_aliases = [value.replace(".", "/") for value in aliases]
            for value in (relative, without_suffix, dotted, path.stem, *aliases, *slash_aliases):
                variants.setdefault(value, relative)
        return variants

    @staticmethod
    def _resolve_import(imported: str, variants: dict[str, str]) -> str | None:
        candidate = imported.removesuffix(".py").removesuffix(".js").removesuffix(".ts")
        direct = variants.get(imported) or variants.get(candidate)
        if direct is not None:
            return direct
        return None


def _is_test(path: str) -> bool:
    parts = set(path.split("/"))
    name = path.rsplit("/", 1)[-1].casefold()
    return bool(parts & {"test", "tests", "spec", "specs", "__tests__"}) or name.startswith(
        ("test_", "test.")
    )


def _identifier_tokens(content: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", content)}


def _relative_change(value: object, root: Path) -> str:
    raw = str(value or "").replace("\\", "/")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return ""
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _git_inventory(root: Path) -> list[Path] | None:
    """Use Git's own tracked/unignored inventory for a Git workspace."""
    try:
        result = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=read-only git project inventory
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        output.append(path)
    return sorted(output)


def _source_revision(records: list[dict[str, Any]], truncated: bool) -> str:
    payload = "\n".join(
        f"{record['path']}\x1f{record.get('sha256')}\x1f{record.get('size')}"
        for record in sorted(records, key=lambda item: item["path"])
    )
    return hashlib.sha256(f"{int(truncated)}\x1f{payload}".encode("utf-8")).hexdigest()


__all__ = ["ProjectIndexBuilder"]
