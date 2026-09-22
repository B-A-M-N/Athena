"""Real Python serializer to Rust native-deserializer seam evidence."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from athena.presentation.native_bridge import native_projection_frame
from athena.presentation.projection import ProjectionState


def test_current_python_projection_decodes_in_native_headless() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    native_binary = Path(
        os.environ.get(
            "ATHENA_NATIVE_BINARY",
            str(repo_root / "native" / "target" / "debug" / "athena-terminal"),
        )
    )
    assert native_binary.is_file(), (
        f"build the native binary before running the producer/consumer seam test: {native_binary}"
    )

    frame = native_projection_frame(ProjectionState(), width=80, height=24)
    completed = subprocess.run(
        [
            str(native_binary),
            "--headless",
            "--bridge-stdin",
            "--command",
            "sleep 1; exit 0",
        ],
        input=json.dumps(frame, sort_keys=True) + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "BRIDGE ERROR" not in output, output
    assert "BRIDGE CONNECTED" in output, output
    assert "ATHENA OI // GLASS COMPUTE [READY]" in output, output
