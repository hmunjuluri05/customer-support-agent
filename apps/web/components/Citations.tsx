import type { Citation } from '@/lib/events';
import { similarity } from '@/lib/format';

/**
 * Sources for the answer above.
 *
 * These are the citations the server validated against the accepted evidence
 * before releasing the answer, so every row here corresponds to a chunk that
 * actually cleared the grounding threshold. The document version is shown
 * because the fixture library deliberately contains a superseded policy.
 */
export function Citations({ citations, label }: { citations: Citation[]; label: string }) {
  if (citations.length === 0) return null;

  return (
    <div className="agent-section">
      <div className="section-label">{label}</div>
      <div className="citations">
        {citations.map((citation) => (
          <div className="citation" key={citation.chunk_id}>
            <div>
              <div className="citation-title">
                {citation.document_title}{' '}
                <span className="citation-score">{citation.document_version}</span>
              </div>
              <div className="citation-path">{citation.heading_path}</div>
            </div>
            <div className="citation-score">sim {similarity(citation.similarity)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
