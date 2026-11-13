// StatusStrip (D5, chat-first shell): the one-glance status row that
// sits above the chat composer — server health, what needs attention,
// open PRs. Purely presentational: every fact arrives as a prop from
// the App-level single sources (header health snapshot, nav badge
// counts), so the strip can never disagree with the nav or the
// status drawer. Tapping it opens the triage drawer.
import type { SystemHealthResponse } from '../apiClient';

export type StripHealthState = 'loading' | 'ok' | 'degraded' | 'error';

// One health fact, one derivation: the strip, the header pill, and
// the old Now containers section all read the same snapshot, so the
// strip's state derives from it with the same rule the container
// section used (all running + nothing unhealthy = ok).
export function stripHealthState(health: SystemHealthResponse | null): StripHealthState {
  if (!health) return 'loading';
  if (health.containers_error) return 'error';
  if (health.containers.length === 0) return 'degraded';
  const allUp = health.containers.every(
    (c) => (c.state ?? '').toLowerCase() === 'running' &&
      (c.health ?? '').toLowerCase() !== 'unhealthy',
  );
  return allUp ? 'ok' : 'degraded';
}

export interface StatusStripProps {
  healthState: StripHealthState;
  healthLabel: string;
  // Null means "not loaded yet" — the pill stays hidden instead of
  // flashing a wrong zero (same contract as the nav badges).
  attentionCount: number | null;
  openPrCount: number | null;
  onTap: () => void;
}

export default function StatusStrip({
  healthState,
  healthLabel,
  attentionCount,
  openPrCount,
  onTap,
}: StatusStripProps) {
  return (
    <button
      type="button"
      className="status-strip"
      data-testid="status-strip"
      data-health-state={healthState}
      aria-label={`Open triage drawer. ${healthLabel}${attentionCount === null ? '' : `, ${attentionCount} need attention`}${openPrCount === null ? '' : `, ${openPrCount} open PRs`}`}
      onClick={onTap}
    >
      <span className={`status-pill status-${healthState}`} data-testid="status-health">
        {healthLabel}
      </span>
      {attentionCount !== null && (
        <span className="status-pill" data-testid="status-attention">
          {attentionCount} {attentionCount === 1 ? 'needs' : 'need'} attention
        </span>
      )}
      {openPrCount !== null && (
        <span className="status-pill" data-testid="status-prs">
          {openPrCount} open {openPrCount === 1 ? 'PR' : 'PRs'}
        </span>
      )}
    </button>
  );
}
