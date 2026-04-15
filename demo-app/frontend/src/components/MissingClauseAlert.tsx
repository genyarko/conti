import type { Finding } from "../types/contract";
import { CATEGORY_LABELS } from "../lib/contract";
import RiskBadge from "./RiskBadge";

interface Props {
  missing: Finding[];
}

export default function MissingClauseAlert({ missing }: Props) {
  if (missing.length === 0) return null;

  return (
    <div className="card p-4 space-y-3 border-orange-500/30">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
          <span className="text-orange-400">⚠</span> Missing standard clauses
          <span className="text-xs text-slate-500">({missing.length})</span>
        </h3>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {missing.map((f) => (
          <div
            key={f.id}
            className="rounded-lg border border-orange-500/30 bg-orange-500/5 p-3 space-y-2"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-semibold text-slate-100">
                {f.title}
              </div>
              <RiskBadge risk={f.risk} compact />
            </div>
            <div className="text-[10px] uppercase tracking-wider text-slate-500">
              {CATEGORY_LABELS[f.category]}
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              {f.summary}
            </p>
            {f.recommendation && (
              <p className="text-xs text-slate-400 leading-relaxed">
                <span className="text-slate-500">Suggestion: </span>
                {f.recommendation}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
