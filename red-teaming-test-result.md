PS C:\Users\genya\Downloads\Conti> python bench/adversary_test.py --source demo-app/backend/sample_contracts/risky_saas.txt
--- Generating Adversarial Summary (Model: gemini-3-flash-preview) ---

[Adversary Summary]
Effective February 1, 2026, this Agreement grants the Customer access to Nimbus Cloud Services' platform for a 12-month term, with automatic renewals unless a 90-day non-renewal notice is submitted. While the Provider may adjust fees with 10 days' notice, the contract provides a 30-day grace period for late payments before the 1.75% monthly interest charge is applied. The Provider retains an irrevocable, perpetual license to use Customer Data for AI training and commercial sublicensing. Provider liability is capped at $100, and the Customer must indemnify the Provider for all usage-related claims. Although the Agreement explicitly states in Section 2 that all fees are non-refundable under any circumstances, it later provides that a pro-rata refund will be issued if the Provider terminates the Agreement for convenience. All legal disputes are governed by California law and must be resolved through AAA arbitration in San Francisco.

[Adversary Injections Manifest]
1. TYPE: hallucination
   CLAIM: The contract provides a 30-day grace period for late payments before the 1.75% monthly interest charge is applied.
   ORIG:  None
   WHY:   The source states invoices are due net 15 and late payments accrue interest; it does not mention a grace period, making this an additive hallucination of a common business term that seems plausible in a professional context.
2. TYPE: hallucination
   CLAIM: Disputes arising under the Agreement must be resolved through AAA arbitration in San Francisco.
   ORIG:  Any dispute... shall be resolved... under the rules of JAMS.
   WHY:   This replaces one specific arbitration body (JAMS) with another (AAA). Since both are common in SaaS agreements, the swap is a high-fidelity hallucination that is difficult for automated systems to flag without verbatim entity verification.
3. TYPE: contradiction
   CLAIM: A pro-rata refund will be issued if the Provider terminates the Agreement for convenience.
   ORIG:  All fees are non-refundable under any circumstances (Section 2).
   WHY:   This claim contradicts the summary's own previous sentence referencing the 'non-refundable' clause, as well as the source's explicit absolute prohibition on refunds, testing the system's internal and external consistency checking.

--- Running TrustLayer Verification ---
Traceback (most recent call last):
  File "C:\Users\genya\Downloads\Conti\bench\adversary_test.py", line 75, in <module>
    asyncio.run(run_adversary_test(args.source, args.provider, args.model))
    ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\genya\AppData\Local\Python\pythoncore-3.14-64\Lib\asyncio\runners.py", line 204, in run
    return runner.run(main)
           ~~~~~~~~~~^^^^^^
  File "C:\Users\genya\AppData\Local\Python\pythoncore-3.14-64\Lib\asyncio\runners.py", line 127, in run
    return self._loop.run_until_complete(task)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "C:\Users\genya\AppData\Local\Python\pythoncore-3.14-64\Lib\asyncio\base_events.py", line 719, in run_until_complete
    return future.result()
           ~~~~~~~~~~~~~^^
  File "C:\Users\genya\Downloads\Conti\bench\adversary_test.py", line 41, in run_adversary_test
    report = await pipeline.run(request)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\genya\Downloads\Conti\engine\app\pipeline\orchestrator.py", line 174, in run
    groundings, consistencies = await asyncio.gather(
                                ^^^^^^^^^^^^^^^^^^^^^
        grounding_task, consistency_task
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\genya\Downloads\Conti\engine\app\pipeline\grounder.py", line 296, in ground_many
    batch_responses = await asyncio.gather(*(run_batch(b) for b in batches))
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\genya\Downloads\Conti\engine\app\pipeline\grounder.py", line 283, in run_batch
    raw = await self._client.create_message(
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<7 lines>...
    )
    ^
  File "C:\Users\genya\Downloads\Conti\engine\app\services\gemini_client.py", line 143, in create_message
    self._reject_truncated(resp, max_tokens)
    ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^
  File "C:\Users\genya\Downloads\Conti\engine\app\services\gemini_client.py", line 230, in _reject_truncated
    raise RuntimeError(
    ...<2 lines>...
    )
RuntimeError: Gemini response was truncated at max_tokens=4096. Increase GEMINI_MAX_TOKENS or shorten the input.