import type { AnalyzeResponse } from "../types/contract";
import { RISK_META } from "../lib/contract";
import { scoreColor, scoreLabel } from "../lib/status";
import IntegrityScoreRing from "./IntegrityScoreRing";
import RiskBadge from "./RiskBadge";

interface Props {
  result: AnalyzeResponse;
}

export default function ContractSummary({ result }: Props) {
  const { summary, findings, removed_findings, clauses } = result;
  const counts = findings.reduce(
    (acc, f) => {
      acc[f.finding.risk] = (acc[f.finding.risk] ?? 0) + 1;
      return acc;
    },
    { critical: 0, warning: 0, info: 0, ok: 0 } as Record<
      "critical" | "warning" | "info" | "ok",
      number
    >,
  );

  return (
    <div className="card p-6 space-y-6 animate-fade-in">
      <div className="flex flex-col lg:flex-row items-start gap-6">
        <IntegrityScoreRing
          score={summary.integrity_score}
          sublabel={scoreLabel(summary.integrity_score)}
        />
        <div className="flex-1 min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-semibold text-slate-100">
              {summary.contract_type}
            </h2>
            <RiskBadge risk={summary.overall_risk} />
            <span className="text-xs text-slate-500 font-mono truncate">
              {result.filename}
            </span>
          </div>
          {summary.plain_language_summary && (
            <p className="text-sm text-slate-300 leading-relaxed">
              {summary.plain_language_summary}
            </p>
          )}
          {summary.key_parties.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="uppercase tracking-wider text-slate-500">
                Parties
              </span>
              {summary.key_parties.map((p) => (
                <span
                  key={p}
                  className="rounded-md border border-line bg-surface px-2 py-0.5 text-slate-300"
                >
                  {p}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <StatTile
          label="Clauses"
          value={clauses.length}
          color="text-slate-100"
          barBg="bg-slate-700/40"
          barFg="bg-slate-400"
        />
        {(["critical", "warning", "info", "ok"] as const).map((r) => (
          <StatTile
            key={r}
            label={RISK_META[r].label}
            value={counts[r]}
            color={RISK_META[r].color}
            barBg={RISK_META[r].barBg}
            barFg={RISK_META[r].barFg}
          />
        ))}
      </div>

      {removed_findings.length > 0 && (
        <div
          className="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-200 flex items-start gap-3"
          style={{ borderColor: scoreColor(30) }}
        >
          <span className="mt-0.5 text-red-400">⚠</span>
          <div>
            <span className="font-semibold">
              {removed_findings.length} hallucinated
              {removed_findings.length === 1 ? " finding" : " findings"}
            </span>{" "}
            removed by TrustLayer — the AI analyst claimed issues not grounded
            in the actual clause text.
          </div>
        </div>
      )}
    </div>
  );
}

function StatTile({
  label,
  value,
  color,
  barBg,
  barFg,
}: {
  label: string;
  value: number;
  color: string;
  barBg: string;
  barFg: string;
}) {
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
      <div className={`mt-2 h-1 rounded-full overflow-hidden ${barBg}`}>
        <div
          className={`h-full ${barFg} transition-all duration-700`}
          style={{ width: value > 0 ? "100%" : "0%" }}
        />
      </div>
    </div>
  );
}
