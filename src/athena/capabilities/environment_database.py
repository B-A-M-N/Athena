"""Database capability family: SQL against SQLite/Postgres-compatible
endpoints (P1-10). Schema introspection; writes are policy-gated WRITE_LOCAL."""

from athena.protocol.capabilities import CapabilityDescriptor
from athena.protocol.capabilities import CapabilityOrigin
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.capabilities import CapabilityResult
from athena.protocol.capabilities import CapabilityResultStatus
from athena.protocol.capabilities import EffectClass
from athena.protocol.capabilities import ExternalEffectContract
from athena.protocol.capabilities import ExternalEffectPhase
from athena.protocol.ids import new_id
from athena.state.external_effects import ExternalEffectRecoveryRequired
from athena.state.external_effects import ExternalEffectStore
from pathlib import Path
from typing import Any
import asyncio
import hashlib
import json
import os

from athena.capabilities.environment_common import _has_symlink_component
from athena.capabilities.environment_common import _legacy_idempotency_key
from athena.capabilities.environment_common import _result
from athena.capabilities.environment_common import _database_request_digest
from athena.capabilities.environment_common import _external_receipt_result
from athena.capabilities.environment_common import _database_effects


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
                "operation": {
                    "type": "string",
                    "enum": [
                        "tables",
                        "schema",
                        "query",
                        "explain",
                        "execute",
                        "database_transaction",
                    ],
                },
                "path": {"type": "string"},
                "sql": {"type": "string"},
                "params": {"type": "array"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "phase": {
                    "type": "string",
                    "enum": [
                        "inspect",
                        "prepare",
                        "dry_run",
                        "apply",
                        "verify",
                        "compensate",
                    ],
                },
                "transaction_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "external_identity": {"type": "string", "minLength": 1, "maxLength": 512},
                "verify_sql": {"type": "string", "maxLength": 100_000},
                "verify_params": {"type": "array"},
                "expected_after_hash": {"type": "string", "maxLength": 128},
                "expected_rowcount": {"type": "integer", "minimum": 0},
                "expected_value": {},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "operation": {"const": "database_transaction"},
                        }
                    },
                    "then": {"required": ["phase"]},
                }
            ],
        },
        effects=frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
            }
        ),
        effect_resolver=_database_effects,
        external_effects={
            "database_transaction": ExternalEffectContract(
                phases=frozenset(ExternalEffectPhase),
                idempotency_required=True,
                reversible=True,
                approval_floor="ask",
                identity_fields=("path", "sql"),
            ),
        },
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        *,
        mutation_store=None,
        artifact_store=None,
        external_store: ExternalEffectStore | None = None,
    ) -> None:
        self._mutations = mutation_store
        self._artifacts = artifact_store
        self._external_store = external_store or ExternalEffectStore()

    @staticmethod
    def _read_only_authorizer(action, arg1, arg2, database, source):
        import sqlite3

        denied = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE,
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

            conn = sqlite3.connect(  # architecture-lint: allow raw-db-outside-state reason=read-only task database view
                f"file:{quote(path, safe='/')}?mode=ro", uri=True
            )
        else:
            conn = sqlite3.connect(  # architecture-lint: allow raw-db-outside-state reason=read-only task database view
                path
            )
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _confine_path(path: str, context) -> str:
        root = getattr(getattr(context, "workspace", None), "root", None)
        candidate = (
            os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
            if root
            else os.path.realpath(path)
        )
        if root:
            root = os.path.realpath(root)
            if candidate != root and not candidate.startswith(root + os.sep):
                raise ValueError("database path outside workspace")
            original = path if os.path.isabs(path) else os.path.join(root, path)
            if _has_symlink_component(original, root):
                raise ValueError("database path cannot traverse a symlink")
        return candidate

    async def _snapshot(self, path: str, task_id: str | None, *, persist: bool = True):
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
        if op == "database_transaction":
            return await self._database_transaction(request, args, context)
        if op == "execute":
            return await self._legacy_database_mutation(request, args, context)
        path = str(args.get("path") or "")
        sql = str(args.get("sql") or "")
        params = list(args.get("params") or [])
        offset = int(args.get("offset") or 0)
        limit = min(int(args.get("limit") or 200), 1000)
        workspace = getattr(context, "workspace", None) if context is not None else None
        if workspace is None or not getattr(workspace, "root", None):
            return _result(
                request,
                ok=False,
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
            return _result(
                request, ok=False, error=f"database does not exist for read operation: {path}"
            )
        if os.path.isfile(path) and not os.access(path, os.R_OK):
            return _result(request, ok=False, error=f"unreadable: {path}")

        def _rows(cur):
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(offset + limit + 1)
            truncated = len(rows) > offset + limit
            rows = rows[offset : offset + limit]
            return json.dumps(
                {
                    "columns": cols,
                    "rows": [[str(v) for v in r] for r in rows],
                    "offset": offset,
                    "limit": limit,
                    "truncated": truncated,
                },
                default=str,
            )

        def _tables():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name "
                    "LIMIT ? OFFSET ?",
                    (limit + 1, offset),
                )
                rows = cur.fetchall()
                return json.dumps(
                    {
                        "rows": [[r[1], r[0]] for r in rows[:limit]],
                        "offset": offset,
                        "limit": limit,
                        "truncated": len(rows) > limit,
                    }
                )
            finally:
                conn.close()

        def _schema():
            conn = self._connect(path, readonly=True)
            self._set_read_only_authorizer(conn)
            try:
                cur = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                    "ORDER BY name LIMIT ? OFFSET ?",
                    (limit + 1, offset),
                )
                rows = cur.fetchall()
                return json.dumps(
                    {
                        "statements": [r[0] for r in rows[:limit]],
                        "offset": offset,
                        "limit": limit,
                        "truncated": len(rows) > limit,
                    }
                )
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

        handlers = {
            "tables": _tables,
            "schema": _schema,
            "query": _query,
            "explain": _explain,
            "execute": _execute,
        }
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
                        if before_ref
                        else {"op": "delete", "target": path}
                    ),
                )
                await self._mutations.mark_started(mutation_id)
        try:
            await asyncio.sleep(0)
            out = handlers[op]()
        except Exception as exc:  # noqa: BLE001 - report database failure truthfully
            if mutation_id is not None:
                await self._mutations.mark_failed(mutation_id, str(exc))
            return _result(request, ok=False, error=str(exc))
        if mutation_id is not None:
            _after_ref, after_hash = await self._snapshot(path, request.task_id, persist=False)
            reversible = before_ref is not None or before_hash is None
            await self._mutations.complete(
                mutation_id,
                after_hash=after_hash,
                reversible=reversible,
                inverse=(
                    {"op": "restore_from_ref", "target": path, "ref": before_ref}
                    if before_ref
                    else {"op": "delete", "target": path}
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

    async def _legacy_database_mutation(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        context=None,
    ) -> CapabilityResult:
        """Lower the legacy database execute operation into a durable transaction."""
        base = {
            **args,
            "operation": "database_transaction",
            "idempotency_key": _legacy_idempotency_key(
                "database",
                request,
                args,
            ),
        }
        prepared = await self._database_transaction(
            request,
            {**base, "phase": "prepare"},
            context,
        )
        if prepared.status is not CapabilityResultStatus.OK:
            return prepared
        try:
            transaction_id = str(json.loads(prepared.output)["transaction_id"])
        except (KeyError, TypeError, ValueError):
            return _result(
                request,
                ok=False,
                error="database preparation receipt is malformed",
            )
        applied = await self._database_transaction(
            request,
            {**base, "phase": "apply", "transaction_id": transaction_id},
            context,
        )
        if applied.status is not CapabilityResultStatus.OK:
            return applied
        try:
            receipt = json.loads(applied.output)
            response = dict(receipt.get("response") or {})
        except (TypeError, ValueError):
            return applied

        # Keep the legacy mutation ledger populated for callers that inspect
        # database.execute history. The external receipt remains the source
        # of truth for crash/replay safety.
        metadata = dict(applied.metadata or {})
        if self._mutations is not None:
            try:
                before_ref = response.get("before_ref")
                before_hash = response.get("before_hash")
                after_hash = response.get("after_hash")
                target = str(response.get("path") or args.get("path") or "")
                inverse = (
                    {"op": "restore_from_ref", "target": target, "ref": before_ref}
                    if before_ref
                    else {"op": "delete", "target": target}
                )
                mutation_id = await self._mutations.record_intent(
                    request.task_id,
                    target,
                    "database.execute",
                    before_ref=before_ref,
                    inverse=inverse,
                )
                await self._mutations.mark_started(mutation_id)
                await self._mutations.complete(
                    mutation_id,
                    after_hash=after_hash,
                    reversible=before_ref is not None or before_hash is None,
                    inverse=inverse,
                )
                metadata["mutation"] = {
                    "resource": target,
                    "operation": "database.execute",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "before_ref": before_ref,
                    "reversible": before_ref is not None or before_hash is None,
                    "mutation_id": mutation_id,
                }
            except Exception as exc:  # noqa: BLE001 - receipt is authoritative
                metadata["mutation_ledger_error"] = str(exc)
        return _result(
            request,
            output=json.dumps({"rowcount": response.get("rowcount", -1)}),
            meta=metadata,
        )

    async def _database_transaction(
        self,
        request: CapabilityRequest,
        args: dict[str, Any],
        context=None,
    ) -> CapabilityResult:
        """Execute one SQLite mutation through a durable external-effect protocol.

        A SQLite file is local, but it is outside Athena's shadow transaction:
        the database engine commits its own state.  The receipt therefore
        records the exact pre-image, post-image, stable idempotency key, and
        compensation boundary instead of treating a database mutation as an
        ordinary shadowable file write.
        """
        try:
            phase = ExternalEffectPhase(str(args.get("phase") or "").lower())
        except ValueError:
            return _result(request, ok=False, error="unknown database transaction phase")
        if phase is not ExternalEffectPhase.INSPECT and request.task_id is None:
            return _result(
                request,
                ok=False,
                error="database transactions require a task-scoped invocation",
            )
        workspace = getattr(context, "workspace", None) if context else None
        if workspace is None or not getattr(workspace, "root", None):
            return _result(
                request,
                ok=False,
                error="database access requires a workspace-bound invocation",
            )
        raw_path = str(args.get("path") or "")
        if not raw_path:
            return _result(request, ok=False, error="path required")
        try:
            path = self._confine_path(raw_path, context)
        except ValueError as exc:
            return _result(request, ok=False, error=str(exc))

        sql = str(args.get("sql") or "").strip()
        if not sql:
            return _result(request, ok=False, error="sql required")
        first = sql.split(None, 1)[0].upper() if sql else ""
        if first in {"SELECT", "VALUES", "EXPLAIN", "PRAGMA"}:
            return _result(
                request,
                ok=False,
                error="database transaction SQL must be mutating",
            )
        params = list(args.get("params") or [])
        request_digest = _database_request_digest(path, sql, params)
        external_identity = self.descriptor.resolve_external_identity(
            args,
            getattr(context, "workspace", None),
        )
        if external_identity is None:
            return _result(request, ok=False, error="database transaction identity unavailable")
        transaction_id = str(args.get("transaction_id") or "")
        idempotency_key = str(args.get("idempotency_key") or "") or None
        if (
            phase
            in {
                ExternalEffectPhase.PREPARE,
                ExternalEffectPhase.DRY_RUN,
                ExternalEffectPhase.APPLY,
                ExternalEffectPhase.COMPENSATE,
            }
            and not idempotency_key
        ):
            return _result(
                request,
                ok=False,
                error=f"database {phase.value} requires an idempotency_key",
            )
        if phase is not ExternalEffectPhase.INSPECT and not transaction_id:
            if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
                transaction_id = new_id("database-tx")
            else:
                return _result(request, ok=False, error="transaction_id required")

        contract = self.descriptor.resolve_external_effect_contract(args)
        if contract is None or phase not in contract.phases:
            return _result(request, ok=False, error="database transaction phase is unsupported")
        if phase is ExternalEffectPhase.INSPECT:
            return _result(
                request,
                output=json.dumps(
                    {
                        "phase": phase.value,
                        "transaction_id": transaction_id or new_id("database-tx"),
                        "external_identity": external_identity,
                        "request_digest": request_digest,
                        "idempotency_required": contract.idempotency_required,
                        "reversible": contract.reversible,
                        "approval_floor": contract.approval_floor,
                    }
                ),
            )

        if phase in {ExternalEffectPhase.PREPARE, ExternalEffectPhase.DRY_RUN}:
            before_exists = os.path.isfile(path)
            before_ref, before_hash = await self._snapshot(
                path,
                request.task_id,
            )
            if before_exists and before_ref is None:
                return _result(
                    request,
                    ok=False,
                    error="database transaction requires an artifact store to preserve its pre-image",
                )
            try:
                receipt = await self._external_store.prepare(
                    transaction_id=transaction_id,
                    task_id=request.task_id,
                    capability_id=request.capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key,
                    phase=phase,
                )
                response = {
                    "path": path,
                    "sql_hash": hashlib.sha256(sql.encode()).hexdigest(),
                    "before_exists": before_exists,
                    "before_ref": before_ref,
                    "before_hash": before_hash,
                    "params_digest": hashlib.sha256(
                        json.dumps(
                            params,
                            sort_keys=True,
                            default=str,
                        ).encode()
                    ).hexdigest(),
                }
                receipt = await self._external_store.finish(
                    transaction_id,
                    status=("PREPARED" if phase is ExternalEffectPhase.PREPARE else "DRY_RUN"),
                    response=response,
                    phase=phase,
                )
            except (ExternalEffectRecoveryRequired, KeyError, OSError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            return _external_receipt_result(request, receipt)

        if phase is ExternalEffectPhase.APPLY:
            try:
                receipt, replay = await self._external_store.begin_apply(
                    transaction_id=transaction_id,
                    task_id=request.task_id,
                    capability_id=request.capability_id,
                    external_identity=external_identity,
                    request_digest=request_digest,
                    idempotency_key=idempotency_key or "",
                )
            except (ExternalEffectRecoveryRequired, KeyError, ValueError) as exc:
                return _result(request, ok=False, error=str(exc))
            if replay:
                return _external_receipt_result(request, receipt)
            if receipt.get("status") != "APPLYING":
                return _result(
                    request,
                    ok=False,
                    error="database apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            try:

                def _apply() -> int:
                    conn = self._connect(path)
                    try:
                        cursor = conn.execute(sql, params)
                        conn.commit()
                        return int(cursor.rowcount)
                    finally:
                        conn.close()

                await asyncio.sleep(0)
                rowcount = _apply()
                _after_ref, after_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
                response = {
                    **dict(receipt.get("response") or {}),
                    "rowcount": rowcount,
                    "after_hash": after_hash,
                }
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="COMPLETED",
                    response=response,
                )
            except Exception as exc:  # noqa: BLE001 - commit outcome may be unknown
                receipt = await self._external_store.finish(
                    transaction_id,
                    status="RECOVERY_REQUIRED",
                    error=str(exc),
                )
                return _result(
                    request,
                    ok=False,
                    error="database apply outcome is uncertain; recovery required",
                    meta={"external_effect": receipt},
                )
            return _result(
                request,
                output=json.dumps(receipt),
                meta={"external_effect": receipt},
            )

        try:
            receipt = await self._external_store.begin_followup(
                transaction_id,
                task_id=request.task_id,
                capability_id=request.capability_id,
                request_digest=(request_digest if phase is ExternalEffectPhase.VERIFY else None),
                phase=phase,
            )
        except (ExternalEffectRecoveryRequired, KeyError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        if (
            phase is ExternalEffectPhase.VERIFY
            and receipt.get("status") in {"VERIFIED", "COMPENSATION_VERIFIED"}
        ) or (
            phase is ExternalEffectPhase.COMPENSATE
            and receipt.get("status")
            in {"COMPENSATED", "COMPENSATION_SENT", "COMPENSATION_VERIFIED"}
        ):
            return _external_receipt_result(request, receipt)

        payload = dict(receipt.get("response") or {})
        if phase is ExternalEffectPhase.VERIFY:
            compensation_verification = (
                receipt.get("verification_target") == "COMPENSATION_PRESTATE"
            )
            current_hash = None
            if os.path.isfile(path):
                _ref, current_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
            expected_hash = (
                str(payload.get("before_hash") or "")
                if compensation_verification
                else str(args.get("expected_after_hash") or payload.get("after_hash") or "")
            )
            verified = bool(current_hash and expected_hash == current_hash)
            if compensation_verification and not bool(payload.get("before_exists")):
                verified = not os.path.lexists(path)
            query_result: dict[str, Any] | None = None
            verify_sql = str(args.get("verify_sql") or "").strip()
            if verify_sql:
                verify_first = verify_sql.split(None, 1)[0].upper()
                if verify_first not in {"SELECT", "VALUES"}:
                    return _result(
                        request,
                        ok=False,
                        error="database verification SQL must be read-only",
                    )

                def _verify_query() -> dict[str, Any]:
                    conn = self._connect(path, readonly=True)
                    self._set_read_only_authorizer(conn)
                    try:
                        cursor = conn.execute(
                            verify_sql,
                            list(args.get("verify_params") or []),
                        )
                        columns = [item[0] for item in cursor.description or ()]
                        rows = cursor.fetchmany(101)
                        return {
                            "columns": columns,
                            "rows": [[str(value) for value in row] for row in rows[:100]],
                            "truncated": len(rows) > 100,
                        }
                    finally:
                        conn.close()

                try:
                    await asyncio.sleep(0)
                    query_result = _verify_query()
                    expected_rowcount = args.get("expected_rowcount")
                    expected_value = args.get("expected_value")
                    if expected_rowcount is not None:
                        verified = verified and len(query_result["rows"]) == int(expected_rowcount)
                    if expected_value is not None:
                        rows = query_result["rows"]
                        verified = (
                            verified
                            and bool(rows)
                            and bool(rows[0])
                            and rows[0][0] == str(expected_value)
                        )
                except Exception as exc:  # noqa: BLE001 - verification is evidence
                    query_result = {"error": str(exc)}
                    verified = False
            receipt = await self._external_store.finish(
                transaction_id,
                status=(
                    "COMPENSATION_VERIFIED"
                    if compensation_verification and verified
                    else "COMPENSATION_VERIFY_FAILED"
                    if compensation_verification
                    else "VERIFIED"
                    if verified
                    else "VERIFY_FAILED"
                ),
                response={
                    **payload,
                    "current_hash": current_hash,
                    "query": query_result,
                    "verified": verified,
                },
                phase=phase,
            )
            return _result(
                request,
                ok=verified,
                output=json.dumps(receipt),
                error="database verification failed" if not verified else None,
                meta={"external_effect": receipt},
            )

        expected_after = str(payload.get("after_hash") or "")
        _ref, current_hash = await self._snapshot(
            path,
            request.task_id,
            persist=False,
        )
        if expected_after and current_hash != expected_after:
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error="database changed after apply; compensation was not attempted",
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="database changed after apply; recovery required",
                meta={"external_effect": receipt},
            )
        try:
            before_exists = bool(payload.get("before_exists"))
            before_ref = payload.get("before_ref")
            if before_exists:
                if not before_ref or self._artifacts is None:
                    raise ValueError("database pre-image is unavailable for compensation")
                data = await self._artifacts.load(str(before_ref))
                await asyncio.sleep(0)
                self._restore_database_file(path, data)
            elif os.path.lexists(path):
                os.unlink(path)
            restored_hash = None
            if before_exists:
                _ref, restored_hash = await self._snapshot(
                    path,
                    request.task_id,
                    persist=False,
                )
                restored = restored_hash == str(payload.get("before_hash") or "")
            else:
                restored = not os.path.lexists(path)
            receipt = await self._external_store.finish(
                transaction_id,
                status="COMPENSATION_VERIFIED" if restored else "RECOVERY_REQUIRED",
                response={
                    **payload,
                    "restored_hash": restored_hash,
                    "restored": restored,
                },
                phase=phase,
            )
            if not restored:
                return _result(
                    request,
                    ok=False,
                    output=json.dumps(receipt),
                    error="database compensation was not verified; recovery required",
                    meta={"external_effect": receipt},
                )
        except Exception as exc:  # noqa: BLE001 - restore outcome may be uncertain
            receipt = await self._external_store.finish(
                transaction_id,
                status="RECOVERY_REQUIRED",
                error=str(exc),
                phase=phase,
            )
            return _result(
                request,
                ok=False,
                error="database compensation outcome is uncertain; recovery required",
                meta={"external_effect": receipt},
            )
        return _result(
            request,
            output=json.dumps(receipt),
            meta={"external_effect": receipt},
        )

    @staticmethod
    def _restore_database_file(path: str, data: bytes) -> None:
        """Restore one captured SQLite image without exposing arbitrary paths."""
        parent = os.path.dirname(path) or "."
        temporary = os.path.join(parent, f".athena-restore-{new_id('db')}")
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
