# CLI parity matrix

This is the release-polish contract for Athena's operator CLI. It records
which durable service/API capability is reachable from the shell, which
interactive-only projection remains intentionally interactive, and where a
transport boundary is deliberate. The Click command tree and the argparse
fallback are required to expose the same command names, aliases, validation,
and exit-code semantics.

## Service/API to CLI

| Service or API capability | HTTP surface | CLI surface | Status |
|---|---|---|---|
| `submit` | `POST /v1/tasks` | `athena run OBJECTIVE` | supported |
| `submit_spec` / `run_task` / `wait_for` | internal service lifecycle | used by `run`, `self`, and API handlers; no separate shell verb | intentional internal path |
| `submit_self_host` and self-host continuation/status | no dedicated HTTP route | `athena self OBJECTIVE`, `athena self continue [MISSION_ID]`, `athena self status [MISSION_ID]` | supported |
| `get_task` / `inspect` | `GET /v1/tasks/{task_id}` | `athena inspect TASK_ID`, `athena tasks show TASK_ID` | supported |
| `get_result` | `GET /v1/tasks/{task_id}/result` | `athena tasks result TASK_ID` | supported |
| `list_tasks` | operator projection | `athena tasks list [--status STATUS]` | supported |
| `list_interrupted` | operator projection | `athena tasks list --status INTERRUPTED` | supported equivalent |
| `cancel` | `POST /v1/tasks/{task_id}/cancel` | `athena cancel TASK_ID`, `athena tasks cancel TASK_ID` | supported |
| `interrupt` | `POST /v1/tasks/{task_id}/interrupt` | `athena tasks interrupt TASK_ID` | supported |
| `steer_task` | `POST /v1/tasks/{task_id}/steer` | `athena tasks steer TASK_ID TEXT...` | supported |
| `provide_input` | `POST /v1/tasks/{task_id}/input` | `athena tasks input TASK_ID ANSWER...` | supported |
| `stream_events` / `stream_all` | `GET /v1/tasks/{task_id}/events` | `athena oi-stream [--task TASK_ID]` | supported |
| `list_sessions` | `GET /v1/sessions` | `athena sessions list` | supported |
| session inspection | `GET /v1/sessions/{session_id}` | `athena sessions show SESSION_ID` | supported |
| `close_session` | `DELETE /v1/sessions/{session_id}` | `athena sessions close SESSION_ID` | supported |
| `resume` / `resume_task` | `POST /v1/sessions/{session_id}/resume` | `athena resume SESSION_ID`, `athena tasks resume TASK_ID` | supported |
| `approve` | `POST /v1/approvals/{approval_id}` | `athena approve APPROVAL_ID [--deny]` | supported |
| model registry | `GET /v1/models` | `athena models` | supported |
| capability registry | `GET /v1/capabilities` | `athena capabilities` | supported |
| schedule store and control leases | no dedicated HTTP route | `athena jobs list/show/enable/disable/run-now/grant/revoke` | supported |
| workflow store | no dedicated HTTP route | `athena workflows list/show` | supported |
| pack manager | no dedicated HTTP route | `athena packs list/search/inspect/install/enable/disable/remove` | supported |
| skill lifecycle | no dedicated HTTP route | `athena skills list/search/inspect/enable/disable` | supported |
| memory candidate lifecycle | no dedicated HTTP route | `athena memory candidates/inspect/promote/discard` | supported |
| MCP status/tools/resources/prompts | no dedicated HTTP route | `athena mcp list/tools/resources/prompts/doctor/reconnect` | supported |
| provider outcome recovery list/inspection | `GET /v1/inference-recoveries[/{attempt_id}]` | `athena inference-recoveries list/show ATTEMPT_ID` | supported |
| provider outcome resolution | `POST /v1/inference-recoveries/{attempt_id}` | `athena inference-recoveries resolve ATTEMPT_ID --resolution ... --note ...` | supported |
| provider liability closeout | `POST /v1/inference-recoveries/{attempt_id}/liability` | `athena inference-recoveries close-liability ATTEMPT_ID --note ...` | supported |
| permissions projection | operator query service | `athena permissions` | supported |
| artifact projection | operator query service | `athena artifacts list [--limit N]` | supported |
| candidate lifecycle projection | operator query service | `athena candidates list/inspect/promote/deprecate` | supported |
| mutation ledger and undo | operator query service | `athena mutations list/undo` | supported |
| compiled context projection | operator query service | `athena context show [--session-id ID]` | supported |
| generated-capability lifecycle | operator query service | `athena generated-capabilities list/show/promote/deprecate` | supported |
| startup/live/readiness health | `/v1/health`, `/v1/live`, `/v1/ready` | `athena doctor startup` | supported; exact probe endpoints remain API-visible |

Every command above delegates to the same `AthenaService` instance used by
the API. The CLI does not create a second task loop, policy engine, scheduler,
or recovery authority.

## Complete command tree

The public nested families are:

```text
sessions          list show close
tasks             list show result cancel interrupt resume steer input
jobs              list show enable disable run-now grant revoke
workflows         list show
packs             list search inspect install enable disable remove
memory            candidates inspect promote discard
mcp               list tools resources prompts doctor reconnect
skills            list search inspect enable disable
inference-recoveries
                  list show resolve close-liability
artifacts         list
candidates        list inspect promote deprecate
mutations         list undo
context           show
generated-capabilities
                  list show promote deprecate
```

Aliases are retained for the established vocabulary: `inspect`/`show`,
`status`/`list`, `run`/`run-now`, `describe`/`show`, `uninstall`/`remove`,
`close`/`close-liability`, and `candidates`/`list` under `memory`.

## Intentional boundaries

Some capabilities are transport-shaped rather than shell-shaped:

| Capability | Surface | Reason |
|---|---|---|
| Voice health/transcription/turn/synthesis and task-result voice | `/v1/voice*`, `/v1/tasks/{task_id}/voice`, `athena chat` voice path | binary/audio request and response semantics are not reduced to a text-only subcommand |
| ACP | `athena acp` | JSON-lines protocol transport, not an operator CRUD family |
| HTTP serving | `athena serve` | starts the loopback API rather than duplicating its routes as CLI commands |
| Interactive OI and chat | `athena chat`, `athena oi-stream` | persistent event streams and presentation controls are intentionally live views |
| `self` workflow and referee/configuration management | `athena self`, `athena referee`, `athena config`, `athena setup` | dedicated lifecycle commands already provide their own typed trees |

The REPL projections `/permissions`, `/diff`, `/undo`, `/context`, candidate
review, and memory promotion are now also available through the standalone
operator commands above. The REPL remains a convenient projection, not a
separate authority.

## Validation, completion, and exit codes

Click is the primary parser. When Click is unavailable, argparse registers the
same nested command families and aliases. Both parsers reject unknown actions,
missing identifiers, invalid enum values, and missing required recovery
evidence before dispatch.

| Code | Meaning |
|---:|---|
| `0` | command completed; an empty list is a valid result |
| `1` | domain failure, missing resource, unavailable subsystem, or service error |
| `2` | usage/validation error, including an invalid command or recovery disposition |
| `130` | operator interrupted the process with Ctrl-C |

Shell completion is generated from the same Click tree:

```bash
eval "$(athena completion bash)"
athena completion zsh
athena completion fish
athena completion powershell
```

The regression contract is exercised by
[`tests/unit/cli/test_command_surface.py`](../tests/unit/cli/test_command_surface.py),
which checks nested help, structured option forwarding, validation exit codes,
completion generation, and argparse shape parity.
