import { useState } from "react";
import type { Claim, ClaimVerdict } from "../types/trustlayer";

interface Props {
  hallucinations: ClaimVerdict[];
  claimsById: Map<string, Claim>;
}

export default function HallucinationLog({
  hallucinations,
  claimsById,
}: Props) {
  const [open, setOpen] = useState(true);

  if (hallucinations.length === 0) {
    return (
      <div className="card p-4 flex items-center gap-3 text-sm text-slate-300 animate-fade-in">
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-400 shrink-0" />
        <span>
          <span className="font-semibold text-emerald-300">
            No hallucinations caught.
          </span>{" "}
          Every claim has at least partial grounding in the source.
        </span>
      </div>
    );
  }

  return (
    <div className="card border border-red-500/40 ring-1 ring-red-500/10 animate-fade-in">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between p-4"
      >
        <div className="flex items-center gap-3">
          <span className="h-2.5 w-2.5 rounded-full bg-red-400" />
          <div>
            <div className="text-sm font-semibold text-red-200">
              {hallucinations.length} hallucination
              {hallucinations.length === 1 ? "" : "s"} removed
            </div>
            <div className="text-xs text-slate-400">
              Ungrounded and inconsistent claims TrustLayer filtered out of the
              output.
            </div>
          </div>
        </div>
        <span className="text-slate-500 text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="px-4 pb-4 space-y-3">
          {hallucinations.map((v) => {
            const claim = claimsById.get(v.claim_id);
            return (
              <li
                key={v.claim_id}
                className="border-l-2 border-red-500/60 pl-3 text-sm"
              >
                <div className="text-slate-100 leading-relaxed">
                  {claim?.text ?? "(claim text unavailable)"}
                </div>
                <div className="mt-1 text-xs text-slate-400">
                  Grounding{" "}
                  <span className="font-mono text-red-300">
                    {v.grounding_score}
                  </span>{" "}
                  · {v.consistency_verdict.replace("_", " ")}
                </div>
                {v.reasoning && (
                  <div className="mt-2 text-xs text-slate-300 leading-relaxed">
                    {v.reasoning}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
