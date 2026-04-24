# Production Readiness Checklist for TrustLayer

Date: 2026-04-24

This checklist focuses on practical, high-leverage improvements to move the current TrustLayer codebase from hackathon-quality to production-grade reliability.

## 0) Immediate hardening (this week)

1. **Split auth domains and tokens**
   - Replace the single `API_AUTH_TOKEN` with scoped tokens (or JWT scopes):
     - `verify:write` for `/verify*`
     - `audit:read` for `/audit/events`
     - `stats:read` for `/stats`
   - Benefit: least privilege and safer partner integrations.

2. **Add secure defaults for deployment**
   - Fail startup in `production` if critical settings are missing:
     - `API_AUTH_TOKEN`
     - `ANTHROPIC_API_KEY`
     - non-empty `TRUSTED_PROXY_IPS` when reverse-proxy mode is enabled.
   - Benefit: avoids silent insecure deployments.

3. **Tighten public surface contract**
   - Keep only `GET /` and `GET /health` public.
   - Explicitly test auth requirements for all other endpoints in CI.

## 1) Reliability + scaling

1. **Move from in-memory to external state for multi-instance deployments**
   - Replace in-memory rate limiter/cache/trace store with Redis-backed equivalents.
   - Benefit: consistency across replicas and horizontal scalability.

2. **Bound expensive operations with timeouts and circuit breakers**
   - Add explicit upstream timeouts and retry budgets around Anthropic calls.
   - Use a circuit-breaker policy on repeated upstream failures.
   - Benefit: protects latency SLOs and prevents cascading failure.

3. **Async job mode for large batches**
   - Add queued batch processing (`202 Accepted` + job ID + status endpoint).
   - Benefit: better UX and resilience for high-volume workloads.

## 2) Observability + SRE readiness

1. **Structured logs with correlation IDs everywhere**
   - Ensure every log line carries `request_id`, endpoint, tenant/key ID (when available), and timing.
   - Emit JSON logs for ingestion into Datadog/ELK.

2. **Metrics export for real monitoring systems**
   - Keep `/stats` for demo UX but add Prometheus/OpenTelemetry metrics.
   - Add RED metrics (rate/errors/duration) + token/cost metrics.

3. **Define SLOs + alerting**
   - Example initial SLOs:
     - p95 latency for `/verify/quick` under target load
     - error rate < 1%
     - queue lag/job timeout thresholds
   - Wire alerts to PagerDuty/Slack.

## 3) Data governance + compliance

1. **Audit and trace tenant isolation**
   - Add `tenant_id`/`api_key_id` to audit + trace records.
   - Enforce tenant-level filtering server-side.

2. **Retention and deletion policies**
   - Define TTL + retention windows for audit/trace data.
   - Add admin endpoints/jobs for retention enforcement and deletion requests.

3. **PII-safe logging**
   - Add content redaction and deny-list filters before persistence/logging.
   - Document data handling policy.

## 4) Security engineering

1. **Defense-in-depth at the edge**
   - Put the API behind an API gateway/WAF.
   - Add per-key quotas and anomaly detection (not just per-IP rate limits).

2. **Secrets and key rotation**
   - Use managed secrets (AWS Secrets Manager/GCP Secret Manager/Vault).
   - Support rotation without downtime.

3. **Supply chain + CI security gates**
   - Add dependency scanning (pip-audit/Snyk), SAST, and container scanning in CI.
   - Block merges on critical vulnerabilities.

## 5) API lifecycle and quality

1. **Version API explicitly**
   - Introduce `/v1` routes and a deprecation policy.

2. **Contract testing**
   - Add OpenAPI schema snapshot tests + backward compatibility checks.

3. **Load and chaos testing in CI/CD**
   - Add repeatable load tests and periodic fault-injection drills.

## Suggested implementation order

1. Auth scopes + tenant identifiers.
2. Redis for limiter/cache/trace.
3. Upstream timeouts/retries/circuit breaker.
4. OTel/Prometheus + SLO alerts.
5. Async batch jobs.
6. Compliance retention + deletion controls.

## Minimum production bar (go-live checklist)

- [ ] No anonymous access to non-public endpoints.
- [ ] Per-key auth + quotas.
- [ ] Cross-instance shared limiter/cache.
- [ ] p95/p99 and error-rate alerts active.
- [ ] Tenant isolation enforced in audit/trace.
- [ ] Security scans required in CI.
- [ ] Runbook for incident response and rollback.
