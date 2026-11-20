from __future__ import annotations

from athena.release.source_manifest import build_manifest, verify_manifest


def test_source_manifest_detects_changes_but_ignores_release_outputs(tmp_path):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, source_sha="abc")

    (tmp_path / "release-artifacts").mkdir()
    (tmp_path / "release-artifacts" / "generated.whl").write_bytes(b"output")
    hypothesis_state = tmp_path / ".hypothesis" / "constants"
    hypothesis_state.mkdir(parents=True)
    hypothesis_state.joinpath("0015c0ec5754ebf3").write_bytes(b"example-db")
    (tmp_path / ".hypothesis" / ".gitignore").write_text("*\n", encoding="utf-8")
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "v").write_text("cache", encoding="utf-8")
    assert verify_manifest(tmp_path, manifest, source_sha="abc")[0] is True

    source.write_text("value = 2\n", encoding="utf-8")
    valid, detail = verify_manifest(tmp_path, manifest, source_sha="abc")
    assert valid is False
    assert "changed" in detail


def test_source_manifest_detects_added_untracked_source_files(tmp_path):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, source_sha="abc")

    (tmp_path / "src" / "untracked_payload.py").write_text("boom\n", encoding="utf-8")
    valid, detail = verify_manifest(tmp_path, manifest, source_sha="abc")
    assert valid is False
    assert "added=src/untracked_payload.py" in detail
