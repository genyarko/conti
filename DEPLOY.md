# Deploying TrustLayer

Two backends + one static frontend. Estimated time: ~20 minutes.

## Services

| Service | What it is | Suggested host |
|---|---|---|
| `trustlayer-engine` | Core `/verify` API (Python, FastAPI) | Render |
| `trustlayer-contract-demo` | Contract reviewer backend (Python, FastAPI) | Render |
| `frontend` | React + Vite SPA | Vercel |

## 1. Deploy both backends to Render

The repo includes `render.yaml` at the root, which defines both services.

1. Push this repo to GitHub.
2. In the Render dashboard → **New → Blueprint** → connect the repo.
3. Render detects `render.yaml` and proposes two web services.
4. On both services, set `ANTHROPIC_API_KEY` in the environment tab.
5. Click **Apply**. Render builds and boots both services.

The blueprint automatically wires the demo backend's `TRUSTLAYER_BASE_URL`
to the engine service's internal URL.

### Health checks
- Engine: `GET /health` → `{ "status": "ok", ... }`
- Contract demo: `GET /health` → `{ "status": "ok", ... }`

## 2. Deploy the frontend to Vercel

```bash
cd demo-app/frontend
cp .env.production.example .env.production.local
# edit: point VITE_TRUSTLAYER_API_URL + VITE_CONTRACT_API_URL at your Render URLs
```

Then either:

**Option A — CLI:**
```bash
npm i -g vercel
vercel --prod
```

**Option B — Dashboard:**
1. Import the repo on Vercel.
2. Set **Root Directory** to `demo-app/frontend`.
3. Under **Environment Variables**, add:
   - `VITE_TRUSTLAYER_API_URL` → engine Render URL
   - `VITE_CONTRACT_API_URL` → contract demo Render URL
4. Build command and output directory are picked up from `vercel.json`.

## 3. Tighten CORS (recommended)

By default, `render.yaml` sets `CORS_ORIGINS=*` for the initial smoke test.
Once you know the Vercel URL, update both services' `CORS_ORIGINS` env to
the exact origins, comma-separated:

```
CORS_ORIGINS=https://trustlayer.vercel.app,https://www.your-domain.com
```

## 4. Smoke test

- Open the Vercel URL.
- **Playground tab**: paste the "Eiffel Tower" sample → Verify → the planted
  "solid gold" claim should land in the Hallucinations bucket.
- **Contract Reviewer tab**: click **Bad NDA** sample. You should see:
  - A populated clause list on the left
  - Risk-tagged findings per clause
  - A `Show before/after` toggle exposing any findings TrustLayer removed

## Alternative platforms

- **Railway / Fly.io**: both backends expose a standard uvicorn app; use the
  `Procfile` in each backend directory and set `ANTHROPIC_API_KEY`.
- **Self-hosted**: `uvicorn engine.app.main:app` from the repo root for the
  engine, and `uvicorn app.main:app --app-dir demo-app/backend` for the demo.
