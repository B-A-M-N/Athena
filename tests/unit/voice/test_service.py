from __future__ import annotations

from dataclasses import dataclass

import pytest

from athena.artifacts.store import ArtifactStore
from athena.models.registry import ProviderRegistry
from athena.protocol.models import ModelInfo
from athena.service.config import VoiceConfig
from athena.voice import VoiceManager


@dataclass
class _VoiceProvider:
    transcribed: list[dict]
    synthesized: list[dict]

    async def list_models(self):
        return [ModelInfo(id="chat", provider="voice", tool_calling=True)]

    async def complete(self, request):
        if False:  # pragma: no cover - makes this an async generator for the registry
            yield request

    def readiness(self):
        return {"state": "ready"}

    async def transcribe_audio(self, data, **kwargs):
        self.transcribed.append({"data": data, **kwargs})
        return {"text": "check the build", "language": "en"}

    async def synthesize_audio(self, text, **kwargs):
        self.synthesized.append({"text": text, **kwargs})
        return b"spoken-audio"


def _manager(tmp_path):
    provider = _VoiceProvider([], [])
    registry = ProviderRegistry()
    registry.register("voice", provider)
    config = VoiceConfig(
        enabled=True,
        transcription_provider="voice",
        synthesis_provider="voice",
        response_format="wav",
    )
    return VoiceManager(registry, config, ArtifactStore(tmp_path / "artifacts")), provider


@pytest.mark.asyncio
async def test_voice_manager_keeps_input_and_output_in_immutable_artifacts(tmp_path):
    manager, provider = _manager(tmp_path)

    transcript = await manager.transcribe(
        b"wav-input",
        mime_type="audio/wav",
        filename="request.wav",
        task_id="task-1",
    )
    assert transcript.text == "check the build"
    assert transcript.artifact.task_id == "task-1"
    assert transcript.artifact.mime_type == "audio/wav"
    assert await manager._artifacts.load(transcript.artifact) == b"wav-input"
    assert provider.transcribed[0]["filename"] == "request.wav"

    spoken = await manager.synthesize("Build is green", task_id="task-1")
    assert spoken.artifact.task_id == "task-1"
    assert spoken.artifact.mime_type == "audio/wav"
    assert await manager._artifacts.load(spoken.artifact) == b"spoken-audio"
    assert provider.synthesized[0]["response_format"] == "wav"


@pytest.mark.asyncio
async def test_voice_manager_rejects_unbounded_or_non_audio_input(tmp_path):
    manager, _ = _manager(tmp_path)
    with pytest.raises(Exception, match=r"audio/\*"):
        await manager.transcribe(b"not audio", mime_type="text/plain")

    tiny = VoiceConfig(
        enabled=True,
        transcription_provider="voice",
        synthesis_provider="voice",
        max_input_bytes=3,
    )
    provider = _VoiceProvider([], [])
    registry = ProviderRegistry()
    registry.register("voice", provider)
    bounded = VoiceManager(registry, tiny, ArtifactStore(tmp_path / "bounded"))
    with pytest.raises(Exception, match="exceeds"):
        await bounded.transcribe(b"1234", mime_type="audio/wav")
