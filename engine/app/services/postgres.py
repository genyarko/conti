"""Postgres-backed audit + trace storage.

Replaces the JSONL/in-memory backends in `audit.py` for production deploys —
the engine pod can be killed between a `/verify` call and a
`/verify/trace/{request_id}` lookup and the trace still resolves.

Activated automatically when `DATABASE_URL` is set; otherwise the
file/in-memory backends remain in place.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from engine.app.models.schemas import VerifyTrace

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    model       TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx       ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS audit_events_endpoint_idx ON audit_events (endpoint);
CREATE INDEX IF NOT EXISTS audit_events_request_idx  ON audit_events (request_id);

CREATE TABLE IF NOT EXISTS verify_traces (
    request_id  TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    payload     JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS verify_traces_expires_idx ON verify_traces (expires_at);
"""


class PostgresStore:
    """Owns the asyncpg connection pool and DDL bootstrap.

    One instance per process. `connect()` runs DDL idempotently so a fresh
    Neon/Supabase database needs no manual migration step.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        statement_cache_size: int = 0,
    ) -> None:
        self._dsn = dsn
        self._min = max(1, int(min_size))
        self._max = max(self._min, int(max_size))
        # Neon (and other PgBouncer-fronted Postgres) can't reuse prepared
        # statements across pooled connections — disabling the cache avoids
        # `prepared statement "__asyncpg_stmt_…" does not exist` errors.
        self._stmt_cache = max(0, int(statement_cache_size))
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min,
            max_size=self._max,
            statement_cache_size=self._stmt_cache,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError(
                "PostgresStore is not connected — call `await connect()` first."
            )
        return self._pool

    async def sweep_expired_traces(self) -> int:
        """Delete trace rows past their TTL. Returns number of rows deleted."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH d AS (
                    DELETE FROM verify_traces WHERE expires_at < now() RETURNING 1
                )
                SELECT count(*) AS n FROM d
                """
            )
        return int(row["n"]) if row else 0


class PgAuditLog:
    """Postgres-backed audit log. Mirrors the AuditLog interface.

    `append` is async but typically fired-and-forgotten by the request
    handler — we don't want to add a DB round-trip to the response path.
    `read_tail` is awaited from `/audit/events`.
    """

    def __init__(self, store: PostgresStore, *, enabled: bool = True) -> None:
        self._store = store
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def append(self, record: dict[str, Any]) -> None:
        if not self._enabled:
            return
        record.setdefault("timestamp", _utc_now_iso())
        ts = _parse_iso(record["timestamp"]) or datetime.now(tz=timezone.utc)
        payload = json.dumps(record, separators=(",", ":"), default=_json_default)
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_events (request_id, endpoint, model, ts, payload)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    """,
                    str(record.get("request_id", "")),
                    str(record.get("endpoint", "")),
                    record.get("model"),
                    ts,
                    payload,
                )
        except Exception as exc:  # noqa: BLE001 — never fail the request on audit
            log.warning("audit insert failed: %s", exc)

    async def read_tail(
        self,
        *,
        limit: int = 100,
        since: Optional[datetime] = None,
        endpoint: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        limit = max(1, min(int(limit), 10_000))
        clauses = []
        params: list[Any] = []
        if since is not None:
            clauses.append(f"ts >= ${len(params) + 1}")
            params.append(since)
        if endpoint is not None:
            clauses.append(f"endpoint = ${len(params) + 1}")
            params.append(endpoint)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = (
            f"SELECT payload FROM audit_events {where} "
            f"ORDER BY ts DESC, id DESC LIMIT ${len(params)}"
        )
        try:
            async with self._store.pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception as exc:  # noqa: BLE001
            log.warning("audit read_tail failed: %s", exc)
            return []
        # Match AuditLog.read_tail's chronological order (oldest first).
        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            out.append(payload)
        return out


class PgTraceStore:
    """Postgres-backed trace store. Mirrors the TraceStore interface."""

    def __init__(
        self,
        store: PostgresStore,
        *,
        ttl_seconds: int,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._ttl = max(1, int(ttl_seconds))
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def save(self, trace: VerifyTrace) -> None:
        if not self._enabled:
            return
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=self._ttl)
        payload = trace.model_dump(mode="json")
        body = json.dumps(payload, separators=(",", ":"), default=_json_default)
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO verify_traces (request_id, endpoint, payload, expires_at)
                    VALUES ($1, $2, $3::jsonb, $4)
                    ON CONFLICT (request_id) DO UPDATE
                       SET endpoint   = EXCLUDED.endpoint,
                           payload    = EXCLUDED.payload,
                           expires_at = EXCLUDED.expires_at
                    """,
                    trace.request_id,
                    trace.endpoint,
                    body,
                    expires_at,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace save failed for %s: %s", trace.request_id, exc)

    async def get(self, request_id: str) -> Optional[VerifyTrace]:
        if not self._enabled:
            return None
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT payload, expires_at FROM verify_traces
                     WHERE request_id = $1
                    """,
                    request_id,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace get failed for %s: %s", request_id, exc)
            return None
        if row is None:
            return None
        if row["expires_at"] < datetime.now(tz=timezone.utc):
            # Lazily ignore expired rows even before the sweeper runs.
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return VerifyTrace.model_validate(payload)


async def run_trace_sweeper(
    store: PostgresStore,
    *,
    interval_seconds: int,
) -> None:
    """Background coroutine that periodically deletes expired trace rows.

    Started in `lifespan` and cancelled on shutdown. Logs a warning on each
    failure but stays alive — a transient DB blip should not kill the engine.
    """
    interval = max(15, int(interval_seconds))
    log.info("trace sweeper started (interval=%ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            n = await store.sweep_expired_traces()
            if n:
                log.info("trace sweeper deleted %d expired rows", n)
        except asyncio.CancelledError:
            log.info("trace sweeper stopping")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("trace sweeper iteration failed: %s", exc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
