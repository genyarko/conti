import type {
  AnalyzeResponse,
  SampleEntry,
  UploadResponse,
} from "../types/contract";
import type { ApiError } from "../types/trustlayer";

const BASE_URL =
  import.meta.env.VITE_CONTRACT_API_URL ?? "http://localhost:8100";

export class ContractApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: ApiError | null,
  ) {
    super(message);
    this.name = "ContractApiError";
  }
}

async function parseError(res: Response): Promise<ContractApiError> {
  let body: ApiError | null = null;
  try {
    body = (await res.json()) as ApiError;
  } catch {
    // non-JSON response
  }
  const message =
    body?.message ??
    body?.error ??
    `Request failed with HTTP ${res.status}`;
  return new ContractApiError(message, res.status, body);
}

export async function uploadFile(
  file: File,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE_URL}/upload`, {
    method: "POST",
    body: form,
    signal,
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as UploadResponse;
}

export async function uploadText(
  text: string,
  filename?: string,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("text", text);
  if (filename) form.append("filename", filename);
  const res = await fetch(`${BASE_URL}/upload`, {
    method: "POST",
    body: form,
    signal,
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as UploadResponse;
}

export async function analyze(
  payload: {
    contract_id?: string;
    text?: string;
    filename?: string;
    skip_verification?: boolean;
  },
  signal?: AbortSignal,
): Promise<AnalyzeResponse> {
  const res = await fetch(`${BASE_URL}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as AnalyzeResponse;
}

export async function listSamples(
  signal?: AbortSignal,
): Promise<SampleEntry[]> {
  const res = await fetch(`${BASE_URL}/samples`, { signal });
  if (!res.ok) throw await parseError(res);
  const data = (await res.json()) as { samples: SampleEntry[] };
  return data.samples;
}

export async function loadSample(
  name: string,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const res = await fetch(`${BASE_URL}/samples/${encodeURIComponent(name)}/load`, {
    method: "POST",
    signal,
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as UploadResponse;
}

export const contractApiBaseUrl = BASE_URL;
