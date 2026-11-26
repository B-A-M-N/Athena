"""Architecture lint: AST domain-host reach-through rule (items 28-29, 48)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

LINT_SCRIPT = Path(__file__).parents[3] / "scripts" / "architecture-lint"


@dataclass
class Hit:
    rule: str
    path: str
    line: int
    text: str
    severity: str


def _namespace():
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    return namespace


def _hits(source: str, *, root: Path | None = None, rel: str = "packs/mechanism.py"):
    namespace = _namespace()
    root = root or LINT_SCRIPT.parents[1]
    return namespace["_domain_host_hits"](
        root,
        root / "src/athena" / rel,
        rel,
        source,
    )


def test_rule_is_registered_as_ast_rule():
    text = LINT_SCRIPT.read_text(encoding="utf-8")
    assert 'id="domain-host-attribute-reachthrough"' in text
    assert "def _domain_host_hits" in text


@pytest.mark.parametrize(
    "source",
    [
        "value = self._host._store.get(task_id)\n",
        "dispatcher = self.service._dispatcher\n",
        "resource = self._ports._resource\n",
        "adapter = getattr(self._host, '_dispatcher', None)\n",
    ],
)
def test_private_owner_reachthrough_fails(source):
    hits = _hits(source)
    assert hits
    assert all(hit.rule == "domain-host-attribute-reachthrough" for hit in hits)
    assert all(hit.severity == "violation" for hit in hits)


@pytest.mark.parametrize(
    "source",
    [
        "value = self._ports.store\n",
        "dispatcher = self.service.dispatcher()\n",
        "adapter = getattr(self._ports, 'dispatcher', None)\n",
    ],
)
def test_public_ports_do_not_fail(source):
    assert _hits(source) == []


def test_migration_ratchet_allows_committed_count_and_fails_growth(tmp_path):
    baseline = tmp_path / "docs" / "architecture-size-baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        '{"domain_host_allowlist":{"files":{"packs/mechanism.py":1}}}',
        encoding="utf-8",
    )
    source = "value = self._host._store.get(task_id)\n"
    hits = _hits(source, root=tmp_path)
    assert len(hits) == 1
    assert hits[0].severity == "info"

    growth = source + "other = self._host._other\n"
    growth_hits = _hits(growth, root=tmp_path)
    assert any(hit.rule == "domain-host-migration-ratchet" for hit in growth_hits)
    assert any(hit.severity == "violation" for hit in growth_hits)
