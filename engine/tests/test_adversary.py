import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from engine.app.services.adversary import AdversaryAgent, AdversarialOutput
from engine.app.prompts.adversary_prompt import ADVERSARY_SYSTEM_PROMPT

@pytest.mark.asyncio
async def test_adversary_agent_generate_success():
    # Mock LLM Client
    mock_client = AsyncMock()
    mock_response = {
        "summary": "This is an adversarial summary.",
        "injections": [
            {
                "type": "hallucination",
                "injected_claim": "False claim 1",
                "original_fact": "True fact 1",
                "reasoning": "Subtle change"
            },
            {
                "type": "contradiction",
                "injected_claim": "False claim 2",
                "original_fact": "None",
                "reasoning": "Internal inconsistency"
            }
        ]
    }
    mock_client.create_message.return_value = json.dumps(mock_response)
    
    # We pass the mock client directly
    agent = AdversaryAgent(client=mock_client, model="claude-3-haiku")
    
    result = await agent.generate_adversarial_summary("Sample contract text")
    
    assert isinstance(result, AdversarialOutput)
    assert result.summary == "This is an adversarial summary."
    assert len(result.injections) == 2
    assert result.injections[0].type == "hallucination"
    assert result.injections[1].type == "contradiction"
    
    mock_client.create_message.assert_called_once()
    args, kwargs = mock_client.create_message.call_args
    assert kwargs["system"] == ADVERSARY_SYSTEM_PROMPT

@pytest.mark.asyncio
async def test_adversary_agent_parse_error():
    mock_client = AsyncMock()
    mock_client.create_message.return_value = "invalid json"
    
    agent = AdversaryAgent(client=mock_client, model="claude-3-haiku")
    
    with pytest.raises(RuntimeError, match="Adversary agent failed to produce valid JSON"):
        await agent.generate_adversarial_summary("Sample contract text")
