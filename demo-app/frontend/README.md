# TrustLayer Frontend

React + Vite + Tailwind playground for the TrustLayer engine.

## Setup

```bash
cd demo-app/frontend
npm install
npm run dev
```

App runs at http://localhost:5173. The TrustLayer engine must be running at
the URL set by `VITE_TRUSTLAYER_API_URL` (default `http://localhost:8000`).

## Structure

```
src/
  components/   # IntegrityScoreRing, ClaimCard, HallucinationLog, PipelineSteps, …
  hooks/        # useVerify — calls /verify and tracks pipeline state
  lib/          # status colors/labels, sample pairs
  services/     # TrustLayer API client
  types/        # TS types mirroring engine schemas
  views/        # PlaygroundView (Phase 8)
```

## Environment

| var                       | default                   | purpose                       |
|---------------------------|---------------------------|-------------------------------|
| `VITE_TRUSTLAYER_API_URL` | `http://localhost:8000`   | TrustLayer engine base URL    |
| `VITE_DEMO_API_URL`       | `http://localhost:8001`   | Contract demo backend (Phase 9) |
