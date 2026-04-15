import { useCallback, useRef, useState } from "react";
import {
  analyze,
  loadSample,
  uploadFile,
  uploadText,
  ContractApiError,
} from "../services/contract";
import type { AnalyzeResponse, UploadResponse } from "../types/contract";

export type AnalyzeStage =
  | "idle"
  | "uploading"
  | "parsing"
  | "analyzing"
  | "verifying"
  | "done"
  | "error";

const STAGE_TIMELINE: { stage: AnalyzeStage; atMs: number }[] = [
  { stage: "analyzing", atMs: 0 },
  { stage: "verifying", atMs: 3500 },
];

export interface UseContractState {
  upload: UploadResponse | null;
  result: AnalyzeResponse | null;
  error: string | null;
  isBusy: boolean;
  stage: AnalyzeStage;
  elapsedMs: number;
  uploadFromFile: (file: File) => Promise<void>;
  uploadFromText: (text: string, filename?: string) => Promise<void>;
  loadFromSample: (name: string) => Promise<void>;
  analyzeNow: (opts?: { skipVerification?: boolean }) => Promise<void>;
  reset: () => void;
}

export function useContract(): UseContractState {
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setBusy] = useState(false);
  const [stage, setStage] = useState<AnalyzeStage>("idle");
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
    setUpload(null);
    setResult(null);
    setError(null);
    setBusy(false);
    setStage("idle");
    setElapsed(0);
  }, []);

  const handleError = (err: unknown) => {
    if ((err as DOMException)?.name === "AbortError") {
      setStage("idle");
      return;
    }
    const msg =
      err instanceof ContractApiError
        ? err.message
        : err instanceof Error
          ? err.message
          : "Unexpected error.";
    setError(msg);
    setStage("error");
  };

  const runUpload = async (
    action: (signal: AbortSignal) => Promise<UploadResponse>,
  ) => {
    abortRef.current?.abort();
    clearTickers();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setResult(null);
    setError(null);
    setBusy(true);
    setStage("uploading");
    setElapsed(0);

    const startedAt = performance.now();
    elapsedTimerRef.current = window.setInterval(() => {
      setElapsed(Math.round(performance.now() - startedAt));
    }, 100);

    try {
      const parsedStage = window.setTimeout(() => setStage("parsing"), 400);
      tickersRef.current.push(parsedStage);
      const u = await action(ctrl.signal);
      clearTickers();
      setUpload(u);
      setStage("idle");
      setElapsed(0);
    } catch (err) {
      clearTickers();
      handleError(err);
    } finally {
      setBusy(false);
    }
  };

  const uploadFromFile = useCallback(async (file: File) => {
    await runUpload((signal) => uploadFile(file, signal));
  }, []);

  const uploadFromText = useCallback(
    async (text: string, filename?: string) => {
      await runUpload((signal) => uploadText(text, filename, signal));
    },
    [],
  );

  const loadFromSample = useCallback(async (name: string) => {
    await runUpload((signal) => loadSample(name, signal));
  }, []);

  const analyzeNow = useCallback(
    async (opts?: { skipVerification?: boolean }) => {
      abortRef.current?.abort();
      clearTickers();
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      if (!upload) {
        setError("Upload a contract first.");
        setStage("error");
        return;
      }

      setResult(null);
      setError(null);
      setBusy(true);
      setStage("analyzing");
      setElapsed(0);

      const startedAt = performance.now();
      elapsedTimerRef.current = window.setInterval(() => {
        setElapsed(Math.round(performance.now() - startedAt));
      }, 100);

      const timeline = opts?.skipVerification
        ? STAGE_TIMELINE.filter((s) => s.stage !== "verifying")
        : STAGE_TIMELINE;
      for (const step of timeline) {
        const id = window.setTimeout(() => setStage(step.stage), step.atMs);
        tickersRef.current.push(id);
      }

      try {
        const r = await analyze(
          {
            contract_id: upload.contract_id,
            skip_verification: opts?.skipVerification,
          },
          ctrl.signal,
        );
        clearTickers();
        setResult(r);
        setStage("done");
      } catch (err) {
        clearTickers();
        handleError(err);
      } finally {
        setBusy(false);
      }
    },
    [upload],
  );

  return {
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
  };
}
