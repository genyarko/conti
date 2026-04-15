import type { ClaimStatus } from "../types/trustlayer";
import { STATUS_META } from "../lib/status";

export default function StatusBadge({
  status,
  compact = false,
}: {
  status: ClaimStatus;
  compact?: boolean;
}) {
  const meta = STATUS_META[status];
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
