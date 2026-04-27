from __future__ import annotations

import threading
from typing import Optional

from app.models.schemas import ParsedContract


class InMemoryContractStore:
    """Thread-safe in-memory store keyed by contract_id.

    The demo runs on a single process so a dict is fine. Swap this for Redis /
    a DB if the demo ever needs cross-process persistence.

    A sidecar `_raw_bytes` map retains the original upload bytes when the
    caller passes them via `put_with_bytes(...)`. Multimodal analysis (Phase
    G3) reads these to render PDF pages on-demand at /analyze time, avoiding
    the cost of rendering on every upload.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._contracts: dict[str, ParsedContract] = {}
        self._raw_bytes: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def put(self, contract: ParsedContract) -> None:
        self._put_internal(contract, raw_bytes=None)

    def put_with_bytes(self, contract: ParsedContract, raw_bytes: bytes) -> None:
        self._put_internal(contract, raw_bytes=raw_bytes)

    def _put_internal(
        self, contract: ParsedContract, *, raw_bytes: Optional[bytes]
    ) -> None:
        with self._lock:
            if len(self._contracts) >= self._max_entries:
                oldest = next(iter(self._contracts))
                self._contracts.pop(oldest, None)
                self._raw_bytes.pop(oldest, None)
            self._contracts[contract.contract_id] = contract
            if raw_bytes is not None:
                self._raw_bytes[contract.contract_id] = raw_bytes

    def get(self, contract_id: str) -> Optional[ParsedContract]:
        with self._lock:
            return self._contracts.get(contract_id)

    def get_raw_bytes(self, contract_id: str) -> Optional[bytes]:
        with self._lock:
            return self._raw_bytes.get(contract_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._contracts)


store = InMemoryContractStore()
