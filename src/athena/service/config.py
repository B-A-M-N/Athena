"""AthenaService configuration.

Defines :class:`AthenaConfig` — the dataclass that configures the application
composition root (:class:`~athena.service.service.AthenaService`). Every field
has a sane default so ``AthenaService(config=AthenaConfig())`` is a working
in-memory/demo runtime, and ``AthenaService.in_memory()`` is specialised for
tests.

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
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from athena.protocol.tasks import AutonomyLevel

__all__ = [
    "AthenaConfig",
    "ProviderConfig",
    "MCPConfig",
    "DEFAULT_DB_PATH",
    "load_config",
    "merge_configs",
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
    api_key: str | None = None
    base_url: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(self.extra)
        kwargs.setdefault("model", self.model)
        if self.credential_id is not None:
            kwargs.setdefault("credential_id", self.credential_id)
        if self.api_key is not None:
            kwargs.setdefault("api_key", self.api_key)
        if self.base_url is not None:
            kwargs.setdefault("base_url", self.base_url)
        return kwargs


@dataclass(frozen=True)
class MCPConfig:
    """A single MCP server to connect at startup (stdio or Streamable HTTP)."""

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: Mapping[str, str] = field(default_factory=dict)
    connect_timeout: float = 10.0


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
    mcp_servers: tuple[MCPConfig, ...] = ()
    context_window: int = 128_000
    reserve_output: int = 4096
    worker_max_parallel: int = 4
    scheduler_interval_seconds: float = 1.0
    scheduler_max_concurrent: int = 0
    profile: str | None = None
    # Role-divided models (Hermes-style): role name -> {"allowed": [...],
    # "privacy": "...", "max_cost_usd": "0.01"}. Roles without an entry fall
    # back to the user's global/primary choice.
    model_roles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # External research acquisition is deny-by-default until the operator
    # explicitly allowlists source domains. Artifact snapshots remain local.
    research_allowed_domains: tuple[str, ...] = ()
    research_denied_domains: tuple[str, ...] = ()
    research_allow_private_network: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def autonomy_level(self) -> AutonomyLevel:
        if isinstance(self.autonomy, AutonomyLevel):
            return self.autonomy
        return AutonomyLevel(self.autonomy or AutonomyLevel.SUPERVISED.value)


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
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
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


def _parse_provider(data: dict[str, Any]) -> ProviderConfig:
    """Parse a provider entry from a config dict."""
    extra = dict(data)
    for key in ("kind", "name", "model", "credential_id", "api_key", "base_url"):
        extra.pop(key, None)
    args = data.get("args")
    if isinstance(args, list):
        args = tuple(args)
    return ProviderConfig(
        kind=data.get("kind", "fake"),
        name=data.get("name", "fake"),
        model=data.get("model", "fake-1"),
        credential_id=data.get("credential_id"),
        api_key=data.get("api_key"),
        base_url=data.get("base_url"),
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
        env=data.get("env") or {},
        secret_env=data.get("secret_env") or {},
        connect_timeout=float(data.get("connect_timeout", 10.0)),
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
        config.autonomy.value
        if isinstance(config.autonomy, AutonomyLevel)
        else config.autonomy
    )
    if config.artifact_root is not None:
        d["artifact_root"] = config.artifact_root
    if config.skills_paths:
        d["skills_paths"] = list(config.skills_paths)
    if config.context_window != 128_000:
        d["context_window"] = config.context_window
    if config.reserve_output != 4096:
        d["reserve_output"] = config.reserve_output
    if config.worker_max_parallel != 4:
        d["worker_max_parallel"] = config.worker_max_parallel
    if config.scheduler_interval_seconds != 1.0:
        d["scheduler_interval_seconds"] = config.scheduler_interval_seconds
    if config.scheduler_max_concurrent != 0:
        d["scheduler_max_concurrent"] = config.scheduler_max_concurrent
    if config.profile is not None:
        d["profile"] = config.profile
    if config.providers:
        d["providers"] = [
            {"kind": p.kind, "name": p.name, "model": p.model,
             "credential_id": p.credential_id, "api_key": p.api_key,
             "base_url": p.base_url, **dict(p.extra)}
            for p in config.providers
        ]
    if config.mcp_servers:
        d["mcp_servers"] = [
            {"name": m.name, "command": m.command, "args": list(m.args),
             "url": m.url, "env": dict(m.env), "secret_env": dict(m.secret_env),
             "connect_timeout": m.connect_timeout}
            for m in config.mcp_servers
        ]
    if config.model_roles:
        d["model_roles"] = {k: dict(v) for k, v in config.model_roles.items()}
    if config.research_allowed_domains:
        d["research_allowed_domains"] = list(config.research_allowed_domains)
    if config.research_denied_domains:
        d["research_denied_domains"] = list(config.research_denied_domains)
    if config.research_allow_private_network:
        d["research_allow_private_network"] = True
    if config.metadata:
        d["metadata"] = dict(config.metadata)
    return d


def config_from_dict(data: dict[str, Any]) -> AthenaConfig:
    """Build an ``AthenaConfig`` from a plain dict (e.g. parsed from TOML)."""
    # Handle nested sub-configs
    providers = tuple(
        _parse_provider(p) for p in data.get("providers", ()) if isinstance(p, dict)
    )
    mcp_servers = tuple(
        _parse_mcp(m) for m in data.get("mcp_servers", ()) if isinstance(m, dict)
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

    return AthenaConfig(
        db_path=data.get("db_path"),
        workspace_root=data.get("workspace_root"),
        autonomy=data.get("autonomy", AutonomyLevel.SUPERVISED),
        artifact_root=data.get("artifact_root"),
        skills_paths=skills,
        providers=providers,
        mcp_servers=mcp_servers,
        context_window=int(data.get("context_window", 128_000)),
        reserve_output=int(data.get("reserve_output", 4096)),
        worker_max_parallel=int(data.get("worker_max_parallel", 4)),
        scheduler_interval_seconds=float(data.get("scheduler_interval_seconds", 1.0)),
        scheduler_max_concurrent=int(data.get("scheduler_max_concurrent", 0)),
        profile=data.get("profile"),
        model_roles=dict(data.get("model_roles") or {}),
        research_allowed_domains=_domains(data.get("research_allowed_domains")),
        research_denied_domains=_domains(data.get("research_denied_domains")),
        research_allow_private_network=bool(
            data.get("research_allow_private_network", False)),
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
        ATHENA_WORKER_MAX_PARALLEL, ATHENA_SCHEDULER_INTERVAL_SECONDS,
        ATHENA_SCHEDULER_MAX_CONCURRENT, ATHENA_PROFILE,
        ATHENA_SKILLS_PATHS (comma-separated)
    """
    result: dict[str, Any] = {}
    env_map: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "ATHENA_DB_PATH": ("db_path", str),
        "ATHENA_DB": ("db_path", str),
        "ATHENA_WORKSPACE": ("workspace_root", str),
        "ATHENA_WORKSPACE_PATH": ("workspace_root", str),
        "ATHENA_AUTONOMY": ("autonomy", str),
        "ATHENA_ARTIFACT_ROOT": ("artifact_root", str),
        "ATHENA_CONTEXT_WINDOW": ("context_window", int),
        "ATHENA_RESERVE_OUTPUT": ("reserve_output", int),
        "ATHENA_WORKER_MAX_PARALLEL": ("worker_max_parallel", int),
        "ATHENA_SCHEDULER_INTERVAL_SECONDS": ("scheduler_interval_seconds", float),
        "ATHENA_SCHEDULER_MAX_CONCURRENT": ("scheduler_max_concurrent", int),
        "ATHENA_PROFILE": ("profile", str),
        "ATHENA_MODEL_ROLES": (
            "model_roles",
            lambda v: json.loads(v) if isinstance(v, str) else v,
        ),
        "ATHENA_SKILLS_PATHS": ("skills_paths", lambda v: tuple(p.strip() for p in v.split(",") if p.strip())),
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
    }
    for env_name, (key, cast) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            try:
                result[key] = cast(value)
            except (ValueError, TypeError):
                pass
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
        merged["skills_paths"] = tuple(
            _expand_user(p) for p in merged["skills_paths"]
        )

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
    """Save an ``AthenaConfig`` to a TOML file."""
    try:
        import tomllib  # noqa: F401  # verify tomllib is available
        import tomli_w
    except ImportError as exc:
        raise RuntimeError(
            "Saving config requires tomli_w (pip install tomli_w)"
        ) from exc
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        import tomli_w
        tomli_w.dump(config_to_dict(config), f)
