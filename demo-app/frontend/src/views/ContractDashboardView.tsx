import { useMemo, useState } from "react";
import type { AnalyzeResponse, Clause } from "../types/contract";
import { RISK_META } from "../lib/contract";
import BeforeAfterView from "../components/BeforeAfterView";
import ClauseDetail from "../components/ClauseDetail";
import ContractSummary from "../components/ContractSummary";
import MissingClauseAlert from "../components/MissingClauseAlert";
import RiskBadge from "../components/RiskBadge";

interface Props {
  result: AnalyzeResponse;
  onReset: () => void;
}

export default function ContractDashboardView({ result, onReset }: Props) {
  const [selectedId, setSelectedId] = useState<string>(
    result.clauses[0]?.section_id ?? "",
  );
  const [showRemoved, setShowRemoved] = useState(false);
  const [showBeforeAfter, setShowBeforeAfter] = useState(
    result.removed_findings.length > 0,
  );

  const findingsByClause = useMemo(() => {
    const map = new Map<string, typeof result.findings>();
    for (const f of result.findings) {
      const arr = map.get(f.finding.section_id) ?? [];
      arr.push(f);
      map.set(f.finding.section_id, arr);
    }
    return map;
  }, [result.findings]);

  const removedByClause = useMemo(() => {
    const map = new Map<string, typeof result.removed_findings>();
    for (const f of result.removed_findings) {
      const arr = map.get(f.finding.section_id) ?? [];
      arr.push(f);
      map.set(f.finding.section_id, arr);
    }
    return map;
  }, [result.removed_findings]);

  const selectedClause = result.clauses.find(
    (c) => c.section_id === selectedId,
  );

  return (
    <div className="max-w-7xl mx-auto px-6 py-8 space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-slate-400">
          Reviewed{" "}
          <span className="text-slate-200 font-semibold">{result.filename}</span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <button
            type="button"
            className={`text-xs px-3 py-1.5 rounded-md border transition-colors ${
              showBeforeAfter
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                : "border-line text-slate-300 hover:border-slate-500"
            }`}
            onClick={() => setShowBeforeAfter((v) => !v)}
          >
            {showBeforeAfter ? "Hide" : "Show"} before/after
          </button>
          <label className="inline-flex items-center gap-2 text-xs text-slate-400 cursor-pointer select-none">
            <input
              type="checkbox"
              className="accent-emerald-400"
              checked={showRemoved}
              onChange={(e) => setShowRemoved(e.target.checked)}
            />
            Show removed findings
            {result.removed_findings.length > 0 && (
              <span className="rounded-full bg-red-500/15 text-red-300 text-[10px] px-1.5 py-0.5 font-mono">
                {result.removed_findings.length}
              </span>
            )}
          </label>
          <button type="button" className="btn-ghost" onClick={onReset}>
            New contract
          </button>
        </div>
      </div>

      <ContractSummary result={result} />

      {showBeforeAfter && <BeforeAfterView result={result} />}

      <MissingClauseAlert missing={result.missing_clauses} />

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-4">
        <aside className="card p-3 lg:sticky lg:top-20 lg:self-start lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto">
          <div className="text-xs uppercase tracking-wider text-slate-500 px-1 py-2">
            Clauses
          </div>
          <ul className="space-y-1">
            {result.clauses.map((c) => (
              <ClauseListItem
                key={c.section_id}
                clause={c}
                findings={findingsByClause.get(c.section_id) ?? []}
                removedCount={
                  (removedByClause.get(c.section_id) ?? []).length
                }
                active={c.section_id === selectedId}
                onClick={() => setSelectedId(c.section_id)}
              />
            ))}
          </ul>
        </aside>

        <main>
          {selectedClause ? (
            <ClauseDetail
              clause={selectedClause}
              findings={findingsByClause.get(selectedClause.section_id) ?? []}
              removed={removedByClause.get(selectedClause.section_id) ?? []}
              showRemoved={showRemoved}
            />
          ) : (
            <div className="card p-6 text-sm text-slate-400">
              Select a clause to see findings.
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function ClauseListItem({
  clause,
  findings,
  removedCount,
  active,
  onClick,
}: {
  clause: Clause;
  findings: { finding: { risk: keyof typeof RISK_META } }[];
  removedCount: number;
  active: boolean;
  onClick: () => void;
}) {
  const highestRisk = findings.reduce<keyof typeof RISK_META | null>((acc, f) => {
    if (!acc) return f.finding.risk;
    return RISK_META[f.finding.risk].order < RISK_META[acc].order
      ? f.finding.risk
      : acc;
  }, null);

  const preview = clause.title || clause.text.slice(0, 60);

  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={`w-full text-left rounded-lg px-3 py-2 transition-colors border ${
          active
            ? "bg-emerald-500/10 border-emerald-500/40"
            : "border-transparent hover:bg-surface"
        }`}
      >
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-slate-500 shrink-0">
            §{clause.section_id}
          </span>
          <span className="text-sm text-slate-200 truncate flex-1">
            {preview}
          </span>
          {highestRisk && <RiskBadge risk={highestRisk} compact />}
        </div>
        <div className="flex items-center gap-2 mt-1 text-[10px] text-slate-500">
          <span>
            {findings.length} finding{findings.length === 1 ? "" : "s"}
          </span>
          {removedCount > 0 && (
            <span className="text-red-400/80">· {removedCount} removed</span>
          )}
        </div>
      </button>
    </li>
  );
}
