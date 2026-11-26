"""Fail closed when a renderer source file escapes the visual golden digest.

The golden manifest pins a ``renderer_revision`` digest over the renderer
sources listed in ``native/assets/oi/visual-goldens/renderer-files.json``.
A rendering file that is missing from that list can change pixels without
invalidating approved goldens.  These tests keep the list complete.
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_LIST_PATH = _ROOT / "native" / "assets" / "oi" / "visual-goldens" / "renderer-files.json"
_SOURCE_ROOT = _ROOT / "native" / "src"
_EXCLUDED_PREFIXES = ("native/src/tests/",)


def _declared() -> set[str]:
    payload = json.loads(_LIST_PATH.read_text(encoding="utf-8"))
    return set(payload["renderer_files"])


def _required() -> set[str]:
    """Every native source outside the standalone test harness.

    Visual output depends on far more than ``render/``: the X11 event loop
    and present path, platform surface/terminal integration, and input
    handling all shape pixels.  Invalidation must therefore trigger on any
    native source change; erring toward over-invalidation is safe, missing
    one is not.
    """
    required = {
        str(path.relative_to(_ROOT))
        for path in _SOURCE_ROOT.rglob("*.rs")
        if not str(path.relative_to(_ROOT)).startswith(_EXCLUDED_PREFIXES)
    }
    return required


def test_renderer_files_manifest_is_present_and_well_formed() -> None:
    payload = json.loads(_LIST_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] >= 2
    assert isinstance(payload["renderer_files"], list) and payload["renderer_files"]


def test_every_renderer_source_is_digested() -> None:
    required = _required()
    assert required, "renderer source scan found nothing"
    missing = required - _declared()
    assert not missing, (
        "renderer sources missing from renderer-files.json (visual-golden "
        f"invalidation would be silently bypassed): {sorted(missing)}"
    )


def test_digest_list_only_references_existing_sources() -> None:
    stale = sorted(path for path in _declared() if not (_ROOT / path).is_file())
    assert not stale, f"renderer-files.json references missing sources: {stale}"


def test_renderer_files_list_has_no_duplicates() -> None:
    entries = json.loads(_LIST_PATH.read_text(encoding="utf-8"))["renderer_files"]
    assert len(entries) == len(set(entries))
