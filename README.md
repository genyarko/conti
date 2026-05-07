# TrustLayer — LLM Output Integrity Checker

![Powered by Gemini](https://img.shields.io/badge/Powered%20by-Gemini-blue?style=for-the-badge&logo=google-gemini&logoColor=white)
![Build with Google AI Studio](https://img.shields.io/badge/Built%20with-Google%20AI%20Studio-orange?style=for-the-badge&logo=google)

A general-purpose API that verifies any LLM output for hallucinations, ungrounded claims, and logical inconsistencies.

> **Hackathon Entry:** This project is built for the **Gemini / Google AI Studio** track. Gemini Pro 3.1 serves as the "agent brain" for deep multimodal reasoning, while Gemini Flash handles high-volume extraction and grounding.

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
# conti
