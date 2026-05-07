# Lobster Trap Integration Strategy

TrustLayer integrates with **Veea Lobster Trap** to provide a comprehensive Enterprise Security & Integrity stack. While TrustLayer focuses on the **Veracity** (truthfulness) of LLM outputs, Lobster Trap provides the **Security** (firewall) layer.

## The "Sandwich" Architecture

Lobster Trap acts as a Deep Prompt Inspection (DPI) proxy that sits between the TrustLayer Engine and the LLM providers (Gemini, Anthropic, etc.).

1.  **Ingress Security (Lobster Trap):** Every prompt sent by TrustLayer is inspected for injections, PII, and malicious intent.
2.  **Reasoning (Gemini Pro/Flash):** The "scrubbed" prompt is processed by the LLM.
3.  **Egress Security (Lobster Trap):** The response is inspected for exfiltration patterns or risky commands. Scoped to the **demo-app assistant flow** — see Integration Point 4 for why.
4.  **Integrity Verification (TrustLayer):** The safe response is then decomposed and verified against the source context for hallucinations and contradictions.

## Integration Points

### 1. Provider Wiring (not a one-line BASE_URL swap)

Lobster Trap exposes an **OpenAI-compatible** `/v1/chat/completions` endpoint. The current clients in `engine/app/services/` do not speak that wire format natively, so proxying requires per-provider changes:

- **Anthropic** (`anthropic_client.py`): the `AsyncAnthropic` SDK is constructed with `api_key=...` only. To route through Lobster Trap, switch to Anthropic's **OpenAI-compatibility endpoint** and pass `base_url="http://localhost:8080/v1"` to the SDK constructor (or use the `openai` package pointed at Lobster Trap with an Anthropic-keyed upstream).
- **Gemini** (`gemini_client.py`): the `google-genai` client uses Vertex AI service-account auth over gRPC, which **cannot be transparently proxied** as REST. The proxied path must use Gemini's **OpenAI-compatibility REST endpoint** (`generativelanguage.googleapis.com/v1beta/openai`) configured behind Lobster Trap.

Both changes are SDK-level, not env-only. A `LOBSTERTRAP_BASE_URL` setting in `engine/config/settings.py` gates whether each client builds its native or proxied variant.

### 2. Unified Audit Log

Extend the Postgres `audit_events` table (and the dict returned by `build_verify_record` in `engine/app/services/audit.py:209`) to carry Lobster Trap metadata per request:

- `security_risk_score`: High/Medium/Low
- `security_intent_detected`: Detected category (e.g., `Exploit`, `PII_Leak`)
- `security_intent_declared`: TrustLayer's declared intent (see Integration Point 3)
- `security_action`: ALLOW / DENY / LOG / HUMAN_REVIEW / QUARANTINE / RATE_LIMIT
- `security_intent_mismatch`: boolean — true when declared ≠ detected

The orchestrator passes these through unchanged; no schema work in the verify trace itself.

### 3. Bidirectional `_lobstertrap` Metadata — Declared vs Detected Intent

This is the differentiator the Veea brief explicitly rewards ("declared-versus-detected intent mismatches"). On every outbound call, TrustLayer attaches a `_lobstertrap` block declaring its intent:

```json
{
  "_lobstertrap": {
    "intent": "grounding_verification",
    "expects": "json_only",
    "caller": "trustlayer.grounder",
    "request_id": "<uuid>"
  }
}
```

Lobster Trap's DPI then reports back a *detected* intent. When detected ≠ declared (e.g., declared `grounding_verification` but detected `data_exfiltration`), TrustLayer:

1. Records the mismatch on the audit row (`security_intent_mismatch=true`).
2. Surfaces it as a flagged finding in the `VerifyTrace`, visible in the explainability UI.
3. Optionally short-circuits the pipeline before reasoning if the policy action is DENY.

This produces the regulator-readable audit trail the brief asks for: every request shows what the agent *claimed* to be doing vs what the firewall *saw*.

### 4. Adversarial Testing — Scoped Honestly

Use TrustLayer's `AdversaryAgent` (`engine/app/services/adversary.py`) to generate prompts that probe Lobster Trap's YAML policies, creating a continuous red-team loop. Track block-rate over time and feed regressions back into the policy pack.

**Egress-inspection scope:** for the engine's `/verify` path, responses are constrained JSON claim verdicts — the exfiltration surface is near-zero, so egress DPI is mostly defense-in-depth there. Egress inspection earns its keep on the **demo-app assistant** path, where the model produces free-form text that could carry leaked context or risky tool-call payloads. Step 3 of the sandwich is therefore primary on the demo-app flow and secondary on `/verify`.

## Enterprise Value

By combining Lobster Trap and TrustLayer, the system addresses the two biggest hurdles to enterprise AI adoption:

- **Safety:** Prevents malicious use, data leaks, and intent-drift (Lobster Trap + declared/detected mismatch).
- **Correctness:** Eliminates hallucinations and ungrounded claims (TrustLayer).
