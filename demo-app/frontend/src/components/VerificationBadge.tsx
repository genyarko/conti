import type { VerificationStatus } from "../types/contract";
import { VERIFICATION_META } from "../lib/contract";

interface Props {
  status: VerificationStatus;
  compact?: boolean;
}

export default function VerificationBadge({ status, compact = false }: Props) {
  const meta = VERIFICATION_META[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border ${meta.border} ${meta.bg} ${meta.color} ${
        compact ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs"
      } font-medium uppercase tracking-wider`}
      title={meta.description}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}
