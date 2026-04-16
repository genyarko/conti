# TrustLayer — LLM Output Integrity Checker

> **Core product:** A general-purpose API that verifies any LLM output for hallucinations, ungrounded claims, and logical inconsistencies.  
> **Showcase demo:** An AI Contract Reviewer powered by TrustLayer, proving the engine works on a high-stakes real-world use case.

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

### Phase 12: User-Selectable Model (post-demo, ~2 hours)

Goal: let the user choose a Claude model per request (Opus 4.6 / Sonnet 4.6 / Haiku 4.5) so they can trade off cost vs. quality. Server-side default still comes from `ANTHROPIC_MODEL`.

1. **Engine — request schema**
   - Add `model: Optional[str] = None` to `VerifyRequest`, `VerifyQuickRequest`, `VerifyClaimsRequest` in `engine/app/models/schemas.py`.
   - Validate against a whitelist in one place (`engine/app/services/models.py`): `ALLOWED_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}`. Reject unknown IDs with 400 + helpful error.

2. **Engine — pipeline plumbing**
   - Thread `model` through `VerifyPipeline.run(...)` down to the Anthropic call sites in `extractor.py`, `grounder.py`, `consistency.py`. Fall back to `settings.anthropic_model` when `None`.
   - No separate "fast model" override needed — the single `model` arg governs the whole pipeline for that request.
   - Include the resolved model in `ReportMetadata.model` (already the field, just ensure it reflects the request-scoped choice, not the default).

3. **Engine — catalog endpoint**
   - Add `GET /models` returning `[{id, label, tier: "flagship"|"balanced"|"fast", input_price_per_mtok, output_price_per_mtok}]` so the frontend has a single source of truth and the dropdown stays in sync with backend whitelist.

4. **Demo backend — same treatment**
   - Add optional `model` to `AnalyzeRequest`. Thread it into `AnalysisPipeline` → analyzer LLM call + every `/verify` call it makes to the engine. When absent, use the backend's `ANTHROPIC_MODEL`.
   - Expose the same `/models` proxy so the frontend can call one URL regardless of which tab it's on.

5. **Frontend — selector**
   - New `components/ModelSelector.tsx`: small dropdown with tier labels and a "per 1M tokens" price hint. Fetch options from `/models` on mount; cache in `useMemo` + `localStorage`.
   - Persist the user's pick in `localStorage.trustlayer.model`. Default to `claude-haiku-4-5-20251001` for cost safety.

6. **Frontend — wire into hooks**
   - `useVerify`: accept `model` param, include in `VerifyRequest` body.
   - `useContract.analyzeNow`: accept `model`, pass through to `/analyze`.
   - Surface the selector in both views' action rows (next to the "Verify" / "Review" button) so the user sees the cost implication at the point of action.

7. **Frontend — show what ran**
   - In `ReportSummary` metadata row and `ContractSummary`, display the model badge (e.g. "Haiku 4.5 · fast") tied to `metadata.model`, so the user can confirm the pipeline used their pick.

8. **Cost guardrail (nice-to-have)**
   - If Opus is selected, show a small "~15× more expensive than Haiku" tooltip on the button.
   - Optional: add `DAILY_OPUS_BUDGET_USD` env var; engine rejects Opus requests once exceeded and falls back to Sonnet with a warning in the response metadata.

9. **Tests**
   - Engine: parametrized tests that `/verify` honors a request-scoped `model` and that an unknown model returns 400.
   - Frontend: hook tests asserting the selected model is included in the request body and persisted across reloads.

10. **Docs**
    - Update `engine/API.md`: document the `model` field and the `/models` endpoint.
    - Update the main `README.md` and this file's Phase 6 notes once shipped.
