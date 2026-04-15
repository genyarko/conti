# TrustLayer

A general-purpose API that verifies any LLM output for hallucinations, ungrounded claims, and logical inconsistencies.

## Layout

- `engine/` — TrustLayer Python + FastAPI service (the core product)
- `demo-app/` — Contract Reviewer demo (React + Vite frontend, FastAPI backend) showcasing TrustLayer

## Quick start

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY

cd engine
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

See `nextSteps.md` for the full implementation roadmap.
# conti
