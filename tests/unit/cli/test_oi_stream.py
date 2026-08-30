from io import StringIO

import pytest

from athena.cli.oi_stream import OIStreamViewer, _RenderScheduler
from athena.protocol.events import make_event


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.asyncio
@pytest.mark.athena_scenario("PROJECTION-001")
async def test_oi_viewer_renders_to_configured_output_and_keeps_partials():
    output = StringIO()
    viewer = OIStreamViewer(output=output, interactive=False)
    await viewer.handle_event(make_event("ModelDelta", {"text": "Hel"}))
    viewer.render(height=6)
    await viewer.handle_event(make_event("ModelDelta", {"text": "lo\n"}))
    viewer.render(height=6)

    assert "Hello" in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.athena_scenario("PROJECTION-001")
async def test_oi_viewer_does_not_write_to_process_stdout(capsys):
    output = StringIO()
    viewer = OIStreamViewer(output=output, interactive=False)
    await viewer.handle_event(make_event("StdoutChunk", {"data": "result\n"}))
    viewer.render(height=6)

    assert capsys.readouterr().out == ""
    assert "result" in output.getvalue()


@pytest.mark.asyncio
async def test_oi_viewer_does_not_prompt_twice_for_kernel_approval_summary():
    class _Service:
        def __init__(self):
            self.approvals = []

        async def approve(self, approval_id, *, granted, scope=None):
            self.approvals.append((approval_id, granted, scope))

    service = _Service()
    viewer = OIStreamViewer(
        service=service,
        interactive=True,
        input_fn=lambda _prompt: "1",
        output=StringIO(),
    )
    await viewer.handle_event(
        make_event(
            "ApprovalRequested",
            {
                "approval_id": "approval-oi",
                "capability_id": "execute",
                "scopes": ["call", "task"],
                "reason": "execution requires authorization",
            },
        )
    )
    await viewer.handle_event(make_event("ApprovalRequested", {"calls": 1}))

    assert service.approvals == [("approval-oi", True, "call")]
    assert viewer._pending_approval is None
    assert viewer._status == "running"


def test_oi_viewer_restores_tty_screen_lifecycle():
    output = _TTY()
    viewer = OIStreamViewer(output=output, interactive=True)

    viewer.open()
    viewer.close()

    assert "\x1b[?1049h" in output.getvalue()
    assert "\x1b[?1049l" in output.getvalue()


def test_oi_render_scheduler_defaults_to_bounded_25_fps():
    viewer = OIStreamViewer(output=StringIO(), interactive=False)
    scheduler = _RenderScheduler(viewer)

    assert scheduler.interval == pytest.approx(1.0 / 25.0)
