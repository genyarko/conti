import { useMemo } from "react";
import type { AnalyzeResponse, VerifiedFinding } from "../types/contract";
import { CATEGORY_LABELS, RISK_META } from "../lib/contract";
import RiskBadge from "./RiskBadge";
import VerificationBadge from "./VerificationBadge";

interface Props {
  result: AnalyzeResponse;
}

export default function BeforeAfterView({ result }: Props) {
  const before = useMemo(
    () => [...result.findings, ...result.removed_findings],
    [result.findings, result.removed_findings],
  );
  const after = result.findings;

  const beforeCounts = countByRisk(before);
  const afterCounts = countByRisk(after);

  const removedCount = result.removed_findings.length;

  return (
    <div className="card p-5 space-y-5 animate-fade-in">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm font-semibold text-slate-100">
            Before / After TrustLayer
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            What the raw AI analyst produced vs. what survived verification.
          </div>
        </div>
        {removedCount > 0 ? (
          <span className="text-xs text-red-300 bg-red-500/10 border border-red-500/30 rounded-full px-3 py-1">
            {removedCount} hallucinated finding
            {removedCount === 1 ? "" : "s"} removed
          </span>
        ) : (
          <span className="text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-3 py-1">
            No hallucinations detected
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Column
          label="Raw AI analyst"
          sublabel="Unverified output — may contain hallucinations"
          accent="slate"
          counts={beforeCounts}
          total={before.length}
          findings={before}
          showRemovedStyling
        />
        <Column
          label="After TrustLayer"
          sublabel="Verified findings grounded in the clause text"
          accent="emerald"
          counts={afterCounts}
          total={after.length}
          findings={after}
        />
      </div>
    </div>
  );
}

function countByRisk(findings: VerifiedFinding[]) {
  return findings.reduce(
    (acc, f) => {
      acc[f.finding.risk] = (acc[f.finding.risk] ?? 0) + 1;
      return acc;
    },
    { critical: 0, warning: 0, info: 0, ok: 0 } as Record<
      "critical" | "warning" | "info" | "ok",
      number
    >,
  );
}

function Column({
  label,
  sublabel,
  accent,
  counts,
  total,
  findings,
  showRemovedStyling = false,
}: {
  label: string;
  sublabel: string;
  accent: "slate" | "emerald";
  counts: Record<"critical" | "warning" | "info" | "ok", number>;
  total: number;
  findings: VerifiedFinding[];
  showRemovedStyling?: boolean;
}) {
  const accentClass =
    accent === "emerald"
      ? "border-emerald-500/40 bg-emerald-500/[0.03]"
      : "border-line bg-surface/60";
  const labelClass =
    accent === "emerald" ? "text-emerald-300" : "text-slate-300";

  return (
    <div className={`rounded-xl border ${accentClass} p-4 space-y-3`}>
      <div>
        <div className={`text-xs uppercase tracking-wider ${labelClass}`}>
          {label}
        </div>
        <div className="text-[11px] text-slate-500 mt-0.5">{sublabel}</div>
      </div>

      <div className="flex items-baseline gap-3">
        <div className="text-3xl font-bold tabular-nums text-slate-100">
          {total}
        </div>
        <div className="text-xs text-slate-500">findings</div>
      </div>

      <div className="flex flex-wrap gap-2 text-[11px]">
        {(["critical", "warning", "info", "ok"] as const).map((r) => (
          <span
            key={r}
            className={`inline-flex items-center gap-1.5 rounded-full border ${RISK_META[r].border} ${RISK_META[r].bg} ${RISK_META[r].color} px-2 py-0.5`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${RISK_META[r].dot}`} />
            {RISK_META[r].label} · {counts[r]}
          </span>
        ))}
      </div>

      <ul className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
        {findings.length === 0 && (
          <li className="text-xs text-slate-500 italic">No findings.</li>
        )}
        {findings.map((vf) => (
          <li
            key={vf.finding.id}
            className={`text-xs leading-snug rounded-md border px-2.5 py-1.5 flex items-start gap-2 ${
              showRemovedStyling && vf.removed
                ? "border-red-500/40 bg-red-500/5 line-through opacity-70"
                : "border-line bg-surface/40"
            }`}
            title={vf.finding.summary}
          >
            <RiskBadge risk={vf.finding.risk} compact />
            <div className="min-w-0 flex-1">
              <div className="text-slate-200 font-medium truncate">
                {vf.finding.title}
              </div>
              <div className="text-[10px] text-slate-500 mt-0.5">
                §{vf.finding.section_id} · {CATEGORY_LABELS[vf.finding.category]}
              </div>
            </div>
            {showRemovedStyling && vf.removed && (
              <VerificationBadge status="hallucination" compact />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
