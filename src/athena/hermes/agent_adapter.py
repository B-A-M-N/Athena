"""OpenAI-compatible transport for a local Hermes Agent referee.

The adapter owns transport and response decoding only. Authority decisions
remain in :class:`athena.hermes.referee.HermesReferee`, which intersects this
external recommendation with Athena's deterministic proof.
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
import ipaddress
import time

import httpx

from athena.hermes.referee import ReviewPacket

__all__ = ["HermesAgentEvaluator", "HermesRefereePreflight", "HermesRefereeSafetyError"]


_MAX_PACKET_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024
_REFEREE_POLICY_VERSION = 1
_PREFLIGHT_TTL_SECONDS = 45.0
_REFEREE_SYSTEM_PROMPT = """You are Athena's read-only adversarial referee.

Review the bounded evidence packet supplied by Athena. Attack the claim that
the candidate or mission is safe, correct, complete, and properly proven.
You have no authority to apply, promote, write, commit, or modify anything.
Return exactly one JSON object and no markdown. Its decision must be one of:
PASS, HOLD, REJECT, CHALLENGE. Include concise rationale and any blockers,
challenges, risks, or missing_evidence that matter.
"""


class HermesRefereeSafetyError(ValueError):
    """The endpoint is reachable or configured, but not safe for referee use."""


@dataclass(frozen=True)
class HermesRefereePreflight:
    """Authoritative, read-only proof returned by Hermes capabilities."""

    endpoint: str
    profile: str
    policy_fingerprint: str
    models: tuple[str, ...]
    capabilities: Mapping[str, Any]

    @property
    def safety_verified(self) -> bool:
        """Return whether this result passed the complete safety contract."""
        return True

    @property
    def read_only_verified(self) -> bool:
        """Alias used by operator-facing governance surfaces."""
        return self.safety_verified

    def to_record(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "profile": self.profile,
            "policy_fingerprint": self.policy_fingerprint,
            "models": list(self.models),
            "safety_verified": self.safety_verified,
            "read_only_verified": self.read_only_verified,
            "runtime": dict(self.capabilities.get("runtime") or {}),
            "referee": dict(self.capabilities.get("referee") or {}),
        }


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
        allow_remote: bool = False,
        allow_insecure_remote: bool = False,
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
        self.allow_remote = bool(allow_remote)
        self.allow_insecure_remote = bool(allow_insecure_remote)
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
        self._preflight_cache: dict[tuple[str, str, str], tuple[float, HermesRefereePreflight]] = {}

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

    async def preflight(self) -> HermesRefereePreflight:
        """Verify the selected profile is an actual no-tools referee.

        This is deliberately separate from ``health``: reachability proves
        only that a server answered, while the capabilities contract proves
        the runtime mode and effective model-visible tool surface.
        """
        _validate_endpoint_safety(
            self.endpoint,
            allow_remote=self.allow_remote,
            allow_insecure_remote=self.allow_insecure_remote,
        )
        policy_fingerprint = _policy_fingerprint()
        cache_key = (self.endpoint, self.profile, policy_fingerprint)
        cached = self._preflight_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _PREFLIGHT_TTL_SECONDS:
            return cached[1]

        models_response = await self._client.get(self._health_url)
        models_response.raise_for_status()
        models_payload = models_response.json()
        if not isinstance(models_payload, Mapping):
            raise HermesRefereeSafetyError("Hermes models response is not an object")
        model_rows = models_payload.get("data")
        if not isinstance(model_rows, list):
            raise HermesRefereeSafetyError("Hermes models response has no model list")
        models = tuple(
            str(row.get("id")) for row in model_rows if isinstance(row, Mapping) and row.get("id")
        )
        if not models:
            raise HermesRefereeSafetyError("Hermes referee profile advertises no models")

        capabilities_response = await self._client.get(self._capabilities_url)
        capabilities_response.raise_for_status()
        capabilities = capabilities_response.json()
        if not isinstance(capabilities, Mapping):
            raise HermesRefereeSafetyError("Hermes capabilities response is not an object")
        runtime = capabilities.get("runtime")
        referee = capabilities.get("referee")
        if not isinstance(runtime, Mapping) or not isinstance(referee, Mapping):
            raise HermesRefereeSafetyError("Hermes referee capability contract is missing")
        if runtime.get("mode") != "referee":
            raise HermesRefereeSafetyError("Hermes profile is not in referee mode")
        if runtime.get("tool_execution") != "disabled":
            raise HermesRefereeSafetyError("Hermes referee tool execution is not disabled")
        if referee.get("enabled") is not True:
            raise HermesRefereeSafetyError("Hermes referee mode is not enabled")
        if referee.get("policy_version") != _REFEREE_POLICY_VERSION:
            raise HermesRefereeSafetyError("Hermes referee policy version is unsupported")
        if referee.get("effective_tools") != []:
            raise HermesRefereeSafetyError("Hermes referee exposes model-visible tools")

        result = HermesRefereePreflight(
            endpoint=self.endpoint,
            profile=self.profile,
            policy_fingerprint=policy_fingerprint,
            models=models,
            capabilities=dict(capabilities),
        )
        self._preflight_cache[cache_key] = (time.monotonic(), result)
        return result

    async def aclose(self) -> None:
        """Close the adapter-owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    @property
    def _capabilities_url(self) -> str:
        return f"{self._api_base}/capabilities"


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


def _policy_fingerprint() -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "runtime_mode": "referee",
                "tool_execution": "disabled",
                "policy_version": _REFEREE_POLICY_VERSION,
                "effective_tools": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]


def _validate_endpoint_safety(
    endpoint: str,
    *,
    allow_remote: bool,
    allow_insecure_remote: bool,
) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HermesRefereeSafetyError("Hermes endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise HermesRefereeSafetyError("Hermes endpoint must not contain URL credentials")
    host = parsed.hostname.rstrip(".").casefold()
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if loopback:
        return
    if not allow_remote:
        raise HermesRefereeSafetyError("remote Hermes endpoint requires explicit allow_remote=true")
    if parsed.scheme != "https" and not allow_insecure_remote:
        raise HermesRefereeSafetyError(
            "remote Hermes endpoint requires HTTPS unless allow_insecure_remote=true"
        )
