# Unauthorized Changes Report

This document summarizes changes identified in the codebase that were not part of the requested directives and diverge from the original repository baseline.

## 1. Absence Grounding Feature
A new feature was implemented to verify claims about missing or absent content (e.g., "The contract is missing a Governing Law clause"). This involves specialized LLM logic to prove non-existence.

### Affected Files:
*   **`engine/app/pipeline/grounder.py`**:
    *   Added `_is_absence_claim` heuristic.
    *   Added `_ground_absence` method for specialized semantic verification.
    *   Modified `ground` and `ground_batch` to handle absence claims separately.
*   **`engine/app/prompts/grounder_prompt.py`**:
    *   Added `ABSENCE_SYSTEM_PROMPT`.
    *   Added `ABSENCE_USER_TEMPLATE` and `build_absence_user_prompt`.
*   **`engine/app/models/schemas.py`**:
    *   Added `MISSING_CLAUSE` to the `ClaimCategory` enum.
*   **`engine/app/pipeline/aggregator.py`**:
    *   Added scoring modifier for `ClaimCategory.MISSING_CLAUSE`.
*   **`engine/tests/test_absence_grounding.py`**:
    *   Created a new test suite specifically for this feature.

## 2. Async Refactoring of Base Services
Core in-memory and file-based services were refactored to use `async` methods to align with the PostgreSQL backend interfaces.

### Affected Files:
*   **`engine/app/services/cache.py`**:
    *   Converted `TTLCache.get`, `set`, and `clear` to `async`.
    *   Added `async get_size` method.
*   **`engine/app/services/rate_limit.py`**:
    *   Converted `SlidingWindowRateLimiter.check` and `reset` to `async`.

## 3. Supporting Modifications
Changes were made to various test files to accommodate the async refactoring and new schema definitions.

### Affected Files:
*   `engine/tests/conftest.py`
*   `engine/tests/test_phase6_endpoints.py`
*   `engine/tests/test_postgres_storage.py`
*   `engine/tests/test_verify_trace.py`
*   `demo-app/backend/app/services/verifier.py`
