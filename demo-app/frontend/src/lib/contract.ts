import type {
  FindingCategory,
  RiskLevel,
  VerificationStatus,
} from "../types/contract";

export const RISK_META: Record<
  RiskLevel,
  {
    label: string;
    color: string;
    bg: string;
    border: string;
    dot: string;
    barBg: string;
    barFg: string;
    order: number;
  }
> = {
  critical: {
    label: "Critical",
    color: "text-red-300",
    bg: "bg-red-500/10",
    border: "border-red-500/40",
    dot: "bg-red-400",
    barBg: "bg-red-500/20",
    barFg: "bg-red-400",
    order: 0,
  },
  warning: {
    label: "Warning",
    color: "text-orange-300",
    bg: "bg-orange-500/10",
    border: "border-orange-500/40",
    dot: "bg-orange-400",
    barBg: "bg-orange-500/20",
    barFg: "bg-orange-400",
    order: 1,
  },
  info: {
    label: "Info",
    color: "text-sky-300",
    bg: "bg-sky-500/10",
    border: "border-sky-500/40",
    dot: "bg-sky-400",
    barBg: "bg-sky-500/20",
    barFg: "bg-sky-400",
    order: 2,
  },
  ok: {
    label: "OK",
    color: "text-emerald-300",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/40",
    dot: "bg-emerald-400",
    barBg: "bg-emerald-500/20",
    barFg: "bg-emerald-400",
    order: 3,
  },
};

export const VERIFICATION_META: Record<
  VerificationStatus,
  {
    label: string;
    color: string;
    bg: string;
    border: string;
    dot: string;
    description: string;
  }
> = {
  verified: {
    label: "Verified",
    color: "text-emerald-300",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/40",
    dot: "bg-emerald-400",
    description: "Grounded in the clause text and logically consistent.",
  },
  uncertain: {
    label: "Uncertain",
    color: "text-yellow-300",
    bg: "bg-yellow-500/10",
    border: "border-yellow-500/40",
    dot: "bg-yellow-400",
    description: "Partial grounding or a minor consistency concern.",
  },
  flagged: {
    label: "Flagged",
    color: "text-orange-300",
    bg: "bg-orange-500/10",
    border: "border-orange-500/40",
    dot: "bg-orange-400",
    description: "Weak source support or contradicts the clause.",
  },
  hallucination: {
    label: "Removed",
    color: "text-red-300",
    bg: "bg-red-500/10",
    border: "border-red-500/40",
    dot: "bg-red-400",
    description: "TrustLayer removed this finding — not grounded in the clause.",
  },
  unchecked: {
    label: "Unchecked",
    color: "text-slate-300",
    bg: "bg-slate-500/10",
    border: "border-slate-500/40",
    dot: "bg-slate-400",
    description: "Verification was skipped.",
  },
};

export const CATEGORY_LABELS: Record<FindingCategory, string> = {
  liability: "Liability",
  termination: "Termination",
  payment: "Payment",
  ip: "IP",
  confidentiality: "Confidentiality",
  data_privacy: "Data privacy",
  dispute: "Dispute resolution",
  renewal: "Renewal",
  indemnity: "Indemnity",
  compliance: "Compliance",
  missing_clause: "Missing clause",
  other: "Other",
};

export function formatSampleName(name: string): string {
  return name
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
