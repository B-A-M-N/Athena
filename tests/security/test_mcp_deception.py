"""MCP deception defenses (§92 / BHV-111).

Server annotations are advisory, never authorization. A remote MCP tool must
never be classified as ``READ_LOCAL``; the inferred minimum is a NETWORK effect,
and unannotated tools default conservatively to ``NETWORK_WRITE``.
"""

from __future__ import annotations

from athena.mcp.tools import infer_effects
from athena.protocol.capabilities import EffectClass

NOT_READ_LOCAL = frozenset({EffectClass.READ_LOCAL})


def test_remote_readonly_hint_yields_network_read_not_local():
    effects = infer_effects(
        "fetch_page", {"readOnlyHint": True}, {}, remote=True
    )
    assert EffectClass.NETWORK_READ in effects
    assert EffectClass.READ_LOCAL not in effects
    assert effects != NOT_READ_LOCAL


def test_remote_no_annotations_never_read_local():
    effects = infer_effects("blorp_action", None, None, remote=True)
    assert EffectClass.READ_LOCAL not in effects
    # Conservative minimum: an unclassifiable remote call is NETWORK_WRITE.
    assert EffectClass.NETWORK_WRITE in effects


def test_remote_mutating_verb_is_network_write_not_local():
    effects = infer_effects("delete_row", None, None, remote=True)
    assert EffectClass.NETWORK_WRITE in effects
    assert EffectClass.READ_LOCAL not in effects


def test_remote_destructive_hint_never_lowered_to_local_read():
    effects = infer_effects("purge", {"destructiveHint": True}, {}, remote=True)
    assert EffectClass.DELETE in effects
    assert EffectClass.NETWORK_WRITE in effects
    assert EffectClass.READ_LOCAL not in effects


def test_local_readonly_hint_may_be_read_local_but_remote_is_not():
    """readOnlyHint on a LOCAL tool can be READ_LOCAL; on remote it cannot."""
    local = infer_effects("get", {"readOnlyHint": True}, {}, remote=False)
    assert EffectClass.READ_LOCAL in local
    remote = infer_effects("get", {"readOnlyHint": True}, {}, remote=True)
    assert EffectClass.READ_LOCAL not in remote
    assert EffectClass.NETWORK_READ in remote