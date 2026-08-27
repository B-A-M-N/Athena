from io import StringIO

import pytest

from athena.cli.oi_stream import OIStreamViewer
from athena.protocol.events import make_event


@pytest.mark.asyncio
async def test_oi_viewer_renders_to_configured_output_and_keeps_partials():
    output = StringIO()
    viewer = OIStreamViewer(output=output, interactive=False)
    await viewer.handle_event(make_event("ModelDelta", {"text": "Hel"}))
    viewer.render(height=6)
    await viewer.handle_event(make_event("ModelDelta", {"text": "lo\n"}))
    viewer.render(height=6)

    assert "Hello" in output.getvalue()


@pytest.mark.asyncio
async def test_oi_viewer_does_not_write_to_process_stdout(capsys):
    output = StringIO()
    viewer = OIStreamViewer(output=output, interactive=False)
    await viewer.handle_event(make_event("StdoutChunk", {"data": "result\n"}))
    viewer.render(height=6)

    assert capsys.readouterr().out == ""
    assert "result" in output.getvalue()
