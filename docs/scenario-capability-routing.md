# Scenario capability routing

Release scenarios declare the host capabilities their bound evidence requires.
The scenario runner probes those capabilities before executing evidence.

## Canonical capabilities

| Capability | Meaning |
|---|---|
| `PURE` | No process, socket, display, or network dependency beyond Python. |
| `FS` | Workspace/filesystem access. |
| `PROCESS` | Owned child process lifecycle. |
| `LOCAL_SOCKET` | Unix-domain socket creation/bind. |
| `TCP_LOOPBACK` | IPv4 TCP socket creation plus loopback bind. |
| `NETWORK_EGRESS` | Host DNS/HTTPS access for dependency acquisition or provider traffic. |
| `DISPLAY` | X11/Wayland presentation surface. |

`PURE` is intentionally exclusive: a scenario that also names another
capability is not pure, and registry validation rejects that combination.

## Test-level declaration

Pytest evidence also declares its own capability with:

```python
@pytest.mark.athena_capability("TCP_LOOPBACK")
```

At collection, the shared root conftest validates the capability name and
probes it.  An unavailable capability produces a skip with the explicit reason
`ENVIRONMENT_UNAVAILABLE: <capability list>`, so an ordinary pytest run cannot
misclassify a runner restriction as Athena product evidence.  Scenario
bindings and test markers are aligned for the current high-risk paths by
`test_scenario_capability_list_matches_test_markers_for_high_risk_paths`.

## Runner contract

Before running evidence, `scripts/scenarios` probes each declared capability.
When one is unavailable, the scenario is recorded as
`status="environment_unavailable"` with `unavailable_capabilities=[...]`; it
does not execute product evidence and is not a pass. Required scenarios with
unavailable capabilities are listed as `required_environment_unavailable` and
block the release gate until a host-capable qualification runner produces a
pass. The runner exits zero only when every required scenario actually passed.

Probe behavior:

- `TCP_LOOPBACK` creates an IPv4 stream socket and binds it to `127.0.0.1:0`.
- `NETWORK_EGRESS` performs DNS resolution for `pypi.org`.
- `DISPLAY` checks the display environment. It deliberately does not start an
  X server; presentation probes must run in the same execution context that
  owns display lifecycle.

This keeps sandbox capability denial out of product release evidence while
making the required host runner explicit and machine-readable.
