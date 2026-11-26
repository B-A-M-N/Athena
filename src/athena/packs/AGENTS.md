# Packs

## Purpose

Packs are declarative, operator-governed contributions that enter existing
Athena surfaces through canonical registries, stores, and dispatch paths.

## Ownership

`PackManager` coordinates pack inspection and composes the installer,
activator, hook registration, and hook runtimes. `source.py` owns untrusted source validation,
bounded remote acquisition, archive extraction, and integrity calculation.
Activation remains responsible for admission and rollback.

## Local Contracts

- Pack files are data; pack code is never loaded into Athena's interpreter.
- Remote bytes are bounded, non-redirecting, provenance-bearing, and quarantined before install.
- Contributions must satisfy manifest authority and re-enter canonical capability, workflow, MCP, skill, or event paths.
- Failed activation must roll back live registrations and durable enabled state.

## Work Guidance

Keep source security, installation, activation, and hook delivery separate by
reason to change. Do not create a second registry or dispatcher for packs.

## Verification

Run pack capability tests, security tests covering archive/path/network gates,
Ruff, compile checks, and `./scripts/architecture-lint --quiet`.

## Child DOX Index

| Path | Contract |
|------|----------|
| `source.py` | Untrusted source validation and bounded archive/network mechanics. |
| `install.py` | Install, upgrade, enable, disable, and uninstall transactions. |
| `activation.py` | Contribution activation ordering and rollback. |
| `contribution_activation.py` | Skill, workflow, alias, instrument, and MCP admission. |
| `hook_activation.py` | Declarative hook contract admission and event-task registration. |
| `hooks.py` | Durable hook replay, retry dispatch, and health. |
| `lifecycle.py` | Stable composition port for the lifecycle components. |
