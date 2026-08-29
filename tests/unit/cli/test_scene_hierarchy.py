from __future__ import annotations

from athena.cli.scene import SceneEntity, normalize_runtime_tree, normalize_workspace_tree


def flattened_ids(nodes) -> tuple[str, ...]:
    out: list[str] = []
    for node in nodes:
        out.append(node.id)
        if node.children:
            out.extend(flattened_ids(node.children))
    return tuple(out)


def test_workspace_paths_are_merged_deterministically_without_directory_status_guesses():
    entities = [
        SceneEntity("resource-z", "resource", "root/B.py", "complete"),
        SceneEntity("resource-a", "resource", "root/a.py", "active"),
        SceneEntity("artifact-m", "artifact", "memory://opaque-id", "complete"),
    ]

    tree = normalize_workspace_tree(reversed(entities))
    again = normalize_workspace_tree(entities)

    assert tree == again
    assert flattened_ids(tree) == (
        "workspace:memory://opaque-id",
        "workspace:root",
        "workspace:root/B.py",
        "workspace:root/a.py",
    )
    assert tree[1].status == "unknown"
    assert [node.status for node in tree[1].children] == ["complete", "active"]


def test_workspace_depth_and_node_caps_emit_truncation_markers():
    deep = normalize_workspace_tree(
        [SceneEntity("deep", "resource", "one/two/three/four.py", "active")],
        max_depth=2,
    )
    assert flattened_ids(deep) == (
        "workspace:one",
        "workspace:one/two",
        "tree:truncated",
    )
    # deep is a single-root tuple; inspect the nested boundary node.
    assert deep[0].children[0].metadata["path_truncated"] is True
    assert deep[0].children[0].children[0].status == "truncated"

    capped = normalize_workspace_tree(
        [
            SceneEntity(f"file-{index}", "resource", f"root/{index}.py", "active")
            for index in range(3)
        ],
        max_nodes=2,
    )
    assert flattened_ids(capped) == (
        "workspace:root",
        "workspace:root/0.py",
        "workspace:root/1.py",
        "tree:truncated",
    )


def test_runtime_hierarchy_preserves_orphans_and_observed_statuses():
    entities = [
        SceneEntity("child", "workflow", "Child", "active", metadata={"parent_id": "missing"}),
        SceneEntity("root", "workflow", "Root", "running", metadata={"parent_id": "absent"}),
    ]

    tree = normalize_runtime_tree(entities)

    # Both are orphan roots (parent IDs absent) — two independent roots.
    assert flattened_ids(tree) == ("workflow:child", "workflow:root")
    assert tree[0].metadata["orphan"] is True
    assert tree[1].metadata["orphan"] is True
    assert all(not node.children for node in tree)
    assert [node.status for node in tree] == ["active", "running"]


def test_runtime_cycles_are_bounded_and_cycle_members_become_roots():
    entities = [
        SceneEntity("a", "task", "A", "active", metadata={"parent_id": "b"}),
        SceneEntity("b", "task", "B", "running", metadata={"parent_id": "a"}),
        SceneEntity("tail", "task", "Tail", "requested", metadata={"parent_id": "a"}),
    ]

    tree = normalize_runtime_tree(entities)

    assert flattened_ids(tree) == ("task:a", "task:b", "task:tail")
    assert all(not node.children for node in tree)
    assert tree[0].metadata["cycle"] is True
    assert tree[1].metadata["cycle"] is True
    assert tree[2].metadata["parent_cycle"] is True


def test_runtime_depth_and_node_caps_emit_truncation_markers():
    chain = [
        SceneEntity("one", "task", "1", "active"),
        SceneEntity("two", "task", "2", "running", metadata={"parent_id": "one"}),
        SceneEntity("three", "task", "3", "requested", metadata={"parent_id": "two"}),
    ]

    depth_capped = normalize_runtime_tree(chain, max_depth=2)
    assert flattened_ids(depth_capped) == ("task:one", "task:two", "tree:truncated")
    # single root; boundary node is nested child of root.
    assert depth_capped[0].children[0].metadata.get("path_truncated") is True

    # max_nodes=2 on a chain where each node has <=1 child — no per-node cap fires.
    # The chain stays intact; verify no unexpected truncation.
    node_capped = normalize_runtime_tree(chain, max_nodes=2)
    assert flattened_ids(node_capped) == ("task:one", "task:two", "task:three")
