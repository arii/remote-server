import type { HistoryRow } from './historyUtils';

// Shared status glyph for History and timeline rows: check,
// cross, or dash in a ring, toned by the row outcome.
export function StatusIcon({ tone }: { tone: HistoryRow['tone'] }) {
  return (
    <svg
      className={`history-icon tone-${tone}`}
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      {tone === 'ok' && <path d="M8.5 12.5l2.5 2.5 4.5-5" />}
      {tone === 'bad' && <path d="M9 9l6 6M15 9l-6 6" />}
      {tone === 'muted' && <path d="M8.5 12h7" />}
    </svg>
  );
}
