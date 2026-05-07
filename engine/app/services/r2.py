from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aioboto3
from botocore.config import Config

log = logging.getLogger(__name__)


class R2Client:
    """Wrapper for Cloudflare R2 using aioboto3 (S3-compatible).

    R2 does not support all S3 features (e.g. ACLs), so we use a minimal
    configuration to ensure compatibility.
    """

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
    ) -> None:
        self._endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self._key_id = access_key_id
        self._secret = secret_access_key
        self._bucket = bucket_name
        self._session = aioboto3.Session()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        # R2 requires virtual-hosted style requests to be disabled for some 
        # operations; path-style is more reliable across account-ID endpoints.
        config = Config(s3={"addressing_style": "path"})
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._key_id,
            aws_secret_access_key=self._secret,
            config=config,
            region_name="auto",  # R2 ignores region but aioboto3 needs one.
        ) as client:
            yield client

    async def upload_jsonl(self, key: str, content: str) -> bool:
        """Upload a string to R2. Returns True on success."""
        if not self._key_id or not self._secret or not self._endpoint:
            log.warning("R2Client: missing credentials, skipping upload to %s", key)
            return False

        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=content.encode("utf-8"),
                    ContentType="application/x-jsonlines",
                )
            log.info("R2Client: uploaded %d bytes to %s/%s", len(content), self._bucket, key)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("R2Client: upload failed to %s: %s", key, exc)
            return False

    async def is_configured(self) -> bool:
        return bool(self._key_id and self._secret and self._endpoint)
