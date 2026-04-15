import type { RiskLevel } from "../types/contract";
import { RISK_META } from "../lib/contract";

interface Props {
  risk: RiskLevel;
  compact?: boolean;
}

export default function RiskBadge({ risk, compact = false }: Props) {
  const meta = RISK_META[risk];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border ${meta.border} ${meta.bg} ${meta.color} ${
        compact ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs"
      } font-medium uppercase tracking-wider`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}
