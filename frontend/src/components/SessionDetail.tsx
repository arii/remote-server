import type { DetailResponse } from '../types';

interface Props {
  detail: DetailResponse | { error: string } | null;
}

export default function SessionDetail({ detail }: Props) {
  if (detail === null) {
    return <p data-testid="detail-placeholder">Select a session to view its transcript.</p>;
  }
  if ('error' in detail) {
    return (
      <div data-testid="detail-error" role="alert">
        <h2>Could not load session</h2>
        <p>{detail.error}</p>
      </div>
    );
  }
  return (
    <article data-testid="session-detail" className="detail">
      <header>
        <h2>{detail.file}</h2>
        <p className="row-meta">
          {detail.repo || 'no repo'} · {detail.cwd || 'no working directory'}
        </p>
      </header>
      {detail.turns.length === 0 ? (
        <p>No turns recorded.</p>
      ) : (
        <ol className="turns">
          {detail.turns.map((t, i) => (
            <li key={`${t.role}-${i}`} data-testid={`turn-${i}`} className={`turn turn-${t.role}`}>
              <strong>{t.role}</strong>
              <p>{t.text || '(no text)'}</p>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}
