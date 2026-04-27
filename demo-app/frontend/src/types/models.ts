export type ModelTier = "flagship" | "balanced" | "fast";
export type ModelProvider = "google" | "anthropic";

export interface ModelEntry {
  provider: ModelProvider;
  id: string;
  label: string;
  tier: ModelTier;
  input_price_per_mtok: number;
  output_price_per_mtok: number;
  available: boolean;
}

export interface ModelsResponse {
  default: { provider: ModelProvider; model: string };
  models: ModelEntry[];
}

export interface ModelPick {
  provider: ModelProvider;
  model: string;
}
