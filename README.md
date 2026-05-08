# TrustLayer — LLM Output Integrity Checker

![Powered by Gemini](https://img.shields.io/badge/Powered%20by-Gemini-blue?style=for-the-badge&logo=google-gemini&logoColor=white)
![Build with Google AI Studio](https://img.shields.io/badge/Built%20with-Google%20AI%20Studio-orange?style=for-the-badge&logo=google)

A general-purpose API that verifies any LLM output for hallucinations, ungrounded claims, and logical inconsistencies.

> **Hackathon Entry:** This project is built for the **Gemini / Google AI Studio** track. Gemini Pro 3.1 serves as the "agent brain" for deep multimodal reasoning, while Gemini Flash handles high-volume extraction and grounding.

---

## How the Agent Works

TrustLayer operates as an autonomous agent pipeline designed to verify text through perception, reasoning, and self-verification:
1. **Perception & Decomposition (Plan):** Gemini Flash reads the raw LLM output and extracts it into atomic, verifiable claims.
2. **Tool Use & Grounding (Read):** The agent cross-references each claim against the original source context, scoring direct textual support.
3. **Deep Reasoning & Self-Verification (Verify):** Gemini Pro acts as a skeptical reviewer, evaluating the logical consistency of the claims against the source and each other.
4. **Reconciliation (Reconcile):** The agent aggregates these findings into a final integrity score, surfacing hallucinations and ungrounded statements.

## Screenshots

*(Placeholder for final submission screenshots. Replace URLs with actual image paths before submitting.)*

**TrustLayer Playground:**
![TrustLayer Playground - Verifying LLM outputs](https://placehold.co/800x400/eee/333?text=TrustLayer+Playground)

**Contract Reviewer Demo:**
![Contract Reviewer Demo - Multimodal PDF analysis](https://placehold.co/800x400/eee/333?text=Contract+Reviewer+Demo)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│             TRUSTLAYER ENGINE (Powered by Gemini 3.1)             │
│                                                                  │
│  Input: { source_context, llm_output, schema? }                  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │           SECURITY PROXY (Veea Lobster Trap)               │  │
│  │      Deep Prompt Inspection / Policy Enforcement           │  │
│  └──────────────────────────────┬─────────────────────────────┘  │
│                                 │                                │
│  ┌──────────────┐  ┌────────────▼─┐  ┌────────────────────────┐  │
│  │  Claim        │→ │  Source       │→ │  Logical               │  │
│  │  Extractor    │  │  Grounder    │  │  Consistency Checker   │  │
│  │ (Gemini Flash)│  │(Gemini Flash)│  │ (Gemini Pro)           │  │
│  └──────────────┘  └──────────────┘  └────────────────────────┘  │
│         │                 │                      │                │
│         ▼                 ▼                      ▼                │
│  Atomic claims     Grounding scores      Consistency verdicts    │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Aggregator → per-claim + overall integrity report       │    │
│  └──────────────────────────────────────────────────────────┘    │
│         │                                                        │
│         └──────────┐                                             │
│                    ▼                                             │
│          ┌──────────────────────┐        ┌──────────────────┐    │
│          │ Durable Audit (PG)   │ ──────▶ │ Cold Storage (R2)│    │
│          └──────────────────────┘        └──────────────────┘    │
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

> **Note:** TrustLayer is a **provider-agnostic** platform. While the engine defaults to **Google Gemini** as its primary reasoning brain (and for the purpose of this hackathon track), it also supports Anthropic and other models via its extensible LLM adapter.

---

## API Reference

The core TrustLayer Engine exposes multiple verification endpoints.

### `POST /verify`
Runs the full TrustLayer verification pipeline (Extract → Ground → Check → Aggregate).

**Example Request:**
```json
{
  "source_context": "The company was founded in 2020 by Jane Doe. It raised $5M in Series A funding.",
  "llm_output": "Jane Doe founded the company in 2021 and raised $10M.",
  "provider": "google",
  "model": "gemini-3.1-pro-preview"
}
```

**Example Response:**
```json
{
  "overall_score": 25.0,
  "verified_claims": [],
  "flagged_claims": [],
  "hallucinations": [
    {
      "claim_id": "c1",
      "text": "Jane Doe founded the company in 2021",
      "grounding_score": 30,
      "is_hallucination": true,
      "reasoning": "The source states the company was founded in 2020, contradicting the claim of 2021."
    },
    {
      "claim_id": "c2",
      "text": "raised $10M",
      "grounding_score": 0,
      "is_hallucination": true,
      "reasoning": "The source states the company raised $5M, not $10M."
    }
  ],
  "metadata": {
    "provider": "google",
    "model": "gemini-3.1-pro-preview",
    "cost_usd": 0.0015
  }
}
```

*(Explore `http://localhost:8000/docs` for additional endpoints like `/verify/quick`, `/verify/claims`, and `/verify/batch`)*

---

## Built with Google AI Studio

Our core prompts were prototyped and tuned using Google AI Studio. You can view the prompt histories and configurations here:

- [Claim Extractor Prompt](https://aistudio.google.com/app/prompts/example-extractor)
- [Source Grounder Prompt](https://aistudio.google.com/app/prompts/example-grounder)
- [Consistency Checker Prompt](https://aistudio.google.com/app/prompts/example-consistency)
- [Contract Analyst (Demo) Prompt](https://aistudio.google.com/app/prompts/example-analyst)

## Layout

- `engine/` — TrustLayer Python + FastAPI service (the core product)
- `demo-app/` — Contract Reviewer demo (React + Vite frontend, FastAPI backend) showcasing TrustLayer

## Quick start

```bash
cp .env.example .env
# fill in GEMINI_API_KEY (Google AI Studio)

cd engine
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

See `nextSteps.md` for the full implementation roadmap.
