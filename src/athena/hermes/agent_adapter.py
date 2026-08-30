"""OpenAI-compatible transport for a local Hermes Agent referee.

The adapter owns transport and response decoding only. Authority decisions
remain in :class:`athena.hermes.referee.HermesReferee`, which intersects this
external recommendation with Athena's deterministic proof.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from athena.hermes.referee import ReviewPacket

__all__ = ["HermesAgentEvaluator"]


_MAX_PACKET_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024
_REFEREE_SYSTEM_PROMPT = """You are Athena's read-only adversarial referee.

Review the bounded evidence packet supplied by Athena. Attack the claim that
the candidate or mission is safe, correct, complete, and properly proven.
You have no authority to apply, promote, write, commit, or modify anything.
Return exactly one JSON object and no markdown. Its decision must be one of:
PASS, HOLD, REJECT, CHALLENGE. Include concise rationale and any blockers,
challenges, risks, or missing_evidence that matter.
"""


class HermesAgentEvaluator:
    """Call Hermes Agent's local OpenAI-compatible chat endpoint.

    The callable returns raw bounded verdict data for ``HermesReferee`` to
    validate. It deliberately contains no candidate or mission policy.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        profile: str = "athena-referee",
        timeout_seconds: float = 60.0,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint:
            raise ValueError("Hermes endpoint cannot be empty")
        if not profile.strip():
            raise ValueError("Hermes profile cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("Hermes timeout must be positive")
        self.endpoint = endpoint
        self.profile = profile.strip()
        self.timeout_seconds = float(timeout_seconds)
        profile_path = quote(self.profile, safe="")
        endpoint_root = endpoint[:-3].rstrip("/") if endpoint.endswith("/v1") else endpoint
        if not endpoint_root.endswith(f"/p/{profile_path}"):
            endpoint_root = f"{endpoint_root}/p/{profile_path}"
        self._api_base = f"{endpoint_root}/v1"
        self._completion_url = f"{self._api_base}/chat/completions"
        self._health_url = f"{self._api_base}/models"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            headers=headers,
        )
        self._owns_client = client is None

    async def __call__(self, packet: ReviewPacket) -> Mapping[str, Any]:
        packet_record = packet.to_record()
        packet_json = json.dumps(
            {"packet_hash": packet.digest(), "packet": packet_record},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(packet_json.encode("utf-8")) > _MAX_PACKET_BYTES:
            raise ValueError("Hermes review packet exceeds the bounded transport limit")

        response = await self._client.post(
            self._completion_url,
            json={
                "model": "hermes-agent",
                "messages": [
                    {"role": "system", "content": _REFEREE_SYSTEM_PROMPT},
                    {"role": "user", "content": packet_json},
                ],
                "temperature": 0,
                "max_tokens": 1024,
                "stream": False,
                "response_format": {"type": "json_object"},
                "metadata": {
                    "athena_role": "referee",
                    "hermes_profile": self.profile,
                },
            },
        )
        response.raise_for_status()
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ValueError("Hermes response exceeds the bounded transport limit")
        return _decode_verdict_content(response.json())

    async def health(self) -> None:
        """Raise when Hermes' local API cannot answer a lightweight probe."""
        response = await self._client.get(self._health_url)
        response.raise_for_status()

    async def aclose(self) -> None:
        """Close the adapter-owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()


def _decode_verdict_content(response: Any) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("Hermes response is not a JSON object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ValueError("Hermes response has no chat completion choice")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ValueError("Hermes response has no chat message")
    content = message.get("content")
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        raw = "".join(parts)
    else:
        raise ValueError("Hermes response content is not text")
    if not raw.strip():
        raise ValueError("Hermes response content is empty")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Hermes response content is not strict JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("Hermes verdict must be a JSON object")
    return dict(decoded)
