import type { AnalyzeResponse, RiskLevel, VerificationStatus } from "../types/contract";

interface Props {
  result: AnalyzeResponse;
}

const RISK_LABEL: Record<RiskLevel, string> = {
  critical: "Critical",
  warning: "Warning",
  info: "Info",
  ok: "OK",
};

const VERIFICATION_LABEL: Record<VerificationStatus, string> = {
  verified: "Verified",
  uncertain: "Uncertain",
  flagged: "Flagged",
  hallucination: "Removed",
  unchecked: "Unchecked",
};

export default function PrintableReport({ result }: Props) {
  const generatedAt = new Date().toLocaleString();
  const counts = countByRisk(result);
  const findingsByClause = groupBy(result.findings, (vf) => vf.finding.section_id);

  return (
    <div className="printable-report hidden print:block text-black">
      <header className="print-header">
        <div className="print-eyebrow">TrustLayer · Contract Review</div>
        <h1 className="print-h1">{result.filename || "Contract"}</h1>
        <div className="print-subline">
          {result.summary.contract_type} · Generated {generatedAt}
        </div>
      </header>

      <section className="print-section">
        <h2 className="print-h2">Summary</h2>
        <div className="print-grid-2">
          <div>
            <div className="print-label">Overall risk</div>
            <div className="print-value">{RISK_LABEL[result.summary.overall_risk]}</div>
          </div>
          <div>
            <div className="print-label">Integrity score</div>
            <div className="print-value">{result.summary.integrity_score} / 100</div>
          </div>
        </div>
        {result.summary.key_parties.length > 0 && (
          <div className="print-row">
            <div className="print-label">Parties</div>
            <div>{result.summary.key_parties.join(", ")}</div>
          </div>
        )}
        {result.summary.plain_language_summary && (
          <p className="print-paragraph">{result.summary.plain_language_summary}</p>
        )}
        <table className="print-counts">
          <thead>
            <tr>
              <th>Critical</th>
              <th>Warning</th>
              <th>Info</th>
              <th>Missing clauses</th>
              <th>Removed by TrustLayer</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{counts.critical}</td>
              <td>{counts.warning}</td>
              <td>{counts.info}</td>
              <td>{result.missing_clauses.length}</td>
              <td>{result.removed_findings.length}</td>
            </tr>
          </tbody>
        </table>
      </section>

      {result.missing_clauses.length > 0 && (
        <section className="print-section">
          <h2 className="print-h2">Missing standard clauses ({result.missing_clauses.length})</h2>
          <ol className="print-list">
            {result.missing_clauses.map((m) => (
              <li key={m.id} className="print-card">
                <div className="print-card-head">
                  <span className="print-card-title">{m.title}</span>
                  <span className={`print-pill print-pill-${m.risk}`}>
                    {RISK_LABEL[m.risk]}
                  </span>
                </div>
                <p className="print-card-body">{m.summary}</p>
                {m.recommendation && (
                  <p className="print-card-rec">
                    <strong>Recommendation:</strong> {m.recommendation}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="print-section">
        <h2 className="print-h2">Findings ({result.findings.length})</h2>
        {result.findings.length === 0 ? (
          <p className="print-empty">No clause-level findings produced for this contract.</p>
        ) : (
          result.clauses.map((clause) => {
            const findings = findingsByClause.get(clause.section_id) ?? [];
            if (findings.length === 0) return null;
            return (
              <div key={clause.section_id} className="print-clause">
                <h3 className="print-h3">
                  §{clause.section_id}
                  {clause.title ? ` — ${clause.title}` : ""}
                </h3>
                {findings.map((vf) => (
                  <div key={vf.finding.id} className="print-card">
                    <div className="print-card-head">
                      <span className="print-card-title">{vf.finding.title}</span>
                      <span className={`print-pill print-pill-${vf.finding.risk}`}>
                        {RISK_LABEL[vf.finding.risk]}
                      </span>
                      <span className={`print-pill print-pill-verify-${vf.verification_status}`}>
                        {VERIFICATION_LABEL[vf.verification_status]} ·{" "}
                        {vf.integrity_score}/100
                      </span>
                    </div>
                    <p className="print-card-body">{vf.finding.summary}</p>
                    {vf.finding.clause_quote && (
                      <blockquote className="print-quote">
                        "{vf.finding.clause_quote}"
                      </blockquote>
                    )}
                    {vf.finding.recommendation && (
                      <p className="print-card-rec">
                        <strong>Recommendation:</strong> {vf.finding.recommendation}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            );
          })
        )}
      </section>

      {result.removed_findings.length > 0 && (
        <section className="print-section">
          <h2 className="print-h2">
            Removed by TrustLayer ({result.removed_findings.length})
          </h2>
          <p className="print-empty">
            The following findings were produced by the AI analyst but suppressed by
            TrustLayer because they were not grounded in the clause text.
          </p>
          <ol className="print-list">
            {result.removed_findings.map((vf) => (
              <li key={vf.finding.id} className="print-card print-card-removed">
                <div className="print-card-head">
                  <span className="print-card-title">{vf.finding.title}</span>
                  <span className="print-pill print-pill-removed">Removed</span>
                </div>
                <p className="print-card-body">{vf.finding.summary}</p>
                {vf.reasoning && (
                  <p className="print-card-rec">
                    <strong>Why removed:</strong> {vf.reasoning}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      <footer className="print-footer">
        Generated by TrustLayer. Findings are AI-assisted and verified against clause
        text. Not legal advice.
      </footer>
    </div>
  );
}

function countByRisk(result: AnalyzeResponse) {
  const counts = { critical: 0, warning: 0, info: 0, ok: 0 };
  for (const vf of result.findings) {
    counts[vf.finding.risk] += 1;
  }
  return counts;
}

function groupBy<T, K>(items: T[], keyFn: (item: T) => K): Map<K, T[]> {
  const map = new Map<K, T[]>();
  for (const item of items) {
    const key = keyFn(item);
    const arr = map.get(key) ?? [];
    arr.push(item);
    map.set(key, arr);
  }
  return map;
}
