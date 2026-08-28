from __future__ import annotations

from athena.project.index.builder import ProjectIndexBuilder


def test_project_index_persists_graph_and_impact(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "lib.py").write_text("def value():\n    return 1\n")
    (tmp_path / "src" / "app.py").write_text("from src.lib import value\n")
    (tmp_path / "tests" / "test_app.py").write_text("from src.app import value\n")
    index = ProjectIndexBuilder().build(str(tmp_path))
    impact = index.impact(["src/lib.py"])
    assert "src/app.py" in impact["direct_dependents"]
    assert (
        "tests/test_app.py" in impact["transitive_dependents"]
        or "tests/test_app.py" in impact["affected_tests"]
    )
    assert impact["index_revision"] == index.index_revision
    assert index.profile["fingerprint"]
    assert index.environment["fingerprint"]


def test_project_index_contains_python_semantic_facts(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(
        "from pathlib import Path\n"
        "class App:\n"
        "    def run(self, path: Path) -> str:\n"
        "        return str(Path(path).read_text())\n"
    )
    index = ProjectIndexBuilder().build(str(tmp_path))
    facts = index.semantic["files"]["app.py"]
    assert facts["backend"] == "python_ast"
    assert {item["name"] for item in facts["definitions"]} == {"App", "run"}
    assert "Path" in facts["references"]
    assert {item["target"] for item in facts["calls"]} >= {"str", "Path.read_text"}
    assert any(item["kind"] == "definition" for item in facts["spans"])
    assert "App" in index.symbols["app.py"]
    assert index.complete is True
    assert index.truncated is False


def test_project_index_uses_unambiguous_semantic_reference_for_impact(tmp_path):
    (tmp_path / "lib.py").write_text(
        "class Compiler:\n    def compile(self):\n        return True\n"
    )
    (tmp_path / "caller.py").write_text(
        "from lib import Compiler\ndef build():\n    return Compiler().compile()\n"
    )

    index = ProjectIndexBuilder().build(str(tmp_path))
    semantic_edges = [edge for edge in index.dependency_edges if edge.get("kind") == "semantic"]
    assert any(
        edge["source"] == "caller.py"
        and edge["target"] == "lib.py"
        and edge["confidence"] == "high"
        for edge in semantic_edges
    )
    assert "caller.py" in index.impact(["lib.py"])["direct_dependents"]


def test_project_index_marks_bounded_scan_incomplete(tmp_path):
    (tmp_path / "one.py").write_text("value = 1\n")
    (tmp_path / "two.py").write_text("value = 2\n")
    index = ProjectIndexBuilder(max_files=1).build(str(tmp_path))
    assert index.complete is False
    assert index.truncated is True
    assert index.truncation_reason == "max_files"
    assert index.semantic["complete"] is False
    assert index.impact(["one.py"])["complete"] is False
