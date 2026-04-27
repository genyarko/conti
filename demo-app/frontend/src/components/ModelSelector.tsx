import { useId, useMemo } from "react";
import type { ModelEntry, ModelPick } from "../types/models";
import { groupByProvider } from "../hooks/useModelCatalog";

interface Props {
  models: ModelEntry[];
  pick: ModelPick | null;
  onChange: (next: ModelPick) => void;
  disabled?: boolean;
  className?: string;
  label?: string;
}

const PROVIDER_LABELS: Record<string, string> = {
  google: "Google · Gemini",
  anthropic: "Anthropic · Claude",
};

const TIER_LABELS: Record<string, string> = {
  flagship: "flagship",
  balanced: "balanced",
  fast: "fast",
};

export default function ModelSelector({
  models,
  pick,
  onChange,
  disabled,
  className = "",
  label = "Model",
}: Props) {
  const groups = useMemo(() => groupByProvider(models), [models]);
  const id = useId();

  const value = pick ? `${pick.provider}::${pick.model}` : "";

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <label
        htmlFor={id}
        className="text-xs font-medium uppercase tracking-wider text-slate-500"
      >
        {label}
      </label>
      <select
        id={id}
        disabled={disabled}
        value={value}
        onChange={(e) => {
          const [provider, model] = e.target.value.split("::");
          if (provider && model) {
            onChange({ provider: provider as ModelPick["provider"], model });
          }
        }}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm shadow-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
      >
        {groups.map((group) => (
          <optgroup
            key={group.provider}
            label={PROVIDER_LABELS[group.provider] ?? group.provider}
          >
            {group.models.map((m) => {
              const tier = TIER_LABELS[m.tier] ?? m.tier;
              const price = `$${m.input_price_per_mtok.toFixed(2)}/$${m.output_price_per_mtok.toFixed(2)}`;
              const note = m.available ? "" : " — not configured";
              return (
                <option
                  key={`${m.provider}::${m.id}`}
                  value={`${m.provider}::${m.id}`}
                  disabled={!m.available}
                >
                  {`${m.label} · ${tier} · ${price}/Mtok${note}`}
                </option>
              );
            })}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
