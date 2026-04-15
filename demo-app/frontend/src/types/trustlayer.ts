export type ClaimCategory =
  | "factual"
  | "interpretive"
  | "recommendation"
  | "quantitative";

export type GroundingLevel = "grounded" | "partially_grounded" | "ungrounded";

export type ConsistencyVerdict =
  | "consistent"
  | "minor_concern"
  | "inconsistent"
  | "contradictory";

export type ClaimStatus =
  | "verified"
  | "uncertain"
  | "flagged"
  | "hallucination";

export interface Claim {
  id: string;
  text: string;
  source_quote?: string | null;
  output_quote?: string | null;
  category: ClaimCategory;
}

export interface ClaimVerdict {
  claim_id: string;
  grounding_score: number;
  grounding_level: GroundingLevel;
  consistency_verdict: ConsistencyVerdict;
  is_hallucination: boolean;
  status: ClaimStatus;
  integrity_score: number;
  matched_passage?: string | null;
  reasoning: string;
}

export interface ReportMetadata {
  model: string;
  request_id: string;
  created_at: string;
  duration_ms: number;
  extractor_ms: number;
  grounder_ms: number;
  consistency_ms: number;
  input_tokens: number;
  output_tokens: number;
  claim_count: number;
}

export interface IntegrityReport {
  overall_score: number;
  verified: ClaimVerdict[];
  uncertain: ClaimVerdict[];
  flagged: ClaimVerdict[];
  hallucinations: ClaimVerdict[];
  claims: Claim[];
  metadata: ReportMetadata;
}

export interface VerifyRequest {
  source_context: string;
  llm_output: string;
}

export type VerifyMode = "full" | "quick";

export interface ApiError {
  error: string;
  message: string;
  details?: unknown;
  retry_after_seconds?: number;
}
