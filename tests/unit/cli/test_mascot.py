"""Tests for mascot/buddy configurability and layout bounding."""

from __future__ import annotations

from io import StringIO

import pytest

from athena.cli.dual_pane import (
    DualPaneSurface,
    Mascot,
    configure_mascots,
    resolve_mascot_name,
)


@pytest.fixture(autouse=True)
def _restore_characters():
    """Tests register/replace characters; restore the built-ins afterwards."""
    yield
    Mascot._register_characters()


def test_builtin_registry_has_multiple_and_includes_owl():
    available = Mascot.available()
    assert "owl" in available
    assert len(available) > 1


def test_default_character_is_owl():
    assert Mascot().character == "owl"


@pytest.mark.parametrize("character", ["owl", "cat", "bot"])
@pytest.mark.parametrize("state", ["idle", "thinking", "executing", "waiting", "done", "failed"])
def test_render_never_exceeds_max_width(character, state):
    mascot = Mascot(character=character)
    mascot.state = state
    mascot.object = Mascot.OBJ_TERMINAL
    mascot.speech = "some quite long operational status line"
    for _ in range(2):  # both animation frames
        for line in mascot.render(max_width=16):
            assert len(line) <= 16


def test_set_character_unknown_returns_false():
    mascot = Mascot()
    assert mascot.set_character("dragon") is False
    assert mascot.character == "owl"
    assert mascot.set_character("cat") is True
    assert mascot.character == "cat"


def test_register_character_custom():
    ok = Mascot.register_character(
        "sparky",
        "Sparky the fox",
        {"idle": ["(fox)", "(fox~)"], "done": "(fox!)"},
    )
    assert ok is True
    assert "sparky" in Mascot.available()
    mascot = Mascot(character="sparky")
    assert mascot.character == "sparky"
    # A state without custom art falls back to the idle frame.
    mascot.state = "thinking"
    assert any("fox" in line for line in mascot.render(max_width=16))


def test_register_character_requires_idle_frame():
    assert Mascot.register_character("bad", "Bad", {"done": "(x)"}) is False
    assert "bad" not in Mascot.available()


def test_register_character_rejects_empty_name():
    assert Mascot.register_character("", "Nobody", {"idle": "(x)"}) is False


def test_configure_mascots_from_config_shape():
    configure_mascots(
        {
            "tux": {
                "label": "Tiny penguin",
                "frames": {"idle": ["(penguin)", "(peng~uin)"]},
            },
            "broken": "not-a-mapping",
            "also_broken": {"frames": {"done": "(x)"}},
        }
    )
    available = Mascot.available()
    assert "tux" in available
    assert available["tux"] == "Tiny penguin"
    assert "broken" not in available
    assert "also_broken" not in available


def test_resolve_mascot_name_precedence(monkeypatch):
    monkeypatch.delenv("ATHENA_MASCOT", raising=False)
    assert resolve_mascot_name(None) == "owl"
    assert resolve_mascot_name("cat") == "cat"
    monkeypatch.setenv("ATHENA_MASCOT", "bot")
    assert resolve_mascot_name(None) == "bot"
    assert resolve_mascot_name("owl") == "owl"  # explicit beats env


def test_surface_mascot_switching():
    surface = DualPaneSurface(output=StringIO(), interactive=False)
    assert surface.mascot_enabled is True
    assert surface.mascot.character == "owl"

    assert surface.set_mascot("cat") is True
    assert surface.mascot.character == "cat"
    assert surface.mascot_enabled is True

    assert surface.set_mascot("off") is True
    assert surface.mascot_enabled is False

    assert surface.set_mascot("on") is True
    assert surface.mascot_enabled is True
    assert surface.mascot.character == "cat"  # character preserved across off

    assert surface.set_mascot("dragon") is False
    assert surface.mascot.character == "cat"


def test_surface_constructor_accepts_mascot_choice(monkeypatch):
    monkeypatch.delenv("ATHENA_MASCOT", raising=False)
    surface = DualPaneSurface(output=StringIO(), interactive=False, mascot="bot")
    assert surface.mascot.character == "bot"

    hidden = DualPaneSurface(output=StringIO(), interactive=False, mascot="off")
    assert hidden.mascot_enabled is False


def test_surface_env_var_fallback(monkeypatch):
    monkeypatch.setenv("ATHENA_MASCOT", "cat")
    surface = DualPaneSurface(output=StringIO(), interactive=False)
    assert surface.mascot.character == "cat"


def test_config_roundtrip_mascot_fields():
    from athena.service.config import (
        AthenaConfig,
        config_from_dict,
        config_to_dict,
    )

    config = AthenaConfig(
        mascot="cat",
        mascots={"tux": {"label": "Tiny penguin", "frames": {"idle": ["(p)", "(p~)"]}}},
    )
    restored = config_from_dict(config_to_dict(config))
    assert restored.mascot == "cat"
    assert restored.mascots == {
        "tux": {"label": "Tiny penguin", "frames": {"idle": ["(p)", "(p~)"]}}
    }

    default = config_from_dict(config_to_dict(AthenaConfig()))
    assert default.mascot is None
    assert default.mascots == {}


def test_config_env_var(monkeypatch):
    from athena.service.config import load_config

    monkeypatch.setenv("ATHENA_MASCOT", "bot")
    config = load_config(cli_overrides={})
    assert config.mascot == "bot"
