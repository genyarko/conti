Review: Batched LLM Operations                                                                                                                                              
                                                                                                                                                                              
  The fix lands the core idea — both ConsistencyChecker.check and ClaimGrounder.ground_many now group claims into a single LLM call with a dedicated batch prompt + schema.   
  New batch templates (CONSISTENCY_BATCH_*, GROUNDER_BATCH_*) are clean and mirror the per-claim shape.                                                                       
                                                                                                                                                                              
  But there are a few real issues worth fixing before calling it done.                                                                                                        
                                                                                                                                                                              
  What works well                                                                                                                                                           

  - Grounder batches only the fallback set (grounder.py:266-307). The fast rapidfuzz path still short-circuits per-claim, so you only pay the LLM tax on claims that actually
  need it. Smart.
  - _build_result is reused across single and batch paths — no logic drift.
  - Single-claim path is preserved for both, keeping existing tests and behavior intact.
  - Batch-result map keyed by claim_id with a per-claim fallback verdict if the model omits one. Graceful degrade.

  Real problems

  1. Batches run sequentially, not in parallel (consistency.py:235-258, grounder.py:272-307)
  for i in range(0, len(claims), batch_size):
      batch = claims[i : i + batch_size]
      raw = await self._client.create_message(...)   # serial
  With 50 claims and batch_size=5, that's 10 sequential round-trips. The pre-batching version likely fanned out with asyncio.gather, so you've traded N parallel calls for
  ⌈N/5⌉ serial ones. Net win for cost and rate limits; possible regression in latency for small-to-medium claim counts. Wrap the chunks in asyncio.gather and keep both wins.

  2. Wasteful create_task then immediate await (consistency.py:261-262)
  contradiction_task = asyncio.create_task(self.find_contradictions(claims))
  contradictions = await contradiction_task
  This doesn't overlap with anything anymore — the source-consistency batch loop already finished. Either start find_contradictions before the batch loop and gather them, or
  drop the task wrapper.

  3. batch_size = 5 is hardcoded twice
  Different documents need different sizes — a 100-page contract with 5 claims per batch may push prompts huge; a short summary could batch 10–20 fine. Promote to settings.

  4. No retry on partial batch responses
  If the model returns 4 of 5 claim_ids, the missing one becomes a default INCONSISTENT, "Batch check failed..." verdict (consistency.py:270) or UNGROUNDED
  (grounder.py:299-307). For a trust product, silently downgrading a claim because of a model omission is worse than spending one more call to retry it individually.

  5. Single-fallback shim re-runs string match (grounder.py:268-270)
  if len(to_fallback) == 1:
      claim, match = to_fallback[0]
      results_by_id[claim.id] = await self.ground(claim, source_context)
  ground() recomputes _best_match from scratch, even though you already have the match in to_fallback[0][1]. Cheap, but unnecessary — and the comment "test compatibility"
  suggests the shim exists for tests, not behavior. Consider extracting a _ground_single_with_match helper instead.

  6. Test coverage gap
  - test_consistency.py covers the 2-claim batch path (test_contradicting_claims_are_linked_bidirectionally) ✅
  - test_grounder.py has no test exercising the new batch fallback path with ≥2 claims needing fallback. The new code in grounder.py:272-307 is effectively untested. Add one
  with two paraphrased claims.

  Minor

  - consistency.py:270 default reasoning "Batch check failed to return a result." will surface in user-facing UI as the explanation for a verdict — worth at least logging a
  warning so it's not invisible.
  - The batch system prompts (CONSISTENCY_BATCH_SYSTEM_PROMPT, GROUNDER_BATCH_SYSTEM_PROMPT) are noticeably shorter than their single-claim counterparts — they dropped the
  strictness/calibration guidance ("when unsure, prefer the more skeptical label", "do not use outside knowledge"). For a TrustLayer, that's likely a quality regression on
  the batched path. Mirror the full guidance.

  Verdict

  Solid implementation of the headline change, but it leaves two easy wins on the table (parallel chunk dispatch, full strictness in batch prompt) and one real coverage gap
  (no batch test for grounder). Fix those three and this is ready.