"""Unit tests for the Postgres-backed audit + trace storage.

These run without a live Postgres — they substitute a fake asyncpg pool so
we can assert the SQL we issue and the round-trip behavior of `PgAuditLog`
and `PgTraceStore` without setting up a containerized DB.
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
            request_id, endpoint, model, ts, payload = args
            self._store.audit_rows.append(
                {
                    "request_id": request_id,
                    "endpoint": endpoint,
                    "model": model,
                    "ts": ts,
                    "payload": payload,
                }
            )
        elif sql_lc.startswith("insert into verify_traces"):
            request_id, endpoint, payload, expires_at = args
            self._store.trace_rows[request_id] = {
                "request_id": request_id,
                "endpoint": endpoint,
                "payload": payload,
                "expires_at": expires_at,
            }
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._store.executed.append((sql, args))
        rows = list(self._store.audit_rows)
        if "endpoint = $" in sql:
            ep = next(a for a in args if isinstance(a, str) and a.startswith("/"))
            rows = [r for r in rows if r["endpoint"] == ep]
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
        if "delete from verify_traces" in sql_lc:
            now = datetime.now(tz=timezone.utc)
            expired = [
                rid
                for rid, row in self._store.trace_rows.items()
                if row["expires_at"] < now
            ]
            for rid in expired:
                del self._store.trace_rows[rid]
            return {"n": len(expired)}
        if "from verify_traces" in sql_lc:
            request_id = args[0]
            return self._store.trace_rows.get(request_id)
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


async def test_sweep_expired_traces_deletes_only_expired(pg_store: PostgresStore):
    store = PgTraceStore(pg_store, ttl_seconds=900, enabled=True)
    await store.save(_make_trace("fresh"))
    await store.save(_make_trace("stale"))
    fake = pg_store._fake  # type: ignore[attr-defined]
    fake.trace_rows["stale"]["expires_at"] = datetime.now(
        tz=timezone.utc
    ) - timedelta(seconds=1)

    deleted = await pg_store.sweep_expired_traces()
    assert deleted == 1
    assert "fresh" in fake.trace_rows
    assert "stale" not in fake.trace_rows


async def test_postgres_store_close_closes_pool(pg_store: PostgresStore):
    await pg_store.close()
    assert pg_store._pool is None  # type: ignore[attr-defined]
