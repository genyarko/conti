import type { Claim, ClaimStatus, ClaimVerdict } from "../types/trustlayer";
import { STATUS_META } from "../lib/status";
import ClaimCard from "./ClaimCard";

interface Props {
  status: ClaimStatus;
  verdicts: ClaimVerdict[];
  claimsById: Map<string, Claim>;
}

export default function ClaimBucket({ status, verdicts, claimsById }: Props) {
  if (verdicts.length === 0) return null;
  const meta = STATUS_META[status];
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${meta.dot}`} />
        <h3 className={`text-xs font-semibold uppercase tracking-wider ${meta.color}`}>
          {meta.label}
        </h3>
        <span className="text-xs text-slate-500 font-mono">
          {verdicts.length}
        </span>
        <span className="text-xs text-slate-500 hidden sm:inline">
          — {meta.description}
        </span>
      </div>
      <div className="space-y-2.5">
        {verdicts.map((v) => {
          const claim = claimsById.get(v.claim_id);
          if (!claim) return null;
          return <ClaimCard key={v.claim_id} claim={claim} verdict={v} />;
        })}
      </div>
    </section>
  );
}
