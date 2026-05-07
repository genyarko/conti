"""Unit tests for the Postgres-backed audit + trace + cache + limiter.

These run without a live Postgres — they substitute a fake asyncpg pool so
we can assert the SQL we issue and the round-trip behavior without setting
up a containerized DB.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from engine.app.models.schemas import (
    IntegrityReport,
    ReportMetadata,
    VerifyTrace,
)
from engine.app.services.postgres import (
    PgAuditLog,
    PgCache,
    PgRateLimiter,
    PgTraceStore,
    PostgresStore,
)


class _FakeConn:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    async def execute(self, sql: str, *args: Any) -> str:
        self._store.executed.append((sql, args))
        sql_lc = sql.strip().lower()
        if sql_lc.startswith("insert into audit_events"):
            (
                request_id,
                api_key_id,
                endpoint,
                model,
                ts,
                payload,
                *_security,
            ) = args
            self._store.audit_rows.append(
                {
                    "request_id": request_id,
                    "api_key_id": api_key_id,
                    "endpoint": endpoint,
                    "model": model,
                    "ts": ts,
                    "payload": payload,
                }
            )
        elif sql_lc.startswith("insert into verify_traces"):
            request_id, endpoint, api_key_id, payload, expires_at = args
            self._store.trace_rows[request_id] = {
                "request_id": request_id,
                "endpoint": endpoint,
                "api_key_id": api_key_id,
                "payload": payload,
                "expires_at": expires_at,
            }
        elif sql_lc.startswith("insert into response_cache"):
            key, body, expires_at = args
            self._store.cache_rows[key] = {
                "key": key,
                "body": body,
                "expires_at": expires_at,
            }
        elif "delete from response_cache" in sql_lc:
            self._store.cache_rows.clear()
        elif "delete from rate_events" in sql_lc:
            self._store.rate_events.clear()
        elif "insert into idempotency_keys" in sql_lc:
            api_key_id, idempotency_key, request_hash, expires_at = args
            self._store.idempotency_keys[(api_key_id, idempotency_key)] = {
                "api_key_id": api_key_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "status_code": None,
                "body": None,
                "expires_at": expires_at,
            }
        elif "update idempotency_keys" in sql_lc:
            api_key_id, idempotency_key, status_code, body = args
            row = self._store.idempotency_keys.get((api_key_id, idempotency_key))
            if row:
                row["status_code"] = status_code
                row["body"] = json.loads(body)
        elif "delete from idempotency_keys" in sql_lc:
            api_key_id, idempotency_key = args
            self._store.idempotency_keys.pop((api_key_id, idempotency_key), None)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._store.executed.append((sql, args))
        rows = list(self._store.audit_rows)
        if "endpoint = $" in sql:
            ep = next(a for a in args if isinstance(a, str) and a.startswith("/"))
            rows = [r for r in rows if r["endpoint"] == ep]
        if "api_key_id = $" in sql:
            kid = next(
                a for a in args
                if isinstance(a, str) and not a.startswith("/")
            )
            rows = [r for r in rows if r.get("api_key_id") == kid]
        if "ts >= $" in sql:
            since = next(a for a in args if isinstance(a, datetime))
            rows = [r for r in rows if r["ts"] >= since]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        limit = args[-1]
        rows = rows[: int(limit)]
        return [{"payload": r["payload"]} for r in rows]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._store.executed.append((sql, args))
        sql_lc = sql.strip().lower()

        # Sweeper: combined CTE that deletes from all three tables.
        if (
            "delete from verify_traces" in sql_lc
            and "delete from response_cache" in sql_lc
            and "delete from rate_events" in sql_lc
        ):
            now = datetime.now(tz=timezone.utc)
            window_seconds = int(args[0]) if args else 60
            event_cutoff = now - timedelta(seconds=window_seconds)

            expired_traces = [
                rid for rid, r in self._store.trace_rows.items()
                if r["expires_at"] < now
            ]
            for rid in expired_traces:
                del self._store.trace_rows[rid]

            expired_cache = [
                k for k, r in self._store.cache_rows.items()
                if r["expires_at"] < now
            ]
            for k in expired_cache:
                del self._store.cache_rows[k]

            kept_events = [
                e for e in self._store.rate_events if e["ts"] >= event_cutoff
            ]
            expired_event_count = len(self._store.rate_events) - len(kept_events)
            self._store.rate_events = kept_events

            return {"n": len(expired_traces) + len(expired_cache) + expired_event_count}

        # Single-table sweeper (legacy verify_traces only) — kept for safety.
        if "delete from verify_traces" in sql_lc and "from t" in sql_lc:
            now = datetime.now(tz=timezone.utc)
            expired = [
                rid for rid, r in self._store.trace_rows.items()
                if r["expires_at"] < now
            ]
            for rid in expired:
                del self._store.trace_rows[rid]
            return {"n": len(expired)}

        # Trace lookup.
        if "from verify_traces" in sql_lc:
            request_id = args[0]
            return self._store.trace_rows.get(request_id)

        # Cache size count.
        if "select count(*) as n from response_cache" in sql_lc:
            now = datetime.now(tz=timezone.utc)
            n = sum(
                1 for r in self._store.cache_rows.values()
                if r["expires_at"] > now
            )
            return {"n": n}

        # Cache lookup.
        if "select body from response_cache" in sql_lc:
            key = args[0]
            row = self._store.cache_rows.get(key)
            if row is None or row["expires_at"] <= datetime.now(tz=timezone.utc):
                return None
            return {"body": row["body"]}

        # Rate limiter check (single CTE: counted + inserted + final SELECT).
        if "with counted as" in sql_lc and "from rate_events" in sql_lc:
            key, limit, window_seconds = args
            now = datetime.now(tz=timezone.utc)
            cutoff = now - timedelta(seconds=int(window_seconds))
            in_window = [
                e for e in self._store.rate_events
                if e["key"] == key and e["ts"] > cutoff
            ]
            n = len(in_window)
            oldest_ts = min((e["ts"] for e in in_window), default=None)
            allowed = n < int(limit)
            if allowed:
                self._store.rate_events.append({"key": key, "ts": now})
            return {"n": n, "oldest_ts": oldest_ts, "allowed": allowed}

        # Budget usage.
        if "estimated_cost_usd" in sql_lc and "audit_events" in sql_lc:
            api_key_id = args[0]
            rows = [r for r in self._store.audit_rows if r.get("api_key_id") == api_key_id]
            # simplified: ignore rolling window in fake for now, just sum all.
            total_usd = sum(float(json.loads(r["payload"]).get("estimated_cost_usd", 0)) for r in rows)
            total_tokens = sum(int(json.loads(r["payload"]).get("total_tokens", 0)) for r in rows)
            return {"usd": total_usd, "tokens": total_tokens}

        # Idempotency lookup.
        if "from idempotency_keys" in sql_lc:
            api_key_id, idempotency_key = args
            return self._store.idempotency_keys.get((api_key_id, idempotency_key))

        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._store.executed.append((sql, args))
        sql_lc = sql.strip().lower()
        if "insert into idempotency_keys" in sql_lc and "returning 1" in sql_lc:
            api_key_id, idempotency_key, request_hash, expires_at = args
            key = (api_key_id, idempotency_key)
            if key in self._store.idempotency_keys:
                return None
            self._store.idempotency_keys[key] = {
                "api_key_id": api_key_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "status_code": None,
                "body": None,
                "expires_at": expires_at,
            }
            return 1
        return None


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePool:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(_FakeConn(self._store))

    async def close(self) -> None:
        self._store.closed = True


class _FakeStore:
    """Holds the in-memory tables that the fake conn reads/writes."""

    def __init__(self) -> None:
        self.audit_rows: list[dict[str, Any]] = []
        self.trace_rows: dict[str, dict[str, Any]] = {}
        self.cache_rows: dict[str, dict[str, Any]] = {}
        self.rate_events: list[dict[str, Any]] = []
        self.idempotency_keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False


@pytest.fixture
def pg_store() -> PostgresStore:
    fake = _FakeStore()
    store = PostgresStore("postgresql://fake/fake")
    # Bypass the real connect() — substitute the fake pool directly.
    store._pool = _FakePool(fake)  # type: ignore[assignment]
    store._fake = fake  # type: ignore[attr-defined]
    return store


async def test_pg_audit_log_appends_and_reads_back(pg_store: PostgresStore):
    audit = PgAuditLog(pg_store, enabled=True)

    await audit.append(
        {
            "request_id": "req_a",
            "endpoint": "/verify",
            "model": "gemini-3-flash-preview",
            "timestamp": "2026-04-29T12:00:00.000Z",
            "overall_score": 95,
        }
    )
    await audit.append(
        {
            "request_id": "req_b",
            "endpoint": "/verify/quick",
            "model": "gemini-3-flash-preview",
            "timestamp": "2026-04-29T12:01:00.000Z",
            "overall_score": 80,
        }
    )

    records = await audit.read_tail(limit=10)
    # Oldest first per AuditLog.read_tail contract.
    assert [r["request_id"] for r in records] == ["req_a", "req_b"]


async def test_pg_audit_log_filters_by_endpoint(pg_store: PostgresStore):
    audit = PgAuditLog(pg_store, enabled=True)
    await audit.append({"request_id": "r1", "endpoint": "/verify", "timestamp": "2026-04-29T12:00:00.000Z"})
    await audit.append({"request_id": "r2", "endpoint": "/verify/quick", "timestamp": "2026-04-29T12:01:00.000Z"})
    await audit.append({"request_id": "r3", "endpoint": "/verify/quick", "timestamp": "2026-04-29T12:02:00.000Z"})

    records = await audit.read_tail(limit=10, endpoint="/verify/quick")
    assert {r["request_id"] for r in records} == {"r2", "r3"}


async def test_pg_audit_log_filters_by_api_key_id(pg_store: PostgresStore):
    audit = PgAuditLog(pg_store, enabled=True)
    await audit.append({
        "request_id": "ra",
        "api_key_id": "alice",
        "endpoint": "/verify",
        "timestamp": "2026-04-29T12:00:00.000Z",
    })
    await audit.append({
        "request_id": "rb",
        "api_key_id": "bob",
        "endpoint": "/verify",
        "timestamp": "2026-04-29T12:01:00.000Z",
    })
    await audit.append({
        "request_id": "rc",
        "api_key_id": "alice",
        "endpoint": "/verify",
        "timestamp": "2026-04-29T12:02:00.000Z",
    })

    alice_records = await audit.read_tail(limit=10, api_key_id="alice")
    assert {r["request_id"] for r in alice_records} == {"ra", "rc"}

    bob_records = await audit.read_tail(limit=10, api_key_id="bob")
    assert {r["request_id"] for r in bob_records} == {"rb"}


async def test_pg_audit_log_disabled_is_noop(pg_store: PostgresStore):
    audit = PgAuditLog(pg_store, enabled=False)
    await audit.append({"request_id": "r", "endpoint": "/verify"})
    assert pg_store._fake.audit_rows == []  # type: ignore[attr-defined]
    assert await audit.read_tail(limit=10) == []


def _make_trace(request_id: str = "req_x") -> VerifyTrace:
    md = ReportMetadata(model="test", request_id=request_id)
    return VerifyTrace(
        request_id=request_id,
        endpoint="/verify",
        report=IntegrityReport(overall_score=100, metadata=md),
        evidence=[],
    )


async def test_pg_trace_store_round_trip(pg_store: PostgresStore):
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    trace = _make_trace("req_round_trip")

    await store.save(trace)
    fetched = await store.get("req_round_trip")
    assert fetched is not None
    assert fetched.request_id == "req_round_trip"
    assert fetched.endpoint == "/verify"


async def test_pg_trace_store_returns_none_for_expired_row(pg_store: PostgresStore):
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    trace = _make_trace("req_expired")
    await store.save(trace)

    # Tamper with the fake's expires_at to simulate a stale row.
    fake = pg_store._fake  # type: ignore[attr-defined]
    fake.trace_rows["req_expired"]["expires_at"] = datetime.now(
        tz=timezone.utc
    ) - timedelta(seconds=1)

    assert await store.get("req_expired") is None


async def test_pg_trace_store_save_uses_upsert(pg_store: PostgresStore):
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    await store.save(_make_trace("req_upsert"))
    await store.save(_make_trace("req_upsert"))
    fake = pg_store._fake  # type: ignore[attr-defined]
    assert len(fake.trace_rows) == 1


async def test_pg_trace_store_isolates_by_api_key_id(pg_store: PostgresStore):
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    await store.save(_make_trace("req_alice"), api_key_id="alice")
    await store.save(_make_trace("req_bob"), api_key_id="bob")

    # Owner can read.
    assert await store.get("req_alice", api_key_id="alice") is not None
    # Cross-tenant read returns None (treated as 404 by the endpoint).
    assert await store.get("req_alice", api_key_id="bob") is None
    assert await store.get("req_bob", api_key_id="alice") is None
    # No-key callers (e.g. internal tooling) bypass the filter.
    assert await store.get("req_alice") is not None


async def test_pg_trace_store_legacy_null_key_readable_by_anyone(
    pg_store: PostgresStore,
):
    # Traces written before the api_key_id column existed have NULL keys;
    # they remain readable so we don't strand pre-existing rows.
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    await store.save(_make_trace("req_legacy"), api_key_id=None)
    assert await store.get("req_legacy", api_key_id="anyone") is not None


async def test_pg_cache_round_trip(pg_store: PostgresStore):
    cache = PgCache(pg_store, ttl_seconds=60)

    miss = await cache.get("k1")
    assert miss is None
    assert cache.misses == 1
    assert cache.hits == 0

    await cache.set("k1", {"value": 123})
    hit = await cache.get("k1")
    assert hit == {"value": 123}
    assert cache.hits == 1


async def test_pg_cache_set_upserts_existing_key(pg_store: PostgresStore):
    cache = PgCache(pg_store, ttl_seconds=60)
    await cache.set("k", {"v": 1})
    await cache.set("k", {"v": 2})
    fake = pg_store._fake  # type: ignore[attr-defined]
    assert len(fake.cache_rows) == 1
    body = fake.cache_rows["k"]["body"]
    assert json.loads(body) == {"v": 2}


async def test_pg_cache_clear_resets_counters_and_rows(pg_store: PostgresStore):
    cache = PgCache(pg_store, ttl_seconds=60)
    await cache.set("k", {"v": 1})
    await cache.get("k")  # bump hits
    await cache.clear()
    assert cache.hits == 0
    assert cache.misses == 0
    assert pg_store._fake.cache_rows == {}  # type: ignore[attr-defined]


async def test_pg_rate_limiter_allows_under_limit_then_blocks(pg_store: PostgresStore):
    limiter = PgRateLimiter(pg_store, limit_per_minute=2, window_seconds=60)

    allowed, remaining, _ = await limiter.check("user1")
    assert allowed is True
    assert remaining == 1

    allowed, remaining, _ = await limiter.check("user1")
    assert allowed is True
    assert remaining == 0

    allowed, remaining, retry_after = await limiter.check("user1")
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0


async def test_pg_rate_limiter_keys_are_independent(pg_store: PostgresStore):
    limiter = PgRateLimiter(pg_store, limit_per_minute=1, window_seconds=60)

    a, _, _ = await limiter.check("user_a")
    b, _, _ = await limiter.check("user_b")
    a_again, _, _ = await limiter.check("user_a")

    assert a is True
    assert b is True
    assert a_again is False  # user_a is over its own limit; user_b is unaffected


async def test_sweep_expired_deletes_across_all_tables(pg_store: PostgresStore):
    trace_store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    cache = PgCache(pg_store, ttl_seconds=900)
    limiter = PgRateLimiter(pg_store, limit_per_minute=10, window_seconds=60)

    await trace_store.save(_make_trace("fresh"))
    await trace_store.save(_make_trace("stale"))
    await cache.set("fresh-key", {"v": 1})
    await cache.set("stale-key", {"v": 2})
    await limiter.check("user1")  # fresh event

    fake = pg_store._fake  # type: ignore[attr-defined]
    now = datetime.now(tz=timezone.utc)
    fake.trace_rows["stale"]["expires_at"] = now - timedelta(seconds=1)
    fake.cache_rows["stale-key"]["expires_at"] = now - timedelta(seconds=1)
    # Push the rate event outside the window so the sweeper deletes it.
    fake.rate_events[0]["ts"] = now - timedelta(seconds=120)

    deleted = await pg_store.sweep_expired(rate_window_seconds=60)
    assert deleted == 3
    assert "fresh" in fake.trace_rows
    assert "stale" not in fake.trace_rows
    assert "fresh-key" in fake.cache_rows
    assert "stale-key" not in fake.cache_rows
    assert fake.rate_events == []


async def test_postgres_store_close_closes_pool(pg_store: PostgresStore):
    await pg_store.close()
    assert pg_store._pool is None  # type: ignore[attr-defined]

async def test_pg_budget_store_get_usage(pg_store: PostgresStore):
    from engine.app.services.postgres import PgBudgetStore
    budget = PgBudgetStore(pg_store)
    audit = PgAuditLog(pg_store, enabled=True)

    # 1. Empty.
    usage = await budget.get_usage("key_1")
    assert usage == (0.0, 0)

    # 2. Add some spend.
    await audit.append({
        "request_id": "r1",
        "api_key_id": "key_1",
        "total_tokens": 1000,
        "estimated_cost_usd": 0.05,
        "endpoint": "/verify",
    })
    await audit.append({
        "request_id": "r2",
        "api_key_id": "key_1",
        "total_tokens": 2000,
        "estimated_cost_usd": 0.10,
        "endpoint": "/verify",
    })
    # Another key.
    await audit.append({
        "request_id": "r3",
        "api_key_id": "key_2",
        "total_tokens": 5000,
        "estimated_cost_usd": 0.50,
        "endpoint": "/verify",
    })

    usage = await budget.get_usage("key_1")
    assert usage[0] == pytest.approx(0.15)
    assert usage[1] == 3000


async def test_pg_idempotency_store_workflow(pg_store: PostgresStore):
    from engine.app.services.postgres import PgIdempotencyStore
    store = PgIdempotencyStore(pg_store, ttl_seconds=60)
    
    key_id = "key_1"
    idem_key = "idem_123"
    req_hash = b"hash123"

    # 1. Miss.
    res = await store.get(key_id, idem_key)
    assert res is None

    # 2. Create in-flight.
    created = await store.create_in_flight(key_id, idem_key, req_hash)
    assert created is True

    # 3. Conflict (already in-flight).
    created = await store.create_in_flight(key_id, idem_key, req_hash)
    assert created is False

    # 4. Lookup in-flight.
    res = await store.get(key_id, idem_key)
    assert res["status_code"] is None
    assert res["request_hash"] == req_hash

    # 5. Save resolved.
    body = {"result": "ok"}
    await store.save_resolved(key_id, idem_key, 200, body)

    # 6. Replay.
    res = await store.get(key_id, idem_key)
    assert res["status_code"] == 200
    assert res["body"] == body

    # 7. Delete.
    await store.delete(key_id, idem_key)
    res = await store.get(key_id, idem_key)
    assert res is None

