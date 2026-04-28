# TrustLayer — LLM Output Integrity Checker

> **Core product:** A general-purpose API that verifies any LLM output for hallucinations, ungrounded claims, and logical inconsistencies.  
> **Showcase demo:** An AI Contract Reviewer powered by TrustLayer, proving the engine works on a high-stakes real-world use case.

*** Re-tighten the org policies after deploy succeeds.

**  Verifier-side skip for absence claims. A finding with title.startswith("Missing") or category == missing_clause shouldn't go through the clause-grounding pass at all —
  it should bypass to a "does the document contain anything about X?" check. That fixes the second bug too (the Set-off case still has a real grounded half, but absence
  claims as a class shouldn't be measured by clause-level grounding).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    TRUSTLAYER ENGINE (core product)               │
│                                                                  │
│  Input: { source_context, llm_output, schema? }                  │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │  Claim        │→ │  Source       │→ │  Logical               │  │
│  │  Extractor    │  │  Grounder    │  │  Consistency Checker   │  │
│  └──────────────┘  └──────────────┘  └────────────────────────┘  │
│         │                 │                      │                │
│         ▼                 ▼                      ▼                │
│  Atomic claims     Grounding scores      Consistency verdicts    │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Aggregator → per-claim + overall integrity report       │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  Output: { verified_claims[], flagged_claims[],                  │
│            hallucinations[], integrity_score, report }            │
└──────────────────────────────────────────────────────────────────┘
                              │
                    used by any app
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  Contract Reviewer     Chatbot Auditor      Research Verifier
  (hackathon demo)       (future app)          (future app)
```

---

## TOP PRIORITY: Gemini / Google AI Studio Track

> **Hackathon track requirement.** The "Technology Partners" track requires the project to use **Gemini (via Google AI Studio or the Gemini API)** for reasoning, chat, or multimodal understanding, with an **agent-driven or automated workflow**. This track now takes precedence over all other phases. Anthropic Claude stays in the codebase as a secondary provider (proves "provider-agnostic" claim), but Gemini becomes the **default** for the live demo.

### Pitch framing (what we tell the judges)

- **Gemini is the agent brain.** Every reasoning step in the contract-reviewer demo — clause splitting, risk analysis, claim extraction, source grounding, consistency checking — runs on Gemini through an agent-driven pipeline.
- **Multimodal is the differentiator.** Contracts arrive as PDFs that often contain scanned pages, signatures, stamps, marked-up redlines, and tables. Gemini Pro reads pages **as images**, not just text — catching things `pdfplumber` cannot. This is the demo moment that text-only tools lose.
- **TrustLayer wraps Gemini in an integrity layer.** The agent doesn't just "use an LLM" — it self-verifies. Gemini Flash drives fast claim extraction and grounding; Gemini Pro drives deep reasoning and consistency checks; the verification report explains *why* each finding survived or got removed.
- **Built and tuned in Google AI Studio.** All prompts (extractor, grounder, consistency, contract-analyst) get iterated in AI Studio first, then exported to the engine. We can show the AI Studio prompt history in the video as proof of workflow.

### Model assignment (per pipeline stage)

| Stage                              | Model                    | Why                                                                 |
|-----------------------------------|--------------------------|---------------------------------------------------------------------|
| Contract analyzer (demo backend)  | `gemini-3.1-pro-preview` | Multimodal — reads PDF pages directly; deep reasoning over clauses. |
| Claim extractor                   | `gemini-3-flash-preview` | Cheap, fast structured-output decomposition.                        |
| Source grounder (semantic check)  | `gemini-3-flash-preview` | High volume, low-stakes per call; latency matters.                  |
| Consistency checker               | `gemini-3.1-pro-preview` | Skeptical-reviewer reasoning; quality > cost here.                  |
| Server-side safe default          | `gemini-3-flash-preview` | Cheapest safe option when caller omits `(provider, model)`.         |

Available Gemini 3.x preview models (as of 2026-04):
- `gemini-3.1-pro-preview` (public preview, released 2026-02-19) — flagship reasoning + multimodal
- `gemini-3.1-pro-preview-customtools` (public preview, released 2026-02-23) — tool-use variant; wire later if we add agent tool-calling
- `gemini-3-flash-preview` (public preview, released 2025-12-17) — fast tier, default safe model

Pin these IDs in `engine/config/settings.py` as `GEMINI_PRO_MODEL` / `GEMINI_FLASH_MODEL` so a single env-var change can flip to GA models when they ship.

### Phase G1: Google AI Studio prompt prototyping (Day 1, ~2 hours, do first)

1. Sign in to Google AI Studio, create a workspace, and generate a Gemini API key. Record it as `GEMINI_API_KEY` in `.env.example` and the team password manager.
2. Port the four prompts to AI Studio chat sessions and tune until output is stable:
   - `engine/app/prompts/extractor_prompt.py` → "Claim Extractor"
   - `engine/app/prompts/grounder_prompt.py` (if separate) → "Source Grounder"
   - `engine/app/prompts/consistency_prompt.py` → "Consistency Checker"
   - `demo-app/backend/app/prompts/contract_analyst.py` (or wherever the analyst prompt lives) → "Contract Analyst"
3. **Convert XML-tag scaffolding to JSON-schema responses.** Gemini handles structured output via `response_mime_type="application/json"` + `response_schema`, not Claude-style `<claim>` tags. Define schemas alongside each prompt.
4. Test each prompt on three fixtures: a clean clause, a risky clause, a hallucinated finding. Save the prompt + schema + sample outputs as a single markdown file per stage in `engine/app/prompts/gemini/`.
5. Save the AI Studio session links — they're shippable artifacts for the submission and visible proof of the "AI Studio" requirement.

### Phase G2: Gemini provider adapter (Day 1, ~3 hours)

Builds on Phase 12's provider-abstraction work, but Gemini is now the *first* non-Anthropic provider, not the second.

1. Add `google-genai` (the new unified SDK) to `engine/pyproject.toml`. **Do not use** the older `google-generativeai` — it's the deprecated path; the new SDK is what AI Studio docs point at as of 2026.
2. Create `engine/app/services/gemini_client.py` mirroring the `AnthropicClient` shape:
   - Class `GeminiClient` with `async def create_message(*, system, user, model, max_tokens, response_schema=None) -> str`.
   - Surface `last_usage: TokenUsage` from the SDK's `usage_metadata` (`prompt_token_count`, `candidates_token_count`).
   - Map system prompts to `system_instruction`; map user prompts to a single `contents=[...]` user turn.
   - Support multimodal: accept `image_parts: list[ImagePart] | None` and append them as `inline_data` parts in the user turn.
3. Promote the existing `ClaudeClient` Protocol (`engine/app/services/anthropic_client.py:38`) to a generic `LLMClient` Protocol so `extractor.py`, `grounder.py`, `consistency.py` accept either client without code changes at the call site.
4. Add catalog entry to `engine/app/services/models.py` (per Phase 12 decision):
   ```python
   {"provider": "google", "id": "gemini-3-flash-preview",  "tier": "fast",     "input_price_per_mtok": ..., "output_price_per_mtok": ...},
   {"provider": "google", "id": "gemini-3.1-pro-preview",  "tier": "flagship", "input_price_per_mtok": ..., "output_price_per_mtok": ...},
   ```
5. Update `lifespan` + `/health` to report Gemini availability (`GEMINI_API_KEY` present?) alongside Anthropic.
6. Set the engine's safe default to `("google", "gemini-3-flash-preview")`. Update `Phase 12 §7` accordingly — the previous "Anthropic Haiku" default is superseded.

### Phase G3: Multimodal contract ingestion (Day 2, ~3 hours — the demo's killer feature)

1. Add `demo-app/backend/app/services/pdf_to_images.py` — render each PDF page to a PNG using `pypdfium2` (lighter than `pdf2image`, no system Poppler dep). Cap at e.g. 25 pages to stay inside Gemini's request budget.
2. Refactor `demo-app/backend/app/services/analyzer.py`:
   - Add a new `multimodal: bool` mode (default **on** for the Gemini track).
   - When on, send `[system_instruction, page_image_1, page_image_2, ..., analyst_prompt]` to Gemini Pro instead of the text-extracted clause map.
   - Ask Gemini to return clauses *and* their bounding-box hints (page number + approximate position phrase) so the UI can highlight the source in the original PDF later.
3. Keep the existing text parsers (`pdf_parser.py`, `docx_parser.py`) as a fallback path for `multimodal=False` and for `.docx` (Gemini multimodal doesn't help there yet).
4. Pick a "wow moment" demo contract: a scanned PDF with handwritten redlines or a stamp, that the text parser butchers but Gemini reads cleanly. Add it to the sample-contract quick-load buttons.
5. Verify cost: a 10-page PDF at ~258 image tokens/page is roughly 2,580 input tokens — well within budget for the demo. Log the per-call cost in `metadata.cost_usd`.

### Phase G4: Agent-driven workflow framing (Day 2, ~1 hour — wording, not code)

The pipeline already *is* agent-driven, but the language needs to match the track's vocabulary so judges recognize it.

1. Rename the orchestrator's user-facing labels: "extract → ground → check → aggregate" → "**Plan → Read → Verify → Reconcile**" (whatever names you settle on, keep them consistent across UI, video, README).
2. Surface the agent steps in the `PipelineSteps` component as an animated trace, with each step labeled by which Gemini model is doing the work. This is what the judges literally see.
3. Add a one-paragraph "How the agent works" section to the README and the slide deck — it should describe the pipeline in agent terminology (perception → reasoning → tool use → self-verification) rather than ML-engineer terminology.
4. In the demo video, narrate the agent steps explicitly: "Gemini Pro reads the PDF pages… Gemini Flash extracts atomic claims… Gemini Pro double-checks each one against the source…"

### Phase G5: Frontend selector + badges (Day 2, ~1 hour)

1. Update `ModelSelector.tsx` (Phase 12 §10): Google group with `gemini-3.1-pro-preview` (flagship) and `gemini-3-flash-preview` (fast) shown first; default selection is Gemini Flash.
2. Add provider logos next to model names (Google "G" and Anthropic mark) — visual proof in the demo screenshots that the system is provider-agnostic with Google as the default.
3. `ReportSummary` / `ContractSummary` badges: "Google · Gemini 3.1 Pro · multimodal" when a multimodal run was used. Tie to `metadata.provider`, `metadata.model`, and a new `metadata.multimodal: bool`.

### Phase G6: Submission deliverables specific to this track

1. **Demo video script update.** Re-record (or splice) the contract demo to lead with the multimodal moment: drop a scanned PDF, show Gemini reading what `pdfplumber` would miss, then show TrustLayer verifying the result.
2. **README banner**: "Powered by Gemini and Google AI Studio" with the AI Studio prompt-session links.
3. **Architecture diagram**: add Google logo on the agent box; add a note that the engine is provider-agnostic but defaults to Gemini.
4. **Cover image**: include the Gemini name/mark.
5. **Submission form**: tick the Gemini / Google AI Studio track. Make sure the listed live URL has the Gemini default working without requiring the user to switch models.

### Risks to watch on this track

- **Rate limits on AI Studio free tier** are tighter than paid Anthropic — pre-cache demo runs, and have a backup recording in case of throttling during live judging.
- **Structured output divergence**: Gemini's JSON-mode is reliable but its schema enforcement is stricter than Claude's "respond with JSON only" prompt — invalid schemas will hard-fail rather than silently degrade. Add schema-validation tests per stage.
- **Multimodal token costs** scale with page count; cap pages and warn the user when a contract exceeds the cap.
- **Don't ship Gemini as the *only* provider.** The provider-agnostic story is what makes TrustLayer interesting beyond the hackathon. Anthropic stays wired up; Gemini is just the default.

---

## Implementation Phases

### Phase 1: Project Setup (Day 1 morning, ~1 hour)
1. Initialize monorepo: `engine/` (Python + FastAPI), `demo-app/` (React + Vite), shared `.env.example`
2. Build `engine/config/settings.py` with Pydantic Settings (API keys, model config, rate limits)
3. Build `engine/app/main.py` — FastAPI scaffold with CORS, health check, error handlers
4. Install core deps: `fastapi`, `uvicorn`, `anthropic`, `rapidfuzz`, `pydantic`
5. Define core data models in `engine/app/models/schemas.py`:
   - `VerifyRequest { source_context: str, llm_output: str, output_schema?: dict }`
   - `Claim { id, text, source_quote?, category }`
   - `ClaimVerdict { claim_id, grounding_score, consistency_verdict, is_hallucination, reasoning }`
   - `IntegrityReport { overall_score, verified[], flagged[], hallucinations[], metadata }`

### Phase 2: Claim Extractor (Day 1 morning, ~2 hours)
1. Build `engine/app/prompts/extractor_prompt.py` — prompt that decomposes any LLM output into atomic, verifiable claims
2. Each claim should include: `{ id, text, type, source_quote_if_any }` where type is one of: `factual`, `interpretive`, `recommendation`, `quantitative`
3. Implement `engine/app/pipeline/extractor.py` — sends LLM output to Claude, parses response into list of `Claim` objects
4. Handle edge cases: outputs with no verifiable claims (pure opinion), structured data (JSON/tables), very long outputs (chunk and extract)
5. Write tests: feed known LLM outputs, verify correct number of claims extracted with correct types

### Phase 3: Source Grounder (Day 1 midday, ~2 hours)
1. Implement `engine/app/pipeline/grounder.py` — for each extracted claim, verify it against the provided `source_context`
2. Build string-matching layer using `rapidfuzz`: find best-matching passage in source for each claim's `source_quote`
3. Build grounding score calculator:
   - 90–100: Direct textual support found → `grounded`
   - 70–89: Partial or inferred support → `partially_grounded`
   - Below 70: No supporting text in source → `ungrounded`
4. For claims without explicit quotes, use semantic overlap: send claim + source to Claude and ask "Is this claim supported by the source? Quote the supporting passage."
5. Return `{ claim_id, grounding_score, matched_passage, match_location }` per claim
6. Write tests: grounded claims score high, fabricated claims score low, paraphrased claims score mid-range

### Phase 4: Logical Consistency Checker (Day 1 afternoon, ~2 hours)
1. Build `engine/app/prompts/consistency_prompt.py` — skeptical reviewer persona that evaluates claims against source and each other
2. Implement `engine/app/pipeline/consistency.py` — two checks per claim:
   - **Source consistency:** Does the claim logically follow from the source context? (reasoning, not just string match)
   - **Internal consistency:** Do any claims in the output contradict each other?
3. Return `{ claim_id, source_consistent: bool, internal_consistent: bool, confidence: 1-10, reasoning: str }`
4. Build contradiction detector: flag pairs of claims that assert opposing things
5. Write tests: logically sound claims pass, contradictory claim sets get flagged, overreaching conclusions get caught

### Phase 5: Aggregator & Report Generator (Day 1 evening, ~2 hours)
1. Implement `engine/app/pipeline/aggregator.py` — combines results from extractor, grounder, and consistency checker
2. Compute per-claim integrity score: `grounding_weight(0.5) × grounding + consistency_weight(0.35) × consistency + type_weight(0.15) × type_modifier`
3. Classify each claim:
   - `verified`: grounding ≥ 90 AND consistent → green
   - `uncertain`: grounding 70–89 OR minor consistency concern → yellow
   - `flagged`: grounding < 70 OR contradicts source → orange
   - `hallucination`: grounding < 50 AND consistency fails → red, auto-removed
4. Compute overall integrity score: weighted average of claim scores, penalized by hallucination count
5. Generate `IntegrityReport` with: overall score, categorized claims, hallucination log with explanations, metadata (token usage, check durations)
6. Build `POST /verify` endpoint — accepts `VerifyRequest`, runs full pipeline, returns `IntegrityReport`
7. Write end-to-end tests: known-good output scores high, output with planted hallucinations gets them caught

### Phase 6: Engine API Polish (Day 2 morning, ~2 hours)
1. Add `POST /verify/quick` endpoint — grounding-only check, skips LLM consistency calls, fast and cheap
2. Add `POST /verify/claims` endpoint — accepts pre-extracted claims (skip extraction step) for users with their own claim logic
3. Add request validation, rate limiting, and structured error responses
4. Implement async parallel execution: run grounding and consistency checks concurrently per claim
5. Add response caching: hash `(source_context + llm_output)` → cache result
6. Write API documentation with example request/response for each endpoint

### Phase 7: Contract Reviewer Demo — Backend (Day 2 midday, ~3 hours)
1. Build `demo-app/backend/parsers/pdf_parser.py` — extract text + structure from PDFs using `pdfplumber`
2. Build `demo-app/backend/parsers/docx_parser.py` — extract text + structure from DOCX using `python-docx`
3. Build `demo-app/backend/parsers/clause_splitter.py` — split raw text into `{ section_id, title, text }` clause objects
4. Build `demo-app/backend/services/analyzer.py` — sends clause map to Claude with contract analyst prompt, returns risk findings as structured JSON
5. Build `demo-app/backend/services/verifier.py` — takes each analysis finding + original clause text, calls TrustLayer `/verify` endpoint, returns verified findings with integrity scores
6. Build demo API endpoints: `POST /upload`, `POST /analyze` (runs analysis + verification in sequence)
7. Prepare 3 demo contracts: bad NDA (one-sided, missing clauses), risky SaaS agreement (auto-renewal trap, no SLA), clean contract (should score high)

### Phase 8: Frontend — TrustLayer Playground (Day 2 afternoon, ~3 hours)
1. Scaffold React app with Vite + Tailwind, set up structure: `components/`, `hooks/`, `services/`, `views/`
2. Build `views/PlaygroundView.tsx` — two text areas: paste source context (left) + LLM output (right), hit "Verify"
3. Build `components/IntegrityScoreRing.tsx` — large animated circular gauge showing overall score (0–100)
4. Build `components/ClaimCard.tsx` — card per extracted claim: claim text, grounding score, consistency verdict, status badge (verified / uncertain / flagged / hallucination)
5. Build `components/HallucinationLog.tsx` — expandable section listing caught hallucinations with reasoning
6. Build `components/PipelineSteps.tsx` — visual stepper showing progress through extract → ground → check → aggregate
7. Wire playground to TrustLayer API, show real-time results

### Phase 9: Frontend — Contract Reviewer Demo (Day 2 evening, ~3 hours)
1. Build `views/ContractUploadView.tsx` — drag-and-drop upload zone with sample contract quick-load buttons
2. Build `views/ContractDashboardView.tsx` — two-panel layout: clause list (left), clause detail with findings (right)
3. Build `components/RiskBadge.tsx` — color-coded risk pills (Critical, Warning, Info, OK)
4. Build `components/ClauseDetail.tsx` — expandable view: original clause, AI finding, recommendation, TrustLayer verification inline (grounding score + consistency badge)
5. Build `components/ContractSummary.tsx` — top bar with contract type, overall risk, integrity score, plain-language summary
6. Build `components/MissingClauseAlert.tsx` — cards for flagged missing standard clauses
7. Add tab navigation: "Playground" (generic verifier) ↔ "Contract Reviewer" (demo app) to show the engine is general-purpose

### Phase 10: Polish & Deploy (Day 3 morning, ~3 hours)
1. Add loading animations and skeleton states for each pipeline stage
2. Add toggle: "Show removed findings" to reveal what TrustLayer filtered out
3. Add before/after view on contract dashboard: original AI findings vs. post-verification findings
4. Responsive design pass for demo-day projection screens
5. Deploy TrustLayer engine to Railway/Render
6. Deploy frontend to Vercel, wire to engine URL
7. Smoke test full flow on deployed URLs: playground verification + contract demo with all 3 test contracts

### Phase 11: Demo & Submission (Day 3 afternoon, ~2 hours)
1. Record video presentation (3–5 min):
   - Problem (30s): "Every company using LLMs ships hallucinations to users. There's no verification layer."
   - Product (30s): "TrustLayer is an integrity API. Feed it any source + LLM output, get a verified report."
   - Playground demo (60s): Paste a Wikipedia paragraph + a ChatGPT summary with planted errors → watch TrustLayer catch them
   - Contract demo (60s): Upload bad NDA → show analysis → show integrity layer catching and removing a hallucinated finding
   - Architecture + business value (60s): Platform play — any LLM app can plug in TrustLayer
2. Create slide deck (8–10 slides): problem, product, architecture diagram, playground screenshot, contract demo screenshot, market size, roadmap
3. Write GitHub README: project overview, architecture diagram, API docs, setup instructions, screenshots
4. Create cover image: split-screen showing the playground and contract reviewer
5. Final submission: GitHub repo URL, live demo URL, video, slides, cover image

### Phase 12: User-Selectable Provider + Model (post-demo)

> **Superseded in part by the Gemini track above.** Phase G2 already adds the Google adapter and promotes the `ClaudeClient` Protocol to a generic `LLMClient`. What remains here is OpenAI support, the `/models` endpoint, the cache-key fix, the budget guardrail, and the frontend selector — all still required, just no longer the *only* path to multi-provider.

Goal: let the user choose **provider + model** per request (Google Gemini 2.5 Pro/Flash, Anthropic Opus/Sonnet/Haiku, OpenAI GPT-5/GPT-5-mini, etc.) so they can trade off cost vs. quality and so TrustLayer can demonstrably claim "provider-agnostic." Server-side default is `("google", "gemini-2.5-flash")` (per Phase G2) and is enforced regardless of what the client sends.

> **Scope shift vs. earlier draft:** the original phase scoped this to picking among Claude models only. Going cross-provider is materially bigger — it requires a provider abstraction (or a router/gateway), per-provider prompt handling, two cost tables, two API keys, and a cache key that includes the provider. Decisions below must happen *before* coding.

#### Decisions to make first

1. **Build vs. route — pick one path before writing code.**
   - **(a) Vercel AI Gateway (recommended for hackathon scale).** One URL, one key, model strings like `"anthropic/claude-opus-4-6"` or `"openai/gpt-5"`. Gateway handles retries, fallback, observability, and adds a `provider` field to each call for free. Trade-off: lose Anthropic prompt caching unless the gateway exposes it; new external dependency.
   - **(b) Build your own provider abstraction.** New `LLMClient` Protocol with `AnthropicAdapter` and `OpenAIAdapter` behind it. Most control; you own every quirk. ~5–10× the work of (a).
   - **(c) Router function with two SDKs.** Pragmatic middle. Keep `AnthropicClient` for Claude calls; add `OpenAIClient`; switch at one call site. Less elegant than (b), faster than (b).
   - For the hackathon demo, **path (a) is the right answer** unless there's a specific reason Gateway can't expose what you need.

2. **Prompt portability strategy.**
   - Existing prompts in `engine/app/prompts/` (extractor / grounder / consistency) are Claude-tuned: XML-tag scaffolding (`<claim>`, `<thinking>`), terse system prompts, "respond with JSON only" instruction. GPT models do measurably better with explicit JSON schema (OpenAI structured output) than with Claude tag conventions.
   - Pick one and document it: **(i)** per-provider prompt variants (more files to maintain, best quality), **(ii)** one generic prompt (under-performs both), or **(iii)** provider-specific JSON-mode wiring (Anthropic tool-use, OpenAI `response_format`).

3. **Fast-model split — keep, drop, or per-provider?**
   - Today, `extractor.py:141`, `grounder.py:134`, `consistency.py:151` all default to `settings.anthropic_fast_model` (Haiku) — only `ReportMetadata.model` is the "headline" model. Currently the metadata lies (says Opus, Haiku ran).
   - Three options: **(i)** drop the fast-path entirely (single user-chosen model governs the whole pipeline → picking Opus is now ~15× more expensive), **(ii)** keep it but only when provider==Anthropic (document that "fast tier" is Claude-only), **(iii)** map fast-tier per provider (Anthropic Haiku, OpenAI gpt-5-mini). Decide explicitly; do not let Phase 12 default into option (i) silently.

#### Implementation outline (after decisions above)

1. **Engine — request schema**
   - Add `provider: Optional[Literal["anthropic", "openai"]]` and `model: Optional[str]` to `VerifyRequest`, `VerifyQuickRequest`, `VerifyClaimsRequest`, `VerifyBatchRequest` in `engine/app/models/schemas.py`. **Don't forget batch** — currently it has `mode` but no model field; if it stays absent, the dropdown will be inconsistent with batch behavior.
   - Validate against a centralized catalog in `engine/app/services/models.py`: `ALLOWED_MODELS = [{"provider": "anthropic", "id": "claude-opus-4-6", ...}, {"provider": "openai", "id": "gpt-5", ...}, ...]`. Reject unknown `(provider, model)` pairs with 400 + helpful error.
   - The catalog also owns prices so cost estimation and `/models` read from one place (avoids drift).

2. **Engine — provider abstraction**
   - Replace the existing `ClaudeClient` Protocol (`engine/app/services/anthropic_client.py:38-46`) with a provider-agnostic `LLMClient` returning normalized `(text, TokenUsage)`.
   - Implement either (a) a single `GatewayAdapter` (Vercel AI Gateway) or (b) two adapters (`AnthropicAdapter`, `OpenAIAdapter`), per the decision above.
   - Normalize token accounting: Anthropic uses `usage.input_tokens` / `usage.output_tokens`; OpenAI uses `usage.prompt_tokens` / `usage.completion_tokens` (plus `cached_tokens`, `reasoning_tokens` on newer models). The adapter must produce a uniform `TokenUsage` so `TokenLedger` (`anthropic_client.py:24-35`) keeps working unchanged.

3. **Engine — pipeline plumbing**
   - Thread `(provider, model)` through `VerifyPipeline.run(...)` to the call sites in `extractor.py`, `grounder.py`, `consistency.py`. Fall back to settings defaults when `None`.
   - Apply the fast-model decision from above at this layer.
   - Set `ReportMetadata.provider` (new field) and `ReportMetadata.model` to the **resolved** values that actually ran — fixing today's silent lie.

4. **Engine — cache key correctness (must-fix before shipping)**
   - `make_cache_key` (used in `engine/app/main.py:624,658,703,833`) is currently keyed on text only. Once `(provider, model)` is request-scoped, two callers with different picks on the same input will see each other's results. Update every call site to `make_cache_key("full", provider, model, source, output)`.
   - Without this fix, the model badge in the UI lies and the audit log lies.

5. **Engine — catalog endpoint**
   - Add `GET /models` returning `[{provider, id, label, tier: "flagship"|"balanced"|"fast", input_price_per_mtok, output_price_per_mtok, available: bool}]`. `available` is `false` when the corresponding API key is unset, so the frontend can grey out unusable options instead of 500ing on submit.
   - Drives the frontend dropdown's provider-grouped UI.

6. **Engine — auth + lifespan**
   - `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (or `AI_GATEWAY_API_KEY` if path (a)) are independently optional. Lifespan warning + `/health` should report per-provider availability.
   - `/models` filters out unavailable providers.

7. **Engine — server-side default safety**
   - Server default is `("google", "gemini-3-flash-preview")` (set in Phase G2). Any caller that omits `provider`/`model` (curl, partner integrations, demo backend if it forgets) gets this cheap default — not whatever `ANTHROPIC_MODEL` or `GEMINI_MODEL` env vars happen to point to.

8. **Engine — cost guardrail (must-have, not nice-to-have, in multi-provider mode)**
   - Once anyone can request Opus or GPT-5, a single legitimate caller within `RATE_LIMIT_PER_MINUTE=10` can burn ~15× the per-request cost of the default. Add `DAILY_FLAGSHIP_BUDGET_USD` (or per-provider variants); engine rejects flagship-tier requests once exceeded and falls back to balanced tier with a warning in `metadata.fallback_reason`.
   - Pairs with Phase 13 item 3 (per-key budget caps + idempotency) — you can ship the global daily cap now and per-key later.

9. **Demo backend — same treatment**
   - Add optional `(provider, model)` to `AnalyzeRequest`. Thread into `AnalysisPipeline` → analyzer LLM call + every `/verify` call it makes to the engine. When absent, use the backend's safe default.
   - Expose `/models` as a proxy to the engine so the frontend calls one URL regardless of tab.

10. **Frontend — selector**
    - New `components/ModelSelector.tsx`: dropdown grouped by provider (Anthropic, OpenAI), showing tier labels, "per 1M tokens" price hint, and disabled state for `available: false`.
    - Fetch options from `/models` on mount; cache in `useMemo`.
    - Persist user pick as `localStorage.trustlayer.modelPick = {provider, model}`.
    - **Validate stored pick against `/models` response on load** — a stored ID like `"claude-haiku-4-5-20251001"` will outlive whitelist updates and 400 every Verify click after deprecation. Fall back to API-provided default if the stored pair is missing or `available: false`.

11. **Frontend — wire into hooks**
    - `useVerify`: accept `(provider, model)`, include in request body.
    - `useContract.analyzeNow`: same.
    - Surface the selector in both views' action rows so the cost implication is visible at point of action.

12. **Frontend — show what ran**
    - `ReportSummary` and `ContractSummary` display a provider+model badge (e.g. "Anthropic · Haiku 4.5 · fast") tied to `metadata.provider` + `metadata.model`. With the cache-key fix in place, this badge is now trustworthy.

13. **Audit + observability**
    - `build_verify_record(...)` (`engine/app/services/audit.py`) gains a `provider` field. `/audit/events` becomes filterable by provider.
    - `/stats` should expose `metrics_by_provider` so latency p95 stops being bimodal-and-misleading once two providers mix.

14. **Tests**
    - Engine: parametrized tests that `/verify` honors `(provider, model)` per request, that unknown pairs return 400, and that **fakes assert on the resolved model arg reaching `create_message`** — otherwise wiring bugs pass silently.
    - Cache: assert two requests with same text but different `(provider, model)` produce distinct cache entries.
    - Default safety: assert a request omitting `(provider, model)` resolves to the safe default, not `settings.anthropic_model`.
    - Budget guardrail: assert flagship requests fall back to balanced once `DAILY_FLAGSHIP_BUDGET_USD` is exceeded.
    - Frontend: hook tests asserting the selected pair is included in the request body, persisted across reloads, and that a stale localStorage pair falls back gracefully.

15. **Docs**
    - Update `engine/API.md`: document `provider` + `model` fields, `/models` shape with `available`, the budget-guardrail behavior.
    - Update `README.md`: TrustLayer is provider-agnostic; how to add a new provider.
    - Update Phase 6 notes here once shipped.

### Phase 13: Production Readiness — Hardening Priorities (post-demo)

Ranked by impact on the codebase as it stands. Items 1–2 close real durability bugs that the current governance/scaling story already depends on; 3–5 build on that foundation.

**Storage philosophy: Postgres-first.** A single managed Postgres (Neon / Supabase / Render free tier) can host audit, trace, cache, and rate-limit state with one TTL sweeper job. Skip Redis until measurements show Postgres write QPS or lock contention is hurting p95 — likely never at current scale. Reach for R2/S3 only as cold-storage offload once audit volume outgrows the Postgres free tier.

1. **Durable audit + trace storage (Postgres-first)**
   - Today, `_audit_log` writes to a local JSONL file (`engine/app/services/audit.py`) and `_trace_store` lives in process memory (`engine/app/main.py`). On Render's ephemeral disk or any multi-instance deploy, audit lines and traces are lost or fragmented — which directly breaks the governance/explainability story TrustLayer is selling.
   - Add one Postgres instance (Neon free tier is the lowest-friction) with two tables:
     - `audit_events (id, request_id, endpoint, model, latency_ms, outcome_counts jsonb, tokens_in, tokens_out, cost_usd, created_at)` — append-only, replaces the JSONL writer.
     - `verify_traces (request_id pk, endpoint, report jsonb, evidence jsonb, expires_at)` — keyed lookup with TTL via an `expires_at` column.
   - Add a single periodic sweeper (FastAPI startup task or `pg_cron`) that runs `DELETE FROM verify_traces WHERE expires_at < now()` and optionally archives `audit_events` older than N days to R2.
   - Optional cold storage: when audit rows exceed the free-tier budget, batch-flush old rows to Cloudflare R2 (no egress fees) as date-partitioned JSONL and `DELETE` them from Postgres. R2 is fine for audit because it's write-mostly + append-only. **Do not** put trace there — keyed lookup with 15-min TTL is the wrong access pattern for object storage.
   - Acceptance: kill the engine pod between a `/verify` call and a `/verify/trace/{request_id}` lookup, and the trace still resolves.

2. **Postgres-backed limiter / cache (then horizontal scale is safe)**
   - Replace in-process `SlidingWindowRateLimiter` and `TTLCache` with Postgres-backed equivalents so multiple replicas share state. Two more tables on the same instance:
     - `response_cache (key pk, body jsonb, expires_at)` — same TTL sweeper as the trace table.
     - `rate_events (key, ts)` — sliding window via `SELECT count(*) FROM rate_events WHERE key = $1 AND ts > now() - interval '1 min'`. Index on `(key, ts)`.
   - Without this, two replicas effectively double the per-user rate limit and split the cache hit rate in half.
   - If load testing later shows the rate-limit table contending on writes, swap *just that table* for Upstash Redis free tier (10k commands/day, native TTL) — Postgres still owns audit/trace/cache. Don't speculatively pre-introduce Redis.
   - Acceptance: scale to 2+ engine instances behind a load balancer and observe consistent rate-limit headers + cache hit rate vs. single-instance.

3. **Per-key budget caps + idempotency keys**
   - Today, `RATE_LIMIT_PER_MINUTE` only bounds *request count*. A misbehaving caller can drive arbitrary Anthropic spend without ever tripping it.
   - Add per-key (or per-tenant) token and USD ceilings on top of the request limit; reject with `429` + budget headers when exceeded.
   - Add support for a client-supplied `Idempotency-Key` header on `/verify*` so safe retries don't double-bill expensive LLM calls. Cache the response keyed by `(api_key_id, idempotency_key)`.
   - Pairs naturally with Anthropic prompt caching for additional cost reduction.

4. **Tenant identifiers in audit + trace**
   - Add `tenant_id` / `api_key_id` fields to `build_verify_record(...)` output and the `VerifyTrace` model.
   - Enforce tenant-level filtering server-side on `/audit/events` and `/verify/trace/{request_id}` — a tenant must never read another tenant's records, even with a valid token.
   - Acceptance: integration test confirming a tenant-A token gets `404` (not `403`-leak) for tenant-B request_ids.

5. **Auth scoping — only if a partner integration forces it**
   - Current shared-secret bearer auth (`engine/app/main.py:150-158`) is fine until there's a real reason to give partners read-only access to a subset of endpoints.
   - Min-viable upgrade when needed: two tokens — `API_AUTH_TOKEN` for `/verify*` and a separate `ADMIN_TOKEN` for `/audit*` and `/stats`.
   - Defer JWT scopes (`verify:write` / `audit:read` / `stats:read`) until there's a concrete partner-integration driver; the JWT machinery is over-engineered for current maturity.
