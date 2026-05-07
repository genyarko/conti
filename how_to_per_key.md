1. First, fix the keying problem                                                                                                                                            
                                                                                                                                                                              
  API_AUTH_TOKEN is one shared bearer; "per-key budget" is meaningless until callers are distinguishable. Smallest viable change:                                             
                                                                                                                                                                              
  CREATE TABLE api_keys (
    id              TEXT PRIMARY KEY,           -- public, e.g. "tlk_a1b2"
    hashed_secret   BYTEA NOT NULL,             -- sha256 of the secret half
    name            TEXT,
    daily_usd_cap   NUMERIC(12,4),              -- NULL = no cap
    daily_token_cap BIGINT,
    monthly_usd_cap NUMERIC(12,4),
    disabled_at     TIMESTAMPTZ
  );

  Header format: Authorization: Bearer <id>.<secret>. auth_middleware (engine/app/main.py:277) splits, looks up by id, compares with hmac.compare_digest, stashes
  request.state.api_key_id. Keep the legacy single-token path behind API_AUTH_TOKEN for dev — synthesize a "default" key id for it so downstream code is uniform.

  2. Reuse audit_events, don't add a spend_events table

  audit_events.payload already carries input_tokens, output_tokens, estimated_cost_usd. Add one column:

  ALTER TABLE audit_events ADD COLUMN api_key_id TEXT;
  CREATE INDEX audit_events_key_ts_idx ON audit_events (api_key_id, ts DESC);

  Budget check is then a single aggregate, post-debit:

  SELECT COALESCE(SUM((payload->>'estimated_cost_usd')::numeric), 0)
  FROM audit_events
  WHERE api_key_id = $1 AND ts > now() - interval '1 day';

  Don't try to pre-estimate the upcoming call — accept worst-case one-call overshoot. That's the standard pattern (Stripe, OpenAI both do post-debit).

  3. Middleware order

  Insert a new budget_middleware between auth_middleware and the existing rate limiter. The pipeline becomes:

  auth → idempotency-replay → req/min limit → USD/token budget → cache → pipeline → debit (audit) → idempotency-store

  On budget breach, return 429 with:
  X-Budget-USD-Limit / -Remaining / -Reset
  X-Budget-Tokens-Limit / -Remaining
  Retry-After: <seconds-until-rolling-window-frees-up>

  Use a different error code from rate-limit ("error": "budget_exceeded") so clients can branch.

  4. Idempotency table

  CREATE TABLE idempotency_keys (
    api_key_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    BYTEA NOT NULL,        -- sha256 of the canonical request body
    status_code     INTEGER,               -- NULL = in-flight
    body            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_key_id, idempotency_key)
  );
  CREATE INDEX idempotency_keys_expires_idx ON idempotency_keys (expires_at);

  Flow inside a new idempotency_middleware (only on POST /verify*):

  1. No header → pass through.
  2. INSERT … ON CONFLICT DO NOTHING an in-flight row (status_code = NULL).
  3. Insert succeeded → run handler, UPDATE row with the final (status_code, body). On 5xx delete the row (Stripe persists errors, but for a billable LLM call the saner
  default is "let the retry actually retry"). Document this choice.
  4. Conflict, request_hash differs → 422 idempotency_key_reuse.
  5. Conflict, status_code set → replay the stored response, add header Idempotent-Replayed: true.
  6. Conflict, status_code NULL → race; respond 409 idempotent_request_in_progress. Don't poll/wait — keeps the handler stateless and avoids holding pool connections.

  TTL = 24h (Stripe convention); the existing run_pg_sweeper (engine/app/services/postgres.py) already deletes by expires_at in one CTE — extend that CTE with one more
  DELETE.

  5. Files to touch

  - engine/config/settings.py — idempotency_ttl_seconds, default_daily_usd_cap, default_daily_token_cap, budget_enabled.
  - engine/app/services/api_keys.py (new) — key resolver + cap fetch (memoized w/ short TTL so hot path is one cached lookup, not a DB hit per request).
  - engine/app/services/postgres.py — PgBudgetStore, PgIdempotencyStore; extend _DDL and the sweeper CTE.
  - engine/app/main.py — two new middlewares; _emit_audit_for_* fns include api_key_id; expose /admin/keys only behind a separate root-token if you want self-service.
  - Tests — race tests must hit a real Postgres (per the existing review note that _FakeConn runs serially — same caveat applies here).

  6. Tradeoffs to flag

  - In-memory fallback is harder. SlidingWindowRateLimiter works without DB; budgets fundamentally don't (you need durable audit_events). Either gate budgets behind
  DATABASE_URL, or accept that single-process deploys get no budget enforcement. I'd gate it.
  - Pool exhaustion. Each /verify already does 4–5 DB roundtrips per the code review; this adds 2 (budget aggregate + idempotency upsert). Bump database_pool_max from 5 → 20+
   before turning this on, or batch the budget check into the rate-limit query.
  - Anthropic prompt caching pairs in for free as long as engine/app/services/pricing.py charges cache-read tokens at the discounted rate (10% on Anthropic). Worth a one-line
   check before claiming the savings flow through to budget headers.
  - Idempotency request_hash should be deterministic. Pydantic model_dum
    status_code     INTEGER,               -- NULL = in-flight
    body            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_key_id, idempotency_key)
  );
  CREATE INDEX idempotency_keys_expires_idx ON idempotency_keys
  (expires_at);

  Flow inside a new idempotency_middleware (only on POST /verify*):

  1. No header → pass through.
  2. INSERT … ON CONFLICT DO NOTHING an in-flight row (status_code =
  NULL).
  3. Insert succeeded → run handler, UPDATE row with the final
  (status_code, body). On 5xx delete the row (Stripe persists errors,
  but for a billable LLM call the saner default is "let the retry
  actually retry"). Document this choice.
  4. Conflict, request_hash differs → 422 idempotency_key_reuse.
  5. Conflict, status_code set → replay the stored response, add header
  Idempotent-Replayed: true.
  6. Conflict, status_code NULL → race; respond 409
  idempotent_request_in_progress. Don't poll/wait — keeps the handler
  stateless and avoids holding pool connections.

  TTL = 24h (Stripe convention); the existing run_pg_sweeper
  (engine/app/services/postgres.py) already deletes by expires_at in one
   CTE — extend that CTE with one more DELETE.

  5. Files to touch

  - engine/config/settings.py — idempotency_ttl_seconds,
  default_daily_usd_cap, default_daily_token_cap, budget_enabled.
  - engine/app/services/api_keys.py (new) — key resolver + cap fetch
  (memoized w/ short TTL so hot path is one cached lookup, not a DB hit
  per request).
  - engine/app/services/postgres.py — PgBudgetStore, PgIdempotencyStore;
   extend _DDL and the sweeper CTE.
  - engine/app/main.py — two new middlewares; _emit_audit_for_* fns
  include api_key_id; expose /admin/keys only behind a separate
  root-token if you want self-service.
  - Tests — race tests must hit a real Postgres (per the existing review
   note that _FakeConn runs serially — same caveat applies here).

  6. Tradeoffs to flag

  - In-memory fallback is harder. SlidingWindowRateLimiter works without
   DB; budgets fundamentally don't (you need durable audit_events).

  - engine/config/settings.py — idempotency_ttl_seconds, default_daily_usd_cap, default_daily_token_cap, budget_enabled.
  - engine/app/services/api_keys.py (new) — key resolver + cap fetch (memoized w/ short TTL so hot path is one cached lookup, not a DB hit per request).
  - engine/app/services/postgres.py — PgBudgetStore, PgIdempotencyStore; extend _DDL and the sweeper CTE.
  - engine/app/main.py — two new middlewares; _emit_audit_for_* fns include api_key_id; expose /admin/keys only behind a separate root-token if you want self-service.
  - Tests — race tests must hit a real Postgres (per the existing review note that _FakeConn runs serially — same caveat applies here).

  6. Tradeoffs to flag

  - In-memory fallback is harder. SlidingWindowRateLimiter works without DB; budgets fundamentally don't (you need durable audit_events). Either gate budgets behind
  DATABASE_URL, or accept that single-process deploys get no budget enforcement. I'd gate it.
  - Pool exhaustion. Each /verify already does 4–5 DB roundtrips per the code review; this adds 2 (budget aggregate + idempotency upsert). Bump database_pool_max from 5 → 20+
   before turning this on, or batch the budget check into the rate-limit query.
  - Anthropic prompt caching pairs in for free as long as engine/app/services/pricing.py charges cache-read tokens at the discounted rate (10% on Anthropic). Worth a one-line
   check before claiming the savings flow through to budget headers.
  - Idempotency request_hash should be deterministic. Pydantic model_dump_json(sort_keys=True) is fine; raw request.body() isn't (whitespace differences). Hash
  post-validation, not pre.