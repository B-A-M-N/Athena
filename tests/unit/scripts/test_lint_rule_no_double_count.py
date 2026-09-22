"""domain-host detector must count each reach-through exactly once (item 48)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

LINT_SCRIPT = Path(__file__).parents[3] / "scripts" / "architecture-lint"


@dataclass
class Hit:
    rule: str
    path: str
    line: int
    text: str
    severity: str


def _hits(source: str, tmp_path: Path):
    source_code = LINT_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source_code)
    body: list[ast.stmt] = []
    wanted = {"_domain_host_hits", "_annotated_allow_by_id"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            body.append(node)
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if (
                "SRC" in targets
                or "AST_DOMAIN_HOST_RULE" in targets
                or "DOMAIN_HOST_ROOTS" in targets
            ):
                body.append(node)
    namespace: dict = {
        "Hit": Hit,
        "ast": ast,
        "json": __import__("json"),
        "re": __import__("re"),
        "Path": Path,
        "SRC": "src/athena",
        "AST_DOMAIN_HOST_RULE": "domain-host-attribute-reachthrough",
        "DOMAIN_HOST_ROOTS": (
            "fusion/",
            "packs/",
            "research/",
            "synthesis/",
            "scheduler/",
            "service/",
        ),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LINT_SCRIPT), "exec"), namespace)
    root = LINT_SCRIPT.parents[1]
    rel = "packs/mechanism.py"
    path = tmp_path / "src" / "athena" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return [
        hit
        for hit in namespace["_domain_host_hits"](root, path, rel, source)
        if hit.severity == "violation"
    ]


def test_private_getattr_on_host_counts_exactly_once(tmp_path):
    hits = _hits("value = getattr(self._host, '_store', None)\n", tmp_path)
    assert len(hits) == 1, [h.text for h in hits]


def test_private_getattr_outside_host_receiver_is_not_a_hit(tmp_path):
    hits = _hits("value = getattr(self._store, '_hidden', None)\n", tmp_path)
    assert hits == []
