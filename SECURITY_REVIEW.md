# Security review — uncommitted changes on `main`

Reviewed the new/modified files for security issues. Severities calibrated to a **single-tenant**, behind-bearer-token deployment.

---

## HIGH — New `GET` endpoints bypass authentication, leaking verification data

The auth gate only fires on `POST /verify*`:

```python
def _requires_auth(request: Request) -> bool:
    return request.method == "POST" and request.url.path.startswith("/verify")
```
`engine/app/main.py:122-123`

The three new endpoints are all `GET`, so the middleware passes them through even when `API_AUTH_TOKEN` is set:

- `GET /stats` — `engine/app/main.py:445`
- `GET /audit/events` — `engine/app/main.py:480`
- `GET /verify/trace/{request_id}` — `engine/app/main.py:548`

The trace endpoint is the worst: it returns the full `IntegrityReport` plus per-claim evidence (`output_quote`, `matched_passage`, grounding/consistency reasoning) — i.e. content derived from customer `source_context` and `llm_output`. Combined with the also-unauthenticated `/audit/events`, an anonymous attacker can:

1. `GET /audit/events?limit=500` to enumerate `request_id` values from recent traffic.
2. `GET /verify/trace/{request_id}` for each → recover prior LLM outputs, source contexts, and reasoning that paying customers sent in.

The 48 bits of UUID-derived entropy in `request_id` is irrelevant once `/audit/events` returns them in plaintext.

**Fix:** widen the auth gate. E.g.:

```python
_PROTECTED_PREFIXES = ("/verify", "/audit", "/stats")

def _requires_auth(request: Request) -> bool:
    if request.url.path in ("/", "/health"):
        return False
    return request.url.path.startswith(_PROTECTED_PREFIXES)
```

(Or move auth onto an APIRouter dependency and apply it to every router that handles non-public data.)

---

## MEDIUM — `/audit/events` reads the entire log into memory; unauthenticated → DoS amplification

`engine/app/services/audit.py:71`:

```python
with self._lock:
    raw = self._path.read_bytes()
```

Default `audit_max_bytes` is 10 MiB, but operators can raise it via `AUDIT_MAX_BYTES`. Combined with the missing auth (Finding 1), a single unauthenticated client can repeatedly trigger a 10+ MiB allocation per request, holding the `threading.Lock` against writers while it does. Also blocks audit writes from concurrent verify calls.

**Fix:** stream the file backwards (mmap + reverse line walk, or maintain an in-memory ring buffer of the last N records). At minimum, gate it behind auth.

---

## MEDIUM — `X-Forwarded-For` blindly trusted for rate-limit keying

`engine/app/main.py:115-119`:

```python
def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
```

If the engine is exposed without a proxy that overwrites `X-Forwarded-For` (or behind one that appends instead of replaces), an attacker can rotate the header per request and never share a bucket with themselves. The sliding-window limiter (`engine/app/services/rate_limit.py`) is keyed on this value, so `RATE_LIMIT_PER_MINUTE` becomes effectively unenforced.

This issue predates the diff but is more impactful now because the audit/trace endpoints amplify the value of the rate limit.

**Fix:** only honor `X-Forwarded-For` when a `TRUSTED_PROXIES` setting matches `request.client.host`; otherwise fall back to `request.client.host`.

---

## LOW — `BatchItemError.message` echoes raw exception text

`engine/app/main.py:846-855`:

```python
except Exception as exc:  # noqa: BLE001
    log.exception("Batch item %d failed", index)
    return BatchItemResult(
        index=index, status="error",
        error=BatchItemError(code=type(exc).__name__,
                             message=str(exc) or "unexpected error"),
    )
```

`str(exc)` on Anthropic SDK / `httpx` errors can include URLs, partial request bodies, retry headers, etc. Not catastrophic, but it's verbatim internal-error text returned to clients. Consider emitting a stable short message and logging the full text server-side.

---

## LOW — Trace store and audit log are global / untenanted

`_trace_store` and `_audit_log` are module-level singletons keyed only by `request_id`/recency. With a single shared `API_AUTH_TOKEN`, every authorized caller sees every trace and every audit event. If you ever move to per-customer tokens (or multi-tenant SaaS), this is a cross-tenant data leak. Worth flagging now so the schema can be extended with a `tenant_id`/`api_key_id` before any third party gets a token.

---

## Items checked and clean

- Audit log JSON output uses `json.dumps(..., default=_json_default)` — no log injection from user input; the only field interpolated from anything user-facing is the `model` name (server-controlled).
- `AuditLog._rotate_locked` uses `os.replace` for atomic swap and stays under the lock — no rotation race.
- `_parse_since_param` validates ISO-8601 before passing to `datetime.fromisoformat`.
- `audit_path` is environment-only (no request-controlled paths) — no path-traversal sink.
- `VerifyBatchRequest.mode` is regex-validated; `_enforce_size` and `_enforce_batch_count` cap payload size and item count.
- `hmac.compare_digest` is used for token comparison (constant-time).
- `_process_batch_item` isolates per-item failures; failure path doesn't leak other items' results.

---

## Suggested order of fixes

1. Apply auth to `/audit/events`, `/stats`, `/verify/trace/*` (HIGH).
2. Stream/bound `read_tail`, or add a per-IP rate limit on `/audit/events` (MEDIUM).
3. Add `TRUSTED_PROXIES` check to `_client_key` (MEDIUM).
4. Sanitize `BatchItemError.message`; log raw text only (LOW).
5. Add a `tenant_id` field on the trace/audit records before adding a second API consumer (LOW).
