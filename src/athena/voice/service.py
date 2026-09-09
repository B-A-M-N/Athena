"""Provider-neutral voice transport.

Voice is an interface capability, not a second agent loop. This module owns
bounded audio validation, immutable artifact capture, and dispatch to a
configured provider adapter. The resulting transcript is ordinary user text
and enters Athena through the normal task admission path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from athena.artifacts.store import ArtifactStore
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.errors import VoiceInputError, VoiceUnavailable


@dataclass(frozen=True)
class VoiceTranscript:
    """A transcription plus the immutable audio artifact it came from."""

    text: str
    artifact: ArtifactRef
    provider: str
    model: str
    language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "artifact": {
                "id": self.artifact.id,
                "uri": self.artifact.uri,
                "hash": self.artifact.hash,
                "mime_type": self.artifact.mime_type,
                "size": self.artifact.size,
            },
            "provider": self.provider,
            "model": self.model,
            "language": self.language,
        }


@dataclass(frozen=True)
class VoiceSynthesis:
    """Spoken audio stored as a task-owned immutable artifact."""

    artifact: ArtifactRef
    provider: str
    model: str
    voice: str
    response_format: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": {
                "id": self.artifact.id,
                "uri": self.artifact.uri,
                "hash": self.artifact.hash,
                "mime_type": self.artifact.mime_type,
                "size": self.artifact.size,
            },
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "response_format": self.response_format,
        }


class VoiceManager:
    """Use explicitly configured provider routes for transcription and TTS."""

    def __init__(self, registry: Any, config: Any, artifacts: ArtifactStore) -> None:
        self._registry = registry
        self._config = config
        self._artifacts = artifacts

    def health(self) -> dict[str, Any]:
        enabled = bool(getattr(self._config, "enabled", False))
        transcription = self._provider_health(
            getattr(self._config, "transcription_provider", None), "transcribe_audio"
        )
        synthesis = self._provider_health(
            getattr(self._config, "synthesis_provider", None), "synthesize_audio"
        )
        return {
            "enabled": enabled,
            "state": "ready"
            if enabled and transcription["ready"] and synthesis["ready"]
            else ("disabled" if not enabled else "degraded"),
            "transcription": transcription,
            "synthesis": synthesis,
            "max_input_bytes": int(getattr(self._config, "max_input_bytes", 0)),
        }

    async def transcribe(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str = "voice-input",
        task_id: str | None = None,
        language: str | None = None,
    ) -> VoiceTranscript:
        self._require_enabled()
        self._validate_audio(data, mime_type)
        provider_name = self._require_provider("transcription_provider")
        provider = self._provider(provider_name)
        method = getattr(provider, "transcribe_audio", None)
        if not callable(method):
            raise VoiceUnavailable(
                f"voice transcription provider {provider_name!r} does not support transcription"
            )
        model = str(getattr(self._config, "transcription_model", "whisper-1"))
        try:
            raw = await method(
                data,
                filename=filename,
                mime_type=mime_type,
                model=model,
                language=language,
            )
        except VoiceUnavailable:
            raise
        except Exception as exc:  # provider errors retain the provider boundary
            raise VoiceUnavailable(
                f"voice transcription failed through {provider_name!r}", cause=exc
            ) from exc
        text, detected_language = _transcript_value(raw)
        if not text:
            raise VoiceInputError("voice provider returned an empty transcript")
        artifact = await self._artifacts.save(
            task_id=task_id,
            content=data,
            mime_type=mime_type,
            producer="voice.input",
            metadata={
                "kind": "voice_input",
                "filename": filename,
                "transcription_provider": provider_name,
                "transcription_model": model,
            },
        )
        return VoiceTranscript(
            text=text,
            artifact=artifact,
            provider=provider_name,
            model=model,
            language=detected_language or language,
        )

    async def synthesize(
        self,
        text: str,
        *,
        task_id: str | None = None,
        voice: str | None = None,
        response_format: str | None = None,
    ) -> VoiceSynthesis:
        self._require_enabled()
        if not isinstance(text, str) or not text.strip():
            raise VoiceInputError("spoken text must be a non-empty string")
        max_chars = int(getattr(self._config, "max_text_chars", 12_000))
        if len(text) > max_chars:
            raise VoiceInputError(f"spoken text exceeds the {max_chars}-character limit")
        provider_name = self._require_provider("synthesis_provider")
        provider = self._provider(provider_name)
        method = getattr(provider, "synthesize_audio", None)
        if not callable(method):
            raise VoiceUnavailable(
                f"voice synthesis provider {provider_name!r} does not support speech output"
            )
        model = str(getattr(self._config, "synthesis_model", "gpt-4o-mini-tts"))
        selected_voice = str(voice or getattr(self._config, "voice", "alloy"))
        selected_format = str(
            response_format or getattr(self._config, "response_format", "mp3")
        ).lower()
        try:
            data = await method(
                text,
                model=model,
                voice=selected_voice,
                response_format=selected_format,
            )
        except VoiceUnavailable:
            raise
        except Exception as exc:
            raise VoiceUnavailable(
                f"voice synthesis failed through {provider_name!r}", cause=exc
            ) from exc
        if not isinstance(data, bytes) or not data:
            raise VoiceUnavailable("voice synthesis provider returned no audio")
        max_output = int(getattr(self._config, "max_output_bytes", 25 * 1024 * 1024))
        if len(data) > max_output:
            raise VoiceInputError("voice synthesis output exceeds the configured size limit")
        mime_type = _audio_mime(selected_format)
        artifact = await self._artifacts.save(
            task_id=task_id,
            content=data,
            mime_type=mime_type,
            producer="voice.output",
            metadata={
                "kind": "voice_output",
                "synthesis_provider": provider_name,
                "synthesis_model": model,
                "voice": selected_voice,
                "response_format": selected_format,
            },
        )
        return VoiceSynthesis(
            artifact=artifact,
            provider=provider_name,
            model=model,
            voice=selected_voice,
            response_format=selected_format,
        )

    def _require_enabled(self) -> None:
        if not bool(getattr(self._config, "enabled", False)):
            raise VoiceUnavailable("voice is disabled; enable [voice] in Athena configuration")

    def _require_provider(self, field: str) -> str:
        name = str(getattr(self._config, field, None) or "").strip()
        if not name:
            raise VoiceUnavailable(f"voice {field} is not configured")
        return name

    def _provider(self, name: str) -> Any:
        try:
            return self._registry.provider_for(name)
        except Exception as exc:
            raise VoiceUnavailable(f"voice provider {name!r} is not registered", cause=exc) from exc

    def _provider_health(self, name: str | None, method_name: str) -> dict[str, Any]:
        provider_name = str(name or "").strip()
        if not provider_name:
            return {"provider": None, "ready": False, "reason": "not_configured"}
        try:
            provider = self._registry.provider_for(provider_name)
        except Exception:
            return {"provider": provider_name, "ready": False, "reason": "not_registered"}
        if not callable(getattr(provider, method_name, None)):
            return {"provider": provider_name, "ready": False, "reason": "unsupported"}
        return {"provider": provider_name, "ready": True}

    def _validate_audio(self, data: bytes, mime_type: str) -> None:
        if not isinstance(data, bytes) or not data:
            raise VoiceInputError("audio input must be non-empty bytes")
        content_type = str(mime_type or "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("audio/"):
            raise VoiceInputError("voice input must use an audio/* content type")
        max_bytes = int(getattr(self._config, "max_input_bytes", 25 * 1024 * 1024))
        if len(data) > max_bytes:
            raise VoiceInputError(f"voice input exceeds the {max_bytes}-byte limit")


def _transcript_value(raw: Any) -> tuple[str, str | None]:
    if isinstance(raw, str):
        return raw.strip(), None
    if isinstance(raw, Mapping):
        text = str(raw.get("text") or "").strip()
        language = raw.get("language")
        return text, str(language) if language else None
    return "", None


def _audio_mime(response_format: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "mpeg": "audio/mpeg",
        "wav": "audio/wav",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "pcm": "audio/pcm",
    }.get(response_format, "audio/mpeg")


__all__ = ["VoiceManager", "VoiceSynthesis", "VoiceTranscript"]
