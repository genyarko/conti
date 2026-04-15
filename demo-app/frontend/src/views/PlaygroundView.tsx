import { useMemo, useState } from "react";
import { useVerify } from "../hooks/useVerify";
import { SAMPLES } from "../lib/samples";
import type { Claim, VerifyMode } from "../types/trustlayer";
import ClaimBucket from "../components/ClaimBucket";
import HallucinationLog from "../components/HallucinationLog";
import LoadingSkeleton from "../components/LoadingSkeleton";
import PipelineSteps from "../components/PipelineSteps";
import ReportSummary from "../components/ReportSummary";

export default function PlaygroundView() {
  const [source, setSource] = useState(SAMPLES[0].source);
  const [output, setOutput] = useState(SAMPLES[0].output);
  const [mode, setMode] = useState<VerifyMode>("full");
  const { report, error, isLoading, stage, elapsedMs, run, reset } = useVerify();

  const canSubmit = source.trim().length > 0 && output.trim().length > 0 && !isLoading;

  const claimsById = useMemo(() => {
    const map = new Map<string, Claim>();
    report?.claims.forEach((c) => map.set(c.id, c));
    return map;
  }, [report]);

  const onVerify = () => {
    run({ source_context: source.trim(), llm_output: output.trim() }, mode);
  };

  const loadSample = (idx: number) => {
    const s = SAMPLES[idx];
    setSource(s.source);
    setOutput(s.output);
    reset();
  };

  return (
    <div className="max-w-6xl mx-auto px-6 py-8 space-y-8">
      <section>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">
          Verify any LLM output against its source.
        </h1>
        <p className="mt-2 text-slate-400 max-w-3xl">
          Paste the ground-truth source on the left and the LLM-generated text on
          the right. TrustLayer extracts atomic claims, checks each against the
          source, and flags hallucinations before they reach users.
        </p>
      </section>

      <section className="flex flex-wrap items-center gap-2">
        <span className="text-xs uppercase tracking-wider text-slate-500 mr-1">
          Try a sample
        </span>
        {SAMPLES.map((s, i) => (
          <button
            key={s.name}
            type="button"
            className="btn-ghost"
            onClick={() => loadSample(i)}
            title={s.description}
          >
            {s.name}
          </button>
        ))}
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TextPanel
          title="Source context"
          subtitle="The trusted ground truth — docs, database rows, retrieved passages."
          value={source}
          onChange={setSource}
          placeholder="Paste the source context the LLM should have grounded its output in…"
          accent="emerald"
        />
        <TextPanel
          title="LLM output"
          subtitle="The generated text you want to verify."
          value={output}
          onChange={setOutput}
          placeholder="Paste the LLM-generated response to verify…"
          accent="blue"
        />
      </section>

      <section className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="btn-primary"
          onClick={onVerify}
          disabled={!canSubmit}
        >
          {isLoading ? (
            <>
              <Spinner /> Verifying…
            </>
          ) : (
            <>Verify output</>
          )}
        </button>
        <ModeToggle mode={mode} onChange={setMode} disabled={isLoading} />
        {(report || error) && !isLoading && (
          <button type="button" className="btn-ghost" onClick={reset}>
            Clear result
          </button>
        )}
        {isLoading && (
          <span className="text-xs text-slate-400 font-mono ml-1">
            {(elapsedMs / 1000).toFixed(1)}s elapsed
          </span>
        )}
      </section>

      {(isLoading || report || error) && (
        <section className="card p-4">
          <PipelineSteps
            stage={error ? "error" : stage}
            mode={mode}
          />
          {error && (
            <div className="mt-3 text-sm text-red-300 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
              {error}
            </div>
          )}
        </section>
      )}

      {isLoading && !report && <LoadingSkeleton stage={stage} />}

      {report && !isLoading && (
        <div className="space-y-6">
          <ReportSummary report={report} />
          <HallucinationLog
            hallucinations={report.hallucinations}
            claimsById={claimsById}
          />
          <div className="space-y-6">
            <ClaimBucket
              status="verified"
              verdicts={report.verified}
              claimsById={claimsById}
            />
            <ClaimBucket
              status="uncertain"
              verdicts={report.uncertain}
              claimsById={claimsById}
            />
            <ClaimBucket
              status="flagged"
              verdicts={report.flagged}
              claimsById={claimsById}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function TextPanel({
  title,
  subtitle,
  value,
  onChange,
  placeholder,
  accent,
}: {
  title: string;
  subtitle: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  accent: "emerald" | "blue";
}) {
  const dot = accent === "emerald" ? "bg-emerald-400" : "bg-blue-400";
  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${dot}`} />
            <h2 className="text-sm font-semibold">{title}</h2>
          </div>
          <p className="text-xs text-slate-500 mt-1">{subtitle}</p>
        </div>
        <span className="text-[11px] text-slate-500 font-mono shrink-0">
          {value.length.toLocaleString()} chars
        </span>
      </div>
      <textarea
        className="textarea"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
      />
    </div>
  );
}

function ModeToggle({
  mode,
  onChange,
  disabled,
}: {
  mode: VerifyMode;
  onChange: (m: VerifyMode) => void;
  disabled: boolean;
}) {
  return (
    <div className="inline-flex rounded-lg border border-line bg-surface overflow-hidden text-xs font-medium">
      {(["full", "quick"] as VerifyMode[]).map((m) => (
        <button
          key={m}
          type="button"
          disabled={disabled}
          onClick={() => onChange(m)}
          className={`px-3 py-2 transition-colors ${
            mode === m
              ? "bg-emerald-500/15 text-emerald-300"
              : "text-slate-400 hover:text-slate-200"
          } disabled:opacity-50`}
        >
          {m === "full" ? "Full pipeline" : "Quick (grounding-only)"}
        </button>
      ))}
    </div>
  );
}

function Spinner() {
  return (
    <span className="inline-block h-4 w-4 rounded-full border-2 border-slate-900/30 border-t-slate-900 animate-spin" />
  );
}
