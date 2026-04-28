import asyncio
import argparse
import sys
import os
from pathlib import Path

# Add the project root to sys.path so we can import engine modules
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from engine.app.services.adversary import AdversaryAgent
from engine.app.pipeline.orchestrator import VerifyPipeline
from engine.app.models.schemas import VerifyRequest

async def run_adversary_test(source_path: str, provider: str = None, model: str = None):
    source_context = Path(source_path).read_text(encoding="utf-8")
    
    agent = AdversaryAgent(provider=provider, model=model)
    print(f"--- Generating Adversarial Summary (Model: {agent._model}) ---")
    adversarial_output = await agent.generate_adversarial_summary(source_context)
    
    print("\n[Adversary Summary]")
    print(adversarial_output.summary)
    
    print("\n[Adversary Injections Manifest]")
    for i, inj in enumerate(adversarial_output.injections):
        print(f"{i+1}. TYPE: {inj.type}")
        print(f"   CLAIM: {inj.injected_claim}")
        print(f"   ORIG:  {inj.original_fact}")
        print(f"   WHY:   {inj.reasoning}")

    print("\n--- Running TrustLayer Verification ---")
    pipeline = VerifyPipeline()
    request = VerifyRequest(
        source_context=source_context,
        llm_output=adversarial_output.summary,
        provider=provider,
        model=model
    )
    
    report = await pipeline.run(request)
    
    print("\n[Verification Report]")
    print(f"Overall Score: {report.overall_score}/100")
    print(f"Hallucinations detected: {len(report.hallucinations)}")
    print(f"Flagged claims: {len(report.flagged)}")
    print(f"Uncertain claims: {len(report.uncertain)}")
    print(f"Verified claims: {len(report.verified)}")
    
    if report.hallucinations:
        print("\n[Detected Hallucinations]")
        for h in report.hallucinations:
            print(f"- {h.reasoning}")

    if report.flagged:
        print("\n[Flagged Claims]")
        for f in report.flagged:
            print(f"- {f.reasoning}")
            
    # Simple evaluation
    injected_claims = [inj.injected_claim.lower() for inj in adversarial_output.injections]
    caught_count = 0
    for h in report.hallucinations + report.flagged:
        # Check if the text of any caught claim matches an injected claim (fuzzy-ish)
        # In practice, the extractor might split them differently, so we look for substrings
        for inj_text in injected_claims:
            if inj_text in h.reasoning.lower() or any(inj_text in c.text.lower() for c in report.claims if c.id == h.claim_id):
                caught_count += 1
                break
                
    print(f"\nSummary: Caught {caught_count} out of {len(adversarial_output.injections)} injected errors.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="demo-app/backend/sample_contracts/risky_saas.txt")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    
    asyncio.run(run_adversary_test(args.source, args.provider, args.model))
