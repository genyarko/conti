# Improvement Opportunities: Project Conti

While Project Conti is highly robust and mature, several areas could be enhanced to make it even more compelling for an enterprise audience or a hackathon submission.

## 1. Technical & Architectural Enhancements

### 🚀 Vector-Based Semantic Grounding
*   **Current:** Uses a hybrid of `rapidfuzz` (string matching) and single-turn LLM semantic checks.
*   **Improvement:** For long documents, integrate a vector database (like Chroma or Pinecone) to perform semantic search. This allows for more precise "matched passage" identification in very large contracts where passing the entire context to an LLM is expensive or hits token limits.

### 📦 Batched LLM Operations — *shipped*
*   **Was:** `ConsistencyChecker` and `ClaimGrounder` iterated over claims, triggering many parallel API calls.
*   **Now:** Both checkers chunk claims into `PIPELINE_BATCH_SIZE` (default 5) and issue one LLM call per batch, with per-claim retry on omitted IDs. Source-consistency batches run in parallel with the contradiction sweep. Grounder still does a fast `rapidfuzz` pass first and only batches the semantic fallbacks.
*   **Future:** Consider a per-source-context cache key so identical contracts re-use prior batch results.

### 🎭 Multi-Agent Debate for Verification
*   **Current:** A linear pipeline (Extract -> Ground -> Check).
*   **Improvement:** Introduce a "Legal Debate" phase where two distinct agents (one "prosecutor" finding flaws and one "defender" justifying support) debate the validity of a claim. A third "judge" agent (Gemini Pro) then makes the final verdict based on the debate transcript. This significantly increases trust in the results.

## 2. Gemini-Specific Optimizations

### 🖼️ Advanced Multimodal Interactivity
*   **Current:** Renders PDF pages to images for the initial analysis.
*   **Improvement:** Allow the user to click on a detected hallucination and have Gemini "re-scan" the specific *visual* area of the original PDF in high resolution to prove the supporting text isn't there (or is misrepresented).

### 🧠 Gemini Flash vs. Pro Tiering — *shipped (operation-type routing)*
*   **Was:** Pipeline used a single model for all stages.
*   **Now:** Stage-level tiering — extractor, grounder, and per-claim source-consistency run on Flash; cross-claim contradiction detection runs on Pro. `ReportMetadata` exposes both `model` and `fast_model` so consumers can attribute cost honestly. Note: takes effect only when `DEFAULT_MODEL` is set to the Pro variant; the current default collapses both tiers to Flash for cost.
*   **Future:** Per-claim complexity scoring (e.g., escalate quantitative or absolute-term claims to Pro on a per-row basis) — deferred because scoring before routing adds latency.

## 3. User Experience & Enterprise Features

### 🤝 Human-in-the-Loop (HITL) Feedback
*   **Current:** UI shows results; user can't interact with the "truth".
*   **Improvement:** Add a "Verify & Correct" button. If a user manually verifies a "hallucination" as actually correct, the system should log this as a "gold standard" example to refine its internal prompts or fine-tune future models.

### 📊 Real-Time Verification Graph
*   **Current:** Results are presented in cards.
*   **Improvement:** A visual graph showing the relationship between claims, clauses, and source passages. Visualizing a "contradiction" as a red line between two related claims in the UI would be very powerful for legal reviewers.

### 🌍 Multilingual Support
*   **Current:** Primarily optimized for English legal text.
*   **Improvement:** Leverage Gemini's multilingual capabilities to support cross-language verification (e.g., verifying an English summary against a Spanish source contract).

## 4. Operational & Security Enhancements

### 🛡️ Red-Teaming & Stress Testing — *shipped*
*   **Was:** Only correctness tests.
*   **Now:** `AdversaryAgent` (`engine/app/services/adversary.py`) generates a believable summary plus a manifest of 2 hallucinations + 1 contradiction; `bench/adversary_test.py` runs it through `VerifyPipeline` and reports per-injection caught/missed via `rapidfuzz` token-set matching against the extracted claim text. Captured baseline lives in `red-teaming-test-result.md`.
*   **Future:** Loop the harness over a corpus of contract types and persist a regression score; promote the bench to CI once the catch rate is stable.
