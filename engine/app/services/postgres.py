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
from engine.app.services.r2 import R2Client

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    api_key_id  TEXT,
    endpoint    TEXT NOT NULL,
    model       TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB NOT NULL,
    security_risk_score      TEXT,
    security_intent_detected TEXT,
    security_intent_declared TEXT,
    security_action          TEXT,
    security_intent_mismatch BOOLEAN DEFAULT false
);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx       ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS audit_events_endpoint_idx ON audit_events (endpoint);
CREATE INDEX IF NOT EXISTS audit_events_request_idx  ON audit_events (request_id);
CREATE INDEX IF NOT EXISTS audit_events_key_ts_idx  ON audit_events (api_key_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_events_mismatch_idx ON audit_events (security_intent_mismatch) WHERE security_intent_mismatch = true;
CREATE INDEX IF NOT EXISTS audit_events_risk_idx     ON audit_events (security_risk_score);

CREATE TABLE IF NOT EXISTS api_keys (
    id              TEXT PRIMARY KEY,
    hashed_secret   BYTEA NOT NULL,
    name            TEXT,
    daily_usd_cap   NUMERIC(12,4),
    daily_token_cap BIGINT,
    monthly_usd_cap NUMERIC(12,4),
    scopes          TEXT[],
    disabled_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes TEXT[];

CREATE TABLE IF NOT EXISTS idempotency_keys (
    api_key_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    BYTEA NOT NULL,
    status_code     INTEGER,
    body            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_key_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idempotency_keys_expires_idx ON idempotency_keys (expires_at);

CREATE TABLE IF NOT EXISTS verify_traces (
    request_id  TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    api_key_id  TEXT,
    payload     JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);
ALTER TABLE verify_traces ADD COLUMN IF NOT EXISTS api_key_id TEXT;
CREATE INDEX IF NOT EXISTS verify_traces_expires_idx ON verify_traces (expires_at);
CREATE INDEX IF NOT EXISTS verify_traces_key_idx     ON verify_traces (api_key_id);

CREATE TABLE IF NOT EXISTS response_cache (
    key         TEXT PRIMARY KEY,
    body        JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS response_cache_expires_idx ON response_cache (expires_at);

CREATE TABLE IF NOT EXISTS rate_events (
    key         TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rate_events_key_ts_idx ON rate_events (key, ts);
CREATE INDEX IF NOT EXISTS rate_events_ts_idx     ON rate_events (ts);

-- Provisional budget holds. Inserted atomically before a /verify call runs;
-- deleted when the call resolves and the audit row carries the real spend.
-- An expires_at TTL bounds the over-spend window if a request crashes
-- before reaching the release path.
CREATE TABLE IF NOT EXISTS budget_reservations (
    id               BIGSERIAL PRIMARY KEY,
    api_key_id       TEXT NOT NULL,
    estimated_usd    NUMERIC(12,4) NOT NULL DEFAULT 0,
    estimated_tokens BIGINT NOT NULL DEFAULT 0,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS budget_reservations_key_idx     ON budget_reservations (api_key_id, expires_at);
CREATE INDEX IF NOT EXISTS budget_reservations_expires_idx ON budget_reservations (expires_at);
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

    async def sweep_expired(self, *, rate_window_seconds: int = 60) -> int:
        """Delete expired rows across trace, cache, and rate-event tables.

        Single CTE: each DELETE is its own data-modifying CTE in the same
        snapshot; the final SELECT sums their RETURNING row counts.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH t AS (
                    DELETE FROM verify_traces WHERE expires_at < now() RETURNING 1
                ),
                c AS (
                    DELETE FROM response_cache WHERE expires_at < now() RETURNING 1
                ),
                r AS (
                    DELETE FROM rate_events
                     WHERE ts < now() - make_interval(secs => $1) RETURNING 1
                ),
                i AS (
                    DELETE FROM idempotency_keys WHERE expires_at < now() RETURNING 1
                ),
                b AS (
                    DELETE FROM budget_reservations WHERE expires_at < now() RETURNING 1
                )
                SELECT
                    (SELECT count(*) FROM t)
                  + (SELECT count(*) FROM c)
                  + (SELECT count(*) FROM r)
                  + (SELECT count(*) FROM i)
                  + (SELECT count(*) FROM b) AS n
                """,
                int(rate_window_seconds),
            )
        return int(row["n"]) if row else 0


class PgBudgetStore:
    """Postgres-backed budget tracker.

    Spend = audit_events sum (real cost from completed calls) + budget_reservations
    sum (provisional holds for in-flight calls). The hold is taken atomically
    in a single CTE so concurrent requests can't both observe a fresh
    "under-budget" snapshot and tunnel past the cap.
    """

    def __init__(self, store: PostgresStore) -> None:
        self._store = store

    async def get_usage(
        self, api_key_id: str
    ) -> tuple[float, int]:
        """Return (daily_usd_spent, daily_tokens_spent), inclusive of
        in-flight reservations so X-Budget-* headers reflect the true
        committed state."""
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    WITH spent AS (
                        SELECT
                            COALESCE(SUM((payload->>'estimated_cost_usd')::numeric), 0) AS usd,
                            COALESCE(SUM((payload->>'total_tokens')::bigint), 0) AS tokens
                        FROM audit_events
                        WHERE api_key_id = $1 AND ts > now() - interval '1 day'
                    ),
                    reserved AS (
                        SELECT
                            COALESCE(SUM(estimated_usd), 0) AS usd,
                            COALESCE(SUM(estimated_tokens), 0) AS tokens
                        FROM budget_reservations
                        WHERE api_key_id = $1 AND expires_at > now()
                    )
                    SELECT
                        spent.usd + reserved.usd AS usd,
                        spent.tokens + reserved.tokens AS tokens
                    FROM spent, reserved
                    """,
                    api_key_id,
                )
                if row:
                    return float(row["usd"]), int(row["tokens"])
        except Exception as exc:  # noqa: BLE001
            log.warning("budget lookup failed for %s: %s", api_key_id, exc)
        return 0.0, 0

    async def try_reserve(
        self,
        api_key_id: str,
        *,
        usd_cap: Optional[float],
        token_cap: Optional[int],
        estimated_usd: float,
        estimated_tokens: int,
        ttl_seconds: int,
    ) -> tuple[Optional[int], float, int]:
        """Atomically check spend + reserved against caps and insert a
        reservation iff the new request would still fit.

        Returns (reservation_id, total_usd, total_tokens). reservation_id is
        None when the cap was hit — total_* reflect the snapshot used for
        the decision so callers can populate response headers.

        Failure mode: fail-CLOSED. A DB error returns (None, 0, 0) which
        the middleware treats as cap exceeded — same posture as the rate
        limiter.
        """
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=max(1, ttl_seconds))
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    WITH spent AS (
                        SELECT
                            COALESCE(SUM((payload->>'estimated_cost_usd')::numeric), 0) AS usd,
                            COALESCE(SUM((payload->>'total_tokens')::bigint), 0) AS tokens
                        FROM audit_events
                        WHERE api_key_id = $1 AND ts > now() - interval '1 day'
                    ),
                    reserved AS (
                        SELECT
                            COALESCE(SUM(estimated_usd), 0) AS usd,
                            COALESCE(SUM(estimated_tokens), 0) AS tokens
                        FROM budget_reservations
                        WHERE api_key_id = $1 AND expires_at > now()
                    ),
                    inserted AS (
                        INSERT INTO budget_reservations (
                            api_key_id, estimated_usd, estimated_tokens, expires_at
                        )
                        SELECT $1, $4, $5, $6
                        FROM spent, reserved
                        WHERE
                            ($2::numeric IS NULL OR spent.usd + reserved.usd + $4 <= $2)
                            AND ($3::bigint IS NULL OR spent.tokens + reserved.tokens + $5 <= $3)
                        RETURNING id
                    )
                    SELECT
                        spent.usd + reserved.usd AS total_usd,
                        spent.tokens + reserved.tokens AS total_tokens,
                        (SELECT id FROM inserted) AS reservation_id
                    FROM spent, reserved
                    """,
                    api_key_id,
                    usd_cap,
                    token_cap,
                    estimated_usd,
                    estimated_tokens,
                    expires_at,
                )
        except Exception as exc:  # noqa: BLE001 — fail-closed
            log.warning("budget reserve failed for %s: %s", api_key_id, exc)
            return None, 0.0, 0
        if row is None:
            return None, 0.0, 0
        rid = row["reservation_id"]
        return (
            int(rid) if rid is not None else None,
            float(row["total_usd"]),
            int(row["total_tokens"]),
        )

    async def release(self, reservation_id: int) -> None:
        """Drop a reservation. Idempotent: a missing row is a no-op (the
        sweeper may have already collected it after a crash)."""
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM budget_reservations WHERE id = $1",
                    int(reservation_id),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("budget release failed for id=%s: %s", reservation_id, exc)


class PgIdempotencyStore:
    """Postgres-backed idempotency key store."""

    def __init__(self, store: PostgresStore, *, ttl_seconds: int) -> None:
        self._store = store
        self._ttl = max(1, int(ttl_seconds))

    async def get(
        self, api_key_id: str, idempotency_key: str
    ) -> Optional[dict[str, Any]]:
        """Return (status_code, body, request_hash) or None."""
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT status_code, body, request_hash
                    FROM idempotency_keys
                    WHERE api_key_id = $1 AND idempotency_key = $2
                    """,
                    api_key_id,
                    idempotency_key,
                )
                if row:
                    return dict(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("idempotency get failed: %s", exc)
        return None

    async def create_in_flight(
        self, api_key_id: str, idempotency_key: str, request_hash: bytes
    ) -> bool:
        """INSERT a NULL status_code row. Return True if created, False on conflict."""
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=self._ttl)
        try:
            async with self._store.pool.acquire() as conn:
                ret = await conn.fetchval(
                    """
                    INSERT INTO idempotency_keys (api_key_id, idempotency_key, request_hash, expires_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    api_key_id,
                    idempotency_key,
                    request_hash,
                    expires_at,
                )
                return ret is not None
        except Exception as exc:  # noqa: BLE001
            log.warning("idempotency create failed: %s", exc)
            return False

    async def save_resolved(
        self,
        api_key_id: str,
        idempotency_key: str,
        status_code: int,
        body: dict[str, Any],
    ) -> None:
        payload = json.dumps(body, separators=(",", ":"), default=_json_default)
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET status_code = $3, body = $4::jsonb
                    WHERE api_key_id = $1 AND idempotency_key = $2
                    """,
                    api_key_id,
                    idempotency_key,
                    status_code,
                    payload,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("idempotency save failed: %s", exc)

    async def delete(self, api_key_id: str, idempotency_key: str) -> None:
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM idempotency_keys WHERE api_key_id = $1 AND idempotency_key = $2",
                    api_key_id,
                    idempotency_key,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("idempotency delete failed: %s", exc)


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
        api_key_id = record.get("api_key_id")
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_events (
                        request_id, api_key_id, endpoint, model, ts, payload,
                        security_risk_score, security_intent_detected, 
                        security_intent_declared, security_action, 
                        security_intent_mismatch
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
                    """,
                    str(record.get("request_id", "")),
                    api_key_id,
                    str(record.get("endpoint", "")),
                    record.get("model"),
                    ts,
                    payload,
                    record.get("security_risk_score"),
                    record.get("security_intent_detected"),
                    record.get("security_intent_declared"),
                    record.get("security_action"),
                    bool(record.get("security_intent_mismatch", False)),
                )
        except Exception as exc:  # noqa: BLE001 — never fail the request on audit
            log.warning("audit insert failed: %s", exc)

    async def read_tail(
        self,
        *,
        limit: int = 100,
        since: Optional[datetime] = None,
        endpoint: Optional[str] = None,
        api_key_id: Optional[str] = None,
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
        if api_key_id is not None:
            clauses.append(f"api_key_id = ${len(params) + 1}")
            params.append(api_key_id)
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

    async def offload_old_records(
        self, r2: R2Client, *, age_days: int, limit: int = 5000
    ) -> int:
        """Move old audit records to R2 cold storage and delete from Postgres.

        Records older than `age_days` are selected in batches. They are
        formatted as a single JSONL blob and uploaded to R2 with a key
        reflecting the current date. If upload succeeds, the records are
        purged from the database.
        """
        if not self._enabled or not await r2.is_configured():
            return 0

        async with self._store.pool.acquire() as conn:
            # 1. Fetch old IDs and payloads.
            rows = await conn.fetch(
                """
                SELECT id, payload FROM audit_events
                 WHERE ts < now() - make_interval(days => $1)
                 ORDER BY ts ASC LIMIT $2
                """,
                int(age_days),
                int(limit),
            )
            if not rows:
                return 0

            # 2. Format as JSONL.
            lines = []
            max_id = 0
            min_id = rows[0]["id"]
            for r in rows:
                p = r["payload"]
                if isinstance(p, dict):
                    lines.append(json.dumps(p, separators=(",", ":"), default=_json_default))
                else:
                    lines.append(str(p))
                max_id = max(max_id, r["id"])

            content = "\n".join(lines) + "\n"
            
            # 3. Upload to R2. Partition by date/time to avoid collisions.
            now = datetime.now(tz=timezone.utc)
            date_path = now.strftime("%Y/%m/%d")
            key = f"audit/{date_path}/audit_{now.strftime('%H%M%S')}_{min_id}_{max_id}.jsonl"
            
            success = await r2.upload_jsonl(key, content)
            if not success:
                return 0

            # 4. Purge from Postgres.
            res = await conn.execute(
                "DELETE FROM audit_events WHERE id >= $1 AND id <= $2",
                min_id,
                max_id,
            )
            # asyncpg.execute returns a string like "DELETE 123"
            try:
                count = int(res.split()[-1])
            except (ValueError, IndexError):
                count = len(rows)
                
            log.info("offloaded %d audit record(s) to R2 cold storage", count)
            return count


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

    async def save(
        self, trace: VerifyTrace, *, api_key_id: Optional[str] = None
    ) -> None:
        if not self._enabled:
            return
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=self._ttl)
        payload = trace.model_dump(mode="json")
        body = json.dumps(payload, separators=(",", ":"), default=_json_default)
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO verify_traces (request_id, endpoint, api_key_id, payload, expires_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    ON CONFLICT (request_id) DO UPDATE
                       SET endpoint   = EXCLUDED.endpoint,
                           api_key_id = EXCLUDED.api_key_id,
                           payload    = EXCLUDED.payload,
                           expires_at = EXCLUDED.expires_at
                    """,
                    trace.request_id,
                    trace.endpoint,
                    api_key_id,
                    body,
                    expires_at,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace save failed for %s: %s", trace.request_id, exc)

    async def get(
        self, request_id: str, *, api_key_id: Optional[str] = None
    ) -> Optional[VerifyTrace]:
        if not self._enabled:
            return None
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT payload, expires_at, api_key_id FROM verify_traces
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
        # Tenant isolation: when the caller's key is provided, only the owning
        # key (or legacy NULL-keyed traces written before isolation existed)
        # can read the trace. Returning None — not 403 — keeps existence
        # private across tenants.
        if api_key_id is not None:
            stored = row["api_key_id"]
            if stored is not None and stored != api_key_id:
                return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return VerifyTrace.model_validate(payload)


class PgCache:
    """Postgres-backed TTL cache. Mirrors the TTLCache async interface.

    Hits/misses are tracked per process — there's no DB-side counter row to
    avoid hot-row contention. For multi-replica metric aggregation, sum the
    per-replica `/health` numbers at the dashboard layer.

    Failure mode: best-effort. A DB error on `get` returns a miss (and the
    pipeline runs for real); a DB error on `set` is logged and ignored.
    Caching is by definition non-load-bearing for correctness.
    """

    def __init__(self, store: PostgresStore, *, ttl_seconds: int) -> None:
        self._store = store
        self._ttl = max(1, int(ttl_seconds))
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Optional[dict]:
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT body FROM response_cache "
                    "WHERE key = $1 AND expires_at > now()",
                    key,
                )
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            log.warning("cache get failed: %s", exc)
            self.misses += 1
            return None
        if row is None:
            self.misses += 1
            return None
        payload = row["body"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.hits += 1
        return payload

    async def set(self, key: str, value: dict) -> None:
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=self._ttl)
        body = json.dumps(value, separators=(",", ":"), default=_json_default)
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO response_cache (key, body, expires_at)
                    VALUES ($1, $2::jsonb, $3)
                    ON CONFLICT (key) DO UPDATE
                       SET body       = EXCLUDED.body,
                           expires_at = EXCLUDED.expires_at
                    """,
                    key,
                    body,
                    expires_at,
                )
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            log.warning("cache set failed: %s", exc)

    async def clear(self) -> None:
        self.hits = 0
        self.misses = 0
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute("DELETE FROM response_cache")
        except Exception as exc:  # noqa: BLE001
            log.warning("cache clear failed: %s", exc)

    async def get_size(self) -> int:
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT count(*) AS n FROM response_cache "
                    "WHERE expires_at > now()"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("cache size failed: %s", exc)
            return 0
        return int(row["n"]) if row else 0


class PgRateLimiter:
    """Postgres-backed per-key sliding-window rate limiter.

    Atomic check-and-insert in a single CTE statement: the `counted` CTE
    snapshots the in-window event count, the `inserted` CTE inserts a new
    event iff that count is under the limit. Concurrent transactions on the
    same key may both see the same snapshot and both insert (READ COMMITTED),
    so the effective limit can briefly exceed `limit_per_minute` by 1–2 under
    burst load. That's accepted — the limiter exists to bound spend, not to
    enforce exact accounting. If exactness is needed, wrap the call in a
    `pg_advisory_xact_lock(hashtext(key))` inside the same transaction.

    Failure mode: fail-CLOSED. A DB error returns 429. The limiter sits on
    paid LLM calls — silently disabling it during a DB blip is the wrong
    direction.
    """

    def __init__(
        self,
        store: PostgresStore,
        *,
        limit_per_minute: int,
        window_seconds: int = 60,
    ) -> None:
        self._store = store
        self._limit = max(1, int(limit_per_minute))
        self._window = max(1, int(window_seconds))

    async def check(self, key: str) -> tuple[bool, int, float]:
        """Return (allowed, remaining, retry_after_seconds)."""
        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    WITH counted AS (
                        SELECT count(*) AS n, min(ts) AS oldest_ts
                          FROM rate_events
                         WHERE key = $1
                           AND ts > now() - make_interval(secs => $3)
                    ),
                    inserted AS (
                        INSERT INTO rate_events (key, ts)
                        SELECT $1, now() FROM counted WHERE counted.n < $2
                        RETURNING 1
                    )
                    SELECT
                        counted.n         AS n,
                        counted.oldest_ts AS oldest_ts,
                        EXISTS (SELECT 1 FROM inserted) AS allowed
                    FROM counted
                    """,
                    key,
                    self._limit,
                    self._window,
                )
        except Exception as exc:  # noqa: BLE001 — fail-closed
            log.warning("rate limiter DB error, failing closed: %s", exc)
            return False, 0, float(self._window)

        if row is None:
            # `counted` always returns one row (count() over empty set = 0),
            # so this branch is defensive. Treat as allow-with-empty-window.
            return True, self._limit - 1, 0.0

        n = int(row["n"])
        allowed = bool(row["allowed"])
        if allowed:
            return True, max(0, self._limit - n - 1), 0.0

        oldest_ts = row["oldest_ts"]
        retry_after = 0.0
        if oldest_ts is not None:
            now = datetime.now(tz=timezone.utc)
            retry_after = max(
                0.0,
                (oldest_ts + timedelta(seconds=self._window) - now).total_seconds(),
            )
        return False, 0, retry_after

    async def reset(self) -> None:
        try:
            async with self._store.pool.acquire() as conn:
                await conn.execute("DELETE FROM rate_events")
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limiter reset failed: %s", exc)


async def run_pg_sweeper(
    store: PostgresStore,
    *,
    interval_seconds: int,
    rate_window_seconds: int = 60,
) -> None:
    """Background coroutine that periodically deletes expired ephemeral rows
    from `verify_traces`, `response_cache`, and `rate_events`.

    Started in `lifespan` and cancelled on shutdown. Logs a warning on each
    failure but stays alive — a transient DB blip should not kill the engine.
    """
    interval = max(15, int(interval_seconds))
    log.info(
        "pg sweeper started (interval=%ss, rate_window=%ss)",
        interval,
        rate_window_seconds,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            n = await store.sweep_expired(rate_window_seconds=rate_window_seconds)
            if n:
                log.info("pg sweeper deleted %d expired rows", n)
        except asyncio.CancelledError:
            log.info("pg sweeper stopping")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("pg sweeper iteration failed: %s", exc)


async def run_audit_offloader(
    audit_log: PgAuditLog,
    r2: R2Client,
    *,
    interval_seconds: int,
    age_days: int,
) -> None:
    """Background coroutine that periodically offloads old audit records to R2.

    Started in `lifespan` and cancelled on shutdown.
    """
    interval = max(60, int(interval_seconds))
    log.info(
        "audit offloader started (interval=%ss, age=%sd)",
        interval,
        age_days,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            if not await r2.is_configured():
                continue
            n = await audit_log.offload_old_records(r2, age_days=age_days)
            if n:
                log.info("audit offloader moved %d rows to R2", n)
        except asyncio.CancelledError:
            log.info("audit offloader stopping")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("audit offloader iteration failed: %s", exc)


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
