import type {
  ClaimStatus,
  ConsistencyVerdict,
  GroundingLevel,
} from "../types/trustlayer";

export const STATUS_META: Record<
  ClaimStatus,
  {
    label: string;
    color: string;
    bg: string;
    border: string;
    ring: string;
    dot: string;
    description: string;
  }
> = {
  verified: {
    label: "Verified",
    color: "text-emerald-300",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/40",
    ring: "ring-emerald-500/20",
    dot: "bg-emerald-400",
    description: "Grounded in source and logically consistent.",
  },
  uncertain: {
    label: "Uncertain",
    color: "text-yellow-300",
    bg: "bg-yellow-500/10",
    border: "border-yellow-500/40",
    ring: "ring-yellow-500/20",
    dot: "bg-yellow-400",
    description: "Partial grounding or a minor consistency concern.",
  },
  flagged: {
    label: "Flagged",
    color: "text-orange-300",
    bg: "bg-orange-500/10",
    border: "border-orange-500/40",
    ring: "ring-orange-500/20",
    dot: "bg-orange-400",
    description: "Weak source support or contradicts the source.",
  },
  hallucination: {
    label: "Hallucination",
    color: "text-red-300",
    bg: "bg-red-500/10",
    border: "border-red-500/40",
    ring: "ring-red-500/20",
    dot: "bg-red-400",
    description:
      "Ungrounded and inconsistent — TrustLayer removes these from output.",
  },
};

export const GROUNDING_LABELS: Record<GroundingLevel, string> = {
  grounded: "Grounded",
  partially_grounded: "Partial support",
  ungrounded: "No source support",
};

export const CONSISTENCY_LABELS: Record<ConsistencyVerdict, string> = {
  consistent: "Consistent",
  minor_concern: "Minor concern",
  inconsistent: "Inconsistent",
  contradictory: "Contradicts source",
};

export function scoreColor(score: number): string {
  if (score >= 85) return "#10b981";
  if (score >= 70) return "#eab308";
  if (score >= 50) return "#f97316";
  return "#ef4444";
}

export function scoreLabel(score: number): string {
  if (score >= 85) return "Trusted";
  if (score >= 70) return "Mostly trusted";
  if (score >= 50) return "Mixed";
  return "Low integrity";
}
