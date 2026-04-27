import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchModels } from "../services/models";
import type {
  ModelEntry,
  ModelPick,
  ModelsResponse,
} from "../types/models";

const STORAGE_KEY = "trustlayer.modelPick";

interface CatalogState {
  catalog: ModelsResponse | null;
  pick: ModelPick | null;
  setPick: (pick: ModelPick) => void;
  isLoading: boolean;
  error: string | null;
}

function readStoredPick(): ModelPick | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ModelPick>;
    if (!parsed?.provider || !parsed?.model) return null;
    return { provider: parsed.provider, model: parsed.model } as ModelPick;
  } catch {
    return null;
  }
}

function writeStoredPick(pick: ModelPick): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(pick));
  } catch {
    // ignore — quota / private mode shouldn't break verification.
  }
}

function reconcile(
  stored: ModelPick | null,
  catalog: ModelsResponse,
): ModelPick {
  // A persisted pick can outlive a catalog change (deprecation, key removed).
  // Fall back to the API-provided default rather than 4xx-ing on every Verify.
  if (stored) {
    const match = catalog.models.find(
      (m) => m.provider === stored.provider && m.id === stored.model,
    );
    if (match && match.available) {
      return stored;
    }
  }
  return { provider: catalog.default.provider, model: catalog.default.model };
}

export function useModelCatalog(): CatalogState {
  const [catalog, setCatalog] = useState<ModelsResponse | null>(null);
  const [pick, setPickState] = useState<ModelPick | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    fetchModels(ctrl.signal)
      .then((c) => {
        setCatalog(c);
        const stored = readStoredPick();
        setPickState(reconcile(stored, c));
        setError(null);
      })
      .catch((err: unknown) => {
        if ((err as DOMException)?.name === "AbortError") return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, []);

  const setPick = useCallback((next: ModelPick) => {
    setPickState(next);
    writeStoredPick(next);
  }, []);

  return useMemo(
    () => ({ catalog, pick, setPick, isLoading, error }),
    [catalog, pick, setPick, isLoading, error],
  );
}

export function groupByProvider(models: ModelEntry[]): {
  provider: string;
  models: ModelEntry[];
}[] {
  // Google first (default in this codebase), then Anthropic, then any others.
  const order: Record<string, number> = { google: 0, anthropic: 1 };
  const groups = new Map<string, ModelEntry[]>();
  for (const m of models) {
    const list = groups.get(m.provider) ?? [];
    list.push(m);
    groups.set(m.provider, list);
  }
  return Array.from(groups.entries())
    .sort(
      ([a], [b]) => (order[a] ?? 99) - (order[b] ?? 99) || a.localeCompare(b),
    )
    .map(([provider, models]) => ({ provider, models }));
}
