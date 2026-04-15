import type { IntegrityReport } from "../types/trustlayer";
import IntegrityScoreRing from "./IntegrityScoreRing";

interface Props {
  report: IntegrityReport;
}

export default function ReportSummary({ report }: Props) {
  const total =
    report.verified.length +
    report.uncertain.length +
    report.flagged.length +
    report.hallucinations.length;

  return (
    <div className="card p-6 flex flex-col sm:flex-row items-center gap-6 animate-fade-in">
      <IntegrityScoreRing score={report.overall_score} />
      <div className="flex-1 w-full grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat
          label="Verified"
          value={report.verified.length}
          total={total}
          color="text-emerald-300"
          bar="bg-emerald-400"
        />
        <Stat
          label="Uncertain"
          value={report.uncertain.length}
          total={total}
          color="text-yellow-300"
          bar="bg-yellow-400"
        />
        <Stat
          label="Flagged"
          value={report.flagged.length}
          total={total}
          color="text-orange-300"
          bar="bg-orange-400"
        />
        <Stat
          label="Hallucinations"
          value={report.hallucinations.length}
          total={total}
          color="text-red-300"
          bar="bg-red-400"
        />
        <MetaRow report={report} />
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  total,
  color,
  bar,
}: {
  label: string;
  value: number;
  total: number;
  color: string;
  bar: string;
}) {
  const pct = total === 0 ? 0 : (value / total) * 100;
  return (
    <div className="rounded-lg border border-line bg-surface/60 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] uppercase tracking-wider text-slate-400">
          {label}
        </span>
        <span className={`text-xl font-semibold tabular-nums ${color}`}>
          {value}
        </span>
      </div>
      <div className="mt-2 h-1 rounded-full bg-line overflow-hidden">
        <div
          className={`h-full ${bar} transition-[width] duration-700`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function MetaRow({ report }: { report: IntegrityReport }) {
  const m = report.metadata;
  const items: [string, string][] = [
    ["Claims", String(m.claim_count)],
    ["Model", m.model],
    ["Duration", `${(m.duration_ms / 1000).toFixed(1)}s`],
    ["Request", m.request_id],
  ];
  return (
    <div className="col-span-2 sm:col-span-4 mt-1 flex flex-wrap items-center gap-x-5 gap-y-1 text-[11px] text-slate-500 font-mono">
      {items.map(([k, v]) => (
        <span key={k}>
          <span className="text-slate-600 uppercase tracking-wider mr-1">{k}</span>
          {v}
        </span>
      ))}
    </div>
  );
}
