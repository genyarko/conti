import { useCallback, useRef, useState } from "react";
import { TrustLayerError, verify } from "../services/trustlayer";
import type {
  IntegrityReport,
  VerifyMode,
  VerifyRequest,
} from "../types/trustlayer";

export type PipelineStage =
  | "idle"
  | "extracting"
  | "grounding"
  | "checking"
  | "aggregating"
  | "done"
  | "error";

export interface UseVerifyState {
  report: IntegrityReport | null;
  error: string | null;
  isLoading: boolean;
  stage: PipelineStage;
  elapsedMs: number;
  run: (payload: VerifyRequest, mode?: VerifyMode) => Promise<void>;
  reset: () => void;
}

// Rough timings for the stepper when the backend runs synchronously.
const STAGE_TIMELINE: { stage: PipelineStage; atMs: number }[] = [
  { stage: "extracting", atMs: 0 },
  { stage: "grounding", atMs: 1200 },
  { stage: "checking", atMs: 3200 },
  { stage: "aggregating", atMs: 5200 },
];

export function useVerify(): UseVerifyState {
  const [report, setReport] = useState<IntegrityReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(false);
  const [stage, setStage] = useState<PipelineStage>("idle");
  const [elapsedMs, setElapsed] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const tickersRef = useRef<number[]>([]);
  const elapsedTimerRef = useRef<number | null>(null);

  const clearTickers = () => {
    tickersRef.current.forEach((id) => window.clearTimeout(id));
    tickersRef.current = [];
    if (elapsedTimerRef.current !== null) {
      window.clearInterval(elapsedTimerRef.current);
      elapsedTimerRef.current = null;
    }
  };

  const reset = useCallback(() => {
    abortRef.current?.abort();
    clearTickers();
    setReport(null);
    setError(null);
    setLoading(false);
    setStage("idle");
    setElapsed(0);
  }, []);

  const run = useCallback(
    async (payload: VerifyRequest, mode: VerifyMode = "full") => {
      abortRef.current?.abort();
      clearTickers();

      const ctrl = new AbortController();
      abortRef.current = ctrl;

      setReport(null);
      setError(null);
      setLoading(true);
      setStage("extracting");
      setElapsed(0);

      const startedAt = performance.now();
      elapsedTimerRef.current = window.setInterval(() => {
        setElapsed(Math.round(performance.now() - startedAt));
      }, 100);

      const timeline =
        mode === "quick"
          ? STAGE_TIMELINE.filter((s) => s.stage !== "checking")
          : STAGE_TIMELINE;
      for (const step of timeline) {
        const id = window.setTimeout(() => setStage(step.stage), step.atMs);
        tickersRef.current.push(id);
      }

      try {
        const result = await verify(payload, mode, ctrl.signal);
        clearTickers();
        setElapsed(result.metadata.duration_ms);
        setStage("done");
        setReport(result);
      } catch (err) {
        clearTickers();
        if ((err as DOMException)?.name === "AbortError") {
          setStage("idle");
          return;
        }
        const msg =
          err instanceof TrustLayerError
            ? err.message
            : err instanceof Error
              ? err.message
              : "Unexpected error verifying output.";
        setError(msg);
        setStage("error");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  return { report, error, isLoading, stage, elapsedMs, run, reset };
}
