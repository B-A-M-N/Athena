"""P1/P2 capability families: service, network, database, workspace, watch.

These complete the deep-capability set from the roadmap. Each is a
capability family registered through the normal registry -> policy ->
executor path; effect envelopes are conservative (mutations gated).

- service    : systemd user/system service control (list/status/start/
               stop/restart/enable/disable/logs). Mutations require approval.
- network    : diagnostics as primitives — http, tcp_connect, dns,
               listeners, connections, ping. Read-only effects.
- database   : SQL against SQLite/Postgres-compatible endpoints with
               schema introspection; writes are policy-gated WRITE_LOCAL.
- workspace  : status/snapshot/restore/diff/changed_files over the task's
               workspace root (checkpoint-manager backed).
- watch      : filesystem/process watchers that push observations into the
               durable event stream while a task runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from typing import Any
from urllib.parse import urlparse

from athena.network import pinned_sync_transport, resolve_addresses
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)

_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _has_symlink_component(path: str, root: str) -> bool:
    """Return whether an existing component between root and path is a link."""
    root_real = os.path.realpath(root)
    candidate = os.path.abspath(path)
    try:
        relative = os.path.relpath(candidate, root_real)
    except ValueError:
        return True
    current = root_real
    for component in relative.split(os.sep):
        if component in ("", "."):
            continue
        if component == "..":
            return True
        current = os.path.join(current, component)
        if os.path.lexists(current) and os.path.islink(current):
            return True
    return False


def _run(cmd: list[str] | str, timeout: float = 15.0, shell=False):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, shell=shell, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

_MUTATIONS = {"start", "stop", "restart", "reload", "enable", "disable",
              "mask", "unmask"}
_UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$")


def _service_state(scope: list[str], unit: str) -> dict[str, Any]:
    """Read machine-parseable service state without invoking a shell."""
    rc, out, err = _run([
        "systemctl", *scope, "show", unit,
        "--no-pager", "--property=LoadState,ActiveState,SubState,UnitFileState",
    ])
    state: dict[str, Any] = {
        "ok": rc == 0,
        "returncode": rc,
    }
    for line in out.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            state[key.casefold()] = value
    if rc != 0:
        state["error"] = (err or out).strip()[:1000]
    return state


class ServiceCapability:
    """systemd service control (user + system scopes)."""

    descriptor = CapabilityDescriptor(
        id="service",
        description=(
            "Operating-system service control via systemd: list services, "
            "status, start/stop/restart/reload, enable/disable, journal "
            "logs. Mutating operations are policy-gated (PRIVILEGED). "
            "Operations: list/status/logs plus the mutation verbs."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "list", "status", "logs", "start", "stop", "restart",
                    "reload", "enable", "disable", "mask", "unmask",
                ]},
                "unit": {"type": "string", "maxLength": 255,
                         "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.@:%\\-]{0,254}$"},
                "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                "user_scope": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.EXECUTE,
            EffectClass.PRIVILEGED, EffectClass.SPAWN_PROCESS,
            EffectClass.WRITE_LOCAL,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        unit = str(args.get("unit") or "").strip()
        scope = ["--user"] if args.get("user_scope") else []
        loop = asyncio.get_running_loop()

        if op == "list":
            def _ls():
                rc, out, err = _run(["systemctl", *scope, "list-units",
                                     "--type=service", "--no-pager",
                                     "--no-legend"])
                return rc, out, err
            rc, out, err = await loop.run_in_executor(None, _ls)
            return _result(request, ok=rc == 0, output=(out or err)[:6000],
                           error=err if rc else None, meta={"rc": rc})

        if not unit:
            return _result(request, ok=False, error="unit required")
        if not _UNIT_NAME.fullmatch(unit):
            return _result(request, ok=False, error="invalid systemd unit name")

        if op == "status":
            def _st():
                return _run(["systemctl", *scope, "status", unit,
                             "--no-pager", "-l"])
            rc, out, err = await loop.run_in_executor(None, _st)
            state = await loop.run_in_executor(
                None, lambda: _service_state(scope, unit))
            payload = {
                "unit": unit, "scope": "user" if scope else "system",
                "state": state, "detail": (out or err)[:6000],
            }
            return _result(request, ok=rc == 0, output=json.dumps(payload),
                           error=err if rc else None, meta=payload)

        if op == "logs":
            lines = max(int(args.get("lines") or 50), 1)

            def _lg():
                return _run(["journalctl", *scope, "-u", unit,
                             "-n", str(lines), "--no-pager"])
            rc, out, err = await loop.run_in_executor(None, _lg)
            return _result(request, ok=rc == 0, output=(out or err)[:8000],
                           error=err if rc else None, meta={"rc": rc})

        if op in _MUTATIONS:
            # Only ever via systemctl with explicit unit; PRIVILEGED effect
            # forces supervised approval under default profiles.
            def _mut():
                before = _service_state(scope, unit)
                rc, out, err = _run(["systemctl", *scope, op, unit], timeout=30)
                after = _service_state(scope, unit)
                rollback = {
                    "operation": {
                        "start": "stop", "stop": "start",
                        "restart": "restart", "reload": "reload",
                        "enable": "disable", "disable": "enable",
                        "mask": "unmask", "unmask": "mask",
                    }[op],
                    "unit": unit,
                    "user_scope": bool(scope),
                }
                return rc, out, err, before, after, rollback
            rc, out, err, before, after, rollback = await loop.run_in_executor(
                None, _mut)
            payload = {
                "operation": op, "unit": unit,
                "scope": "user" if scope else "system",
                "before": before, "after": after,
                "returncode": rc,
                "rollback": rollback,
                "detail": (out or err).strip()[:4000],
            }
            return _result(request, ok=rc == 0,
                           output=json.dumps(payload),
                           error=err if rc else None,
                           meta=payload)

        return _result(request, ok=False, error=f"unknown operation: {op}")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------

def _network_effects(arguments) -> frozenset[EffectClass]:
    """Resolve network authority from the concrete operation and method."""
    operation = str(arguments.get("operation") or "").lower()
    if operation not in {
        "http", "tcp_connect", "dns", "listeners", "connections", "ping",
    }:
        raise ValueError(f"operation {operation!r} has no network effect contract")
    if operation == "http":
        method = str(arguments.get("method") or "GET").upper()
        if method in _SAFE_HTTP_METHODS:
            return frozenset({EffectClass.NETWORK_READ})
        # A mutating HTTP method is not a read merely because the capability
        # also returns a response body. This is the authority seen by policy.
        return frozenset({EffectClass.NETWORK_WRITE})
    if operation in {"listeners", "connections"}:
        return frozenset({EffectClass.READ_LOCAL})
    return frozenset({EffectClass.NETWORK_READ})


class NetworkCapability:
    """Machine networking diagnostics as first-class primitives."""

    descriptor = CapabilityDescriptor(
        id="network",
        description=(
            "Network diagnostics: HTTP requests (method/headers/body), raw "
            "TCP connect probes, DNS resolution, listening sockets, active "
            "connections, ping. Restricted outbound requests pin the DNS "
            "address checked by policy."
        ),
        input_schema={
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "http"},
                        "url": {"type": "string", "minLength": 1},
                        "method": {"type": "string", "minLength": 1, "maxLength": 16},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "body": {"type": "string", "maxLength": 2_000_000},
                        "follow_redirects": {"type": "boolean"},
                    },
                    "required": ["operation", "url"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "tcp_connect"},
                        "host": {"type": "string", "minLength": 1},
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                    },
                    "required": ["operation", "host", "port"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "dns"},
                        "name": {"type": "string", "minLength": 1},
                    },
                    "required": ["operation", "name"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"operation": {"const": "listeners"}},
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"operation": {"const": "connections"}},
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "ping"},
                        "host": {"type": "string", "minLength": 1},
                        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                    },
                    "required": ["operation", "host"],
                    "additionalProperties": False,
                },
            ],
        },
        effects=frozenset({EffectClass.READ_LOCAL, EffectClass.NETWORK_READ,
                           EffectClass.NETWORK_WRITE}),
        effect_resolver=_network_effects,
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        timeout = min(float(args.get("timeout") or 10.0), 30.0)
        loop = asyncio.get_running_loop()
        context = kw.get("context")
        network_policy = getattr(
            getattr(context, "workspace", None), "network_policy", None)
        policy_name = getattr(network_policy, "value", network_policy)

        # Network diagnostics are still network effects.  Enforce the task's
        # workspace policy at the capability boundary for every outbound
        # operation, rather than relying on the HTTP branch alone.
        if op in {"http", "tcp_connect", "dns", "ping"} and policy_name == "deny":
            return _result(request, ok=False,
                           error="network denied by workspace policy")

        def _restricted_addresses(host: str) -> tuple[str | None, tuple[str, ...]]:
            """Reject localhost/private/metadata targets in restricted mode.

            Resolve names before connecting so a public-looking hostname that
            resolves into a private or link-local address cannot be used as a
            basic SSRF primitive.  The HTTP branch also disables redirects in
            restricted mode because a redirect target is a new destination.
            """
            if policy_name != "restricted":
                return None, ()
            candidate = host.strip().strip("[]").lower().rstrip(".")
            if not candidate or candidate in {"localhost", "localhost.localdomain"}:
                return "restricted network policy rejects local targets", ()
            try:
                addresses = {ipaddress.ip_address(candidate)}
                address_strings: tuple[str, ...] = (candidate,)
            except ValueError:
                try:
                    address_strings = resolve_addresses(candidate, 0)
                    addresses = {ipaddress.ip_address(address) for address in address_strings}
                except (OSError, socket.gaierror):
                    return f"unable to resolve host under restricted network policy: {host}", ()
            if any(
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
                for address in addresses
            ):
                return f"restricted network policy rejects private/local host: {host}", ()
            return None, tuple(address_strings)

        if op == "http":
            url = str(args.get("url") or "")
            method = str(args.get("method") or "GET").upper()
            if not url:
                return _result(request, ok=False, error="url required")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return _result(request, ok=False,
                               error="url must use http or https and include a host")
            hostname = parsed.hostname
            restricted_error, pinned_addresses = _restricted_addresses(hostname)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)
            follow_redirects = bool(args.get("follow_redirects", False))
            if policy_name == "restricted" and follow_redirects:
                return _result(request, ok=False,
                               error="restricted network policy disallows redirects")

            import httpx

            def _http():
                client_args: dict[str, Any] = {
                    "timeout": timeout,
                    "follow_redirects": follow_redirects,
                }
                if policy_name == "restricted":
                    client_args["trust_env"] = False
                    client_args["transport"] = pinned_sync_transport(
                        hostname, pinned_addresses
                    )
                with httpx.Client(**client_args) as c:
                    with c.stream(
                        method,
                        url,
                        headers=dict(args.get("headers") or {}),
                        content=args.get("body"),
                    ) as resp:
                        body = b""
                        for chunk in resp.iter_bytes():
                            body += chunk
                            if len(body) >= 8192:
                                body = body[:8192]
                                break
                        return {
                            "status": resp.status_code,
                            "headers": dict(resp.headers),
                            "body_head": body.decode(resp.encoding or "utf-8", errors="replace"),
                            "body_truncated": len(body) >= 8192,
                            "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
                        }

            try:
                info = await loop.run_in_executor(None, _http)
            except Exception as exc:  # noqa: BLE001 - report network failure truthfully
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=json.dumps(info, indent=2)[:4000],
                           meta={"status": info["status"]})

        if op == "tcp_connect":
            host = str(args.get("host") or "127.0.0.1")
            port = int(args.get("port") or 0)
            if not (0 < port < 65536):
                return _result(request, ok=False, error="valid port required")
            restricted_error, pinned_addresses = _restricted_addresses(host)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _tcp():
                s = socket.socket()
                s.settimeout(timeout)
                try:
                    s.connect((pinned_addresses[0] if pinned_addresses else host, port))
                    return True
                except OSError:
                    return False
                finally:
                    s.close()

            open_ = await loop.run_in_executor(None, _tcp)
            return _result(request, output=f"{host}:{port} "
                           + ("open" if open_ else "closed"),
                           meta={"open": open_})

        if op == "dns":
            name = str(args.get("name") or "localhost")
            restricted_error, _ = _restricted_addresses(name)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _dns():
                infos = socket.getaddrinfo(name, None)
                uniq = sorted({str(i[4][0]) for i in infos})
                return f"{name} -> {', '.join(uniq)}"

            try:
                text = await loop.run_in_executor(None, _dns)
            except socket.gaierror as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=text)

        if op == "listeners":
            def _ls():
                _rc, out, err = _run(["ss", "-tlnp"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(
                None, _ls))[:5000])

        if op == "connections":
            def _cx():
                _rc, out, err = _run(["ss", "-tnp"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(
                None, _cx))[:5000])

        if op == "ping":
            host = str(args.get("host") or "")
            if not host:
                return _result(request, ok=False, error="host required")
            restricted_error, pinned_addresses = _restricted_addresses(host)
            if restricted_error:
                return _result(request, ok=False, error=restricted_error)

            def _pg():
                target = pinned_addresses[0] if pinned_addresses else host
                rc, out, err = _run(["ping", "-c", "3", "-W", "2", target])
                return rc, out or err

            rc, out = await loop.run_in_executor(None, _pg)
            return _result(request, ok=rc == 0, output=out[:3000])

        return _result(request, ok=False, error=f"unknown operation: {op}")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def _database_effects(arguments) -> frozenset[EffectClass]:
    """Resolve database authority from the operation *and* SQL shape.

    The operation label alone is not a trustworthy security boundary: a
    caller can put an UPDATE inside a ``query`` request. Only a narrow
    read-only SQL prefix is treated as observational; everything else is
    classified as a local mutation before policy evaluation.
    """
    op = str(arguments.get("operation") or "").lower()
    if op not in {"tables", "schema", "query", "explain", "execute"}:
        raise ValueError(f"operation {op!r} has no declared effect classification")
    if op == "execute":
        return frozenset({EffectClass.WRITE_LOCAL})
    if op == "query":
        sql = str(arguments.get("sql") or "").lstrip()
        first = sql.split(None, 1)[0].upper() if sql else ""
        if first not in {"SELECT", "VALUES"}:
            return frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL})
    return frozenset({EffectClass.READ_LOCAL})

class DatabaseCapability:
    """SQL execution with schema introspection (SQLite built-in)."""

    descriptor = CapabilityDescriptor(
        id="database",
        description=(
            "Database access: connect to a SQLite file, inspect schema/tables, "
            "run queries, EXPLAIN, and "
            "execute writes (WRITE_LOCAL, policy-gated). Operations: "
            "tables/schema/query/explain/execute. Results are paginated."
        ),
        input_schema={
            "type": "object",
            "required": ["operation", "path"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "tables", "schema", "query", "explain", "execute"]},
                "path": {"type": "string"},
                "sql": {"type": "string"},
                "params": {"type": "array"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL,
        }),
        effect_resolver=_database_effects,
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, *, mutation_store=None, artifact_store=None) -> None:
        self._mutations = mutation_store
        self._artifacts = artifact_store

    @staticmethod
    def _read_only_authorizer(action, arg1, arg2, database, source):
        import sqlite3
        denied = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
        }
        # These functions are not part of the normal query surface and may be
        # supplied by an extension.  Keep the read-only contract true even if
        # the host process has loaded SQLite extensions elsewhere.
        if action == getattr(sqlite3, "SQLITE_FUNCTION", -1):
            function_name = str(arg2 or arg1 or "").casefold()
            if function_name in {"load_extension", "readfile", "writefile"}:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

    @classmethod
    def _set_read_only_authorizer(cls, conn) -> None:
        conn.set_authorizer(cls._read_only_authorizer)

    def _connect(self, path: str, *, readonly: bool = False):
        import sqlite3
        if readonly:
            from urllib.parse import quote
            conn = sqlite3.connect(
                f"file:{quote(path, safe='/')}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _confine_path(path: str, context) -> str:
        root = getattr(getattr(context, "workspace", None), "root", None)
        candidate = os.path.realpath(
            path if os.path.isabs(path) else os.path.join(root, path)
        ) if root else os.path.realpath(path)
        if root:
            root = os.path.realpath(root)
            if candidate != root and not candidate.startswith(root + os.sep):
                raise ValueError("database path outside workspace")
            original = path if os.path.isabs(path) else os.path.join(root, path)
            if _has_symlink_component(original, root):
                raise ValueError("database path cannot traverse a symlink")
        return candidate

    async def _snapshot(
        self, path: str, task_id: str | None, *, persist: bool = True
    ):
        if not os.path.isfile(path):
            return None, None
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        ref = None
        if persist and self._artifacts is not None:
            try:
                saved = await self._artifacts.save(
                    task_id=task_id,
                    content=data,
                    mime_type="application/x-sqlite3",
                    producer="database",
                )
                ref = saved.uri
            except Exception:
                ref = None
        return ref, digest

    async def invoke(self, request: CapabilityRequest, context=None, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        path = str(args.get("path") or "")
        sql = str(args.get("sql") or "")
        params = list(args.get("params") or [])
        offset = int(args.get("offset") or 0)
        limit = min(int(args.get("limit") or 200), 1000)
        loop = asyncio.get_running_loop()

        workspace = getattr(context, "workspace", None) if context is not None else None
        if workspace is None or not getattr(workspace, "root", None):
            return _result(
                request, ok=False,
                error="database access requires a workspace-bound invocation",
            )
        if not path:
            return _result(request, ok=False, error="path required")
        try:
            path = self._confine_path(path, context)
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))
        if op != "tables" and not path:
            return _result(request, ok=False, error="path required")
        if op in ("query", "explain", "execute") and not sql:
            return _result(request, ok=False, error="sql required")
        readonly = op in {"tables", "schema", "query", "explain"}
        if readonly and not os.path.isfile(path):
            return _result(request, ok=False,
                           error=f"database does not exist for read operation: {path}")
        if os.path.isfile(path) and not os.access(path, os.R_OK):
            return _result(request, ok=False, error=f"unreadable: {path}")

        def _rows(cur):
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(offset + limit + 1)
            truncated = len(rows) > offset + limit
            rows = rows[offset:offset + limit]
            return json.dumps({"columns": cols,
                               "rows": [[str(v) for v in r] for r in rows],
                               "offset": offset, "limit": limit,
                               "truncated": truncated},
                              default=str)

        def _tables():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name "
                    "LIMIT ? OFFSET ?", (limit + 1, offset))
                rows = cur.fetchall()
                return json.dumps({
                    "rows": [[r[1], r[0]] for r in rows[:limit]],
                    "offset": offset, "limit": limit,
                    "truncated": len(rows) > limit,
                })
            finally:
                conn.close()

        def _schema():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "ORDER BY name LIMIT ? OFFSET ?", (limit + 1, offset))
                rows = cur.fetchall()
                return json.dumps({
                    "statements": [r[0] for r in rows[:limit]],
                    "offset": offset, "limit": limit,
                    "truncated": len(rows) > limit,
                })
            finally:
                conn.close()

        def _query():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(sql, params)
                return _rows(cur)
            finally:
                conn.close()

        def _explain():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                return _rows(cur)
            finally:
                conn.close()

        def _execute():
            conn = self._connect(path)
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return json.dumps({"rowcount": cur.rowcount})
            finally:
                conn.close()

        handlers = {"tables": _tables, "schema": _schema, "query": _query,
                    "explain": _explain, "execute": _execute}
        if op not in handlers:
            return _result(request, ok=False, error=f"unknown operation: {op}")
        mutation_id = None
        before_ref = None
        before_hash = None
        if op == "execute":
            before_ref, before_hash = await self._snapshot(path, request.task_id)
            if self._mutations is not None:
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    path,
                    "database.execute",
                    before_ref=before_ref,
                    inverse=(
                        {"op": "restore_from_ref", "target": path, "ref": before_ref}
                        if before_ref else {"op": "delete", "target": path}
                    ),
                )
                await self._mutations.mark_started(mutation_id)
        try:
            out = await loop.run_in_executor(None, handlers[op])
        except Exception as exc:  # noqa: BLE001 - report database failure truthfully
            if mutation_id is not None:
                await self._mutations.mark_failed(mutation_id, str(exc))
            return _result(request, ok=False, error=str(exc))
        if mutation_id is not None:
            _after_ref, after_hash = await self._snapshot(
                path, request.task_id, persist=False
            )
            reversible = before_ref is not None or before_hash is None
            await self._mutations.complete(
                mutation_id,
                after_hash=after_hash,
                reversible=reversible,
                inverse=(
                    {"op": "restore_from_ref", "target": path, "ref": before_ref}
                    if before_ref else {"op": "delete", "target": path}
                ),
            )
            out_meta = {
                "mutation": {
                    "resource": path,
                    "operation": "database.execute",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "before_ref": before_ref,
                    "reversible": reversible,
                    "mutation_id": mutation_id,
                }
            }
            return _result(request, output=out, meta=out_meta)
        return _result(request, output=out)


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------

class WorkspaceCapability:
    """Status / snapshot / diff / changed-files for the task workspace."""

    def __init__(self, checkpoint_manager=None, *, mutation_store=None,
                 mutation_observer=None) -> None:
        self._checkpoints = checkpoint_manager
        self._mutations = mutation_store
        self._mutation_observer = mutation_observer

    descriptor = CapabilityDescriptor(
        id="workspace",
        description=(
            "Workspace lifecycle: status summary, snapshot (via checkpoint "
            "manager), restore, changed-file listing (git when available, "
            "mtime scan otherwise), and generated-artifact cleanup hints. "
            "Operations: status/changed_files/snapshot/restore."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "status", "changed_files", "snapshot", "restore"]},
                "label": {"type": "string"},
                "checkpoint_id": {"type": "string"},
                "expected_fingerprint": {"type": "string", "minLength": 1},
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL, EffectClass.DELETE,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def _bind_context(self, context) -> str | None:
        return context.workspace.root if context else None

    async def invoke(self, request: CapabilityRequest, context=None,
                     **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        root = self._bind_context(context)
        loop = asyncio.get_running_loop()
        if root is None:
            return _result(request, ok=False,
                           error="no workspace bound to this call")

        if op == "status":
            def _st():
                files = sum(len(f) for _, _, f in os.walk(root))
                size = sum(
                    os.path.getsize(os.path.join(d, f))
                    for d, _, fs in os.walk(root) for f in fs
                    if os.path.exists(os.path.join(d, f)))
                git = "yes" if os.path.isdir(os.path.join(root, ".git")) else "no"
                return f"root={root}\nfiles={files}\nbytes={size}\ngit={git}"
            return _result(request, output=await loop.run_in_executor(None, _st))

        if op == "changed_files":
            def _changed():
                rc, out, _ = _run(["git", "-C", root, "status", "--porcelain"])
                if rc == 0 and out.strip():
                    return out
                # fallback: newest-modified files
                entries = []
                for d, _, fs in os.walk(root):
                    if ".git" in d:
                        continue
                    for f in fs:
                        p = os.path.join(d, f)
                        try:
                            entries.append((os.path.getmtime(p),
                                            os.path.relpath(p, root)))
                        except OSError:
                            pass
                entries.sort(reverse=True)
                return "\n".join(f"{e[1]}" for e in entries[:25])
            return _result(request, output=await loop.run_in_executor(
                None, _changed))

        if op == "snapshot":
            mgr = self._checkpoints
            label = str(args.get("label") or "workspace-snapshot")
            manifest = await mgr.capture(task_id=request.task_id or "unknown",
                                         workspace_root=root, label=label)
            cid = manifest.get("checkpoint_id") or manifest.get("id")
            return _result(request, output=f"snapshot {cid} "
                           f"({manifest.get('file_count')} files)",
                           meta={"checkpoint_id": cid})

        if op == "restore":
            cid = str(args.get("checkpoint_id") or "")
            if not cid or self._checkpoints is None:
                return _result(request, ok=False,
                               error="checkpoint_id required")
            mutation_id = None
            before_checkpoint = None
            task_id = request.task_id or "unknown"
            if self._mutations is not None:
                # A restore is a real workspace mutation. Capture the
                # before-state and write the intent before touching the
                # target, so a partial restore is recoverable and auditable.
                before_checkpoint = await self._checkpoints.capture(
                    task_id=task_id,
                    workspace_root=root,
                    label=f"before-restore-{cid}",
                )
                expected = await self._checkpoints.fingerprint(root)
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    root,
                    "workspace.restore",
                    before_ref=f"checkpoint://{before_checkpoint['id']}",
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                    metadata={"checkpoint_id": cid, "expected_fingerprint": expected},
                )
                await self._mutations.mark_started(mutation_id)
            try:
                outcome = await self._checkpoints.restore(
                    checkpoint_id=cid,
                    workspace_root=root,
                    expected_fingerprint=(
                        str(args.get("expected_fingerprint") or expected)
                        if self._mutations is not None else args.get("expected_fingerprint")
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if mutation_id is not None:
                    await self._mutations.mark_recovery_required(mutation_id)
                return _result(request, ok=False, error=str(exc))
            if mutation_id is not None:
                if before_checkpoint is None:
                    raise RuntimeError("restore mutation has no before checkpoint")
                await self._mutations.complete(
                    mutation_id,
                    after_hash=outcome.get("workspace_fingerprint"),
                    reversible=True,
                    inverse={"checkpoint_id": before_checkpoint["id"]},
                )
            mutation = None
            if mutation_id is not None:
                mutation = {
                    "mutation_id": mutation_id,
                    "resource": root,
                    "operation": "workspace.restore",
                    "after_hash": outcome.get("workspace_fingerprint"),
                    "before_ref": (
                        f"checkpoint://{before_checkpoint['id']}"
                        if before_checkpoint is not None else None
                    ),
                    "reversible": True,
                    "inverse": {"checkpoint_id": before_checkpoint["id"]}
                    if before_checkpoint is not None else None,
                }
            return _result(
                request,
                output=json.dumps(outcome)[:1000],
                meta={"mutation": mutation} if mutation is not None else None,
            )

        return _result(request, ok=False, error=f"unknown operation: {op}")
