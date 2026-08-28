"""Optional semantic facts for the bounded project index.

The index must remain useful when no third-party parser is installed.  The
stdlib Python AST therefore provides the first real semantic backend, while
other languages continue to use the lexical facts collected by the builder.
The analyzer never imports or executes project code.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from importlib.util import find_spec
from typing import Any


class SemanticProjectAnalyzer:
    """Extract bounded semantic facts without making indexing fail closed."""

    def __init__(self) -> None:
        self._tree_sitter = _tree_sitter_available()

    @property
    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {
            "backend": "python_ast",
            "available": True,
            "optional": True,
            "tree_sitter_available": self._tree_sitter,
            "supported_languages": ["Python"],
        }

    def analyze(self, *, path: str, content: str, language: str | None) -> dict[str, Any]:
        """Return semantic facts for one file, or a bounded fallback record."""
        if language != "Python":
            return {
                "backend": "lexical",
                "language": language,
                "complete": False,
                "confidence": "lexical",
            }
        try:
            tree = ast.parse(content, filename=path)
        except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
            return {
                "backend": "python_ast",
                "language": language,
                "complete": False,
                "confidence": "syntax_error",
                "error": type(exc).__name__,
            }
        collector = _PythonFacts()
        collector.visit(tree)
        return collector.record()


class _PythonFacts(ast.NodeVisitor):
    def __init__(self) -> None:
        self.definitions: list[dict[str, Any]] = []
        self.imports: set[str] = set()
        self.exports: set[str] = set()
        self.references: set[str] = set()
        self.calls: list[dict[str, str]] = []
        self.types: list[dict[str, str]] = []
        self.spans: list[dict[str, Any]] = []
        self._scope: list[str] = ["<module>"]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node, "function")
        self._visit_function_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node, "function")
        self._visit_function_body(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node, "class")
        for base in node.bases:
            value = _expression_name(base)
            if value:
                self.types.append({"owner": node.name, "relation": "base", "target": value})
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.add(alias.name)
            self._span("import", alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            value = f"{module}:{alias.name}" if module else alias.name
            self.imports.add(value)
            self._span("import", value, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.add(node.id)
            self._span("reference", node.id, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = _expression_name(node.func)
        if target:
            self.calls.append({"owner": self._scope[-1], "target": target})
            self._span("call", target, node)
        self.generic_visit(node)

    def _definition(self, node: ast.AST, kind: str) -> None:
        name = str(getattr(node, "name", ""))
        if not name:
            return
        self.definitions.append(
            {
                "name": name,
                "kind": kind,
                "scope": self._scope[-1],
            }
        )
        if self._scope == ["<module>"]:
            self.exports.add(name)
        self._span("definition", name, node)

    def _visit_function_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._annotation_types(node)
        self._scope.append(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for statement in node.body:
            self.visit(statement)
        self._scope.pop()

    def _annotation_types(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        annotations: list[ast.expr] = [
            value.annotation
            for value in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            if value.annotation is not None
        ]
        if node.args.vararg and node.args.vararg.annotation is not None:
            annotations.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation is not None:
            annotations.append(node.args.kwarg.annotation)
        if node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            target = _expression_name(annotation)
            if target:
                self.types.append(
                    {
                        "owner": node.name,
                        "relation": "annotation",
                        "target": target,
                    }
                )

    def _span(self, kind: str, name: str, node: ast.AST) -> None:
        start_line = int(getattr(node, "lineno", 1))
        start_col = int(getattr(node, "col_offset", 0))
        end_line = int(getattr(node, "end_lineno", start_line))
        end_col = int(getattr(node, "end_col_offset", start_col))
        self.spans.append(
            {
                "kind": kind,
                "name": name,
                "start": {"line": start_line, "column": start_col},
                "end": {"line": end_line, "column": end_col},
            }
        )

    def record(self) -> dict[str, Any]:
        return {
            "backend": "python_ast",
            "language": "Python",
            "complete": True,
            "confidence": "high",
            "definitions": _unique_records(self.definitions, ("name", "kind", "scope")),
            "imports": sorted(self.imports),
            "exports": sorted(self.exports),
            "references": sorted(self.references),
            "calls": _unique_records(self.calls, ("owner", "target")),
            "types": _unique_records(self.types, ("owner", "relation", "target")),
            "spans": self.spans[:4096],
        }


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value)
    if isinstance(node, ast.Call):
        return _expression_name(node.func)
    return None


def _unique_records(
    values: Iterable[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    output: list[dict[str, Any]] = []
    for value in values:
        marker = tuple(value.get(key) for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(dict(value))
    return output


def _tree_sitter_available() -> bool:
    return find_spec("tree_sitter") is not None


__all__ = ["SemanticProjectAnalyzer"]
