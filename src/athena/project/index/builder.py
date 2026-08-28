"""Bounded lexical project-index builder with optional semantic upgrades."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from athena.project.index.models import ProjectIndex
from athena.project.index.lexical import extract_imports, extract_symbols
from athena.project.index.semantic import SemanticProjectAnalyzer
from athena.project.profile import (
    _IGNORED_DIRS,
    _PROFILE_FILES,
    _SOURCE_EXTENSIONS,
    ProjectInspector,
    _token_matches,
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
        profile = self._inspector.inspect(str(root_path))
        profile_record = profile.to_dict()
        environment = dict(profile_record.pop("environment", {}) or {})

        records: list[dict[str, Any]] = []
        contents: dict[str, str] = {}
        imports: dict[str, tuple[str, ...]] = {}
        symbols: dict[str, tuple[str, ...]] = {}
        references: dict[str, set[str]] = {}
        semantic_files: dict[str, dict[str, Any]] = {}
        analyzer = SemanticProjectAnalyzer()
        files, truncated = self._files(root_path)
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
            if record["language"] is None:
                continue
            imported = extract_imports(contents.get(relative, ""))
            imports[relative] = tuple(imported)
            names = extract_symbols(contents.get(relative, ""))
            symbols[relative] = tuple(names)
            semantic = analyzer.analyze(
                path=relative,
                content=contents.get(relative, ""),
                language=record["language"],
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
            for name in names:
                references.setdefault(name, set()).add(relative)
            for name in semantic.get("references") or ():
                references.setdefault(str(name), set()).add(relative)

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

        # Turn only unambiguous Python semantic references into impact edges.
        # A same-name collision is deliberately left unresolved so
        # verification widens instead of claiming false precision.
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
                if target == source:
                    continue
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
        for source, content in contents.items():
            if _is_test(source):
                continue
            stem = Path(source).stem
            for test, test_content in contents.items():
                if not _is_test(test):
                    continue
                if _mentions_identifier(test_content, stem):
                    test_associations.setdefault(source, set()).add(test)

        generated_dirs = set(profile.generated_dirs)
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
            (profile.fingerprint + "\x1f" + revision_payload).encode("utf-8")
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
                **analyzer.status(),
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
        count = 0
        output: list[Path] = []
        truncated = False
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_DIRS)
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
            for value in (relative, without_suffix, dotted, path.stem):
                variants.setdefault(value, relative)
        return variants

    @staticmethod
    def _resolve_import(imported: str, variants: dict[str, str]) -> str | None:
        candidate = imported.removesuffix(".py").removesuffix(".js").removesuffix(".ts")
        direct = variants.get(imported) or variants.get(candidate)
        if direct is not None:
            return direct
        for value, relative in variants.items():
            if _token_matches(imported, value) or _token_matches(candidate, value):
                return relative
        return None


def _is_test(path: str) -> bool:
    parts = set(path.split("/"))
    name = path.rsplit("/", 1)[-1].casefold()
    return bool(parts & {"test", "tests", "spec", "specs", "__tests__"}) or name.startswith(
        ("test_", "test.")
    )


def _mentions_identifier(content: str, value: str) -> bool:
    return bool(
        value and re.search(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])", content)
    )


def _source_revision(records: list[dict[str, Any]], truncated: bool) -> str:
    payload = "\n".join(
        f"{record['path']}\x1f{record.get('sha256')}\x1f{record.get('size')}"
        for record in sorted(records, key=lambda item: item["path"])
    )
    return hashlib.sha256(f"{int(truncated)}\x1f{payload}".encode("utf-8")).hexdigest()


__all__ = ["ProjectIndexBuilder"]
