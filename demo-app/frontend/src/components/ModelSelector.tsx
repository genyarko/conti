import { useId, useMemo } from "react";
import type { ModelEntry, ModelPick } from "../types/models";
import { groupByProvider } from "../hooks/useModelCatalog";

interface Props {
  models: ModelEntry[];
  pick: ModelPick | null;
  onChange: (next: ModelPick) => void;
  disabled?: boolean;
  isLoading?: boolean;
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

const SELECT_CLASSES =
  "rounded-md border border-line bg-panel px-2 py-1 text-sm text-slate-100 shadow-sm focus:border-emerald-400 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-50";

// Native <option> rendering ignores Tailwind classes on most browsers; inline
// styles are the only way to guarantee readable colors in dark mode.
const OPTION_STYLE = { backgroundColor: "#111a2e", color: "#f1f5f9" } as const;

export default function ModelSelector({
  models,
  pick,
  onChange,
  disabled,
  isLoading,
  className = "",
  label = "Model",
}: Props) {
  const groups = useMemo(() => groupByProvider(models), [models]);
  const id = useId();

  const value = pick ? `${pick.provider}::${pick.model}` : "";
  const showPlaceholder = isLoading || models.length === 0;

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <label
        htmlFor={id}
        className="text-xs font-medium uppercase tracking-wider text-slate-500"
      >
        {label}
      </label>
      {showPlaceholder ? (
        <select id={id} disabled className={SELECT_CLASSES}>
          <option style={OPTION_STYLE}>Loading models…</option>
        </select>
      ) : (
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
          className={SELECT_CLASSES}
        >
          {groups.map((group) => (
            <optgroup
              key={group.provider}
              label={PROVIDER_LABELS[group.provider] ?? group.provider}
              style={OPTION_STYLE}
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
                    style={OPTION_STYLE}
                  >
                    {`${m.label} · ${tier} · ${price}/Mtok${note}`}
                  </option>
                );
              })}
            </optgroup>
          ))}
        </select>
      )}
    </div>
  );
}
