from __future__ import annotations

from athena.project.index.coordinator import ProjectIndexCoordinator
from athena.project.index.builder import ProjectIndexBuilder
from athena.project.index.models import ProjectIndex


class _Builder:
    def __init__(self):
        self.calls = 0

    def build(self, root: str) -> ProjectIndex:
        self.calls += 1
        return ProjectIndex(root=root, index_revision=f"revision-{self.calls}")


class _Store:
    def __init__(self):
        self.value = None

    async def get(self, root):
        return self.value

    async def save(self, index):
        self.value = index


async def test_index_coordinator_reuses_and_invalidates_one_revision(tmp_path):
    builder = _Builder()
    store = _Store()
    coordinator = ProjectIndexCoordinator(store, builder)
    root = str(tmp_path)

    first = await coordinator.current(root)
    cached = await coordinator.current(root)
    assert first is cached
    assert builder.calls == 1

    (tmp_path / "changed.py").write_text("value = 1\n")
    assert coordinator.mark_stale_for_paths([str(tmp_path / "changed.py")]) == (root,)
    second = await coordinator.current(root)
    assert second.index_revision == "revision-2"
    assert builder.calls == 2


async def test_restart_does_not_accept_persisted_index_after_source_change(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    builder = ProjectIndexBuilder()
    store = _Store()

    first = await ProjectIndexCoordinator(store, builder).current(str(tmp_path))
    assert first.source_revision

    source.write_text("value = 2\n")
    restarted = ProjectIndexCoordinator(store, ProjectIndexBuilder())
    second = await restarted.current(str(tmp_path))

    assert second.source_revision != first.source_revision
    assert second.files[0]["sha256"] != first.files[0]["sha256"]


async def test_source_verified_freshness_detects_external_edits_without_invalidation(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    coordinator = ProjectIndexCoordinator(_Store(), ProjectIndexBuilder())

    first = await coordinator.current(str(tmp_path))
    source.write_text("value = 2\n")
    verified = await coordinator.current(str(tmp_path), freshness="source_verified")

    assert verified.source_revision != first.source_revision
    assert verified.files[0]["sha256"] != first.files[0]["sha256"]
