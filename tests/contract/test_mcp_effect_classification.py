"""Contract: MCP effect classification is conservative and never downgrades.

Inferring effects for a REMOTE MCP tool (BHV-111 / §92):
  * never reduces to just {READ_LOCAL} — minimum is {NETWORK_READ},
  * a readOnlyHint cannot downgrade below that minimum,
  * a destructiveHint yields DELETE + NETWORK_WRITE.
"""

from __future__ import annotations

import pytest

from athena.mcp.tools import infer_effects
from athena.protocol.capabilities import EffectClass

def _has(effects, cls) -> bool:
    return cls in effects

@pytest.mark.athena_claim("BHV-109")
@pytest.mark.athena_evidence("test", "invariant")
class TestMcpEffectClassification:
    def test_remote_tool_never_local_only(self):
        # An innocuous tool name with a readOnlyHint is still a network op.
        effects = infer_effects("get_user", {"readOnlyHint": True}, {}, remote=True)
        assert not _has(effects, EffectClass.READ_LOCAL)
        assert _has(effects, EffectClass.NETWORK_READ)

    def test_generic_remote_tool_has_network_minimum(self):
        effects = infer_effects("do_stuff", {}, {}, remote=True)
        assert not _has(effects, EffectClass.READ_LOCAL)
        assert EffectClass.NETWORK_READ in effects or EffectClass.NETWORK_WRITE in effects

    def test_readonly_hint_cannot_downgrade(self):
        # Even a readOnlyHint leaves NETWORK_READ in place (never local).
        effects = infer_effects("fetch_page", {"readOnlyHint": True}, {}, remote=True)
        assert _has(effects, EffectClass.NETWORK_READ)
        assert not _has(effects, EffectClass.READ_LOCAL)

    def test_destructive_hint_is_delete_and_network_write(self):
        effects = infer_effects("delete_user", {"destructiveHint": True}, {}, remote=True)
        assert _has(effects, EffectClass.DELETE)
        assert _has(effects, EffectClass.NETWORK_WRITE)

    def test_local_tool_is_never_remote_by_default(self):
        effects = infer_effects("read_file", {}, {}, remote=False)
        assert not _has(effects, EffectClass.NETWORK_WRITE)