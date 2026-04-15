from __future__ import annotations

import threading
from typing import Optional

from app.models.schemas import ParsedContract


class InMemoryContractStore:
    """Thread-safe in-memory store keyed by contract_id.

    The demo runs on a single process so a dict is fine. Swap this for Redis /
    a DB if the demo ever needs cross-process persistence.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._contracts: dict[str, ParsedContract] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def put(self, contract: ParsedContract) -> None:
        with self._lock:
            if len(self._contracts) >= self._max_entries:
                # Evict the oldest inserted entry.
                oldest = next(iter(self._contracts))
                self._contracts.pop(oldest, None)
            self._contracts[contract.contract_id] = contract

    def get(self, contract_id: str) -> Optional[ParsedContract]:
        with self._lock:
            return self._contracts.get(contract_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._contracts)


store = InMemoryContractStore()
