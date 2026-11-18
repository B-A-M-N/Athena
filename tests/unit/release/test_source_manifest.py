from __future__ import annotations

from athena.release.source_manifest import build_manifest, verify_manifest


def test_source_manifest_detects_changes_but_ignores_release_outputs(tmp_path):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, source_sha="abc")

    (tmp_path / "release-artifacts").mkdir()
    (tmp_path / "release-artifacts" / "generated.whl").write_bytes(b"output")
    assert verify_manifest(tmp_path, manifest, source_sha="abc")[0] is True

    source.write_text("value = 2\n", encoding="utf-8")
    valid, detail = verify_manifest(tmp_path, manifest, source_sha="abc")
    assert valid is False
    assert "changed" in detail
