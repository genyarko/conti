export type RiskLevel = "critical" | "warning" | "info" | "ok";

export type FindingCategory =
  | "liability"
  | "termination"
  | "payment"
  | "ip"
  | "confidentiality"
  | "data_privacy"
  | "dispute"
  | "renewal"
  | "indemnity"
  | "compliance"
  | "missing_clause"
  | "other";

export type VerificationStatus =
  | "verified"
  | "uncertain"
  | "flagged"
  | "hallucination"
  | "unchecked";

export interface Clause {
  section_id: string;
  title: string;
  text: string;
  start_char: number;
  end_char: number;
}

export interface Finding {
  id: string;
  section_id: string;
  title: string;
  risk: RiskLevel;
  category: FindingCategory;
  summary: string;
  recommendation: string;
  clause_quote?: string | null;
}

export interface VerifiedFinding {
  finding: Finding;
  verification_status: VerificationStatus;
  integrity_score: number;
  grounding_score: number;
  reasoning: string;
  removed: boolean;
}

export interface ContractSummary {
  contract_type: string;
  overall_risk: RiskLevel;
  integrity_score: number;
  plain_language_summary: string;
  key_parties: string[];
}

export interface AnalyzeResponse {
  contract_id: string;
  filename: string;
  doc_type: string;
  summary: ContractSummary;
  clauses: Clause[];
  findings: VerifiedFinding[];
  removed_findings: VerifiedFinding[];
  missing_clauses: Finding[];
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface UploadResponse {
  contract_id: string;
  filename: string;
  doc_type: string;
  num_clauses: number;
  char_count: number;
  clauses: Clause[];
  raw_text: string;
}

export interface SampleEntry {
  name: string;
  filename: string;
  size_bytes: number;
}
