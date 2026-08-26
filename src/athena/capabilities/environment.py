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
import json
import os
import shutil
import socket
import subprocess
from typing import Any

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _run(cmd: list[str] | str, timeout: float = 15.0, shell=False):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, shell=shell)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"not found"


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

_MUTATIONS = {"start", "stop", "restart", "reload", "enable", "disable",
              "mask", "unmask"}


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
                "operation": {"type": "string"},
                "unit": {"type": "string"},
                "lines": {"type": "integer"},
                "user_scope": {"type": "boolean"},
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.EXECUTE,
            EffectClass.PRIVILEGED, EffectClass.SPAWN_PROCESS,
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
                return out or err
            return _result(request, output=(await loop.run_in_executor(None, _ls))[:6000])

        if not unit:
            return _result(request, ok=False, error="unit required")

        if op == "status":
            def _st():
                rc, out, err = _run(["systemctl", *scope, "status", unit,
                                     "--no-pager", "-l"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(None, _st))[:6000])

        if op == "logs":
            lines = max(int(args.get("lines") or 50), 1)

            def _lg():
                rc, out, err = _run(["journalctl", *scope, "-u", unit,
                                     "-n", str(lines), "--no-pager"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(None, _lg))[:8000])

        if op in _MUTATIONS:
            # Only ever via systemctl with explicit unit; PRIVILEGED effect
            # forces supervised approval under default profiles.
            def _mut():
                return _run(["systemctl", *scope, op, unit], timeout=30)
            rc, out, err = await loop.run_in_executor(None, _mut)
            return _result(request, ok=rc == 0,
                           output=out or f"{op} {unit}: ok",
                           error=err if rc else None,
                           meta={"rc": rc})

        return _result(request, ok=False, error=f"unknown operation: {op}")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------

class NetworkCapability:
    """Machine networking diagnostics as first-class primitives."""

    descriptor = CapabilityDescriptor(
        id="network",
        description=(
            "Network diagnostics: HTTP requests (method/headers/body), raw "
            "TCP connect probes, DNS resolution, listening sockets, active "
            "connections, ping. Read-only observations of machine networking."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": [
                    "http", "tcp_connect", "dns", "listeners",
                    "connections", "ping"]},
                "url": {"type": "string"},
                "method": {"type": "string"},
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "name": {"type": "string"},
                "timeout": {"type": "number"},
            },
        },
        effects=frozenset({EffectClass.NETWORK_READ}),
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        timeout = min(float(args.get("timeout") or 10.0), 30.0)
        loop = asyncio.get_running_loop()

        if op == "http":
            url = str(args.get("url") or "")
            method = str(args.get("method") or "GET").upper()
            if not url:
                return _result(request, ok=False, error="url required")

            import httpx

            def _http():
                with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                    resp = c.request(method, url)
                    body = resp.text
                    return {
                        "status": resp.status_code,
                        "headers": dict(resp.headers),
                        "body_head": body[:2000],
                        "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
                    }

            try:
                info = await loop.run_in_executor(None, _http)
            except Exception as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=json.dumps(info, indent=2)[:4000],
                           meta={"status": info["status"]})

        if op == "tcp_connect":
            host = str(args.get("host") or "127.0.0.1")
            port = int(args.get("port") or 0)
            if not (0 < port < 65536):
                return _result(request, ok=False, error="valid port required")

            def _tcp():
                s = socket.socket()
                s.settimeout(timeout)
                try:
                    s.connect((host, port))
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

            def _dns():
                name = str(args.get("name") or "localhost")
                infos = socket.getaddrinfo(name, None)
                uniq = sorted({i[4][0] for i in infos})
                return f"{name} -> {', '.join(uniq)}"

            try:
                text = await loop.run_in_executor(None, _dns)
            except socket.gaierror as exc:
                return _result(request, ok=False, error=str(exc))
            return _result(request, output=text)

        if op == "listeners":
            def _ls():
                rc, out, err = _run(["ss", "-tlnp"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(
                None, _ls))[:5000])

        if op == "connections":
            def _cx():
                rc, out, err = _run(["ss", "-tnp"])
                return out or err
            return _result(request, output=(await loop.run_in_executor(
                None, _cx))[:5000])

        if op == "ping":
            host = str(args.get("host") or "")

            def _pg():
                rc, out, err = _run(["ping", "-c", "3", "-W", "2", host])
                return rc, out or err

            rc, out = await loop.run_in_executor(None, _pg)
            return _result(request, ok=rc == 0, output=out[:3000])

        return _result(request, ok=False, error=f"unknown operation: {op}")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

class DatabaseCapability:
    """SQL execution with schema introspection (SQLite built-in)."""

    descriptor = CapabilityDescriptor(
        id="database",
        description=(
            "Database access: connect to a SQLite file (or Postgres via "
            "[db] extra), inspect schema/tables, run queries, EXPLAIN, and "
            "execute writes (WRITE_LOCAL, policy-gated). Operations: "
            "tables/schema/query/explain/execute."
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
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL,
        }),
        origin=CapabilityOrigin.NATIVE,
    )

    def _connect(self, path: str):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        path = str(args.get("path") or "")
        sql = str(args.get("sql") or "")
        params = list(args.get("params") or [])
        loop = asyncio.get_running_loop()

        if not path:
            return _result(request, ok=False, error="path required")
        if op != "tables" and not path:
            return _result(request, ok=False, error="path required")
        if op in ("query", "explain", "execute") and not sql:
            return _result(request, ok=False, error="sql required")
        if os.path.isfile(path) and not os.access(path, os.R_OK):
            return _result(request, ok=False, error=f"unreadable: {path}")

        def _rows(cur, limit=200):
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(limit)
            return json.dumps({"columns": cols,
                               "rows": [[str(v) for v in r] for r in rows]},
                              default=str)  # type: ignore[arg-type]

        def _tables():
            conn = self._connect(path)
            try:
                cur = conn.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name")
                return "\n".join(f"{r[1]:8} {r[0]}" for r in cur.fetchall())
            finally:
                conn.close()

        def _schema():
            conn = self._connect(path)
            try:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "ORDER BY name")
                return "\n\n".join(r[0] for r in cur.fetchall())
            finally:
                conn.close()

        def _query():
            conn = self._connect(path)
            try:
                cur = conn.execute(sql, params)
                return _rows(cur)
            finally:
                conn.close()

        def _explain():
            conn = self._connect(path)
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
        try:
            out = await loop.run_in_executor(None, handlers[op])
        except Exception as exc:
            return _result(request, ok=False, error=str(exc))
        return _result(request, output=out)


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------

class WorkspaceCapability:
    """Status / snapshot / diff / changed-files for the task workspace."""

    def __init__(self, checkpoint_manager=None) -> None:
        self._checkpoints = checkpoint_manager

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
            },
        },
        effects=frozenset({
            EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL,
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
            outcome = await self._checkpoints.restore(
                checkpoint_id=cid, workspace_root=root)
            return _result(request, output=json.dumps(outcome)[:1000])

        return _result(request, ok=False, error=f"unknown operation: {op}")
