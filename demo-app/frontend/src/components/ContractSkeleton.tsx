import type { AnalyzeStage } from "../hooks/useContract";

interface Props {
  stage: AnalyzeStage;
}

export default function ContractSkeleton({ stage }: Props) {
  const label = STAGE_LABELS[stage] ?? "Working…";
  const sub = STAGE_SUBLABELS[stage] ?? "";

  return (
    <div className="max-w-7xl mx-auto px-6 py-8 space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-slate-100 flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            {label}
          </div>
          <div className="text-xs text-slate-500 mt-1">{sub}</div>
        </div>
        <StageRail stage={stage} />
      </div>

      <div className="card p-6 flex flex-col lg:flex-row items-center gap-6">
        <div className="shimmer h-[180px] w-[180px] rounded-full shrink-0" />
        <div className="flex-1 w-full space-y-3">
          <div className="shimmer h-6 w-1/3 rounded" />
          <div className="shimmer h-3 w-5/6 rounded" />
          <div className="shimmer h-3 w-4/6 rounded" />
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 pt-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="shimmer h-16 rounded-lg" />
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-4">
        <div className="card p-3 space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="shimmer h-10 rounded-lg" />
          ))}
        </div>
        <div className="space-y-4">
          <div className="card p-4 space-y-3">
            <div className="shimmer h-4 w-32 rounded" />
            <div className="shimmer h-3 w-full rounded" />
            <div className="shimmer h-3 w-11/12 rounded" />
            <div className="shimmer h-3 w-4/5 rounded" />
          </div>
          {Array.from({ length: 2 }).map((_, i) => (
            <div key={i} className="card p-4 space-y-3">
              <div className="flex items-center gap-2">
                <div className="shimmer h-5 w-16 rounded-full" />
                <div className="shimmer h-5 w-24 rounded-full" />
              </div>
              <div className="shimmer h-4 w-2/3 rounded" />
              <div className="shimmer h-3 w-full rounded" />
              <div className="shimmer h-3 w-5/6 rounded" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const STAGE_LABELS: Record<AnalyzeStage, string> = {
  idle: "Ready",
  uploading: "Uploading contract",
  parsing: "Parsing clauses",
  analyzing: "AI analyst reviewing clauses",
  verifying: "TrustLayer verifying findings",
  done: "Done",
  error: "Error",
};

const STAGE_SUBLABELS: Record<AnalyzeStage, string> = {
  idle: "",
  uploading: "Sending file to the demo backend.",
  parsing: "Splitting the contract into numbered clauses.",
  analyzing: "Claude is surfacing risks clause by clause.",
  verifying:
    "Each AI finding is being grounded in the original clause text. Hallucinations will be removed.",
  done: "",
  error: "Something went wrong.",
};

function StageRail({ stage }: { stage: AnalyzeStage }) {
  const order: AnalyzeStage[] = [
    "uploading",
    "parsing",
    "analyzing",
    "verifying",
    "done",
  ];
  const active = order.indexOf(stage);
  return (
    <div className="flex gap-1.5 w-48">
      {order.slice(0, -1).map((s, i) => {
        const done = active > i || stage === "done";
        const isActive = i === active;
        return (
          <div
            key={s}
            className={`flex-1 h-1.5 rounded-full transition-colors ${
              done
                ? "bg-emerald-400"
                : isActive
                  ? "bg-emerald-500/60 animate-pulse"
                  : "bg-line"
            }`}
          />
        );
      })}
    </div>
  );
}
