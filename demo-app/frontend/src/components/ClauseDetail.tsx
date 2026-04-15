import type { Clause, VerifiedFinding } from "../types/contract";
import { CATEGORY_LABELS } from "../lib/contract";
import { scoreColor } from "../lib/status";
import RiskBadge from "./RiskBadge";
import VerificationBadge from "./VerificationBadge";

interface Props {
  clause: Clause;
  findings: VerifiedFinding[];
  removed: VerifiedFinding[];
  showRemoved: boolean;
}

export default function ClauseDetail({
  clause,
  findings,
  removed,
  showRemoved,
}: Props) {
  const visible = [...findings];
  if (showRemoved) visible.push(...removed);

  return (
    <div className="space-y-5 animate-fade-in">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <span className="rounded-md bg-surface border border-line px-2 py-0.5 text-xs text-slate-400 font-mono">
            §{clause.section_id}
          </span>
          {clause.title && (
            <h3 className="text-base font-semibold text-slate-100">
              {clause.title}
            </h3>
          )}
        </div>
        <div className="card p-4 text-sm leading-relaxed text-slate-300 whitespace-pre-wrap">
          {clause.text}
        </div>
      </div>

      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h4 className="text-sm font-semibold text-slate-200">
            Findings ({findings.length})
          </h4>
          {removed.length > 0 && (
            <span className="text-[11px] uppercase tracking-wider text-red-400/70">
              +{removed.length} removed by TrustLayer
            </span>
          )}
        </div>

        {visible.length === 0 ? (
          <div className="card p-4 text-sm text-slate-400">
            No findings on this clause.
          </div>
        ) : (
          visible.map((vf) => <FindingCard key={vf.finding.id} vf={vf} />)
        )}
      </div>
    </div>
  );
}

function FindingCard({ vf }: { vf: VerifiedFinding }) {
  const { finding } = vf;
  const removed = vf.removed;

  return (
    <div
      className={`card p-4 space-y-3 ${
        removed ? "opacity-60 border-red-500/30" : ""
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <RiskBadge risk={finding.risk} compact />
            <span className="text-[10px] uppercase tracking-wider text-slate-500">
              {CATEGORY_LABELS[finding.category]}
            </span>
            {removed && (
              <span className="text-[10px] uppercase tracking-wider text-red-300">
                Suppressed
              </span>
            )}
          </div>
          <div className="text-sm font-semibold text-slate-100">
            {finding.title}
          </div>
        </div>
        <VerificationBadge status={vf.verification_status} compact />
      </div>

      {finding.clause_quote && (
        <blockquote className="border-l-2 border-emerald-500/60 pl-3 text-xs leading-relaxed text-slate-400 italic">
          "{finding.clause_quote}"
        </blockquote>
      )}

      <p className="text-sm text-slate-300 leading-relaxed">
        {finding.summary}
      </p>

      {finding.recommendation && (
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">
            Recommendation
          </div>
          <p className="text-sm text-slate-300 leading-relaxed">
            {finding.recommendation}
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-slate-400 pt-1 border-t border-line/60">
        <Stat label="Grounding" value={vf.grounding_score} />
        <Stat label="Integrity" value={vf.integrity_score} />
        {vf.reasoning && (
          <span className="text-slate-500 italic truncate" title={vf.reasoning}>
            {vf.reasoning.length > 120
              ? `${vf.reasoning.slice(0, 120)}…`
              : vf.reasoning}
          </span>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-wider text-slate-500">
        {label}
      </span>
      <span className="font-mono font-semibold" style={{ color: scoreColor(value) }}>
        {value}
      </span>
    </span>
  );
}
