import type { PipelineStage } from "../hooks/useVerify";

interface Props {
  stage: PipelineStage;
  mode?: "full" | "quick";
}

const STEPS: {
  id: Exclude<PipelineStage, "idle" | "done" | "error">;
  label: string;
  sub: string;
}[] = [
  { id: "extracting", label: "Extract", sub: "Decompose into atomic claims" },
  { id: "grounding", label: "Ground", sub: "Match each claim to source" },
  { id: "checking", label: "Check", sub: "Evaluate logical consistency" },
  { id: "aggregating", label: "Aggregate", sub: "Score and categorize" },
];

const ORDER: PipelineStage[] = [
  "idle",
  "extracting",
  "grounding",
  "checking",
  "aggregating",
  "done",
];

export default function PipelineSteps({ stage, mode = "full" }: Props) {
  const steps = mode === "quick" ? STEPS.filter((s) => s.id !== "checking") : STEPS;
  const currentIdx = ORDER.indexOf(stage);

  return (
    <ol className="flex items-stretch gap-2 sm:gap-3">
      {steps.map((s, i) => {
        const stepIdx = ORDER.indexOf(s.id);
        const isActive = stage === s.id;
        const isDone = stage === "done" || stepIdx < currentIdx;
        const isError = stage === "error" && i === 0;
        return (
          <li
            key={s.id}
            className={`flex-1 min-w-0 rounded-lg border px-3 py-2.5 transition-colors ${
              isError
                ? "border-red-500/50 bg-red-500/5"
                : isActive
                  ? "border-emerald-500/60 bg-emerald-500/5"
                  : isDone
                    ? "border-emerald-500/30 bg-emerald-500/[0.03]"
                    : "border-line bg-surface/50"
            }`}
          >
            <div className="flex items-center gap-2">
              <StepIcon done={isDone} active={isActive} idx={i + 1} />
              <span
                className={`text-xs font-semibold uppercase tracking-wider ${
                  isActive
                    ? "text-emerald-300"
                    : isDone
                      ? "text-emerald-400/80"
                      : "text-slate-400"
                }`}
              >
                {s.label}
              </span>
            </div>
            <div className="text-[11px] text-slate-500 mt-1 truncate">{s.sub}</div>
          </li>
        );
      })}
    </ol>
  );
}

function StepIcon({
  done,
  active,
  idx,
}: {
  done: boolean;
  active: boolean;
  idx: number;
}) {
  if (done && !active) {
    return (
      <span className="h-5 w-5 rounded-full bg-emerald-500/20 text-emerald-300 grid place-items-center text-[11px]">
        ✓
      </span>
    );
  }
  if (active) {
    return (
      <span className="relative h-5 w-5 rounded-full grid place-items-center">
        <span className="absolute inset-0 rounded-full border-2 border-emerald-400 border-t-transparent animate-spin" />
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
      </span>
    );
  }
  return (
    <span className="h-5 w-5 rounded-full border border-line text-slate-500 grid place-items-center text-[10px] font-mono">
      {idx}
    </span>
  );
}
