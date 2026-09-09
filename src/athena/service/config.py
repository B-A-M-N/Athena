"""AthenaService configuration.

Defines :class:`AthenaConfig` — the dataclass that configures the application
composition root (:class:`~athena.service.service.AthenaService`). Every field
has a safe setup/inspection default; ``AthenaService(config=AthenaConfig())``
starts without a model provider and reports an explicit unconfigured state.
``AthenaService.in_memory()`` is the explicit deterministic test/demo factory.

Config layering (deterministic precedence, lowest to highest):

1. Built-in defaults (the dataclass defaults)
2. Global config file (``~/.config/athena/config.toml``)
3. Project config file (``.athena/config.toml`` in cwd or git root)
4. Named profile (``[profile.<name>]`` in either config file; selected via
   ``ATHENA_PROFILE`` env var or ``--profile`` CLI flag)
5. Environment variables (``ATHENA_*`` prefix)
6. Explicit CLI flags (highest precedence)
"""

from __future__ import annotations

import json
from enum import StrEnum
from importlib import import_module
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from athena.protocol.tasks import AutonomyLevel
from athena.protocol.policy import DEFAULT_PRINCIPAL_ID
from athena.policy.credentials import write_user_secret
from athena.memory.embeddings import DEFAULT_FASTEMBED_MODEL

try:
    tomllib = import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover - legacy/minimal Python builds
    tomllib = import_module("tomli")

__all__ = [
    "AthenaConfig",
    "HermesSupervisionMode",
    "HermesRefereeConfig",
    "ProviderConfig",
    "MCPConfig",
    "VoiceConfig",
    "DEFAULT_DB_PATH",
    "load_config",
    "merge_configs",
    "write_toml_atomic_private",
]


def DEFAULT_DB_PATH() -> str:
    import os
    from pathlib import Path

    home = Path(os.environ.get("ATHENA_HOME") or Path.home() / ".athena")
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "athena.db")


# ---------------------------------------------------------------------------
# Config file paths
# ---------------------------------------------------------------------------


def global_config_path() -> Path:
    """Return the global user config path (``~/.config/athena/config.toml``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "athena" / "config.toml"
    return Path.home() / ".config" / "athena" / "config.toml"


def project_config_paths(cwd: str | None = None) -> list[Path]:
    """Return candidate project config paths, in precedence order.

    Project config lives in ``.athena/config.toml``. We search upward from
    ``cwd`` (or the real cwd) to the filesystem root, returning every
    ``.athena/config.toml`` found, root-most FIRST and closest-to-cwd LAST so
    that loading them in order gives project-local files higher precedence.
    """
    base = Path(cwd or os.getcwd()).resolve()
    chain = [base, *_parent_chain(base)]
    return [parent / ".athena" / "config.toml" for parent in reversed(chain)]


def _parent_chain(p: Path) -> list[Path]:
    """Parents of ``p`` excluding ``p`` itself, from direct parent to root."""
    return list(p.parents)


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """A model provider to register at startup.

    ``kind`` selects the adapter: ``"fake"``, ``"openai"`` (OpenAI-compatible),
    or ``"anthropic"``. Remaining fields are passed to the adapter constructor.
    """

    kind: str = "fake"
    name: str = "fake"
    model: str = "fake-1"
    credential_id: str | None = None
    credential_ids: tuple[str, ...] = ()
    api_key: str | None = None
    base_url: str | None = None
    # Authentication is explicit route policy: ``none`` is appropriate for a
    # deliberately unauthenticated local endpoint; ``bearer``/``required``
    # require a configured credential. ``None`` lets the adapter choose its
    # conservative topology-based default.
    authentication: str | None = None
    # ``None`` uses the provider-profile default. Hosted OpenAI-compatible
    # routes default to automatic prefix caching; local presets default off.
    cache_mode: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    latency_class: str | None = None

    def to_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(self.extra)
        kwargs.setdefault("model", self.model)
        if self.credential_id is not None:
            kwargs.setdefault("credential_id", self.credential_id)
        if self.credential_ids:
            kwargs.setdefault("credential_ids", tuple(self.credential_ids))
        if self.api_key is not None:
            kwargs.setdefault("api_key", self.api_key)
        if self.base_url is not None:
            kwargs.setdefault("base_url", self.base_url)
        if self.authentication is not None:
            kwargs.setdefault("authentication", self.authentication)
        if self.latency_class is not None:
            kwargs.setdefault("latency_class", self.latency_class)
        return kwargs


@dataclass(frozen=True)
class MCPConfig:
    """A single MCP server to connect at startup (stdio or Streamable HTTP)."""

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    credential_id: str | None = None
    auth_scheme: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    secret_headers: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    denied_resources: tuple[str, ...] = ()
    allowed_prompts: tuple[str, ...] = ()
    denied_prompts: tuple[str, ...] = ()
    connect_timeout: float = 10.0
    required: bool = False


@dataclass(frozen=True)
class VoiceConfig:
    """Explicit provider routes and bounds for voice input/output.

    Voice stays opt-in because it can send audio to a remote provider and may
    incur separate provider charges. Providers are named registry entries;
    credentials remain owned by the normal provider/SecretManager path.
    """

    enabled: bool = False
    transcription_provider: str | None = None
    transcription_model: str = "whisper-1"
    synthesis_provider: str | None = None
    synthesis_model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    response_format: str = "mp3"
    max_input_bytes: int = 25 * 1024 * 1024
    max_output_bytes: int = 25 * 1024 * 1024
    max_text_chars: int = 12_000

    def __post_init__(self) -> None:
        for field_name in (
            "transcription_provider",
            "transcription_model",
            "synthesis_provider",
            "synthesis_model",
            "voice",
            "response_format",
        ):
            value = getattr(self, field_name)
            if value is not None and not str(value).strip():
                raise ValueError(f"voice.{field_name} cannot be empty")
        for field_name in ("max_input_bytes", "max_output_bytes", "max_text_chars"):
            value = int(getattr(self, field_name))
            if value <= 0:
                raise ValueError(f"voice.{field_name} must be positive")
            object.__setattr__(self, field_name, value)
        response_format = str(self.response_format).strip().lower()
        if response_format not in {"mp3", "mpeg", "wav", "opus", "aac", "flac", "pcm"}:
            raise ValueError("voice.response_format must be a supported audio format")
        object.__setattr__(self, "response_format", response_format)


class HermesSupervisionMode(StrEnum):
    """Operator-selected strength of the optional Hermes boundary."""

    OFF = "off"
    ADVISORY = "advisory"
    REQUIRED = "required"


@dataclass(frozen=True)
class HermesRefereeConfig:
    """Optional operator-configured Hermes Agent governance endpoint."""

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8643"
    profile: str = "athena-referee"
    timeout_seconds: float = 60.0
    credential_id: str | None = None
    # Remote Hermes endpoints require an explicit operator opt-in. Loopback
    # endpoints remain valid over HTTP for local deployments.
    allow_remote: bool = False
    allow_insecure_remote: bool = False
    managed: bool = False
    runtime_root: str | None = None
    # ``enabled`` controls the transport lifecycle.  Supervision policy is a
    # separate explicit choice so an optional referee cannot become a core
    # Athena dependency by accident.
    self_host_supervision: str | HermesSupervisionMode | None = None
    # Deprecated compatibility input for pre-policy config files.  It is
    # normalized to ``self_host_supervision`` and is not serialized.
    required_for_self_host: bool | None = None

    def __post_init__(self) -> None:
        raw_mode = self.self_host_supervision
        if raw_mode is None:
            if not self.enabled:
                mode = HermesSupervisionMode.OFF
            elif self.required_for_self_host is False:
                mode = HermesSupervisionMode.ADVISORY
            else:
                mode = HermesSupervisionMode.REQUIRED
        else:
            try:
                mode = HermesSupervisionMode(str(raw_mode).strip().lower())
            except ValueError as exc:
                valid = ", ".join(item.value for item in HermesSupervisionMode)
                raise ValueError(
                    f"hermes_referee.self_host_supervision must be one of: {valid}"
                ) from exc
        object.__setattr__(self, "self_host_supervision", mode.value)
        object.__setattr__(self, "required_for_self_host", mode is HermesSupervisionMode.REQUIRED)

    @property
    def supervision_mode(self) -> HermesSupervisionMode:
        """Return the normalized self-host supervision policy."""
        return HermesSupervisionMode(str(self.self_host_supervision))

    @property
    def transport_enabled(self) -> bool:
        """Whether the configured Hermes transport should be constructed."""
        return self.enabled


# ---------------------------------------------------------------------------
# Main config dataclass
# ---------------------------------------------------------------------------


@dataclass
class AthenaConfig:
    """Configuration for the :class:`AthenaService` application root."""

    db_path: str | None = None
    workspace_root: str | None = None
    autonomy: str | AutonomyLevel = AutonomyLevel.SUPERVISED
    artifact_root: str | None = None
    skills_paths: tuple[str, ...] = ()
    providers: tuple[ProviderConfig, ...] = ()
    # Operator-owned execution backend profiles. Values are declarative
    # records; model/task input can only reference a profile name.
    execution_backends: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    mcp_servers: tuple[MCPConfig, ...] = ()
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    local_runtime_supervisor: bool = False
    hermes_referee: HermesRefereeConfig = field(default_factory=HermesRefereeConfig)
    context_window: int = 128_000
    reserve_output: int = 4096
    # Stable cache namespace for one authenticated user/tenant. Keep this
    # distinct between principals when one service process serves multiple
    # users; the value is hashed before it reaches a provider.
    cache_namespace: str = DEFAULT_PRINCIPAL_ID
    # Optional process-injected embedding provider. It is intentionally not
    # serialized to TOML; deployments may replace the default FastEmbed
    # provider with a concrete local/remote adapter at composition time.
    memory_embedding_provider: Any | None = None
    # The default provider is lazy and only loads/downloads this model when a
    # semantic index or query is requested.
    memory_embedding_model: str = DEFAULT_FASTEMBED_MODEL
    memory_embedding_cache_dir: str | None = None
    # ``max_parallel_tasks`` is the canonical concurrency setting.  The
    # legacy constructor/key remains accepted so old configs migrate without
    # silently changing their limit.
    worker_max_parallel: int | None = None
    max_parallel_tasks: int = 4
    # Worker slot release (P1-17): how long a parked wait (WAITING_INPUT,
    # WAITING_APPROVAL) may hold its worker coroutine before the run returns
    # and the slot frees. The durable continuation (open question / pending
    # approval) relaunches the task on the operator's action.
    parked_slot_wait_s: float = 300.0
    parked_resource_retention_mode: str = "release"
    parked_resource_retain_seconds: float = 300.0
    # Worker task lease (P0-1): how long a claimed task's lease runs before it
    # could be reclaimed, and the heartbeat cadence divisor. The heartbeat
    # renews at lease_duration/divisor, so a live worker never lets a healthy
    # lease expire; only a genuinely dead process's leases are reclaimable.
    worker_lease_duration_seconds: float = 300.0
    worker_lease_renewal_divisor: float = 3.0
    scheduler_interval_seconds: float = 1.0
    scheduler_max_concurrent: int = 0
    profile: str | None = None
    # Deployment capability contract. Entries are native capability ids or
    # explicit references such as ``mcp:browser``, ``skill:linting``,
    # ``pack:repo-tools``, and ``delegate:reviewer``. A named capability
    # profile contributes its own required entries.
    required_capabilities: tuple[str, ...] = ()
    capability_profile: str | None = None
    capability_profiles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Role-divided models (Hermes-style): role name -> {"allowed": [...],
    # "privacy": "...", "max_cost_usd": "0.01"}. Roles without an entry fall
    # back to the user's global/primary choice.
    model_roles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # External research acquisition is deny-by-default until the operator
    # explicitly allowlists source domains. Artifact snapshots remain local.
    research_allowed_domains: tuple[str, ...] = ()
    research_denied_domains: tuple[str, ...] = ()
    research_allow_private_network: bool = False
    # Optional first-party JSON discovery endpoint. Discovery returns
    # untrusted candidate metadata only; source acquisition still goes through
    # ResearchCapability's immutable snapshot + SSRF policy path.
    research_discovery_endpoint: str | None = None
    # Multiple first-party discovery indexes may be queried and fused. The
    # singular field remains a compatibility alias for older config files.
    research_discovery_endpoints: tuple[str, ...] = ()
    research_discovery_timeout: float = 10.0
    # Credential names for optional first-party search adapters. Raw API keys
    # stay in SecretManager; these fields are only opaque credential IDs.
    research_brave_api_key_credential: str | None = None
    research_tavily_api_key_credential: str | None = None
    # Structured browser automation (P1-28): a zero-arg callable returning a
    # BrowserDriver (Playwright-shaped). ``browser_enabled`` opts into the
    # first-party Playwright launcher for file/TOML configuration; the
    # injectable factory remains available for remote and test drivers.
    browser_enabled: bool = False
    browser_engine: str = "chromium"
    browser_headless: bool = True
    browser_launch_args: tuple[str, ...] = ()
    browser_executable_path: str | None = None
    browser_channel: str | None = None
    browser_cdp_endpoint: str | None = None
    browser_session_scope: str = "task"
    browser_timeout_ms: int = 12_000
    browser_viewport: tuple[int, int] | None = (1024, 768)
    browser_driver_factory: Any | None = None
    # Terminal UI: which mascot/buddy the surfaces show (a registered
    # character name, or "off" to hide the mascot column). ``mascots``
    # registers user-defined characters ([mascots.<name>] in TOML) with
    # "label" plus a "frames" table of state -> art string(s).
    mascot: str | None = None
    mascots: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Terminal presentation: auto/glass/ansi/plain. Glass requires a
    # confirmed Kitty graphics transport; otherwise it falls back to ANSI.
    display: str = "auto"
    animations: bool = True
    reduced_motion: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.worker_max_parallel is not None:
            self.max_parallel_tasks = int(self.worker_max_parallel)
        self.max_parallel_tasks = max(1, int(self.max_parallel_tasks))
        # Keep the legacy read surface truthful after a canonical setting is
        # loaded; it is an alias, not a second concurrency authority.
        self.worker_max_parallel = self.max_parallel_tasks
        self.parked_slot_wait_s = max(0.0, float(self.parked_slot_wait_s))
        self.parked_resource_retention_mode = (
            str(self.parked_resource_retention_mode or "release").strip().lower()
        )
        if self.parked_resource_retention_mode not in {
            "retain",
            "checkpoint_and_release",
            "release",
        }:
            raise ValueError(
                "parked_resource_retention_mode must be retain, checkpoint_and_release, or release"
            )
        self.parked_resource_retain_seconds = max(0.0, float(self.parked_resource_retain_seconds))
        self.worker_lease_duration_seconds = max(1.0, float(self.worker_lease_duration_seconds))
        self.worker_lease_renewal_divisor = max(1.0, float(self.worker_lease_renewal_divisor))
        self.research_discovery_timeout = max(0.1, float(self.research_discovery_timeout))
        self.required_capabilities = _normalize_capability_ids(self.required_capabilities)
        profiles = _parse_capability_profiles(self.capability_profiles)
        profiles = {
            profile_name: values
            for name, values in profiles.items()
            if (profile_name := str(name).strip())
        }
        self.capability_profiles = profiles
        self.execution_backends = {
            str(name): dict(value)
            for name, value in dict(self.execution_backends or {}).items()
            if isinstance(value, Mapping)
        }
        if self.capability_profile is not None:
            selected = str(self.capability_profile).strip()
            self.capability_profile = selected or None
            if selected and selected not in profiles:
                raise ValueError(f"unknown capability_profile: {selected}")
        self.memory_embedding_model = str(
            self.memory_embedding_model or DEFAULT_FASTEMBED_MODEL
        ).strip()
        if not self.memory_embedding_model:
            self.memory_embedding_model = DEFAULT_FASTEMBED_MODEL
        if self.memory_embedding_cache_dir is not None:
            cache_dir = str(self.memory_embedding_cache_dir).strip()
            self.memory_embedding_cache_dir = cache_dir or None
        endpoints = tuple(
            str(value).strip() for value in self.research_discovery_endpoints if str(value).strip()
        )
        if self.research_discovery_endpoint:
            endpoint = str(self.research_discovery_endpoint).strip()
            if endpoint and endpoint not in endpoints:
                endpoints = (endpoint, *endpoints)
        self.research_discovery_endpoints = endpoints
        if self.research_discovery_endpoint is None and len(endpoints) == 1:
            self.research_discovery_endpoint = endpoints[0]
        engine = str(self.browser_engine or "chromium").strip().lower()
        if engine not in {"chromium", "firefox", "webkit"}:
            raise ValueError("browser_engine must be chromium, firefox, or webkit")
        self.browser_engine = engine
        self.browser_launch_args = tuple(str(arg) for arg in self.browser_launch_args)
        scope = str(self.browser_session_scope or "task").strip().lower()
        if scope not in {"task", "session"}:
            raise ValueError("browser_session_scope must be task or session")
        self.browser_session_scope = scope
        self.browser_timeout_ms = max(1, int(self.browser_timeout_ms))
        if self.browser_viewport is not None:
            width, height = (int(value) for value in self.browser_viewport)
            if width <= 0 or height <= 0:
                raise ValueError("browser_viewport dimensions must be positive")
            self.browser_viewport = (width, height)

    @property
    def autonomy_level(self) -> AutonomyLevel:
        if isinstance(self.autonomy, AutonomyLevel):
            return self.autonomy
        return AutonomyLevel(self.autonomy or AutonomyLevel.SUPERVISED.value)

    @property
    def effective_required_capabilities(self) -> tuple[str, ...]:
        """Return direct and selected-profile requirements without duplicates."""
        values = list(self.required_capabilities)
        if self.capability_profile:
            values.extend(self.capability_profiles.get(self.capability_profile, ()))
        # A configured required MCP transport is itself a readiness
        # prerequisite; tool discovery is not a substitute for transport
        # health and must not silently downgrade this contract.
        values.extend(f"mcp:{server.name}" for server in self.mcp_servers if server.required)
        return _normalize_capability_ids(values)


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` onto ``base``. Later (override) wins.

    Nested dicts are merged recursively; all other values (including lists)
    are replaced wholesale.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge_configs(*configs: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge multiple config dicts. Later configs override earlier ones."""
    result: dict[str, Any] = {}
    for c in configs:
        result = deep_merge(result, c)
    return result


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def load_toml_file(path: str | Path) -> dict[str, Any]:
    """Load a TOML config file. Returns ``{}`` if the file does not exist."""
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def _normalize_capability_ids(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = values.split(",")
    result: list[str] = []
    for value in values or ():
        item = str(value).strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)


def _parse_capability_profiles(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return {}
    profiles: dict[str, tuple[str, ...]] = {}
    for name, raw in value.items():
        if isinstance(raw, Mapping):
            raw = raw.get("required_capabilities", raw.get("required", ()))
        profiles[str(name)] = _normalize_capability_ids(raw)
    return profiles


def _parse_provider(data: dict[str, Any]) -> ProviderConfig:
    """Parse a provider entry from a config dict."""
    extra = dict(data)
    for key in (
        "kind",
        "name",
        "model",
        "credential_id",
        "credential_ids",
        "api_key",
        "base_url",
        "authentication",
        "cache_mode",
        "latency_class",
    ):
        extra.pop(key, None)
    args = data.get("args")
    if isinstance(args, list):
        args = tuple(args)
    return ProviderConfig(
        kind=data.get("kind", "fake"),
        name=data.get("name", "fake"),
        model=data.get("model", "fake-1"),
        credential_id=data.get("credential_id"),
        credential_ids=tuple(str(item) for item in data.get("credential_ids") or ()),
        api_key=data.get("api_key"),
        base_url=data.get("base_url"),
        authentication=data.get("authentication"),
        cache_mode=data.get("cache_mode"),
        latency_class=data.get("latency_class"),
        extra=extra,
    )


def _parse_mcp(data: dict[str, Any]) -> MCPConfig:
    """Parse an MCP server entry from a config dict."""
    args = data.get("args")
    if isinstance(args, list):
        args = tuple(args)
    elif isinstance(args, str):
        args = (args,)
    return MCPConfig(
        name=data["name"],
        command=data.get("command"),
        args=args or (),
        url=data.get("url"),
        credential_id=data.get("credential_id"),
        auth_scheme=data.get("auth_scheme"),
        env=data.get("env") or {},
        secret_env=data.get("secret_env") or {},
        headers=data.get("headers") or {},
        secret_headers=data.get("secret_headers") or {},
        allowed_tools=tuple(str(item) for item in data.get("allowed_tools") or ()),
        denied_tools=tuple(str(item) for item in data.get("denied_tools") or ()),
        allowed_resources=tuple(str(item) for item in data.get("allowed_resources") or ()),
        denied_resources=tuple(str(item) for item in data.get("denied_resources") or ()),
        allowed_prompts=tuple(str(item) for item in data.get("allowed_prompts") or ()),
        denied_prompts=tuple(str(item) for item in data.get("denied_prompts") or ()),
        connect_timeout=float(data.get("connect_timeout", 10.0)),
        required=bool(data.get("required", False)),
    )


def _parse_hermes_referee(data: Any) -> HermesRefereeConfig:
    """Parse the bounded external-referee settings without accepting secrets."""
    if not isinstance(data, Mapping):
        return HermesRefereeConfig()
    endpoint = str(data.get("endpoint", HermesRefereeConfig.endpoint)).strip()
    profile = str(data.get("profile", HermesRefereeConfig.profile)).strip()
    timeout = float(data.get("timeout_seconds", HermesRefereeConfig.timeout_seconds))
    if not endpoint:
        raise ValueError("hermes_referee.endpoint cannot be empty")
    if not profile:
        raise ValueError("hermes_referee.profile cannot be empty")
    if timeout <= 0:
        raise ValueError("hermes_referee.timeout_seconds must be positive")
    credential_id = data.get("credential_id")
    raw_mode = data.get("self_host_supervision")
    legacy_required = data.get("required_for_self_host")
    if raw_mode is None:
        # Preserve the old configuration's secure behavior while making the
        # new policy explicit in the normalized object.
        enabled = bool(data.get("enabled", False))
        mode = None
        required_for_self_host = bool(legacy_required) if legacy_required is not None else None
    else:
        mode = str(raw_mode).strip().lower()
        # Policy selection must not provision or connect a transport.  The
        # lifecycle is enabled only by an explicit setting (or by the
        # manager's successful setup/repair transaction).
        enabled = bool(data.get("enabled", False))
        required_for_self_host = None
    return HermesRefereeConfig(
        enabled=enabled,
        endpoint=endpoint,
        profile=profile,
        timeout_seconds=timeout,
        credential_id=str(credential_id) if credential_id else None,
        allow_remote=bool(data.get("allow_remote", False)),
        allow_insecure_remote=bool(data.get("allow_insecure_remote", False)),
        managed=bool(data.get("managed", False)),
        runtime_root=str(data.get("runtime_root")) if data.get("runtime_root") else None,
        self_host_supervision=mode,
        required_for_self_host=required_for_self_host,
    )


def _expand_user(value: str) -> str:
    """Expand ``~`` and env vars in a path string."""
    return os.path.expandvars(os.path.expanduser(value))


def config_to_dict(config: AthenaConfig) -> dict[str, Any]:
    """Convert an ``AthenaConfig`` to a plain dict for serialization."""
    d: dict[str, Any] = {}
    if config.db_path is not None:
        d["db_path"] = config.db_path
    if config.workspace_root is not None:
        d["workspace_root"] = config.workspace_root
    d["autonomy"] = (
        config.autonomy.value if isinstance(config.autonomy, AutonomyLevel) else config.autonomy
    )
    if config.artifact_root is not None:
        d["artifact_root"] = config.artifact_root
    if config.skills_paths:
        d["skills_paths"] = list(config.skills_paths)
    if config.context_window != 128_000:
        d["context_window"] = config.context_window
    if config.reserve_output != 4096:
        d["reserve_output"] = config.reserve_output
    if config.cache_namespace != DEFAULT_PRINCIPAL_ID:
        d["cache_namespace"] = config.cache_namespace
    if config.memory_embedding_model != DEFAULT_FASTEMBED_MODEL:
        d["memory_embedding_model"] = config.memory_embedding_model
    if config.memory_embedding_cache_dir is not None:
        d["memory_embedding_cache_dir"] = config.memory_embedding_cache_dir
    if config.max_parallel_tasks != 4:
        d["max_parallel_tasks"] = config.max_parallel_tasks
    if config.parked_slot_wait_s != 300.0:
        d["parked_slot_wait_s"] = config.parked_slot_wait_s
    if config.parked_resource_retention_mode != "release":
        d["parked_resource_retention_mode"] = config.parked_resource_retention_mode
    if config.parked_resource_retain_seconds != 300.0:
        d["parked_resource_retain_seconds"] = config.parked_resource_retain_seconds
    if config.worker_lease_duration_seconds != 300.0:
        d["worker_lease_duration_seconds"] = config.worker_lease_duration_seconds
    if config.worker_lease_renewal_divisor != 3.0:
        d["worker_lease_renewal_divisor"] = config.worker_lease_renewal_divisor
    if config.scheduler_interval_seconds != 1.0:
        d["scheduler_interval_seconds"] = config.scheduler_interval_seconds
    if config.scheduler_max_concurrent != 0:
        d["scheduler_max_concurrent"] = config.scheduler_max_concurrent
    if config.profile is not None:
        d["profile"] = config.profile
    if config.required_capabilities:
        d["required_capabilities"] = list(config.required_capabilities)
    if config.capability_profile is not None:
        d["capability_profile"] = config.capability_profile
    if config.capability_profiles:
        d["capability_profiles"] = {
            name: {"required_capabilities": list(values)}
            for name, values in config.capability_profiles.items()
        }
    if config.providers:
        d["providers"] = [
            {
                key: value
                for key, value in {
                    "kind": p.kind,
                    "name": p.name,
                    "model": p.model,
                    "credential_id": p.credential_id,
                    "credential_ids": list(p.credential_ids),
                    "base_url": p.base_url,
                    "authentication": p.authentication,
                    "cache_mode": p.cache_mode,
                    "latency_class": p.latency_class,
                    **{
                        key: value
                        for key, value in p.extra.items()
                        if str(key).casefold() not in {"api_key", "apikey", "access_token"}
                    },
                }.items()
                if value is not None
            }
            for p in config.providers
        ]
    if config.local_runtime_supervisor:
        d["local_runtime_supervisor"] = True
    if config.execution_backends:
        d["execution_backends"] = {
            str(name): dict(value) for name, value in config.execution_backends.items()
        }
    if config.mcp_servers:
        d["mcp_servers"] = [
            {
                key: value
                for key, value in {
                    "name": m.name,
                    "command": m.command,
                    "args": list(m.args),
                    "url": m.url,
                    "credential_id": m.credential_id,
                    "auth_scheme": m.auth_scheme,
                    "env": dict(m.env),
                    "secret_env": dict(m.secret_env),
                    "headers": dict(m.headers),
                    "secret_headers": dict(m.secret_headers),
                    "allowed_tools": list(m.allowed_tools),
                    "denied_tools": list(m.denied_tools),
                    "allowed_resources": list(m.allowed_resources),
                    "denied_resources": list(m.denied_resources),
                    "allowed_prompts": list(m.allowed_prompts),
                    "denied_prompts": list(m.denied_prompts),
                    "connect_timeout": m.connect_timeout,
                    "required": m.required,
                }.items()
                if value is not None
            }
            for m in config.mcp_servers
        ]
    if config.voice != VoiceConfig():
        d["voice"] = {
            key: value
            for key, value in {
                "enabled": config.voice.enabled,
                "transcription_provider": config.voice.transcription_provider,
                "transcription_model": config.voice.transcription_model,
                "synthesis_provider": config.voice.synthesis_provider,
                "synthesis_model": config.voice.synthesis_model,
                "voice": config.voice.voice,
                "response_format": config.voice.response_format,
                "max_input_bytes": config.voice.max_input_bytes,
                "max_output_bytes": config.voice.max_output_bytes,
                "max_text_chars": config.voice.max_text_chars,
            }.items()
            if value is not None
        }
    if config.hermes_referee != HermesRefereeConfig():
        d["hermes_referee"] = {
            "enabled": config.hermes_referee.enabled,
            "endpoint": config.hermes_referee.endpoint,
            "profile": config.hermes_referee.profile,
            "timeout_seconds": config.hermes_referee.timeout_seconds,
            "allow_remote": config.hermes_referee.allow_remote,
            "allow_insecure_remote": config.hermes_referee.allow_insecure_remote,
            "managed": config.hermes_referee.managed,
            "self_host_supervision": config.hermes_referee.supervision_mode.value,
            **(
                {"credential_id": config.hermes_referee.credential_id}
                if config.hermes_referee.credential_id
                else {}
            ),
            **(
                {"runtime_root": config.hermes_referee.runtime_root}
                if config.hermes_referee.runtime_root
                else {}
            ),
        }
    if config.model_roles:
        d["model_roles"] = {k: dict(v) for k, v in config.model_roles.items()}
    if config.research_allowed_domains:
        d["research_allowed_domains"] = list(config.research_allowed_domains)
    if config.research_denied_domains:
        d["research_denied_domains"] = list(config.research_denied_domains)
    if config.research_allow_private_network:
        d["research_allow_private_network"] = True
    if len(config.research_discovery_endpoints) > 1:
        d["research_discovery_endpoints"] = list(config.research_discovery_endpoints)
    elif config.research_discovery_endpoint is not None:
        d["research_discovery_endpoint"] = config.research_discovery_endpoint
    if config.research_discovery_timeout != 10.0:
        d["research_discovery_timeout"] = config.research_discovery_timeout
    if config.research_brave_api_key_credential is not None:
        d["research_brave_api_key_credential"] = config.research_brave_api_key_credential
    if config.research_tavily_api_key_credential is not None:
        d["research_tavily_api_key_credential"] = config.research_tavily_api_key_credential
    if config.browser_enabled:
        d["browser_enabled"] = True
    if config.browser_engine != "chromium":
        d["browser_engine"] = config.browser_engine
    if not config.browser_headless:
        d["browser_headless"] = False
    if config.browser_launch_args:
        d["browser_launch_args"] = list(config.browser_launch_args)
    if config.browser_executable_path is not None:
        d["browser_executable_path"] = config.browser_executable_path
    if config.browser_channel is not None:
        d["browser_channel"] = config.browser_channel
    if config.browser_cdp_endpoint is not None:
        d["browser_cdp_endpoint"] = config.browser_cdp_endpoint
    if config.browser_session_scope != "task":
        d["browser_session_scope"] = config.browser_session_scope
    if config.browser_timeout_ms != 12_000:
        d["browser_timeout_ms"] = config.browser_timeout_ms
    if config.browser_viewport != (1024, 768):
        d["browser_viewport"] = list(config.browser_viewport) if config.browser_viewport else None
    if config.mascot is not None:
        d["mascot"] = config.mascot
    if config.mascots:
        d["mascots"] = {k: dict(v) for k, v in config.mascots.items()}
    if config.display != "auto":
        d["display"] = config.display
    if not config.animations:
        d["animations"] = False
    if config.reduced_motion:
        d["reduced_motion"] = True
    if config.metadata:
        d["metadata"] = dict(config.metadata)
    return d


def config_from_dict(data: dict[str, Any]) -> AthenaConfig:
    """Build an ``AthenaConfig`` from a plain dict (e.g. parsed from TOML)."""
    # Handle nested sub-configs
    providers = tuple(_parse_provider(p) for p in data.get("providers", ()) if isinstance(p, dict))
    mcp_servers = tuple(_parse_mcp(m) for m in data.get("mcp_servers", ()) if isinstance(m, dict))
    voice_data = data.get("voice")
    if not isinstance(voice_data, Mapping):
        voice_data = {}
    voice = VoiceConfig(
        enabled=bool(voice_data.get("enabled", False)),
        transcription_provider=(
            str(voice_data["transcription_provider"])
            if voice_data.get("transcription_provider")
            else None
        ),
        transcription_model=str(voice_data.get("transcription_model", "whisper-1")),
        synthesis_provider=(
            str(voice_data["synthesis_provider"]) if voice_data.get("synthesis_provider") else None
        ),
        synthesis_model=str(voice_data.get("synthesis_model", "gpt-4o-mini-tts")),
        voice=str(voice_data.get("voice", "alloy")),
        response_format=str(voice_data.get("response_format", "mp3")),
        max_input_bytes=int(voice_data.get("max_input_bytes", 25 * 1024 * 1024)),
        max_output_bytes=int(voice_data.get("max_output_bytes", 25 * 1024 * 1024)),
        max_text_chars=int(voice_data.get("max_text_chars", 12_000)),
    )
    # skills_paths may be a list in TOML
    skills = data.get("skills_paths", ())
    if isinstance(skills, list):
        skills = tuple(skills)
    elif isinstance(skills, str):
        skills = (skills,)

    def _domains(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return tuple(str(v).strip() for v in (value or ()) if str(v).strip())

    def _endpoints(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return tuple(str(v).strip() for v in (value or ()) if str(v).strip())

    discovery_endpoints = _endpoints(data.get("research_discovery_endpoints"))
    legacy_discovery_endpoint = (
        str(data["research_discovery_endpoint"]).strip()
        if data.get("research_discovery_endpoint")
        else None
    )

    display = str(data.get("display", "auto")).strip().lower()
    if display not in {"auto", "glass", "ansi", "plain"}:
        display = "auto"
    browser_args = data.get("browser_launch_args", ())
    if isinstance(browser_args, str):
        browser_args = (browser_args,)
    else:
        browser_args = tuple(str(value) for value in (browser_args or ()))
    viewport = data.get("browser_viewport", (1024, 768))
    if viewport is not None:
        viewport = tuple(int(value) for value in viewport)

    return AthenaConfig(
        db_path=data.get("db_path"),
        workspace_root=data.get("workspace_root"),
        autonomy=data.get("autonomy", AutonomyLevel.SUPERVISED),
        artifact_root=data.get("artifact_root"),
        skills_paths=skills,
        providers=providers,
        execution_backends=dict(data.get("execution_backends") or {}),
        mcp_servers=mcp_servers,
        voice=voice,
        local_runtime_supervisor=bool(data.get("local_runtime_supervisor", False)),
        hermes_referee=_parse_hermes_referee(data.get("hermes_referee")),
        context_window=int(data.get("context_window", 128_000)),
        reserve_output=int(data.get("reserve_output", 4096)),
        cache_namespace=str(
            data.get("cache_namespace", DEFAULT_PRINCIPAL_ID) or DEFAULT_PRINCIPAL_ID
        ).strip()
        or DEFAULT_PRINCIPAL_ID,
        memory_embedding_model=str(
            data.get("memory_embedding_model", DEFAULT_FASTEMBED_MODEL) or DEFAULT_FASTEMBED_MODEL
        ).strip()
        or DEFAULT_FASTEMBED_MODEL,
        memory_embedding_cache_dir=(
            str(data["memory_embedding_cache_dir"]).strip()
            if data.get("memory_embedding_cache_dir")
            else None
        ),
        max_parallel_tasks=int(data.get("max_parallel_tasks", data.get("worker_max_parallel", 4))),
        parked_slot_wait_s=float(data.get("parked_slot_wait_s", 300.0)),
        parked_resource_retention_mode=str(data.get("parked_resource_retention_mode", "release")),
        parked_resource_retain_seconds=float(data.get("parked_resource_retain_seconds", 300.0)),
        worker_lease_duration_seconds=float(data.get("worker_lease_duration_seconds", 300.0)),
        worker_lease_renewal_divisor=float(data.get("worker_lease_renewal_divisor", 3.0)),
        scheduler_interval_seconds=float(data.get("scheduler_interval_seconds", 1.0)),
        scheduler_max_concurrent=int(data.get("scheduler_max_concurrent", 0)),
        profile=data.get("profile"),
        required_capabilities=_normalize_capability_ids(data.get("required_capabilities")),
        capability_profile=(
            str(data["capability_profile"]).strip() if data.get("capability_profile") else None
        ),
        capability_profiles=_parse_capability_profiles(data.get("capability_profiles")),
        model_roles=dict(data.get("model_roles") or {}),
        research_allowed_domains=_domains(data.get("research_allowed_domains")),
        research_denied_domains=_domains(data.get("research_denied_domains")),
        research_allow_private_network=bool(data.get("research_allow_private_network", False)),
        research_discovery_endpoint=legacy_discovery_endpoint,
        research_discovery_endpoints=discovery_endpoints,
        research_discovery_timeout=float(data.get("research_discovery_timeout", 10.0)),
        research_brave_api_key_credential=(
            str(
                data.get(
                    "research_brave_api_key_credential",
                    data.get("research_brave_credential"),
                )
            ).strip()
            if data.get("research_brave_api_key_credential", data.get("research_brave_credential"))
            else None
        ),
        research_tavily_api_key_credential=(
            str(
                data.get(
                    "research_tavily_api_key_credential",
                    data.get("research_tavily_credential"),
                )
            ).strip()
            if data.get(
                "research_tavily_api_key_credential", data.get("research_tavily_credential")
            )
            else None
        ),
        browser_enabled=bool(data.get("browser_enabled", False)),
        browser_engine=str(data.get("browser_engine", "chromium") or "chromium"),
        browser_headless=bool(data.get("browser_headless", True)),
        browser_launch_args=browser_args,
        browser_executable_path=(
            str(data["browser_executable_path"]) if data.get("browser_executable_path") else None
        ),
        browser_channel=str(data["browser_channel"]) if data.get("browser_channel") else None,
        browser_cdp_endpoint=(
            str(data["browser_cdp_endpoint"]) if data.get("browser_cdp_endpoint") else None
        ),
        browser_session_scope=str(data.get("browser_session_scope", "task")),
        browser_timeout_ms=int(data.get("browser_timeout_ms", 12_000)),
        browser_viewport=viewport if "browser_viewport" in data else (1024, 768),
        mascot=data.get("mascot"),
        mascots={
            str(k): dict(v) for k, v in (data.get("mascots") or {}).items() if isinstance(v, dict)
        },
        display=display,
        animations=bool(data.get("animations", True)),
        reduced_motion=bool(data.get("reduced_motion", False)),
        metadata=data.get("metadata") or {},
    )


# ---------------------------------------------------------------------------
# Environment variable parsing
# ---------------------------------------------------------------------------


def _env_map() -> dict[str, Any]:
    """Parse ``ATHENA_*`` environment variables into a config dict.

    Supported variables:
        ATHENA_DB_PATH, ATHENA_WORKSPACE, ATHENA_AUTONOMY,
        ATHENA_ARTIFACT_ROOT, ATHENA_CONTEXT_WINDOW,
        ATHENA_CACHE_NAMESPACE, ATHENA_MEMORY_EMBEDDING_MODEL,
        ATHENA_MEMORY_EMBEDDING_CACHE_DIR, ATHENA_WORKER_MAX_PARALLEL,
        ATHENA_SCHEDULER_INTERVAL_SECONDS,
        ATHENA_SCHEDULER_MAX_CONCURRENT, ATHENA_PROFILE,
        ATHENA_CAPABILITY_PROFILE, ATHENA_REQUIRED_CAPABILITIES,
        ATHENA_SKILLS_PATHS (comma-separated), ATHENA_MASCOT,
        ATHENA_DISPLAY, ATHENA_ANIMATIONS, ATHENA_REDUCED_MOTION,
        ATHENA_VOICE_ENABLED, ATHENA_VOICE_TRANSCRIPTION_PROVIDER,
        ATHENA_VOICE_SYNTHESIS_PROVIDER,
        ATHENA_LOCAL_RUNTIME_SUPERVISOR
    """
    result: dict[str, Any] = {}
    env_map: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "ATHENA_DB_PATH": ("db_path", str),
        "ATHENA_DB": ("db_path", str),
        "ATHENA_WORKSPACE": ("workspace_root", str),
        "ATHENA_WORKSPACE_PATH": ("workspace_root", str),
        "ATHENA_AUTONOMY": ("autonomy", str),
        "ATHENA_LOCAL_RUNTIME_SUPERVISOR": (
            "local_runtime_supervisor",
            lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"},
        ),
        "ATHENA_ARTIFACT_ROOT": ("artifact_root", str),
        "ATHENA_CONTEXT_WINDOW": ("context_window", int),
        "ATHENA_RESERVE_OUTPUT": ("reserve_output", int),
        "ATHENA_CACHE_NAMESPACE": ("cache_namespace", str),
        "ATHENA_MEMORY_EMBEDDING_MODEL": ("memory_embedding_model", str),
        "ATHENA_MEMORY_EMBEDDING_CACHE_DIR": ("memory_embedding_cache_dir", str),
        "ATHENA_MAX_PARALLEL_TASKS": ("max_parallel_tasks", int),
        "ATHENA_PARKED_SLOT_WAIT_S": ("parked_slot_wait_s", float),
        "ATHENA_PARKED_RESOURCE_RETENTION_MODE": (
            "parked_resource_retention_mode",
            str,
        ),
        "ATHENA_PARKED_RESOURCE_RETAIN_SECONDS": (
            "parked_resource_retain_seconds",
            float,
        ),
        "ATHENA_WORKER_LEASE_DURATION_SECONDS": ("worker_lease_duration_seconds", float),
        "ATHENA_WORKER_LEASE_RENEWAL_DIVISOR": ("worker_lease_renewal_divisor", float),
        # Deprecated alias; canonical serialization always writes
        # max_parallel_tasks.
        "ATHENA_WORKER_MAX_PARALLEL": ("max_parallel_tasks", int),
        "ATHENA_SCHEDULER_INTERVAL_SECONDS": ("scheduler_interval_seconds", float),
        "ATHENA_SCHEDULER_MAX_CONCURRENT": ("scheduler_max_concurrent", int),
        "ATHENA_PROFILE": ("profile", str),
        "ATHENA_CAPABILITY_PROFILE": ("capability_profile", str),
        "ATHENA_REQUIRED_CAPABILITIES": (
            "required_capabilities",
            lambda v: _normalize_capability_ids(v),
        ),
        "ATHENA_MASCOT": ("mascot", str),
        "ATHENA_DISPLAY": ("display", str),
        "ATHENA_ANIMATIONS": (
            "animations",
            lambda v: str(v).strip().lower() not in {"0", "false", "no", "off"},
        ),
        "ATHENA_REDUCED_MOTION": (
            "reduced_motion",
            lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"},
        ),
        "ATHENA_MODEL_ROLES": (
            "model_roles",
            lambda v: json.loads(v) if isinstance(v, str) else v,
        ),
        "ATHENA_SKILLS_PATHS": (
            "skills_paths",
            lambda v: tuple(p.strip() for p in v.split(",") if p.strip()),
        ),
        "ATHENA_RESEARCH_ALLOWED_DOMAINS": (
            "research_allowed_domains",
            lambda v: tuple(p.strip() for p in v.split(",") if p.strip()),
        ),
        "ATHENA_RESEARCH_DENIED_DOMAINS": (
            "research_denied_domains",
            lambda v: tuple(p.strip() for p in v.split(",") if p.strip()),
        ),
        "ATHENA_RESEARCH_ALLOW_PRIVATE_NETWORK": (
            "research_allow_private_network",
            lambda v: str(v).strip().lower() in {"1", "true", "yes"},
        ),
        "ATHENA_RESEARCH_DISCOVERY_ENDPOINT": ("research_discovery_endpoint", str),
        "ATHENA_RESEARCH_DISCOVERY_ENDPOINTS": (
            "research_discovery_endpoints",
            lambda v: tuple(p.strip() for p in v.split(",") if p.strip()),
        ),
        "ATHENA_RESEARCH_DISCOVERY_TIMEOUT": ("research_discovery_timeout", float),
        "ATHENA_RESEARCH_BRAVE_API_KEY_CREDENTIAL": (
            "research_brave_api_key_credential",
            str,
        ),
        "ATHENA_RESEARCH_TAVILY_API_KEY_CREDENTIAL": (
            "research_tavily_api_key_credential",
            str,
        ),
    }
    for env_name, (key, cast) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            try:
                result[key] = cast(value)
            except (ValueError, TypeError):
                pass
    hermes: dict[str, Any] = {}
    hermes_env: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
        (
            "ATHENA_HERMES_REFEREE_ENABLED",
            "enabled",
            lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"},
        ),
        ("ATHENA_HERMES_REFEREE_ENDPOINT", "endpoint", str),
        ("ATHENA_HERMES_REFEREE_PROFILE", "profile", str),
        ("ATHENA_HERMES_REFEREE_TIMEOUT_SECONDS", "timeout_seconds", float),
        ("ATHENA_HERMES_REFEREE_CREDENTIAL_ID", "credential_id", str),
        ("ATHENA_HERMES_REFEREE_SELF_HOST_SUPERVISION", "self_host_supervision", str),
    )
    for env_name, key, cast in hermes_env:
        value = os.environ.get(env_name)
        if not value:
            continue
        try:
            hermes[key] = cast(value)
        except (ValueError, TypeError):
            continue
    if hermes:
        result["hermes_referee"] = hermes
    voice: dict[str, Any] = {}
    voice_env: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
        (
            "ATHENA_VOICE_ENABLED",
            "enabled",
            lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"},
        ),
        ("ATHENA_VOICE_TRANSCRIPTION_PROVIDER", "transcription_provider", str),
        ("ATHENA_VOICE_TRANSCRIPTION_MODEL", "transcription_model", str),
        ("ATHENA_VOICE_SYNTHESIS_PROVIDER", "synthesis_provider", str),
        ("ATHENA_VOICE_SYNTHESIS_MODEL", "synthesis_model", str),
        ("ATHENA_VOICE_NAME", "voice", str),
        ("ATHENA_VOICE_RESPONSE_FORMAT", "response_format", str),
        ("ATHENA_VOICE_MAX_INPUT_BYTES", "max_input_bytes", int),
        ("ATHENA_VOICE_MAX_OUTPUT_BYTES", "max_output_bytes", int),
        ("ATHENA_VOICE_MAX_TEXT_CHARS", "max_text_chars", int),
    )
    for env_name, key, cast in voice_env:
        value = os.environ.get(env_name)
        if not value:
            continue
        try:
            voice[key] = cast(value)
        except (ValueError, TypeError):
            continue
    if voice:
        result["voice"] = voice
    return result


# ---------------------------------------------------------------------------
# Layered config loading
# ---------------------------------------------------------------------------


def load_config(
    *,
    cwd: str | None = None,
    profile: str | None = None,
    explicit_path: str | Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AthenaConfig:
    """Load and merge config from all layers.

    Precedence (lowest to highest):
        1. Built-in defaults (AthenaConfig defaults)
        2. Global config file (``~/.config/athena/config.toml``)
        3. Project config file (``.athena/config.toml``)
        4. Named profile (from either config file)
        5. Environment variables
        6. CLI overrides (explicit flags)

    Args:
        cwd: Working directory for project config discovery.
        profile: Profile name. Falls back to ``ATHENA_PROFILE`` env var.
        explicit_path: Override config file path (disables auto-discovery).
        cli_overrides: Explicit highest-precedence overrides (e.g. CLI flags).
    """
    # Layer 1: built-in defaults are implicit in AthenaConfig()
    merged: dict[str, Any] = {}

    # Layer 2: global config file
    if explicit_path is not None:
        global_data = load_toml_file(explicit_path)
    else:
        global_data = load_toml_file(global_config_path())
    merged = deep_merge(merged, global_data)

    # Layer 3: project config files (search upward from cwd)
    if explicit_path is None:
        for candidate in project_config_paths(cwd):
            data = load_toml_file(candidate)
            merged = deep_merge(merged, data)

    # Layer 4: named profile
    selected_profile = profile or os.environ.get("ATHENA_PROFILE")
    if selected_profile:
        profile_data = _extract_profile(merged, selected_profile)
        merged = deep_merge(merged, profile_data)

    # Layer 5: environment variables
    env_data = _env_map()
    # Remove profile from env_data if we already resolved it via CLI arg
    if profile is not None and "profile" in env_data:
        env_data.pop("profile", None)
    merged = deep_merge(merged, env_data)

    # Layer 6: explicit CLI overrides (highest)
    if cli_overrides:
        # Filter out None values so they don't override lower layers
        filtered = {k: v for k, v in cli_overrides.items() if v is not None}
        merged = deep_merge(merged, filtered)

    # Expand ~ and env vars in path fields
    for key in ("db_path", "workspace_root", "artifact_root"):
        if key in merged and isinstance(merged[key], str):
            merged[key] = _expand_user(merged[key])
    if "skills_paths" in merged and isinstance(merged["skills_paths"], (list, tuple)):
        merged["skills_paths"] = tuple(_expand_user(p) for p in merged["skills_paths"])

    return config_from_dict(merged)


def _extract_profile(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Extract a named profile from a parsed config dict.

    Profiles live under ``[profile.<name>]`` in TOML. We return the profile's
    dict so it can be merged onto the root config.
    """
    profiles = data.get("profile", {})
    if isinstance(profiles, dict):
        profile_data = profiles.get(name)
        if isinstance(profile_data, dict):
            return profile_data
    return {}


def save_config(config: AthenaConfig, path: str | Path) -> None:
    """Save an ``AthenaConfig`` without placing provider secrets in TOML.

    A legacy in-memory ``api_key`` is migrated to the owner-only secret store
    at the explicit save boundary.  The serialized config keeps only the
    credential name; callers that construct a config programmatically remain
    backward-compatible until they save it.
    """
    providers = []
    for provider in config.providers:
        if provider.api_key:
            credential_id = provider.credential_id or _generated_credential_id(provider.name)
            write_user_secret(credential_id, provider.api_key)
            provider = replace(provider, credential_id=credential_id, api_key=None)
        providers.append(provider)
    if providers != list(config.providers):
        config = replace(config, providers=tuple(providers))
    write_toml_atomic_private(path, config_to_dict(config))


def write_toml_atomic_private(path: str | Path, data: Mapping[str, Any]) -> None:
    """Write TOML through an owner-private, durable atomic replacement.

    This is the common writer for operator configuration.  It refuses
    symlinked destinations and parent traversal, creates Athena-owned config
    directories privately, fsyncs both the temporary file and its directory,
    and removes an incomplete temporary file on failure.
    """
    try:
        import tomli_w
    except ImportError as exc:
        raise RuntimeError("Saving config requires tomli_w (pip install tomli_w)") from exc

    p = Path(path)
    if p.is_symlink():
        raise ValueError(f"refusing to replace symlinked config path: {p}")
    _reject_symlinked_parents(p.parent)
    # New Athena-owned directories are private. An existing parent may be a
    # deliberately shared project directory and must keep its operator-chosen
    # permissions.
    p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "wb") as handle:
            temp_fd = -1
            tomli_w.dump(dict(data), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, p)
        directory_fd = os.open(str(p.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_fd != -1:
            os.close(temp_fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _generated_credential_id(provider_name: str) -> str:
    """Create a valid private-store name for a legacy provider key."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(provider_name)).strip("._-")
    safe_name = safe_name or "provider"
    return f"{safe_name[:119]}_api_key"


def _reject_symlinked_parents(path: Path) -> None:
    """Reject a config destination whose directory path redirects elsewhere."""
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"refusing to use symlinked config directory: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent
