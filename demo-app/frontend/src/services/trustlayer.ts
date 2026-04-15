import type {
  ApiError,
  IntegrityReport,
  VerifyMode,
  VerifyRequest,
} from "../types/trustlayer";

const BASE_URL =
  import.meta.env.VITE_TRUSTLAYER_API_URL ?? "http://localhost:8000";

export class TrustLayerError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: ApiError | null,
  ) {
    super(message);
    this.name = "TrustLayerError";
  }
}

async function parseError(res: Response): Promise<TrustLayerError> {
  let body: ApiError | null = null;
  try {
    body = (await res.json()) as ApiError;
  } catch {
    // non-JSON response — fall through
  }
  const message =
    body?.message ??
    body?.error ??
    `Request failed with HTTP ${res.status}`;
  return new TrustLayerError(message, res.status, body);
}

export async function verify(
  payload: VerifyRequest,
  mode: VerifyMode = "full",
  signal?: AbortSignal,
): Promise<IntegrityReport> {
  const path = mode === "quick" ? "/verify/quick" : "/verify";
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as IntegrityReport;
}

export async function health(): Promise<{
  status: string;
  model: string;
  anthropic_configured: boolean;
}> {
  const res = await fetch(`${BASE_URL}/health`);
  if (!res.ok) throw await parseError(res);
  return await res.json();
}

export const engineBaseUrl = BASE_URL;
