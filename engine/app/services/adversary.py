from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from engine.app.prompts.adversary_prompt import (
    ADVERSARY_RESPONSE_SCHEMA,
    ADVERSARY_SYSTEM_PROMPT,
    build_adversary_user_prompt,
)
from engine.app.services import llm_factory
from engine.app.services.anthropic_client import TokenLedger

log = logging.getLogger(__name__)

@dataclass
class InjectedError:
    type: str
    injected_claim: str
    original_fact: str
    reasoning: str

@dataclass
class AdversarialOutput:
    summary: str
    injections: list[InjectedError]

class AdversaryAgent:
    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        ledger: Optional[TokenLedger] = None,
    ) -> None:
        if client is not None:
            self._client = client
            self._model = model or "unknown-model"
        else:
            resolved = llm_factory.resolve(provider=provider, model=model)
            self._client = llm_factory.get_client(resolved.provider)
            self._model = resolved.model
        
        self._ledger = ledger
        self._max_tokens = llm_factory.max_tokens_for(self._model)

    async def generate_adversarial_summary(self, source_context: str) -> AdversarialOutput:
        """Generates a summary with subtle hallucinations and contradictions."""
        log.info("Generating adversarial summary for stress testing...")
        
        raw = await self._client.create_message(
            system=ADVERSARY_SYSTEM_PROMPT,
            user=build_adversary_user_prompt(source_context),
            model=self._model,
            max_tokens=self._max_tokens,
            response_schema=ADVERSARY_RESPONSE_SCHEMA,
        )
        
        if self._ledger is not None:
            self._ledger.record_from(self._client)
            
        try:
            data = json.loads(raw)
            injections = [
                InjectedError(**inj) for inj in data.get("injections", [])
            ]
            return AdversarialOutput(
                summary=data.get("summary", ""),
                injections=injections
            )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("Failed to parse adversary output: %s", e)
            raise RuntimeError(f"Adversary agent failed to produce valid JSON: {e}")
