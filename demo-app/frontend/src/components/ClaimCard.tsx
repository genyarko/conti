import { useState } from "react";
import type { Claim, ClaimVerdict } from "../types/trustlayer";
import {
  CONSISTENCY_LABELS,
  GROUNDING_LABELS,
  STATUS_META,
  scoreColor,
} from "../lib/status";
import StatusBadge from "./StatusBadge";

interface Props {
  claim: Claim;
  verdict: ClaimVerdict;
}

export default function ClaimCard({ claim, verdict }: Props) {
  const [open, setOpen] = useState(false);
  const meta = STATUS_META[verdict.status];

  return (
    <div
      className={`card ${meta.border} border ring-1 ${meta.ring} animate-fade-in`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left p-4 flex items-start gap-4"
      >
        <div
          className={`mt-1 h-2.5 w-2.5 rounded-full shrink-0 ${meta.dot}`}
          aria-hidden
        />
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <StatusBadge status={verdict.status} />
            <span className="text-[10px] uppercase tracking-wider text-slate-500 font-medium">
              {claim.category}
            </span>
          </div>
          <p className="text-sm text-slate-100 leading-relaxed">{claim.text}</p>
          <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-slate-400">
            <Stat
              label="Grounding"
              value={verdict.grounding_score}
              sub={GROUNDING_LABELS[verdict.grounding_level]}
            />
            <Stat
              label="Integrity"
              value={verdict.integrity_score}
              sub={CONSISTENCY_LABELS[verdict.consistency_verdict]}
            />
          </div>
        </div>
        <span
          className="text-slate-500 text-xs shrink-0 mt-1"
          aria-hidden
        >
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 -mt-1 border-t border-line/60 pt-3 space-y-3 text-sm animate-fade-in">
          {verdict.matched_passage && (
            <QuoteBlock label="Matched passage in source" text={verdict.matched_passage} />
          )}
          {claim.source_quote && !verdict.matched_passage && (
            <QuoteBlock label="Claim's source quote" text={claim.source_quote} />
          )}
          {claim.output_quote && (
            <QuoteBlock label="From LLM output" text={claim.output_quote} subtle />
          )}
          {verdict.reasoning && (
            <div>
              <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">
                Reasoning
              </div>
              <p className="text-slate-300 leading-relaxed whitespace-pre-wrap">
                {verdict.reasoning}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: number;
  sub: string;
}) {
  const color = scoreColor(value);
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] uppercase tracking-wider text-slate-500">
        {label}
      </span>
      <span className="font-mono font-semibold" style={{ color }}>
        {value}
      </span>
      <span className="text-slate-400">· {sub}</span>
    </div>
  );
}

function QuoteBlock({
  label,
  text,
  subtle = false,
}: {
  label: string;
  text: string;
  subtle?: boolean;
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">
        {label}
      </div>
      <blockquote
        className={`border-l-2 pl-3 text-sm leading-relaxed ${
          subtle
            ? "border-slate-600 text-slate-400"
            : "border-emerald-500/60 text-slate-200"
        }`}
      >
        "{text}"
      </blockquote>
    </div>
  );
}
