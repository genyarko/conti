import type { ModelsResponse } from "../types/models";

const BASE_URL =
  import.meta.env.VITE_TRUSTLAYER_API_URL ?? "http://localhost:8000";

export async function fetchModels(signal?: AbortSignal): Promise<ModelsResponse> {
  const res = await fetch(`${BASE_URL}/models`, { signal });
  if (!res.ok) throw new Error(`Failed to load /models (HTTP ${res.status}).`);
  return (await res.json()) as ModelsResponse;
}
