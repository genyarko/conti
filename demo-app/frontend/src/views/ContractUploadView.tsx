import { useCallback, useEffect, useRef, useState } from "react";
import { useContract } from "../hooks/useContract";
import { listSamples } from "../services/contract";
import type { SampleEntry } from "../types/contract";
import { formatSampleName } from "../lib/contract";
import ContractDashboardView from "./ContractDashboardView";
import ContractSkeleton from "../components/ContractSkeleton";

export default function ContractUploadView() {
  const {
    upload,
    result,
    error,
    isBusy,
    stage,
    elapsedMs,
    uploadFromFile,
    uploadFromText,
    loadFromSample,
    analyzeNow,
    reset,
  } = useContract();

  const [samples, setSamples] = useState<SampleEntry[]>([]);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [pasted, setPasted] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    listSamples()
      .then((s) => {
        if (!cancelled) setSamples(s);
      })
      .catch((err) => {
        if (!cancelled)
          setSampleError(
            err instanceof Error ? err.message : "Could not fetch samples.",
          );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Automatically analyze once upload succeeds.
  useEffect(() => {
    if (upload && !result && !isBusy && stage !== "error") {
      analyzeNow();
    }
  }, [upload, result, isBusy, stage, analyzeNow]);

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files?.[0];
      if (file) uploadFromFile(file);
    },
    [uploadFromFile],
  );

  if (result) {
    return (
      <ContractDashboardView
        result={result}
        onReset={() => {
          reset();
          setPasted("");
        }}
      />
    );
  }

  if (isBusy && (stage === "analyzing" || stage === "verifying")) {
    return <ContractSkeleton stage={stage} />;
  }

  return (
    <div className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      <section>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">
          Upload a contract to review.
        </h1>
        <p className="mt-2 text-slate-400 max-w-2xl">
          A contract-analyst LLM surfaces risks clause by clause. Every finding
          is then verified by TrustLayer — hallucinated issues are removed
          before they reach you.
        </p>
      </section>

      <section>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={`card p-10 text-center transition-colors ${
            dragOver
              ? "border-emerald-400 bg-emerald-500/5"
              : "border-dashed border-line"
          }`}
        >
          <div className="text-4xl mb-3">📄</div>
          <div className="text-lg font-semibold text-slate-100">
            Drop a contract here
          </div>
          <div className="text-sm text-slate-400 mt-1">
            PDF, DOCX, or plain text. Or{" "}
            <button
              type="button"
              className="text-emerald-300 hover:text-emerald-200 underline underline-offset-2"
              onClick={() => fileInputRef.current?.click()}
              disabled={isBusy}
            >
              browse for a file
            </button>
            .
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,.txt"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) uploadFromFile(file);
              e.target.value = "";
            }}
          />
        </div>
      </section>

      <section>
        <div className="text-xs uppercase tracking-wider text-slate-500 mb-3">
          Or try a sample contract
        </div>
        {sampleError && (
          <div className="text-sm text-red-300 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2 mb-3">
            {sampleError}
          </div>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {samples.length === 0 && !sampleError && (
            <div className="text-sm text-slate-500 col-span-full">
              Loading samples…
            </div>
          )}
          {samples.map((s) => (
            <button
              key={s.name}
              type="button"
              className="card p-4 text-left hover:border-emerald-500/40 hover:bg-emerald-500/5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={isBusy}
              onClick={() => loadFromSample(s.name)}
            >
              <div className="font-semibold text-slate-100">
                {formatSampleName(s.name)}
              </div>
              <div className="text-xs text-slate-500 font-mono mt-1">
                {s.filename} · {(s.size_bytes / 1024).toFixed(1)} KB
              </div>
              <div className="text-xs text-slate-400 mt-2">
                {sampleBlurb(s.name)}
              </div>
            </button>
          ))}
        </div>
      </section>

      <section className="card p-4 space-y-3">
        <div className="text-xs uppercase tracking-wider text-slate-500">
          Or paste contract text
        </div>
        <textarea
          className="textarea min-h-[160px]"
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          placeholder="Paste contract text here…"
          spellCheck={false}
        />
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            disabled={isBusy || pasted.trim().length < 50}
            onClick={() => uploadFromText(pasted.trim(), "pasted.txt")}
          >
            Review pasted text
          </button>
          <span className="text-xs text-slate-500">
            {pasted.length.toLocaleString()} chars
          </span>
        </div>
      </section>

      {(isBusy || error) && (
        <section className="card p-4 space-y-2">
          <StageIndicator stage={stage} elapsedMs={elapsedMs} />
          {error && (
            <div className="text-sm text-red-300 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
              {error}
              <button
                type="button"
                className="ml-3 underline underline-offset-2"
                onClick={reset}
              >
                reset
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function sampleBlurb(name: string): string {
  if (name.includes("bad")) return "One-sided NDA with missing standard clauses.";
  if (name.includes("risky")) return "SaaS agreement with auto-renewal trap.";
  if (name.includes("clean")) return "Well-drafted services agreement.";
  return "Sample contract.";
}

const STAGE_LABELS: Record<string, string> = {
  idle: "Ready",
  uploading: "Uploading",
  parsing: "Parsing clauses",
  analyzing: "Analyzing clauses with Claude",
  verifying: "Verifying findings with TrustLayer",
  done: "Done",
  error: "Error",
};

function StageIndicator({
  stage,
  elapsedMs,
}: {
  stage: string;
  elapsedMs: number;
}) {
  const order = ["uploading", "parsing", "analyzing", "verifying", "done"];
  const activeIdx = order.indexOf(stage);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-slate-100">
          {STAGE_LABELS[stage] ?? stage}
        </div>
        <div className="text-xs font-mono text-slate-400">
          {(elapsedMs / 1000).toFixed(1)}s
        </div>
      </div>
      <div className="flex gap-2">
        {order.slice(0, -1).map((s, i) => {
          const isActive = i === activeIdx;
          const isDone = activeIdx > i || stage === "done";
          return (
            <div
              key={s}
              className={`flex-1 h-1.5 rounded-full transition-colors ${
                isDone
                  ? "bg-emerald-400"
                  : isActive
                    ? "bg-emerald-500/60 animate-pulse"
                    : "bg-line"
              }`}
            />
          );
        })}
      </div>
    </div>
  );
}
