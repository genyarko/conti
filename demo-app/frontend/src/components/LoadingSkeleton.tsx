import type { PipelineStage } from "../hooks/useVerify";

interface Props {
  stage?: PipelineStage;
}

const STAGE_HINT: Record<PipelineStage, string> = {
  idle: "",
  extracting: "Decomposing the LLM output into atomic, verifiable claims…",
  grounding: "Matching each claim to passages in your source context…",
  checking: "Evaluating logical consistency with a skeptical reviewer…",
  aggregating: "Scoring claims and assembling the integrity report…",
  done: "",
  error: "",
};

export default function LoadingSkeleton({ stage = "extracting" }: Props) {
  const hint = STAGE_HINT[stage];

  return (
    <div className="space-y-4 animate-fade-in">
      {hint && (
        <div className="text-xs text-slate-400 flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
          {hint}
        </div>
      )}
      <div className="card p-6 flex flex-col sm:flex-row items-center gap-6">
        <div className="shimmer h-[180px] w-[180px] rounded-full shrink-0" />
        <div className="flex-1 w-full grid grid-cols-2 sm:grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="shimmer h-20 rounded-lg" />
          ))}
        </div>
      </div>
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="card p-4 space-y-3">
          <div className="flex items-center gap-2">
            <div className="shimmer h-5 w-20 rounded-full" />
            <div className="shimmer h-3 w-16 rounded" />
          </div>
          <div className="shimmer h-4 w-5/6 rounded" />
          <div className="shimmer h-3 w-2/3 rounded" />
        </div>
      ))}
    </div>
  );
}
