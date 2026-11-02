import type { SessionRecord } from '../types';

interface Props {
  sessions: SessionRecord[];
  selected: string | null;
  onSelect: (file: string) => void;
}

export function sessionKey(s: SessionRecord): string {
  return s.id ?? s.file;
}

export default function SessionList({ sessions, selected, onSelect }: Props) {
  if (sessions.length === 0) {
    return <p data-testid="session-empty">No sessions yet.</p>;
  }
  return (
    <ul data-testid="session-list" className="session-list">
      {sessions.map((s) => (
        <li key={sessionKey(s)}>
          <button
            type="button"
            data-testid={`session-row-${s.file}`}
            aria-current={selected === s.file ? 'true' : undefined}
            className={selected === s.file ? 'row selected' : 'row'}
            onClick={() => onSelect(s.file)}
          >
            <span className="row-title">{s.title}</span>
            <span className="row-meta">
              {s.repo || 'no repo'} · {new Date(s.mtime * 1000).toLocaleString()}
              {s.source ? ` · ${s.source}` : ''}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
