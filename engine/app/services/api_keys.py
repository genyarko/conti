from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache

from engine.app.services.postgres import PostgresStore
from engine.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    daily_usd_cap: Optional[float]
    daily_token_cap: Optional[int]
    # None or a tuple containing "*" means the key is unrestricted (backward
    # compatible with rows that predate scope enforcement). Otherwise the
    # request's required scope must be present in the tuple.
    scopes: Optional[tuple[str, ...]] = None

    def has_scope(self, required: str) -> bool:
        if self.scopes is None:
            return True
        if "*" in self.scopes:
            return True
        return required in self.scopes


class ApiKeyService:
    """Resolves and caches API key metadata.

    Supports the new `id.secret` format and falls back to the legacy
    `API_AUTH_TOKEN` for local development.
    """

    def __init__(self, store: Optional[PostgresStore]) -> None:
        self._store = store
        # Short TTL cache to avoid hitting the DB for every request's auth check
        # and budget cap lookup. 60s is enough to absorb bursts while keeping
        # cap/disable changes relatively fresh.
        self._meta_cache: TTLCache[str, Optional[ApiKey]] = TTLCache(
            maxsize=1024, ttl=60
        )
        self._secret_cache: TTLCache[str, bytes] = TTLCache(maxsize=1024, ttl=60)

    async def resolve(self, bearer: str) -> Optional[ApiKey]:
        """Verify the bearer token and return the associated ApiKey.

        Bearer format: `<id>.<secret>`. `id` is public; `secret` is hashed.
        """
        if not bearer:
            return None

        # Legacy fallback for dev/tests.
        if settings.api_auth_token and hmac.compare_digest(
            bearer.strip(), settings.api_auth_token
        ):
            return ApiKey(
                id="default",
                name="Legacy Dev Key",
                daily_usd_cap=settings.default_daily_usd_cap,
                daily_token_cap=settings.default_daily_token_cap,
            )

        if "." not in bearer:
            return None

        key_id, _, secret = bearer.partition(".")
        key_id = key_id.strip()
        secret = secret.strip()
        if not key_id or not secret:
            return None

        # 1. Fetch hashed secret and verify.
        hashed_secret = await self._get_hashed_secret(key_id)
        if not hashed_secret:
            return None

        # Compare provided secret's hash with stored hash.
        provided_hash = hashlib.sha256(secret.encode()).digest()
        if not hmac.compare_digest(provided_hash, hashed_secret):
            return None

        # 2. Return metadata (cached).
        return await self._get_metadata(key_id)

    async def _get_hashed_secret(self, key_id: str) -> Optional[bytes]:
        if key_id in self._secret_cache:
            return self._secret_cache[key_id]

        if not self._store:
            return None

        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT hashed_secret FROM api_keys WHERE id = $1 AND disabled_at IS NULL",
                    key_id,
                )
                if row:
                    res = bytes(row["hashed_secret"])
                    self._secret_cache[key_id] = res
                    return res
        except Exception as exc:  # noqa: BLE001
            log.warning("API key secret lookup failed for %s: %s", key_id, exc)

        return None

    async def _get_metadata(self, key_id: str) -> Optional[ApiKey]:
        if key_id in self._meta_cache:
            return self._meta_cache[key_id]

        if not self._store:
            return None

        try:
            async with self._store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, name, daily_usd_cap, daily_token_cap, scopes
                    FROM api_keys
                    WHERE id = $1 AND disabled_at IS NULL
                    """,
                    key_id,
                )
                if row:
                    raw_scopes = row["scopes"]
                    scopes = (
                        tuple(raw_scopes)
                        if raw_scopes is not None
                        else None
                    )
                    key = ApiKey(
                        id=row["id"],
                        name=row["name"] or row["id"],
                        daily_usd_cap=(
                            float(row["daily_usd_cap"])
                            if row["daily_usd_cap"] is not None
                            else None
                        ),
                        daily_token_cap=row["daily_token_cap"],
                        scopes=scopes,
                    )
                    self._meta_cache[key_id] = key
                    return key
        except Exception as exc:  # noqa: BLE001
            log.warning("API key meta lookup failed for %s: %s", key_id, exc)

        self._meta_cache[key_id] = None
        return None
