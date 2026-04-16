Security Scan Results

  🔴 CRITICAL

  Unauthenticated public endpoints → Anthropic billing DoS
  - engine/app/main.py:243-272, demo-app/backend/app/main.py:113-193
  - Both Render services (trustlayer-engine.onrender.com, trustlayer-contract-demo.onrender.com) expose /verify, /analyze, /upload with no auth. Each call hits the paid Anthropic API.    
  - Fix: Bearer-token middleware or IP allowlist before staying public.

  🟠 HIGH

  CORS wildcard methods/headers with allow_credentials=True
  - engine/app/main.py:70-73, demo-app/backend/app/main.py:56-59
  - Origins are whitelisted (good), but allow_methods=["*"] + allow_headers=["*"] turn into a live footgun if an origin is ever misconfigured.
  - Fix: allow_methods=["GET","POST"], allow_headers=["Content-Type"].

  Rate limit too loose for billed API
  - engine/app/main.py:88-119 (60 req/min/IP). Each /verify = 2 Anthropic calls.
  - Fix: Drop to ~10/min and/or per-key quotas.

  🟡 MEDIUM

  Prompt injection via contract text
  - demo-app/backend/app/prompts/analyzer_prompt.py:67-76 — user text interpolated into the prompt unescaped.
  - Fix: JSON-encode clauses before interpolation or wrap in explicit data markers.

  Unbounded dict[str, Any] / list fields
  - demo-app/backend/app/models/schemas.py:59, engine/app/models/schemas.py:55-58
  - Crafted DOCX with thousands of paragraphs → memory bloat.
  - Fix: max_items on clause lists, max_length on metadata.

  🟢 LOW

  Missing max_length on AnalyzeRequest.text — demo-app/backend/app/models/schemas.py:92-95. Runtime guard exists but no Pydantic-level cap.

  ✅ Clean

  Secrets (.env.production holds only public URLs), path traversal on /samples/{name}, frontend XSS (no dangerouslySetInnerHTML), logging (no contract bodies/keys), error handling        
  (generic internal_error), dep pins.