from __future__ import annotations

import asyncio
import os
import signal
from io import StringIO

import pytest

from athena.cli.animation import AnimationClock, OIAnimator
from athena.cli.framebuffer import OIFrameBuffer, pillow_available
from athena.cli.dual_pane import DualPaneSurface
from athena.cli.layout import LayoutMode, compute_layout
from athena.cli.projection import ProjectionState
from athena.cli.render.ansi import CellGridDiffRenderer, cell_width, fit_cells
from athena.cli.render.kitty import KittyAsset, KittyCapabilityProbe, KittyGraphicsProtocol, select_renderer
from athena.cli.render.scene import render_scene_lines
from athena.cli.scene import build_oi_scene
from athena.cli.terminal import TerminalSession, sanitize_terminal_text
from athena.service.config import AthenaConfig, config_from_dict, config_to_dict, load_config


@pytest.mark.parametrize("status", ["READY", "THINKING", "EXECUTING", "APPROVAL", "FAILURE", "RECOVERING"])
def test_oi_content_never_changes_equal_aperture_geometry(status: str) -> None:
    layout = compute_layout(160, 45)
    assert layout.apertures_equal
    assert layout.operator.width == layout.oi.width
    assert layout.operator.height == layout.oi.height
    assert layout.oi.width > 0 and layout.oi.height > 0
    # Scene state is content, not layout input.
    state = ProjectionState(status=status)
    assert state.status == status
    assert compute_layout(160, 45).oi == layout.oi


def test_layout_thresholds_and_plain_mode() -> None:
    assert compute_layout(160, 45).mode is LayoutMode.GLASS_FULL
    assert compute_layout(120, 40).mode is LayoutMode.GLASS_COMPACT
    assert compute_layout(100, 24).mode is LayoutMode.ANSI_INSTRUMENT
    assert compute_layout(80, 18).mode is LayoutMode.PLAIN


def test_sanitizer_removes_terminal_protocols_but_keeps_text() -> None:
    value = "ok\x1b[31m red\x1b[0m\x1b]8;;https://evil.invalid\x07link\x1b]8;;\x07\x1b_Ga=T;payload\x1b\\\nnext\x00"
    clean = sanitize_terminal_text(value)
    assert clean == "ok redlink\nnext"
    assert "https" not in clean
    assert "payload" not in clean


def test_diff_renderer_does_not_clear_on_animation_tick() -> None:
    output = StringIO()
    renderer = CellGridDiffRenderer(output)
    first = renderer.draw(["alpha", "buddy 0"], columns=20)
    output.seek(0)
    output.truncate(0)
    second = renderer.draw(["alpha", "buddy 1"], columns=20)
    assert "\x1b[2J" in first
    assert "\x1b[2J" not in second
    assert second
    assert renderer.last_changed_spans


def test_unicode_fit_uses_terminal_cell_width() -> None:
    fitted = fit_cells("界界界", 5).rstrip()
    assert cell_width(fitted) == 5
    assert fitted.endswith("…")


def test_animation_is_presentation_only_and_reduced_motion_is_still() -> None:
    animator = OIAnimator()
    animator.set_state("EXECUTING", "graph")
    before = animator.visual.semantic_state
    assert animator.tick(0.25)
    assert animator.visual.semantic_state == before

    reduced = OIAnimator(reduced_motion=True)
    reduced.set_state("EXECUTING", "graph")
    assert reduced.tick(0.25) is False
    assert reduced.visual.phase == 0

    idle = OIAnimator()
    idle.visual.transition = 1.0
    assert idle.tick(0.25) is False
    assert idle.visual.dirty is False


def test_animation_transitions_between_semantic_buddy_anchors() -> None:
    animator = OIAnimator()
    animator.set_state("EXECUTING", "graph")

    assert animator.visual.previous_anchor == "center"
    assert animator.visual.buddy_anchor == "graph"
    assert animator.visual.transition == 0
    animator.tick(0.1)
    assert 0 < animator.visual.transition < 1


@pytest.mark.skipif(not pillow_available(), reason="Pillow is optional")
def test_glass_framebuffer_caches_static_scene_layer() -> None:
    layout = compute_layout(120, 40)
    state = ProjectionState(status="EXECUTING")
    scene = build_oi_scene(state, layout.oi)
    animator = OIAnimator()
    animator.set_state("EXECUTING", "graph")
    framebuffer = OIFrameBuffer()

    first = framebuffer.render(scene, animator.visual, 640, 360)
    animator.tick(0.1)
    second = framebuffer.render(scene, animator.visual, 640, 360)

    assert first is not None and second is not None
    assert first.png != second.png
    assert len(framebuffer._base_frames) == 1


def test_animation_clock_only_repaints_the_glass_layer() -> None:
    surface = DualPaneSurface(output=StringIO(), error=StringIO(), interactive=False)
    surface._full_screen = True
    surface.display = "ansi"
    surface.repaint_oi = lambda **_: pytest.fail("ANSI animation should not repaint")

    surface._animation_tick(0.1)


def test_oi_scene_renders_observed_entities_instead_of_demo_fixtures() -> None:
    state = ProjectionState()
    state.reduce(
        "CapabilityRequested",
        {
            "call_id": "call-live",
            "capability_id": "fs",
            "arguments": {"operation": "read", "path": "src/live.py"},
        },
    )
    scene = build_oi_scene(state, compute_layout(120, 40).oi)
    rendered = "\n".join(render_scene_lines(state, scene, width=80, height=20))

    assert any(entity.label == "src/live.py" for entity in scene.entities)
    assert "src/live.py" in rendered
    assert "kernel.py" not in rendered
    assert "verify-release" not in rendered


def test_kitty_probe_and_safe_fallback() -> None:
    response = "\x1b_Gi=31;OK\x1b\\"
    assert KittyCapabilityProbe.confirmed(response)
    assert not KittyCapabilityProbe.confirmed("kitty graphics OK")
    assert not KittyCapabilityProbe.confirmed("unsupported")
    assert select_renderer("glass", capability_confirmed=False) == "ansi"
    protocol = KittyGraphicsProtocol()
    payload = protocol.present(KittyAsset(4, b"png"), x=2, y=3, columns=10, rows=5)
    assert "i=4" in payload
    assert "\x1b[4;3H" in payload
    assert "a=p,i=4,c=10,r=5,C=1,z=-1" in payload
    assert "x=2,y=3" not in payload
    assert protocol.cleanup()
    assert protocol.cleanup() == ""


def test_kitty_transmission_chunks_payloads_at_protocol_limit() -> None:
    protocol = KittyGraphicsProtocol()
    payload = protocol.encode(KittyAsset(5, b"x" * 5000))
    commands = payload.split("\x1b_G")[1:]
    assert len(commands) == 2
    assert "a=t,f=100,i=5,q=2,m=1;" in commands[0]
    assert "q=2,m=0;" in commands[1]
    assert all(len(command.split(";", 1)[1].removesuffix("\x1b\\")) <= 4096 for command in commands)


def test_kitty_reuses_one_image_identity_for_frame_replacement() -> None:
    protocol = KittyGraphicsProtocol()
    protocol.present(KittyAsset(9, b"first"), x=1, y=1, columns=4, rows=2)
    protocol.present(KittyAsset(9, b"second"), x=1, y=1, columns=4, rows=2)

    cleanup = protocol.cleanup()
    assert cleanup.count("a=d,d=i,i=9") == 1
    assert protocol.cleanup() == ""


def test_kitty_support_query_is_non_destructive() -> None:
    query = KittyGraphicsProtocol.query_support()
    assert "a=q,i=31,s=1,v=1,t=d,f=24" in query
    assert "\x1b[c" in query


def test_terminal_session_is_idempotent_for_non_tty() -> None:
    output = StringIO()
    session = TerminalSession(output)
    session.open()
    session.open()
    session.close()
    session.close()
    assert output.getvalue() == ""


class _TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_terminal_session_restores_handlers_and_screen_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _TTYBuffer()
    previous = {
        signal.SIGINT: signal.SIG_IGN,
        signal.SIGTERM: signal.SIG_DFL,
    }
    if hasattr(signal, "SIGHUP"):
        previous[signal.SIGHUP] = signal.SIG_IGN
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "getsignal", lambda signum: previous[signum])
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.append((signum, handler)))

    session = TerminalSession(output)
    session.open()
    session.close()
    session.close()

    assert output.getvalue() == TerminalSession.ENTER + TerminalSession.LEAVE
    assert [signum for signum, _handler in installed[:2]] == [signal.SIGINT, signal.SIGTERM]
    restored = installed[2:]
    assert {signum: handler for signum, handler in restored} == previous
    assert session.active is False


def test_terminal_session_signal_cleanup_preserves_default_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _TTYBuffer()
    installed: list[tuple[int, object]] = []
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.append((signum, handler)))
    monkeypatch.setattr(os, "kill", lambda pid, signum: killed.append((pid, signum)))

    session = TerminalSession(output)
    session.open()
    with pytest.raises(KeyboardInterrupt):
        session._handle_signal(signal.SIGINT, None)
    assert session.active is False

    session.open()
    session._handle_signal(signal.SIGTERM, None)
    assert session.active is False
    assert killed == [(os.getpid(), signal.SIGTERM)]

    if hasattr(signal, "SIGHUP"):
        session.open()
        session._handle_signal(signal.SIGHUP, None)
        assert killed[-1] == (os.getpid(), signal.SIGHUP)


@pytest.mark.asyncio
async def test_animation_clock_awaited_shutdown_handles_callback_failure() -> None:
    calls = 0

    def broken_callback(_dt: float) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("paint failed")

    clock = AnimationClock(broken_callback)
    clock.start()
    await asyncio.sleep(0.12)
    await clock.stop_async()
    assert clock._task is None
    assert calls >= 1
    await clock.stop_async()


def test_display_settings_round_trip_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AthenaConfig(display="glass", animations=False, reduced_motion=True)
    assert config_from_dict(config_to_dict(config)).display == "glass"
    assert config_from_dict(config_to_dict(config)).animations is False
    assert config_from_dict(config_to_dict(config)).reduced_motion is True
    monkeypatch.setenv("ATHENA_DISPLAY", "ansi")
    monkeypatch.setenv("ATHENA_ANIMATIONS", "0")
    monkeypatch.setenv("ATHENA_REDUCED_MOTION", "1")
    loaded = load_config(explicit_path="/tmp/athena-does-not-exist.toml")
    assert loaded.display == "ansi"
    assert loaded.animations is False
    assert loaded.reduced_motion is True
